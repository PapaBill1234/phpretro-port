package accountgateway

import (
	"errors"
	"net/url"
	"testing"
)

func TestOpenAIWireContract(t *testing.T) {
	x := &fakeExchange{raw: RawToken{Signed: "signed"}}
	cfg := NewOpenAIConfig("http://127.0.0.1:49152/auth/callback", "urn:uuid:550e8400-e29b-41d4-a716-446655440000", "PHPRetro", x, fakeValidator{})
	cfg.Now = nil
	g := New(cfg)
	if g.configErr != nil {
		t.Fatal(g.configErr)
	}
	x.raw.Scopes = append([]string(nil), cfg.RequiredScopes...)
	g.cfg.Validator = fakeValidator{id: VerifiedIdentity{Scopes: append([]string(nil), cfg.RequiredScopes...)}}
	p, e := g.Begin("one")
	if e != nil {
		t.Fatal(e)
	}
	u, _ := url.Parse(p.AuthorizeURL)
	q := u.Query()
	if u.Scheme != "https" || u.Host != "auth.openai.com" || q.Get("resource") != "https://api.openai.com/v1" || q.Get("ext_agent_host_id") != cfg.HostID || q.Get("agent_name_hint") != "PHPRetro" {
		t.Fatalf("bad initial query: %v", q)
	}
	if q.Get("scope") != "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct" {
		t.Fatalf("scopes: %q", q.Get("scope"))
	}
	s := q.Get("state")
	if _, e = g.Callback("one", s, "code", "issued-one", cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
	p, e = g.Begin("one")
	if e != nil {
		t.Fatal(e)
	}
	u, _ = url.Parse(p.AuthorizeURL)
	q = u.Query()
	if q.Get("agent_name_hint") != "" {
		t.Fatal("returning name hint")
	}
	if _, e = g.Callback("one", q.Get("state"), "code", "", cfg.CallbackURI, ""); e != nil {
		t.Fatal(e)
	}
}

func TestOpenAIContractDenialAndValidation(t *testing.T) {
	for _, id := range []string{"urn:uuid:550e8400-e29b-41d4-a716-446655440000", "urn:ietf:params:oauth:jwk-thumbprint:sha-256:abc_DEF-123", "did:key:z6Mkabc"} {
		if !validHostID(id) {
			t.Errorf("host id rejected: %s", id)
		}
	}
	for _, id := range []string{"did:web:example.com", "urn:uuid:not"} {
		if validHostID(id) {
			t.Errorf("host id accepted: %s", id)
		}
	}
	x := &fakeExchange{raw: RawToken{Signed: "signed"}}
	cfg := NewOpenAIConfig("http://127.0.0.1:49152/auth/callback", "urn:uuid:550e8400-e29b-41d4-a716-446655440000", "PHPRetro", x, fakeValidator{})
	g := New(cfg)
	p, err := g.Begin("deny")
	if err != nil {
		t.Fatal(err)
	}
	s, c := cbargs(t, p)
	if _, err := g.Callback("deny", s, "code", c, cfg.CallbackURI+"/wrong", "denied"); !errors.Is(err, ErrInvalid) {
		t.Fatal(err)
	}
	if _, err := g.Callback("deny", s, "code", c, cfg.CallbackURI, "denied"); !errors.Is(err, ErrDenied) {
		t.Fatal(err)
	}
	if _, err := g.Callback("deny", s, "code", c, cfg.CallbackURI, "denied"); !errors.Is(err, ErrInvalid) {
		t.Fatal("denial replay")
	}
	for _, appName := range []string{"", "   "} {
		bad := NewOpenAIConfig(cfg.CallbackURI, cfg.HostID, appName, x, fakeValidator{})
		if New(bad).configErr == nil {
			t.Fatalf("blank app name accepted: %q", appName)
		}
	}
	f := setup(t)
	p = start(t, f, "state")
	s, c = cbargs(t, p)
	if _, err := f.g.Callback("state", "wrong", "code", c, f.g.cfg.CallbackURI, "denied"); !errors.Is(err, ErrInvalid) {
		t.Fatal(err)
	}
	if _, err := f.g.Begin("state"); !errors.Is(err, ErrBusy) {
		t.Fatal("wrong state consumed")
	}
}
