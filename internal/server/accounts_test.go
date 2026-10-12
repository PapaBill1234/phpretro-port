// SPDX-License-Identifier: GPL-3.0-only
package server

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/PapaBill1234/phpretro-port/internal/auth"
	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

type fakeAccounts struct {
	role  string
	err   error
	calls int
}

const testCSRF = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func (a *fakeAccounts) Login(context.Context, string, string, string, string, string) (string, auth.Session, auth.Principal, error) {
	a.calls++
	return testCSRF, auth.Session{UserID: 9, CSRF: testCSRF}, auth.Principal{ID: 9, Username: "fixture", Role: a.role}, a.err
}
func (a *fakeAccounts) Current(context.Context, string, string) (auth.Session, auth.Principal, error) {
	a.calls++
	return auth.Session{UserID: 9, CSRF: testCSRF, RoleRevision: 1, MFARevision: testCSRF}, auth.Principal{ID: 9, Username: "fixture", Role: a.role}, a.err
}
func (a *fakeAccounts) Logout(context.Context, string) error { a.calls++; return a.err }

type editableBackend struct {
	fakeBackend
	updates int
	err     error
}

func (s *editableBackend) UpdateSite(_ context.Context, proof auth.Proof, site storage.Site) (storage.Site, error) {
	s.updates++
	if proof.UserID != 9 {
		return site, auth.ErrForbidden
	}
	site.SettingsRevision++
	return site, s.err
}
func TestAccountRequestBoundaries(t *testing.T) {
	config, _ := NewAccountConfig("http://127.0.0.1:8790", false, "")
	update := `{"version":"site-update.v1","siteName":"Fixture","activeTheme":"legacy","defaultLocale":"nl","enabledLocales":["nl"],"expectedRevision":7}`
	for _, tc := range []struct {
		name, path, method, body, origin, csrf, role string
		err, storeErr                                error
		status                                       int
		updates                                      int
	}{
		{name: "login Origin required", path: "/api/v1/auth/login", method: "POST", body: `{"username":"fixture","password":"x"}`, status: 403},
		{name: "cross Origin denied", path: "/api/v1/auth/login", method: "POST", origin: "https://attacker.example", status: 403},
		{name: "unknown credential field", path: "/api/v1/auth/login", method: "POST", origin: config.Origin, body: `{"username":"fixture","password":"x","rank":99}`, status: 400},
		{name: "trailing JSON", path: "/api/v1/auth/login", method: "POST", origin: config.Origin, body: `{} {}`, status: 400},
		{name: "oversize JSON", path: "/api/v1/auth/login", method: "POST", origin: config.Origin, body: strings.Repeat("x", 8193), status: 413},
		{name: "invalid credentials", path: "/api/v1/auth/login", method: "POST", origin: config.Origin, body: `{"username":"fixture","password":"x"}`, err: auth.ErrInvalid, status: 401},
		{name: "rate limit", path: "/api/v1/auth/login", method: "POST", origin: config.Origin, body: `{}`, err: auth.ErrLimited, status: 429},
		{name: "member cannot read admin", path: "/api/v1/housekeeping/site", method: "GET", role: "member", status: 403},
		{name: "member cannot mutate", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, role: "member", status: 403},
		{name: "admin must supply CSRF", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, body: update, role: "admin", status: 403},
		{name: "guest mutation", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, err: auth.ErrUnauthenticated, status: 401},
		{name: "admin valid mutation", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, role: "admin", status: 200, updates: 1},
		{name: "invalid enabled locales", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: strings.Replace(update, `["nl"]`, `["en"]`, 1), role: "admin", status: 400},
		{name: "concurrent revision conflict", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, role: "admin", storeErr: storage.ErrConflict, status: 409, updates: 1},
		{name: "transactional staff revoke", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, role: "admin", storeErr: auth.ErrForbidden, status: 403, updates: 1},
		{name: "private database error", path: "/api/v1/housekeeping/site", method: "PUT", origin: config.Origin, csrf: testCSRF, body: update, role: "admin", storeErr: errors.New("secret-fixture-error"), status: 503, updates: 1},
		{name: "logout without CSRF", path: "/api/v1/auth/logout", method: "POST", origin: config.Origin, role: "member", status: 403},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := &fakeAccounts{role: tc.role, err: tc.err}
			store := &editableBackend{err: tc.storeErr}
			h := HandlerWithAccounts(store, a, config, func(context.Context) error { return nil })
			r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			r.Header.Set("Content-Type", "application/json")
			if tc.origin != "" {
				r.Header.Set("Origin", tc.origin)
			}
			if tc.csrf != "" {
				r.Header.Set("X-CSRF-Token", tc.csrf)
			}
			r.AddCookie(&http.Cookie{Name: config.Cookie, Value: testCSRF})
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != tc.status || store.updates != tc.updates {
				t.Fatalf("status=%d updates=%d body=%s", w.Code, store.updates, w.Body.String())
			}
			if strings.Contains(w.Body.String(), "secret-fixture-error") {
				t.Fatal("internal error disclosed")
			}
			if w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("cacheable account response")
			}
		})
	}
}
func TestOriginAndProxyConfiguration(t *testing.T) {
	for _, tc := range []struct {
		origin string
		secure bool
	}{{"http://public.example", false}, {"http://127.0.0.1:8790/", false}, {"https://example.test/path", true}, {"https://user:password@example.test", true}, {"http://example.test", true}, {"https://example.test?x=y", true}} {
		if _, err := NewAccountConfig(tc.origin, tc.secure, ""); err == nil {
			t.Fatal("unsafe origin accepted")
		}
	}
	c, err := NewAccountConfig("https://example.test", true, "192.0.2.0/24")
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest("GET", "/", nil)
	r.RemoteAddr = "198.51.100.3:80"
	r.Header.Set("X-Real-IP", "203.0.113.4")
	if c.peer(r) != "198.51.100.3" {
		t.Fatal("untrusted proxy header used")
	}
	r.RemoteAddr = "192.0.2.8:80"
	if c.peer(r) != "203.0.113.4" {
		t.Fatal("explicit trusted proxy ignored")
	}
	w := httptest.NewRecorder()
	c.cookie(w, testCSRF, false)
	cookie := w.Result().Cookies()[0]
	if cookie.Name != "__Host-phpretro_session" || !cookie.Secure || !cookie.HttpOnly || cookie.Domain != "" || cookie.Path != "/" || cookie.SameSite != http.SameSiteLaxMode {
		t.Fatal("cookie protections missing")
	}
}
