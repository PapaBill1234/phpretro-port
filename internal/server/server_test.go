package server

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

type fakeBackend struct {
	databaseErr, redisErr, siteErr error
	calls                          int
}

func (b *fakeBackend) DatabaseReady(context.Context) error { b.calls++; return b.databaseErr }
func (b *fakeBackend) RedisReady(context.Context) error    { b.calls++; return b.redisErr }
func (b *fakeBackend) Site(context.Context) (storage.Site, error) {
	b.calls++
	return storage.Site{Version: "site.v1", SiteName: "Persisted name", ActiveTheme: "legacy", DefaultLocale: "nl", EnabledLocales: []string{"en", "nl"}, SettingsRevision: 7, Health: "ok"}, b.siteErr
}

func TestEndpoints(t *testing.T) {
	secret := errors.New("sensitive database connection error")
	for _, tc := range []struct {
		name, path, method string
		backend            fakeBackend
		status             int
		contains           string
	}{
		{"liveness without dependencies", "/api/v1/health", "GET", fakeBackend{databaseErr: secret, redisErr: secret}, 200, `"status":"ok"`},
		{"ready", "/api/v1/ready", "GET", fakeBackend{}, 200, `"redis":true`},
		{"database unavailable", "/api/v1/ready", "GET", fakeBackend{databaseErr: secret}, 503, `"database":false`},
		{"cache unavailable", "/api/v1/ready", "GET", fakeBackend{redisErr: secret}, 503, `"redis":false`},
		{"persisted settings", "/api/v1/site", "GET", fakeBackend{}, 200, `"settingsRevision":7`},
		{"cache degraded settings remain available", "/api/v1/site", "GET", fakeBackend{redisErr: secret}, 200, `"health":"degraded"`},
		{"settings unavailable", "/api/v1/site", "GET", fakeBackend{siteErr: secret}, 503, `"code":"site_unavailable"`},
		{"method rejected", "/api/v1/site", "POST", fakeBackend{}, 405, "Method Not Allowed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			Handler(&tc.backend).ServeHTTP(rr, httptest.NewRequest(tc.method, tc.path, nil))
			if rr.Code != tc.status || !strings.Contains(rr.Body.String(), tc.contains) {
				t.Fatalf("status=%d body=%s", rr.Code, rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), secret.Error()) {
				t.Fatal("internal error leaked")
			}
			if tc.path == "/api/v1/health" && tc.backend.calls != 0 {
				t.Fatal("liveness called dependencies")
			}
			if tc.status != http.StatusMethodNotAllowed && rr.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("missing no-store")
			}
		})
	}
}
