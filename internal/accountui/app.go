// SPDX-License-Identifier: GPL-3.0-only
// Package accountui serves the local account manager. It never sends tokens to the browser.
package accountui

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	gateway "github.com/PapaBill1234/phpretro-port/internal/accountgateway"
	vault "github.com/PapaBill1234/phpretro-port/internal/accountvault"
	"html/template"
	"net/http"
	"net/url"
	"sync"
	"time"
)

type Store interface {
	State() vault.State
	Save(vault.State) error
}
type browser struct {
	csrf    string
	expires time.Time
}
type attempt struct {
	label, session string
	expires        time.Time
}
type App struct {
	mu                           sync.Mutex // One lifecycle order across callbacks, selections and sign-out.
	store                        Store
	g                            *gateway.Gateway
	provider                     *gateway.Provider
	origin, host, providerOrigin string
	blocked                      bool
	browsers                     map[string]browser
	attempts                     map[string]attempt
}

func New(store Store, callback string, ep gateway.Endpoints, client *http.Client) (*App, error) {
	u, e := url.Parse(callback)
	if e != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Path != "/auth/callback" {
		return nil, gateway.ErrInvalid
	}
	provider, e := gateway.NewProvider(ep, client)
	if e != nil {
		return nil, e
	}
	providerURL, _ := url.Parse(ep.Authorization)
	a := &App{store: store, provider: provider, providerOrigin: providerURL.Scheme + "://" + providerURL.Host, origin: "http://" + u.Host, host: u.Host, browsers: map[string]browser{}, attempts: map[string]attempt{}}
	state := store.State()
	changed := false
	for i := range state.Accounts {
		if state.Accounts[i].PendingOperation != "" {
			clear(&state.Accounts[i])
			state.Accounts[i].RevocationUnconfirmed = true
			if state.Active == state.Accounts[i].Label {
				state.Active = ""
			}
			changed = true
		}
	}
	if changed && store.Save(state) != nil {
		return nil, gateway.ErrUnavailable
	}
	cfg := gateway.NewOpenAIConfig(callback, state.HostID, "PHPRetro Account Gateway", provider, provider)
	cfg.AuthorizationEndpoint = ep.Authorization
	cfg.Commit = a.commit
	cfg.Hint = func(label string) string {
		for _, x := range store.State().Accounts {
			if x.Label == label {
				return x.IDToken
			}
		}
		return ""
	}
	a.g = gateway.New(cfg)
	profiles := []gateway.Profile{}
	for _, x := range state.Accounts {
		profiles = append(profiles, gateway.Profile{Label: x.Label, ClientID: x.ClientID, Identity: x.Subject, HostID: state.HostID, Active: x.Label == state.Active && usable(x)})
	}
	if a.g.RestoreProfiles(profiles) != nil {
		return nil, gateway.ErrInvalid
	}
	return a, nil
}
func usable(x vault.Account) bool {
	return !x.NeedsSignIn && x.PendingOperation == "" && x.AccessToken != "" && x.RefreshToken != "" && time.Now().Before(x.ExpiresAt)
}
func clear(x *vault.Account) {
	x.IDToken = ""
	x.AccessToken = ""
	x.RefreshToken = ""
	x.ExpiresAt = time.Time{}
	x.Scopes = nil
	x.NeedsSignIn = true
	x.PendingOperation = ""
}
func (a *App) save(s vault.State) error {
	if e := a.store.Save(s); e != nil {
		a.blocked = true
		return gateway.ErrUnavailable
	}
	return nil
}
func (a *App) commit(p gateway.Profile, r gateway.RawToken) error {
	if a.blocked || r.Credentials == nil || r.Credentials.AccessToken == "" || r.Credentials.RefreshToken == "" || !time.Now().Before(r.Credentials.ExpiresAt) {
		return gateway.ErrDenied
	}
	state := a.store.State()
	x := vault.Account{Label: p.Label, ClientID: p.ClientID, Subject: p.Identity, IDToken: r.Signed, AccessToken: r.Credentials.AccessToken, RefreshToken: r.Credentials.RefreshToken, ExpiresAt: r.Credentials.ExpiresAt, Scopes: append([]string(nil), r.Scopes...)}
	found := false
	for i, v := range state.Accounts {
		if v.Label == p.Label {
			state.Accounts[i] = x
			found = true
		}
	}
	if !found {
		state.Accounts = append(state.Accounts, x)
	}
	return a.save(state)
}
func random() string {
	b := make([]byte, 32)
	if _, e := rand.Read(b); e != nil {
		return ""
	}
	return base64.RawURLEncoding.EncodeToString(b)
}
func equal(a, b string) bool { return a != "" && subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1 }

const cookieName = "phpretro_account_browser"

func (a *App) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	a.mu.Lock()
	defer a.mu.Unlock()
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "+a.providerOrigin+"; frame-ancestors 'none'; base-uri 'none'")
	if r.URL.RawPath != "" {
		http.Error(w, "Invalid local path.", 400)
		return
	}
	if r.Host != a.host {
		http.Error(w, "Invalid local host.", 403)
		return
	}
	if len(r.URL.RawQuery) > 65536 {
		http.Error(w, "Invalid request.", 400)
		return
	}
	if r.URL.Path == "/auth/callback" {
		a.callback(w, r)
		return
	}
	if r.URL.Path != "/" && r.URL.Path != "/begin" && r.URL.Path != "/select" && r.URL.Path != "/logout" {
		http.NotFound(w, r)
		return
	}
	if r.URL.Path == "/" && r.Method == "GET" {
		a.page(w, r)
		return
	}
	if r.Method != "POST" {
		http.Error(w, "Method not allowed.", 405)
		return
	}
	if a.blocked {
		http.Error(w, "Private storage failed. Account operations are blocked; restart after repairing storage.", 503)
		return
	}
	if r.Header.Get("Origin") != a.origin {
		http.Error(w, "Invalid request origin.", 403)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	if r.ParseForm() != nil {
		http.Error(w, "Invalid request.", 400)
		return
	}
	for _, values := range r.Form {
		if len(values) != 1 {
			http.Error(w, "Invalid request.", 400)
			return
		}
	}
	if r.URL.RawQuery != "" {
		http.Error(w, "Invalid request.", 400)
		return
	}
	c, e := r.Cookie(cookieName)
	if e != nil {
		http.Error(w, "Invalid browser session.", 403)
		return
	}
	b, ok := a.browsers[c.Value]
	if !ok || !time.Now().Before(b.expires) || !equal(b.csrf, r.PostForm.Get("csrf")) {
		http.Error(w, "Invalid browser session.", 403)
		return
	}
	label := r.PostForm.Get("label")
	if label != "account-1" && label != "account-2" {
		http.Error(w, "Invalid account.", 400)
		return
	}
	switch r.URL.Path {
	case "/begin":
		pending, e := a.g.Begin(label)
		if e != nil {
			http.Error(w, "Sign-in is unavailable or already pending.", 409)
			return
		}
		q, _ := url.Parse(pending.AuthorizeURL)
		for state, t := range a.attempts {
			if !time.Now().Before(t.expires) {
				delete(a.attempts, state)
			}
		}
		a.attempts[q.Query().Get("state")] = attempt{label: label, session: c.Value, expires: pending.ExpiresAt}
		// ID-token hints appear only in the provider authorization redirect, never logs or page data.
		http.Redirect(w, r, pending.AuthorizeURL, 303)
	case "/select":
		state := a.store.State()
		found := false
		for _, x := range state.Accounts {
			if x.Label == label && usable(x) {
				found = true
			}
		}
		if !found {
			http.Error(w, "Sign in to this account first.", 409)
			return
		}
		state.Active = label
		if a.save(state) != nil {
			http.Error(w, "Private storage failed. Account operations are blocked.", 503)
			return
		}
		if a.g.Select(label) != nil {
			a.blocked = true
			http.Error(w, "Account selection failed.", 503)
			return
		}
		http.Redirect(w, r, "/?notice=selected", 303)
	case "/logout":
		a.g.Cancel(label)
		for k, t := range a.attempts {
			if t.label == label {
				delete(a.attempts, k)
			}
		}
		state := a.store.State()
		index := -1
		for i, x := range state.Accounts {
			if x.Label == label {
				index = i
			}
		}
		if index < 0 {
			http.Error(w, "Unknown account.", 400)
			return
		}
		old := state.Accounts[index]
		if state.Active == label {
			state.Active = ""
		}
		state.Accounts[index].PendingOperation = "logout"
		if a.save(state) != nil {
			http.Error(w, "Private storage failed. Account operations are blocked.", 503)
			return
		}
		confirmed := old.RefreshToken == "" || a.provider.Revoke(old.RefreshToken, old.ClientID) == nil
		clear(&state.Accounts[index])
		state.Accounts[index].RevocationUnconfirmed = !confirmed
		if a.save(state) != nil {
			http.Error(w, "Private storage failed. Restart to recover pending sign-out.", 503)
			return
		}
		notice := "signed-out"
		if !confirmed {
			notice = "revocation-unconfirmed"
		}
		http.Redirect(w, r, "/?notice="+notice, 303)
	}
}
func (a *App) callback(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" {
		http.Error(w, "Method not allowed.", 405)
		return
	}
	if a.blocked {
		http.Error(w, "Private storage failed. Account operations are blocked.", 503)
		return
	}
	q, e := url.ParseQuery(r.URL.RawQuery)
	if e != nil {
		http.Error(w, "Invalid callback.", 400)
		return
	}
	for _, v := range q {
		if len(v) != 1 {
			http.Error(w, "Invalid callback.", 400)
			return
		}
	}
	state := q.Get("state")
	t, ok := a.attempts[state]
	c, e := r.Cookie(cookieName)
	if !ok || e != nil || !equal(t.session, c.Value) {
		http.Error(w, "Invalid callback session.", 403)
		return
	}
	b, ok := a.browsers[c.Value]
	if !ok || !time.Now().Before(b.expires) {
		http.Error(w, "Browser session expired.", 403)
		return
	}
	delete(a.attempts, state) // No retry of a code after it may have reached the provider.
	_, e = a.g.Callback(t.label, state, q.Get("code"), q.Get("client_id"), a.origin+"/auth/callback", q.Get("error"))
	if e != nil {
		a.g.Cancel(t.label)
		if a.blocked {
			http.Error(w, "Private storage failed. Account operations are blocked.", 503)
			return
		}
		http.Redirect(w, r, "/?notice=sign-in-failed", 303)
		return
	}
	http.Redirect(w, r, "/?notice=signed-in", 303)
}

type row struct {
	Label, Title, Status           string
	Active, Connected, Unconfirmed bool
}
type view struct {
	CSRF, Notice string
	Rows         []row
	Blocked      bool
}

var page = template.Must(template.New("accounts").Parse(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ChatGPT accounts</title><style>body{font:16px system-ui;max-width:720px;margin:48px auto;padding:24px;background:#f5f6f8;color:#19202b}article{background:white;padding:24px;border-radius:16px;margin:16px 0;border:1px solid #d9dfe6}button{padding:10px 16px;margin:4px;border-radius:8px;border:1px solid #9da8b6;background:white;cursor:pointer}h1{font-size:32px}form{display:inline}p{line-height:1.5}.notice{padding:16px;background:#e7eefc;border-radius:10px}</style></head><body><h1>ChatGPT accounts</h1><p>Sign in to up to two accounts, then choose the account you want to use. Each account keeps its own authorization.</p>{{if .Notice}}<p class="notice">{{.Notice}}</p>{{end}}{{if .Blocked}}<p class="notice">Private storage failed. Account operations are blocked; restart after repairing storage.</p>{{else}}{{range .Rows}}<article><h2>{{.Title}}{{if .Active}} · Selected{{end}}</h2><p>{{.Status}}</p>{{if .Unconfirmed}}<p>Remote sign-out was not confirmed. You can disconnect this app in ChatGPT Settings.</p>{{end}}<form method="post" action="/begin"><input type="hidden" name="csrf" value="{{$.CSRF}}"><input type="hidden" name="label" value="{{.Label}}"><button>Sign in with ChatGPT</button></form>{{if .Connected}}<form method="post" action="/select"><input type="hidden" name="csrf" value="{{$.CSRF}}"><input type="hidden" name="label" value="{{.Label}}"><button>Use this account</button></form>{{end}}<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{$.CSRF}}"><input type="hidden" name="label" value="{{.Label}}"><button>Sign out</button></form></article>{{end}}{{end}}<p>Authorizations are completed on OpenAI’s sign-in page. This manager does not change the account signed in to Codex.</p></body></html>`))

func (a *App) page(w http.ResponseWriter, r *http.Request) {
	now := time.Now()
	for k, b := range a.browsers {
		if !now.Before(b.expires) {
			delete(a.browsers, k)
		}
	}
	var session string
	if c, e := r.Cookie(cookieName); e == nil {
		if _, ok := a.browsers[c.Value]; ok {
			session = c.Value
		}
	}
	if session == "" {
		if len(a.browsers) >= 32 {
			http.Error(w, "Too many browser sessions.", 429)
			return
		}
		session = random()
		csrf := random()
		if session == "" || csrf == "" {
			http.Error(w, "Browser session unavailable.", 503)
			return
		}
		a.browsers[session] = browser{csrf: csrf, expires: now.Add(30 * time.Minute)}
		http.SetCookie(w, &http.Cookie{Name: cookieName, Value: session, Path: "/", HttpOnly: true, SameSite: http.SameSiteLaxMode, MaxAge: 1800})
	}
	notices := map[string]string{"selected": "Account selected.", "signed-in": "Sign-in complete. Choose Use this account to select it.", "signed-out": "Signed out.", "revocation-unconfirmed": "Signed out locally. Remote revocation was not confirmed; disconnect this app in ChatGPT Settings if needed.", "sign-in-failed": "Sign-in failed or was declined. Start a new sign-in attempt."}
	state := a.store.State()
	v := view{CSRF: a.browsers[session].csrf, Notice: notices[r.URL.Query().Get("notice")], Blocked: a.blocked}
	for i, label := range []string{"account-1", "account-2"} {
		x := row{Label: label, Title: []string{"Account 1", "Account 2"}[i], Status: "Not signed in."}
		for _, s := range state.Accounts {
			if s.Label == label {
				x.Unconfirmed = s.RevocationUnconfirmed
				x.Connected = usable(s)
				x.Active = state.Active == label && x.Connected
				if x.Connected {
					x.Status = "Signed in. Authorization expires " + s.ExpiresAt.UTC().Format("15:04 UTC") + "."
				} else {
					x.Status = "Sign in required."
				}
			}
		}
		v.Rows = append(v.Rows, x)
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if page.Execute(w, v) != nil {
		return
	}
}

var _ http.Handler = (*App)(nil)

// WithActive pins the selected verified credential bundle for one backend operation.
// It never falls back to a different account.
func (a *App) WithActive(use func(gateway.Credentials) error) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.blocked || use == nil {
		return gateway.ErrUnavailable
	}
	state := a.store.State()
	for _, x := range state.Accounts {
		if x.Label == state.Active && usable(x) {
			return use(gateway.Credentials{AccessToken: x.AccessToken, RefreshToken: x.RefreshToken, ExpiresAt: x.ExpiresAt})
		}
	}
	return gateway.ErrDenied
}
