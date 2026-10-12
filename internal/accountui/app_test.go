// SPDX-License-Identifier: GPL-3.0-only
package accountui

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	gateway "github.com/PapaBill1234/phpretro-port/internal/accountgateway"
	vault "github.com/PapaBill1234/phpretro-port/internal/accountvault"
	jwt "github.com/golang-jwt/jwt/v5"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const allScopes = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"

type grant struct{ subject, nonce, client, verifierChallenge string }
type fixture struct {
	tokenEntered, tokenRelease      chan struct{}
	storeDir                        string
	provider                        *httptest.Server
	ui                              *httptest.Server
	ep                              gateway.Endpoints
	app                             *App
	store                           *vault.Vault
	browser                         *http.Client
	mu                              sync.Mutex
	subject                         string
	grants                          map[string]grant
	key                             *rsa.PrivateKey
	kid                             string
	jwksHits, tokenHits, revokeHits atomic.Int32
	revokeStatus                    int
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{subject: "one", grants: map[string]grant{}, kid: "key-1", revokeStatus: 200}
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		t.Fatal(e)
	}
	f.key = key
	f.provider = httptest.NewTLSServer(http.HandlerFunc(f.serveProvider))
	t.Cleanup(f.provider.Close)
	base := f.provider.URL
	f.ep = gateway.Endpoints{Issuer: base, Discovery: base + "/discovery", Authorization: base + "/authorize", Token: base + "/token", JWKS: base + "/jwks", Revocation: base + "/revoke"}
	f.storeDir = filepath.Join(t.TempDir(), "vault")
	f.store, e = vault.Open(f.storeDir)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { f.store.Close() })
	listener, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	callback := "http://" + listener.Addr().String() + "/auth/callback"
	f.app, e = New(f.store, callback, f.ep, f.provider.Client())
	if e != nil {
		t.Fatal(e)
	}
	f.ui = httptest.NewUnstartedServer(f.app)
	f.ui.Listener = listener
	f.ui.Start()
	t.Cleanup(f.ui.Close)
	jar, _ := cookiejar.New(nil)
	f.browser = &http.Client{Jar: jar, Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	return f
}
func (f *fixture) serveProvider(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch r.URL.Path {
	case "/discovery":
		json.NewEncoder(w).Encode(map[string]string{"issuer": f.ep.Issuer, "authorization_endpoint": f.ep.Authorization, "token_endpoint": f.ep.Token, "jwks_uri": f.ep.JWKS, "revocation_endpoint": f.ep.Revocation})
	case "/jwks":
		f.jwksHits.Add(1)
		json.NewEncoder(w).Encode(map[string]any{"keys": []any{map[string]any{"kty": "RSA", "kid": f.kid, "alg": "RS256", "n": base64.RawURLEncoding.EncodeToString(f.key.N.Bytes()), "e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(f.key.E)).Bytes())}}})
	case "/authorize":
		q := r.URL.Query()
		client := q.Get("client_id")
		if client == "dynamic_agent_client" {
			client = "issued-" + f.subject
		}
		code := fmt.Sprint("code-", len(f.grants))
		f.grants[code] = grant{subject: f.subject, nonce: q.Get("nonce"), client: client, verifierChallenge: q.Get("code_challenge")}
		callback, _ := url.Parse(q.Get("redirect_uri"))
		callback.RawQuery = url.Values{"state": {q.Get("state")}, "code": {code}, "client_id": {client}}.Encode()
		http.Redirect(w, r, callback.String(), 303)
	case "/token":
		f.tokenHits.Add(1)
		if f.tokenEntered != nil {
			close(f.tokenEntered)
			<-f.tokenRelease
		}
		if r.Method != "POST" || r.ParseForm() != nil {
			w.WriteHeader(400)
			return
		}
		g, ok := f.grants[r.Form.Get("code")]
		if !ok || r.Form.Get("client_id") != g.client {
			w.WriteHeader(400)
			return
		}
		// PKCE verified by the provider fixture, not merely asserted present.
		if challenge(r.Form.Get("code_verifier")) != g.verifierChallenge {
			w.WriteHeader(400)
			return
		}
		delete(f.grants, r.Form.Get("code"))
		tok := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{"iss": f.ep.Issuer, "sub": g.subject, "aud": g.client, "nonce": g.nonce, "iat": time.Now().Unix(), "exp": time.Now().Add(time.Hour).Unix()})
		tok.Header["kid"] = f.kid
		signed, e := tok.SignedString(f.key)
		if e != nil {
			w.WriteHeader(500)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"id_token": signed, "access_token": "access-secret-" + g.subject, "refresh_token": "refresh-secret-" + g.subject, "token_type": "Bearer", "expires_in": 3600, "scope": allScopes, "client_id": g.client})
	case "/revoke":
		f.revokeHits.Add(1)
		if r.ParseForm() != nil || r.Form.Get("token_type_hint") != "refresh_token" || !strings.HasPrefix(r.Form.Get("token"), "refresh-secret-") || !strings.HasPrefix(r.Form.Get("client_id"), "issued-") {
			w.WriteHeader(400)
			return
		}
		w.WriteHeader(f.revokeStatus)
	default:
		w.WriteHeader(404)
	}
}
func challenge(verifier string) string {
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}
func readBody(t *testing.T, r *http.Response) string {
	t.Helper()
	defer r.Body.Close()
	b, e := io.ReadAll(r.Body)
	if e != nil {
		t.Fatal(e)
	}
	return string(b)
}
func (f *fixture) csrf(t *testing.T) string {
	t.Helper()
	r, e := f.browser.Get(f.ui.URL + "/")
	if e != nil {
		t.Fatal(e)
	}
	if r.StatusCode != 200 {
		t.Fatal("page", r.StatusCode)
	}
	body := readBody(t, r)
	if strings.Contains(body, "access-secret") || strings.Contains(body, "refresh-secret") {
		t.Fatal("page leaked credentials")
	}
	matches := regexp.MustCompile(`name="csrf" value="([^"]+)"`).FindStringSubmatch(body)
	if len(matches) != 2 {
		t.Fatal("CSRF field missing")
	}
	return matches[1]
}
func (f *fixture) post(t *testing.T, path, label string) *http.Response {
	t.Helper()
	csrf := f.csrf(t)
	req, _ := http.NewRequest("POST", f.ui.URL+path, strings.NewReader(url.Values{"label": {label}, "csrf": {csrf}}.Encode()))
	req.Header.Set("Origin", f.ui.URL)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r, e := f.browser.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func (f *fixture) authorize(t *testing.T, label, subject string) string {
	t.Helper()
	f.mu.Lock()
	f.subject = subject
	f.mu.Unlock()
	r := f.post(t, "/begin", label)
	if r.StatusCode != 303 {
		t.Fatal("begin", r.StatusCode, readBody(t, r))
	}
	loc := r.Header.Get("Location")
	r.Body.Close()
	client := f.provider.Client()
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	r, e := client.Get(loc)
	if e != nil {
		t.Fatal(e)
	}
	defer r.Body.Close()
	if r.StatusCode != 303 {
		t.Fatal("authorize")
	}
	return r.Header.Get("Location")
}
func (f *fixture) complete(t *testing.T, callback string) *http.Response {
	t.Helper()
	r, e := f.browser.Get(callback)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func (f *fixture) login(t *testing.T, label, subject string) {
	t.Helper()
	r := f.complete(t, f.authorize(t, label, subject))
	defer r.Body.Close()
	if r.StatusCode != 303 || r.Header.Get("Location") != "/?notice=signed-in" {
		t.Fatal("callback", r.StatusCode, r.Header.Get("Location"))
	}
}
func TestTwoAccountSignInSwitchAndRestart(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	if f.store.State().Active != "" {
		t.Fatal("sign-in implicitly selected account")
	}
	r := f.post(t, "/select", "account-1")
	r.Body.Close()
	if r.StatusCode != 303 {
		t.Fatal("select")
	}
	f.login(t, "account-2", "two")
	if f.store.State().Active != "account-1" {
		t.Fatal("second sign-in changed active account")
	}
	r = f.post(t, "/select", "account-2")
	r.Body.Close()
	if r.StatusCode != 303 {
		t.Fatal("switch")
	}
	var token string
	if e := f.app.WithActive(func(c gateway.Credentials) error { token = c.AccessToken; return nil }); e != nil || token != "access-secret-two" {
		t.Fatal("wrong credential selected")
	}
	before := f.store.State()
	if len(before.Accounts) != 2 {
		t.Fatal("accounts lost")
	}
	// Restore uses encrypted registrations; a new app does not replay historical OAuth proof.
	f.ui.Close()
	f.store.Close()
	reopened, e := vault.Open(f.storeDir)
	if e != nil {
		t.Fatal(e)
	}
	f.store = reopened
	next, e := New(f.store, f.ui.URL+"/auth/callback", f.ep, f.provider.Client())
	if e != nil {
		t.Fatal(e)
	}
	if e = next.WithActive(func(c gateway.Credentials) error {
		if c.AccessToken != "access-secret-two" {
			t.Fatal("wrong restored account")
		}
		return nil
	}); e != nil {
		t.Fatal(e)
	}
	if f.store.State().HostID != before.HostID || f.store.State().Active != "account-2" {
		t.Fatal("lost registration selection")
	}
}
func TestCallbackCookieDuplicateStateReplayAndReturningIdentity(t *testing.T) {
	f := newFixture(t)
	callback := f.authorize(t, "account-1", "one")
	r, e := http.Get(callback)
	if e != nil {
		t.Fatal(e)
	}
	r.Body.Close()
	if r.StatusCode != 403 || f.tokenHits.Load() != 0 {
		t.Fatal("cookie bypass")
	}
	u, _ := url.Parse(callback)
	bad := callback + "&state=" + url.QueryEscape(u.Query().Get("state"))
	r = f.complete(t, bad)
	r.Body.Close()
	if r.StatusCode != 400 || f.tokenHits.Load() != 0 {
		t.Fatal("duplicate callback accepted")
	}
	r = f.complete(t, callback)
	r.Body.Close()
	if r.Header.Get("Location") != "/?notice=signed-in" {
		t.Fatal("valid callback lost")
	}
	r = f.complete(t, callback)
	r.Body.Close()
	if r.StatusCode != 403 || f.tokenHits.Load() != 1 {
		t.Fatal("callback replayed")
	}
	callback = f.authorize(t, "account-1", "different-user")
	r = f.complete(t, callback)
	r.Body.Close()
	if r.Header.Get("Location") != "/?notice=sign-in-failed" || f.store.State().Accounts[0].Subject != "one" {
		t.Fatal("returning identity replaced")
	}
}
func TestCSRFAndHostFailBeforeProvider(t *testing.T) {
	f := newFixture(t)
	csrf := f.csrf(t)
	for _, origin := range []string{"", "https://evil.example"} {
		req, _ := http.NewRequest("POST", f.ui.URL+"/begin", strings.NewReader(url.Values{"label": {"account-1"}, "csrf": {csrf}}.Encode()))
		req.Header.Set("Origin", origin)
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		r, e := f.browser.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		r.Body.Close()
		if r.StatusCode != 403 {
			t.Fatal("origin accepted")
		}
	}
	req, _ := http.NewRequest("GET", f.ui.URL+"/", nil)
	req.Host = "attacker.example"
	r, e := f.browser.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	r.Body.Close()
	if r.StatusCode != 403 {
		t.Fatal("host accepted")
	}
	if f.tokenHits.Load() != 0 {
		t.Fatal("provider contacted")
	}
}
func TestLogoutCancelsPendingAndRetainsMapping(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	callback := f.authorize(t, "account-1", "one")
	r := f.post(t, "/logout", "account-1")
	r.Body.Close()
	state := f.store.State()
	x := state.Accounts[0]
	if r.StatusCode != 303 || x.RefreshToken != "" || x.IDToken != "" || x.ClientID != "issued-one" || x.Subject != "one" || !x.NeedsSignIn || f.revokeHits.Load() != 1 {
		t.Fatal("sign-out state")
	}
	r = f.complete(t, callback)
	r.Body.Close()
	if r.StatusCode != 403 {
		t.Fatal("late callback restored signed-out account")
	}
	if e := f.app.WithActive(func(gateway.Credentials) error { t.Fatal("used signed-out token"); return nil }); e == nil {
		t.Fatal("signed-out active credential")
	}
}
func TestRevocationUnconfirmedAndPendingRestart(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	f.mu.Lock()
	f.revokeStatus = 503
	f.mu.Unlock()
	r := f.post(t, "/logout", "account-1")
	r.Body.Close()
	if r.Header.Get("Location") != "/?notice=revocation-unconfirmed" || !f.store.State().Accounts[0].RevocationUnconfirmed {
		t.Fatal("false remote confirmation")
	}
	f.login(t, "account-1", "one")
	state := f.store.State()
	state.Accounts[0].PendingOperation = "logout"
	state.Active = ""
	if e := f.store.Save(state); e != nil {
		t.Fatal(e)
	}
	next, e := New(f.store, f.ui.URL+"/auth/callback", f.ep, f.provider.Client())
	if e != nil {
		t.Fatal(e)
	}
	if next == nil || f.store.State().Accounts[0].AccessToken != "" || !f.store.State().Accounts[0].RevocationUnconfirmed {
		t.Fatal("crash recovery reused tokens")
	}
}

type failingStore struct {
	Store
	fail bool
}

func (s *failingStore) Save(v vault.State) error {
	if s.fail {
		return fmt.Errorf("private-secret")
	}
	return s.Store.Save(v)
}
func TestPersistenceFailureBlocksCallbackAndSelection(t *testing.T) {
	f := newFixture(t)
	failed := &failingStore{Store: f.store}
	f.app.store = failed // commit consults a.store; startup hint remains read-only.
	callback := f.authorize(t, "account-1", "one")
	failed.fail = true
	r := f.complete(t, callback)
	body := readBody(t, r)
	if r.StatusCode != 503 || strings.Contains(body, "secret") || len(f.store.State().Accounts) != 0 || !f.app.blocked {
		t.Fatal("failed save advertised success")
	}
	if e := f.app.WithActive(func(gateway.Credentials) error { t.Fatal("credential use after save failure"); return nil }); e == nil {
		t.Fatal("save failure did not block")
	}
}
func TestUnknownSigningKeyRefreshIsBounded(t *testing.T) {
	f := newFixture(t)
	initial := f.jwksHits.Load()
	f.mu.Lock()
	f.kid = "key-2"
	f.mu.Unlock()
	f.login(t, "account-1", "one")
	if f.jwksHits.Load() != initial+1 {
		t.Fatal("unknown key not refreshed")
	}
}

func TestFailedSelectionAndSignOutStayBlocked(t *testing.T) {
	for _, operation := range []string{"/select", "/logout"} {
		t.Run(operation, func(t *testing.T) {
			f := newFixture(t)
			f.login(t, "account-1", "one")
			f.login(t, "account-2", "two")
			r := f.post(t, "/select", "account-1")
			r.Body.Close()
			bad := &failingStore{Store: f.store, fail: true}
			f.app.store = bad
			r = f.post(t, operation, "account-2")
			r.Body.Close()
			if r.StatusCode != 503 || f.store.State().Active != "account-1" || !f.app.blocked || f.revokeHits.Load() != 0 {
				t.Fatal("failed durable operation published state")
			}
		})
	}
}
func TestCallbackWrongStateAndProviderDenialDoNotExchange(t *testing.T) {
	f := newFixture(t)
	callback := f.authorize(t, "account-1", "one")
	u, _ := url.Parse(callback)
	q := u.Query()
	q.Set("state", "wrong")
	u.RawQuery = q.Encode()
	r := f.complete(t, u.String())
	r.Body.Close()
	if r.StatusCode != 403 || f.tokenHits.Load() != 0 {
		t.Fatal("wrong state exchanged")
	}
	u, _ = url.Parse(callback)
	q = u.Query()
	q.Del("code")
	q.Set("error", "access_denied")
	u.RawQuery = q.Encode()
	r = f.complete(t, u.String())
	r.Body.Close()
	if r.Header.Get("Location") != "/?notice=sign-in-failed" || f.tokenHits.Load() != 0 {
		t.Fatal("provider denial exchanged")
	}
	r = f.complete(t, callback)
	r.Body.Close()
	if r.StatusCode != 403 {
		t.Fatal("denied transaction replayed")
	}
}
func TestExpiredSelectionFailsWithoutFallback(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	f.login(t, "account-2", "two")
	state := f.store.State()
	state.Accounts[0].ExpiresAt = time.Now().Add(-time.Second)
	state.Active = "account-1"
	if e := f.store.Save(state); e != nil {
		t.Fatal(e)
	}
	if e := f.app.WithActive(func(gateway.Credentials) error { t.Fatal("fell back to other account"); return nil }); e == nil {
		t.Fatal("expired credential used")
	}
	r := f.post(t, "/select", "account-1")
	r.Body.Close()
	if r.StatusCode != 409 {
		t.Fatal("expired account selected")
	}
}
func TestUnknownKeyCooldownAndDiscoveryMismatch(t *testing.T) {
	f := newFixture(t)
	f.mu.Lock()
	f.kid = "key-2"
	f.mu.Unlock()
	f.login(t, "account-1", "one")
	hits := f.jwksHits.Load()
	f.mu.Lock()
	f.kid = "key-3"
	f.mu.Unlock()
	callback := f.authorize(t, "account-2", "two")
	r := f.complete(t, callback)
	r.Body.Close()
	if r.Header.Get("Location") != "/?notice=sign-in-failed" || f.jwksHits.Load() != hits || len(f.store.State().Accounts) != 1 {
		t.Fatal("unknown kid flood refreshed or authenticated")
	}
	bad := f.ep
	bad.Token = f.provider.URL + "/wrong-token"
	if _, e := gateway.NewProvider(bad, f.provider.Client()); e == nil {
		t.Fatal("mismatched discovery accepted")
	}
}

type nthFailureStore struct {
	Store
	n      int
	failAt int
}

func (s *nthFailureStore) Save(v vault.State) error {
	s.n++
	if s.n == s.failAt {
		return fmt.Errorf("private-secret")
	}
	return s.Store.Save(v)
}
func TestPostRevocationSaveFailureRecoversMarkerOnRestart(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	bad := &nthFailureStore{Store: f.store, failAt: 2}
	f.app.store = bad
	r := f.post(t, "/logout", "account-1")
	r.Body.Close()
	if r.StatusCode != 503 || f.store.State().Accounts[0].PendingOperation != "logout" || f.revokeHits.Load() != 1 {
		t.Fatal("missing durable sign-out recovery marker")
	}
	if _, e := New(f.store, f.ui.URL+"/auth/callback", f.ep, f.provider.Client()); e != nil {
		t.Fatal(e)
	}
	x := f.store.State().Accounts[0]
	if x.AccessToken != "" || !x.NeedsSignIn || !x.RevocationUnconfirmed {
		t.Fatal("restart reused uncertain sign-out")
	}
}

func TestCallbackAndSignOutAreOrdered(t *testing.T) {
	f := newFixture(t)
	f.login(t, "account-1", "one")
	csrf := f.csrf(t)
	callback := f.authorize(t, "account-1", "one")
	entered, release := make(chan struct{}), make(chan struct{})
	f.mu.Lock()
	f.tokenEntered = entered
	f.tokenRelease = release
	f.mu.Unlock()
	callbackDone := make(chan *http.Response, 1)
	go func() {
		r, e := f.browser.Get(callback)
		if e != nil {
			callbackDone <- nil
			return
		}
		callbackDone <- r
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("exchange not entered")
	}
	req, _ := http.NewRequest("POST", f.ui.URL+"/logout", strings.NewReader(url.Values{"label": {"account-1"}, "csrf": {csrf}}.Encode()))
	req.Header.Set("Origin", f.ui.URL)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	logoutDone := make(chan *http.Response, 1)
	go func() {
		r, e := f.browser.Do(req)
		if e != nil {
			logoutDone <- nil
			return
		}
		logoutDone <- r
	}()
	select {
	case r := <-logoutDone:
		close(release)
		if r != nil {
			r.Body.Close()
		}
		t.Fatal("sign-out interleaved before callback committed")
	case <-time.After(30 * time.Millisecond):
	}
	close(release)
	for _, done := range []chan *http.Response{callbackDone, logoutDone} {
		select {
		case r := <-done:
			if r == nil {
				t.Fatal("HTTP operation failed")
			}
			r.Body.Close()
			if r.StatusCode != 303 {
				t.Fatal("ordered operation failed", r.StatusCode)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("operation deadlocked")
		}
	}
	if x := f.store.State().Accounts[0]; x.AccessToken != "" || !x.NeedsSignIn {
		t.Fatal("late callback resurrected sign-out")
	}
}
