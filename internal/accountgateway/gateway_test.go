// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"errors"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeExchange struct {
	mu               sync.Mutex
	raw              RawToken
	err              error
	calls            int
	entered, release chan struct{}
}

func (f *fakeExchange) Exchange(_, client, _, _ string) (RawToken, error) {
	f.mu.Lock()
	f.calls++
	r, e := f.raw, f.err
	if r.ClientID == "" || r.ClientID == initialClient {
		r.ClientID = client
	}
	en, rel := f.entered, f.release
	f.mu.Unlock()
	if en != nil {
		close(en)
		<-rel
	}
	return r, e
}

type fakeValidator struct {
	id       VerifiedIdentity
	err      error
	subjects map[string]string
}

func (f fakeValidator) Verify(_ RawToken, e Expected) (VerifiedIdentity, error) {
	i := f.id
	if i.Subject == "" {
		i.Subject = f.subjects[e.ClientID]
		if i.Subject == "" {
			i.Subject = "acct+1"
		}
	}
	if i.ClientID == "" {
		i.ClientID = e.ClientID
	}
	if i.Nonce == "" {
		i.Nonce = e.Nonce
	}
	if i.HostID == "" {
		i.HostID = e.HostID
	}
	if i.Scopes == nil {
		i.Scopes = []string{"openid", "api-plan"}
	}
	return i, f.err
}

type fixture struct {
	g   *Gateway
	x   *fakeExchange
	now *time.Time
}

func waitExchange(t *testing.T, entered, release chan struct{}) {
	t.Helper()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		close(release)
		t.Fatal("exchange did not start before timeout")
	}
}

func setup(t *testing.T) fixture {
	t.Helper()
	now := time.Now()
	x := &fakeExchange{raw: RawToken{Signed: "signed", Scopes: []string{"openid", "api-plan"}}}
	g := New(Config{AuthorizationEndpoint: "https://login.example/authorize", CallbackURI: "http://127.0.0.1:49152/auth/callback", HostID: "stable-host", RequiredScopes: []string{"openid", "api-plan"}, Exchange: x, Validator: fakeValidator{}, Now: func() time.Time { return now }})
	if g.configErr != nil {
		t.Fatal(g.configErr)
	}
	return fixture{g, x, &now}
}
func start(t *testing.T, f fixture, label string) Pending {
	t.Helper()
	p, e := f.g.Begin(label)
	if e != nil {
		t.Fatal(e)
	}
	return p
}
func cbargs(t *testing.T, p Pending) (string, string) {
	if t != nil {
		t.Helper()
	}
	u, e := url.Parse(p.AuthorizeURL)
	if e != nil {
		if t != nil {
			t.Fatal(e)
		}
		return "", ""
	}
	client := u.Query().Get("client_id")
	if client == initialClient {
		client = "issued-" + p.Label
	}
	return u.Query().Get("state"), client
}
func finish(f fixture, label string, p Pending, client string) (Profile, error) {
	s, _ := cbargs(nil, p)
	return f.g.Callback(label, s, "code", client, f.g.cfg.CallbackURI, "")
}
func TestConfigAndPKCE(t *testing.T) {
	f := setup(t)
	p := start(t, f, "one")
	u, _ := url.Parse(p.AuthorizeURL)
	q := u.Query()
	if q.Get("client_id") != initialClient || q.Get("redirect_uri") != f.g.cfg.CallbackURI || q.Get("code_challenge_method") != "S256" || q.Get("scope") != "openid api-plan" {
		t.Fatalf("bad authorize request: %s", u)
	}
	if q.Get("code_challenge") == "" || q.Get("state") == "" || q.Get("nonce") == "" {
		t.Fatal("missing random authorization binding")
	}
	for _, c := range []Config{{}, {AuthorizationEndpoint: "https://x", CallbackURI: "http://127.0.0.1:0/auth/callback", HostID: "h", RequiredScopes: []string{"openid", "plan"}}, {AuthorizationEndpoint: "https://x", CallbackURI: "http://localhost:2/auth/callback", HostID: "h", RequiredScopes: []string{"openid", "plan"}}, {AuthorizationEndpoint: "https://x", CallbackURI: "http://127.0.0.1:2/auth/callback?x=1", HostID: "h", RequiredScopes: []string{"openid", "plan"}}, {AuthorizationEndpoint: "https://x", CallbackURI: "http://127.0.0.1:2/auth/callback", HostID: "h", RequiredScopes: []string{"openid"}}} {
		if New(c).configErr == nil {
			t.Fatalf("accepted invalid config: %+v", c)
		}
	}
	if _, e := f.g.Begin("other"); e != nil {
		t.Fatal(e)
	}
}
func TestCrossedStateClientAndScopes(t *testing.T) {
	f := setup(t)
	a := start(t, f, "a")
	b := start(t, f, "b")
	sa, ca := cbargs(t, a)
	_, cb := cbargs(t, b)
	if _, e := f.g.Callback("a", sa, "code", ca, f.g.cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
	if _, e := f.g.Callback("b", sa, "code", cb, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrInvalid) {
		t.Fatalf("crossed state: %v", e)
	}
	if _, e := f.g.Callback("b", func() string { s, _ := cbargs(t, b); return s }(), "code", cb, f.g.cfg.CallbackURI, "denied"); !errors.Is(e, ErrDenied) {
		t.Fatalf("provider denial: %v", e)
	}
	f.x.raw.Scopes = []string{"openid", "api-plan-extra"}
	s, _ := cbargs(t, b)
	if _, e := f.g.Callback("b", s, "code", cb, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrDenied) {
		t.Fatalf("substring scope accepted or failure was unsanitized: %v", e)
	}
	if len(f.g.Profiles()) != 1 {
		t.Fatalf("scope failure installed a profile: %+v", f.g.Profiles())
	}
	if _, e := f.g.Callback("b", s, "code", cb, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrInvalid) {
		t.Fatalf("scope-failed callback reusable: %v", e)
	}
}
func TestValidatorBindingAndFailurePreservesProfile(t *testing.T) {
	f := setup(t)
	p := start(t, f, "a")
	s, c := cbargs(t, p)
	got, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, "")
	if e != nil {
		t.Fatal(e)
	}
	if got.HostID != "stable-host" || got.Identity != "acct+1" {
		t.Fatalf("bad verified profile: %+v", got)
	}
	if e = f.g.Select("a"); e != nil {
		t.Fatal(e)
	}
	old := f.g.Profiles()[0]
	f.g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Subject: "acct+2", ClientID: c, Nonce: "wrong", HostID: "stable-host", Scopes: []string{"openid", "api-plan"}}}
	p2 := start(t, f, "a")
	s2, c2 := cbargs(t, p2)
	if _, e = f.g.Callback("a", s2, "code", c2, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrDenied) {
		t.Fatalf("nonce mismatch accepted: %v", e)
	}
	after := f.g.Profiles()[0]
	if old != after {
		t.Fatalf("failed validation replaced registration: before=%+v after=%+v", old, after)
	}
}
func TestRawAndVerifiedScopesRequireExactMembership(t *testing.T) {
	for _, verified := range []bool{false, true} {
		f := setup(t)
		p := start(t, f, "scope")
		s, c := cbargs(t, p)
		if verified {
			f.g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Subject: "acct+1", Scopes: []string{"openid", "api-plan-extra"}}}
		} else {
			f.x.raw.Scopes = []string{"openid", "api-plan-extra"}
		}
		if _, err := f.g.Callback("scope", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(err, ErrDenied) {
			t.Fatalf("verified=%v substring scope result: %v", verified, err)
		}
		if _, err := f.g.Callback("scope", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(err, ErrInvalid) {
			t.Fatalf("verified=%v scope failure was reusable: %v", verified, err)
		}
	}
	f := setup(t)
	f.x.raw.Scopes = []string{"openid", "api-plan", "extra"}
	f.g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Subject: "acct+1", Scopes: []string{"openid", "api-plan", "extra"}}}
	p := start(t, f, "extra")
	s, c := cbargs(t, p)
	if _, err := f.g.Callback("extra", s, "code", c, f.g.cfg.CallbackURI, ""); err != nil {
		t.Fatalf("exact required scopes with extras rejected: %v", err)
	}
}

func TestProviderDenialDoesNotConsumePendingClaim(t *testing.T) {
	f := setup(t)
	p := start(t, f, "denied")
	s, c := cbargs(t, p)
	if _, err := f.g.Callback("denied", s, "code", c, f.g.cfg.CallbackURI, "access_denied"); !errors.Is(err, ErrDenied) {
		t.Fatal(err)
	}
	if _, err := f.g.Begin("denied"); !errors.Is(err, ErrBusy) {
		t.Fatalf("provider denial unexpectedly cleared pending state: %v", err)
	}
	*f.now = p.ExpiresAt
	if _, err := f.g.Begin("denied"); err != nil {
		t.Fatalf("expired preclaim denial blocked retry: %v", err)
	}
}

func TestVerifiedBindingFailuresPreserveRegistrationAndConsumeClaim(t *testing.T) {
	for _, mutate := range []func(*VerifiedIdentity){
		func(i *VerifiedIdentity) { i.ClientID = "other-client" },
		func(i *VerifiedIdentity) { i.HostID = "other-host" },
		func(i *VerifiedIdentity) { i.Nonce = "other-nonce" },
	} {
		f := setup(t)
		p := start(t, f, "bound")
		s, c := cbargs(t, p)
		if _, err := f.g.Callback("bound", s, "code", c, f.g.cfg.CallbackURI, ""); err != nil {
			t.Fatal(err)
		}
		before := f.g.Profiles()[0]
		f.g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Subject: "acct+1", ClientID: c, Nonce: "n", HostID: "stable-host", Scopes: []string{"openid", "api-plan"}}}
		p = start(t, f, "bound")
		s, c = cbargs(t, p)
		u, err := url.Parse(p.AuthorizeURL)
		if err != nil {
			t.Fatal(err)
		}
		id := VerifiedIdentity{Subject: "acct+1", ClientID: c, Nonce: u.Query().Get("nonce"), HostID: "stable-host", Scopes: []string{"openid", "api-plan"}}
		mutate(&id)
		f.g.cfg.Validator = fakeValidator{id: id}
		if _, err := f.g.Callback("bound", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(err, ErrDenied) {
			t.Fatalf("binding accepted: %v", err)
		}
		if f.g.Profiles()[0] != before {
			t.Fatal("failed binding replaced existing registration")
		}
		if _, err := f.g.Callback("bound", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(err, ErrInvalid) {
			t.Fatalf("failed binding reusable: %v", err)
		}
	}
}

func TestMissingValidatorAndCallbackReplay(t *testing.T) {
	f := setup(t)
	p := start(t, f, "a")
	s, c := cbargs(t, p)
	f.g.cfg.Validator = nil
	if _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrUnavailable) {
		t.Fatal(e)
	}
	f.g.cfg.Validator = fakeValidator{}
	if _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
	if _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrInvalid) {
		t.Fatalf("replay: %v", e)
	}
}
func TestExpiryAtBoundaryAndAfterExchange(t *testing.T) {
	f := setup(t)
	p := start(t, f, "a")
	s, c := cbargs(t, p)
	*f.now = p.ExpiresAt
	if _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrExpired) {
		t.Fatalf("boundary expiry: %v", e)
	}
	f = setup(t)
	p = start(t, f, "a")
	s, c = cbargs(t, p)
	f.x.entered = make(chan struct{})
	f.x.release = make(chan struct{})
	done := make(chan error, 1)
	go func() { _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); done <- e }()
	waitExchange(t, f.x.entered, f.x.release)
	*f.now = p.ExpiresAt
	close(f.x.release)
	if e := <-done; !errors.Is(e, ErrExpired) {
		t.Fatalf("async expiry: %v", e)
	}
}
func TestCallbacksRaceAndBeginBusy(t *testing.T) {
	f := setup(t)
	p := start(t, f, "a")
	s, c := cbargs(t, p)
	f.x.entered = make(chan struct{})
	f.x.release = make(chan struct{})
	const n = 16
	errs := make(chan error, n)
	go func() { _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); errs <- e }()
	waitExchange(t, f.x.entered, f.x.release)
	if _, e := f.g.Begin("a"); !errors.Is(e, ErrBusy) {
		t.Fatalf("Begin while callback: %v", e)
	}
	for i := 0; i < n-1; i++ {
		go func() { _, e := f.g.Callback("a", s, "code", c, f.g.cfg.CallbackURI, ""); errs <- e }()
	}
	close(f.x.release)
	success := 0
	for i := 0; i < n; i++ {
		if <-errs == nil {
			success++
		}
	}
	if success != 1 {
		t.Fatalf("successful callbacks=%d", success)
	}
	f.x.mu.Lock()
	calls := f.x.calls
	f.x.mu.Unlock()
	if calls != 1 {
		t.Fatalf("exchange calls=%d", calls)
	}
}
func TestSelectDuringCallbackAndDistinctSameEmailIDs(t *testing.T) {
	f := setup(t)
	p := start(t, f, "one")
	s, c := cbargs(t, p)
	f.x.entered = make(chan struct{})
	f.x.release = make(chan struct{})
	done := make(chan error, 1)
	go func() { _, e := f.g.Callback("one", s, "code", c, f.g.cfg.CallbackURI, ""); done <- e }()
	waitExchange(t, f.x.entered, f.x.release)
	f.g.mu.Lock()
	f.g.profiles["other"] = Profile{Label: "other", HostID: "stable-host", ClientID: "issued-other", Identity: "acct+2"}
	f.g.mu.Unlock()
	if e := f.g.Select("other"); e != nil {
		t.Fatal(e)
	}
	close(f.x.release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	for _, p := range f.g.Profiles() {
		if p.Label == "other" && !p.Active {
			t.Fatal("concurrent selection undone")
		}
		if p.Label == "one" && p.Active {
			t.Fatal("callback activated profile")
		}
	}
	if strings.Contains(strings.Join([]string{f.g.Profiles()[0].ClientID, f.g.Profiles()[0].Identity}, " "), "verifier") {
		t.Fatal("secret leaked")
	}
}
func TestReturningIdentitySwapDeniedAndDistinctSubjects(t *testing.T) {
	f := setup(t)
	f.g.cfg.Validator = fakeValidator{subjects: map[string]string{"issued-plus1": "acct+1", "issued-plus2": "acct+2"}}
	p := start(t, f, "plus1")
	s, c := cbargs(t, p)
	if _, e := f.g.Callback("plus1", s, "code", c, f.g.cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
	p = start(t, f, "plus2")
	s, c = cbargs(t, p)
	if _, e := f.g.Callback("plus2", s, "code", c, f.g.cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
	ps := f.g.Profiles()
	if len(ps) != 2 || ps[0].Identity == ps[1].Identity {
		t.Fatalf("registrations collapsed: %+v", ps)
	}
	f.g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Subject: "wrong", Scopes: []string{"openid", "api-plan"}}}
	p = start(t, f, "plus1")
	s, c = cbargs(t, p)
	if _, e := f.g.Callback("plus1", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrDenied) {
		t.Fatalf("identity swap: %v", e)
	}
}

func TestConfigCopiesAndValidatesRequiredScopes(t *testing.T) {
	scopes := []string{"openid", "api-plan"}
	cfg := Config{AuthorizationEndpoint: "https://login.example/authorize", CallbackURI: "http://127.0.0.1:49152/auth/callback", HostID: "stable-host", RequiredScopes: scopes, Exchange: &fakeExchange{}, Validator: fakeValidator{}}
	g := New(cfg)
	if g.configErr != nil {
		t.Fatal(g.configErr)
	}
	scopes[1] = "openid"
	p, err := g.Begin("one")
	if err != nil {
		t.Fatal(err)
	}
	u, _ := url.Parse(p.AuthorizeURL)
	if got := u.Query().Get("scope"); got != "openid api-plan" {
		t.Fatalf("caller mutated required scopes: %q", got)
	}
	for _, invalid := range [][]string{{"openid", ""}, {"openid", "has space"}, {"openid", "bad\ttoken"}, {"openid", "café"}, {"openid"}} {
		c := cfg
		c.RequiredScopes = invalid
		if New(c).configErr == nil {
			t.Errorf("accepted invalid required scopes %q", invalid)
		}
	}
}

func TestCapacityCountsDistinctLabelsAndAllowsExistingProfile(t *testing.T) {
	f := setup(t)
	for _, label := range []string{"one", "two"} {
		p := start(t, f, label)
		s, c := cbargs(t, p)
		if _, err := f.g.Callback(label, s, "code", c, f.g.cfg.CallbackURI, ""); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := f.g.Begin("three"); !errors.Is(err, ErrBusy) {
		t.Fatalf("third profile label accepted: %v", err)
	}
	if _, err := f.g.Begin("one"); err != nil {
		t.Fatalf("existing profile could not reauthorize: %v", err)
	}
}

func TestFailedExchangeAndValidationConsumeCallbackAndSanitizeErrors(t *testing.T) {
	for _, validatorFailure := range []bool{false, true} {
		f := setup(t)
		p := start(t, f, "one")
		s, c := cbargs(t, p)
		secret := "sensitive-token-and-provider-payload"
		if validatorFailure {
			f.g.cfg.Validator = fakeValidator{err: errors.New(secret)}
		} else {
			f.x.err = errors.New(secret)
		}
		_, err := f.g.Callback("one", s, "code", c, f.g.cfg.CallbackURI, "")
		if !errors.Is(err, ErrDenied) || strings.Contains(err.Error(), secret) {
			t.Fatalf("external failure leaked or had wrong error: %v", err)
		}
		if _, err = f.g.Callback("one", s, "code", c, f.g.cfg.CallbackURI, ""); !errors.Is(err, ErrInvalid) {
			t.Fatalf("failed callback was reusable: %v", err)
		}
		if _, err = f.g.Begin("one"); err != nil {
			t.Fatalf("failed callback did not clear pending state: %v", err)
		}
	}
}

func TestCallbackRejectsWrongClientOrRedirect(t *testing.T) {
	f := setup(t)
	p := start(t, f, "a")
	s, c := cbargs(t, p)
	if _, e := f.g.Callback("a", s, "code", c, "http://127.0.0.1:49152/other", ""); !errors.Is(e, ErrInvalid) {
		t.Fatal("wrong callback accepted")
	}
	if _, e := f.g.Callback("a", s, "code", "dynamic_agent_client", f.g.cfg.CallbackURI, ""); !errors.Is(e, ErrInvalid) {
		t.Fatal("dynamic client returned as issued client")
	}
	if strings.TrimSpace(c) == "" {
		t.Fatal("missing client")
	}
}
