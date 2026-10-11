// SPDX-License-Identifier: GPL-3.0-only
package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/PapaBill1234/phpretro-port/internal/auth"
	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

type AccountService interface {
	Login(context.Context, string, string, string, string, string) (string, auth.Session, auth.Principal, error)
	Current(context.Context, string, string) (auth.Session, auth.Principal, error)
	Logout(context.Context, string) error
}
type SettingsStore interface {
	Backend
	UpdateSite(context.Context, auth.Proof, storage.Site) (storage.Site, error)
}
type AccountConfig struct {
	Origin         string
	Secure         bool
	Cookie         string
	TrustedProxies []netip.Prefix
}

func NewAccountConfig(origin string, secure bool, proxies string) (AccountConfig, error) {
	c := AccountConfig{Origin: origin, Secure: secure, Cookie: "__Host-phpretro_session"}
	u, err := url.Parse(origin)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" || u.Opaque != "" {
		return c, errors.New("invalid public origin")
	}
	if secure {
		if u.Scheme != "https" {
			return c, errors.New("secure cookies require HTTPS")
		}
	} else {
		ip, parseErr := netip.ParseAddr(u.Hostname())
		if u.Scheme != "http" || (u.Hostname() != "localhost" && (parseErr != nil || !ip.IsLoopback())) {
			return c, errors.New("insecure cookies require loopback HTTP")
		}
		c.Cookie = "phpretro_session"
	}
	if proxies != "" {
		for _, v := range strings.Split(proxies, ",") {
			p, err := netip.ParsePrefix(strings.TrimSpace(v))
			if err != nil {
				return c, errors.New("invalid trusted proxy range")
			}
			c.TrustedProxies = append(c.TrustedProxies, p)
		}
	}
	return c, nil
}
func (c AccountConfig) peer(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	peer, err := netip.ParseAddr(host)
	if err != nil {
		return "unknown"
	}
	peer = peer.Unmap()
	for _, p := range c.TrustedProxies {
		if p.Contains(peer) {
			if candidate, err := netip.ParseAddr(r.Header.Get("X-Real-IP")); err == nil {
				return candidate.Unmap().String()
			}
		}
	}
	return peer.String()
}
func (c AccountConfig) originOK(r *http.Request) bool {
	return len(r.Header.Values("Origin")) == 1 && r.Header.Get("Origin") == c.Origin
}
func (c AccountConfig) token(r *http.Request) string {
	cookie, err := r.Cookie(c.Cookie)
	if err != nil {
		return ""
	}
	return cookie.Value
}
func (c AccountConfig) cookie(w http.ResponseWriter, value string, clear bool) {
	cookie := &http.Cookie{Name: c.Cookie, Value: value, Path: "/", Secure: c.Secure, HttpOnly: true, SameSite: http.SameSiteLaxMode, MaxAge: int(auth.SessionTTL / time.Second)}
	if clear {
		cookie.Value = ""
		cookie.MaxAge = -1
		cookie.Expires = time.Unix(1, 0)
	}
	http.SetCookie(w, cookie)
}
func failure(w http.ResponseWriter, status int, code string) {
	respond(w, status, map[string]any{"error": map[string]string{"code": code, "message": "The request could not be completed."}})
}
func authFailure(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, auth.ErrInvalid):
		failure(w, 401, "invalid_credentials")
	case errors.Is(err, auth.ErrUnauthenticated):
		failure(w, 401, "unauthenticated")
	case errors.Is(err, auth.ErrForbidden):
		failure(w, 403, "forbidden")
	case errors.Is(err, auth.ErrLimited):
		w.Header().Set("Retry-After", "900")
		failure(w, 429, "rate_limited")
	default:
		failure(w, 503, "dependency_unavailable")
	}
}
func jsonBody(w http.ResponseWriter, r *http.Request, target any) bool {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		failure(w, 415, "invalid_request")
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 8192)
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			failure(w, 413, "invalid_request")
		} else {
			failure(w, 400, "invalid_request")
		}
		return false
	}
	if !utf8.Valid(raw) {
		failure(w, 400, "invalid_request")
		return false
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if decoder.Decode(target) != nil {
		failure(w, 400, "invalid_request")
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		failure(w, 400, "invalid_request")
		return false
	}
	return true
}

func HandlerWithAccounts(store SettingsStore, accounts AccountService, config AccountConfig, identityReady func(context.Context) error) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/", Handler(store))
	mux.HandleFunc("GET /api/v1/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		db := store.DatabaseReady(ctx) == nil
		cache := store.RedisReady(ctx) == nil
		identity := identityReady(ctx) == nil
		code, status := 200, "ok"
		if !db || !cache || !identity {
			code, status = 503, "degraded"
		}
		respond(w, code, map[string]any{"status": status, "database": db, "redis": cache, "identity": identity})
	})
	mux.HandleFunc("POST /api/v1/auth/login", func(w http.ResponseWriter, r *http.Request) {
		if !config.originOK(r) {
			failure(w, 403, "invalid_origin")
			return
		}
		var in struct {
			Username string `json:"username"`
			Password string `json:"password"`
			OTP      string `json:"otp"`
		}
		if !jsonBody(w, r, &in) {
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		token, session, principal, err := accounts.Login(ctx, in.Username, in.Password, in.OTP, config.peer(r), config.token(r))
		if err != nil {
			authFailure(w, err)
			return
		}
		config.cookie(w, token, false)
		respond(w, 200, auth.ViewFor(session, principal))
	})
	mux.HandleFunc("GET /api/v1/auth/session", func(w http.ResponseWriter, r *http.Request) {
		token := config.token(r)
		if token == "" {
			respond(w, 200, auth.Guest())
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		session, principal, err := accounts.Current(ctx, token, config.peer(r))
		if err != nil {
			if errors.Is(err, auth.ErrUnauthenticated) {
				config.cookie(w, "", true)
				respond(w, 200, auth.Guest())
			} else {
				authFailure(w, err)
			}
			return
		}
		respond(w, 200, auth.ViewFor(session, principal))
	})
	current := func(w http.ResponseWriter, r *http.Request, ctx context.Context, mutate, admin bool) (auth.Session, bool) {
		if mutate && !config.originOK(r) {
			failure(w, 403, "invalid_origin")
			return auth.Session{}, false
		}
		session, principal, err := accounts.Current(ctx, config.token(r), config.peer(r))
		if err != nil {
			authFailure(w, err)
			return session, false
		}
		if admin && principal.Role != "admin" {
			authFailure(w, auth.ErrForbidden)
			return session, false
		}
		if mutate && (len(r.Header.Values("X-CSRF-Token")) != 1 || !auth.Equal(r.Header.Get("X-CSRF-Token"), session.CSRF)) {
			failure(w, 403, "invalid_csrf")
			return session, false
		}
		return session, true
	}
	mux.HandleFunc("POST /api/v1/auth/logout", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		if _, ok := current(w, r, ctx, true, false); !ok {
			return
		}
		if err := accounts.Logout(ctx, config.token(r)); err != nil {
			authFailure(w, err)
			return
		}
		config.cookie(w, "", true)
		respond(w, 200, auth.Guest())
	})
	mux.HandleFunc("GET /api/v1/housekeeping/site", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		if _, ok := current(w, r, ctx, false, true); !ok {
			return
		}
		site, err := store.Site(ctx)
		if err != nil {
			failure(w, 503, "dependency_unavailable")
			return
		}
		respond(w, 200, site)
	})
	mux.HandleFunc("PUT /api/v1/housekeeping/site", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		session, ok := current(w, r, ctx, true, true)
		if !ok {
			return
		}
		var in struct {
			Version          string   `json:"version"`
			SiteName         string   `json:"siteName"`
			ActiveTheme      string   `json:"activeTheme"`
			DefaultLocale    string   `json:"defaultLocale"`
			EnabledLocales   []string `json:"enabledLocales"`
			ExpectedRevision uint64   `json:"expectedRevision"`
		}
		if !jsonBody(w, r, &in) {
			return
		}
		site := storage.Site{Version: "site.v1", SiteName: strings.TrimSpace(in.SiteName), ActiveTheme: in.ActiveTheme, DefaultLocale: in.DefaultLocale, EnabledLocales: in.EnabledLocales, SettingsRevision: in.ExpectedRevision, Health: "ok"}
		if in.Version != "site-update.v1" || storage.ValidateSite(site) != nil {
			failure(w, 400, "invalid_request")
			return
		}
		site, err := store.UpdateSite(ctx, session.Proof(), site)
		if err != nil {
			if errors.Is(err, storage.ErrConflict) {
				failure(w, 409, "settings_conflict")
			} else {
				authFailure(w, err)
			}
			return
		}
		respond(w, 200, site)
	})
	return mux
}
