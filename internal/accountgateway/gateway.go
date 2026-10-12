// SPDX-License-Identifier: GPL-3.0-only
// Package accountgateway contains the provider-neutral authorization state machine.
package accountgateway

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	ErrInvalid     = errors.New("invalid authorization")
	ErrBusy        = errors.New("authorization already pending")
	ErrExpired     = errors.New("authorization expired")
	ErrReplay      = errors.New("authorization already consumed")
	ErrDenied      = errors.New("authorization denied")
	ErrUnavailable = errors.New("identity validator unavailable")
)

const initialClient = "dynamic_agent_client"

var openAIScopes = []string{"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}

type Profile struct {
	Label, HostID, ClientID, Identity string
	Active                            bool
}
type Pending struct {
	Label, AuthorizeURL string
	ExpiresAt           time.Time
}
type RawToken struct {
	Signed      string       `json:"-"`
	ClientID    string       `json:"-"`
	Scopes      []string     `json:"-"`
	Credentials *Credentials `json:"-"`
}

func (RawToken) String() string   { return "[REDACTED]" }
func (RawToken) GoString() string { return "[REDACTED]" }

type VerifiedIdentity struct {
	Subject, ClientID, Nonce, HostID string
	Scopes                           []string
}
type Expected struct {
	Nonce, ClientID, HostID, Subject string
	Returning                        bool
}
type Exchange interface {
	Exchange(code, clientID, redirectURI, verifier string) (RawToken, error)
}
type Validator interface {
	Verify(raw RawToken, expected Expected) (VerifiedIdentity, error)
}
type Config struct {
	AuthorizationEndpoint, CallbackURI, HostID string
	Resource, AgentName                        string
	RequiredScopes                             []string
	Exchange                                   Exchange
	Validator                                  Validator
	Now                                        func() time.Time
	Lifetime                                   time.Duration
}
type transaction struct {
	profile, state, nonce, verifier, challenge, clientID, hostID, subject string
	returning                                                             bool
	expires                                                               time.Time
	validating                                                            bool
}
type Gateway struct {
	mu        sync.Mutex
	cfg       Config
	configErr error
	pending   map[string]*transaction
	profiles  map[string]Profile
}

// NewOpenAIConfig creates the documented Sign in with ChatGPT wire configuration.
func NewOpenAIConfig(callbackURI, hostID, appName string, exchange Exchange, validator Validator) Config {
	return Config{AuthorizationEndpoint: "https://auth.openai.com/api/accounts/authorize", CallbackURI: callbackURI, HostID: hostID, Resource: "https://api.openai.com/v1", AgentName: appName, RequiredScopes: append([]string(nil), openAIScopes...), Exchange: exchange, Validator: validator}
}
func New(c Config) *Gateway {
	c.RequiredScopes = append([]string(nil), c.RequiredScopes...)
	if c.Now == nil {
		c.Now = time.Now
	}
	if c.Lifetime <= 0 || c.Lifetime > 10*time.Minute {
		c.Lifetime = 5 * time.Minute
	}
	g := &Gateway{cfg: c, pending: map[string]*transaction{}, profiles: map[string]Profile{}}
	g.configErr = validateConfig(c)
	return g
}
func validHostID(s string) bool {
	if strings.HasPrefix(s, "urn:uuid:") {
		v := strings.TrimPrefix(s, "urn:uuid:")
		b, e := hex.DecodeString(strings.ReplaceAll(v, "-", ""))
		return e == nil && len(b) == 16 && b[6]>>4 == 4 && b[8]>>6 == 2 && len(v) == 36 && v[8] == '-' && v[13] == '-' && v[18] == '-' && v[23] == '-'
	}
	if strings.HasPrefix(s, "urn:ietf:params:oauth:jwk-thumbprint:") {
		v := strings.TrimPrefix(s, "urn:ietf:params:oauth:jwk-thumbprint:")
		parts := strings.Split(v, ":")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return false
		}
		for _, r := range parts[0] {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return false
			}
		}
		for _, r := range parts[1] {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return false
			}
		}
		return true
	}
	if strings.HasPrefix(s, "did:key:") {
		v := strings.TrimPrefix(s, "did:key:")
		if len(v) < 2 {
			return false
		}
		for _, r := range v {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return false
			}
		}
		return true
	}
	return false
}
func validateConfig(c Config) error {
	u, e := url.Parse(c.AuthorizationEndpoint)
	if e != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" {
		return ErrInvalid
	}
	cb, e := url.Parse(c.CallbackURI)
	if e != nil || cb.Scheme != "http" || cb.Hostname() != "127.0.0.1" || cb.User != nil || cb.RawQuery != "" || cb.Fragment != "" || cb.Path != "/auth/callback" {
		return ErrInvalid
	}
	p, e := strconv.Atoi(cb.Port())
	if e != nil || p < 1 || p > 65535 {
		return ErrInvalid
	}
	if strings.TrimSpace(c.HostID) == "" {
		return ErrInvalid
	}
	if u.Host == "auth.openai.com" && !validHostID(c.HostID) {
		return ErrInvalid
	}
	if u.Host == "auth.openai.com" && strings.TrimSpace(c.AgentName) == "" {
		return ErrInvalid
	}
	if c.Resource != "" {
		r, e := url.Parse(c.Resource)
		if e != nil || r.Scheme != "https" || r.Host == "" || r.User != nil || r.RawQuery != "" || r.Fragment != "" {
			return ErrInvalid
		}
	}
	openid, grant := false, false
	for _, s := range c.RequiredScopes {
		if !validScope(s) {
			return ErrInvalid
		}
		if s == "openid" {
			openid = true
		} else {
			grant = true
		}
	}
	if !openid || !grant {
		return ErrInvalid
	}
	return nil
}
func (g *Gateway) Begin(label string) (Pending, error) {
	label = strings.TrimSpace(label)
	if g.configErr != nil || label == "" {
		return Pending{}, ErrInvalid
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	for k, t := range g.pending {
		if !t.validating && !g.cfg.Now().Before(t.expires) {
			delete(g.pending, k)
		}
	}
	if g.pending[label] != nil {
		return Pending{}, ErrBusy
	}
	if _, ok := g.profiles[label]; !ok {
		labels := map[string]bool{}
		for k := range g.pending {
			labels[k] = true
		}
		for k := range g.profiles {
			labels[k] = true
		}
		if len(labels) >= 2 {
			return Pending{}, ErrBusy
		}
	}
	state, e := token(32)
	if e != nil {
		return Pending{}, e
	}
	nonce, e := token(32)
	if e != nil {
		return Pending{}, e
	}
	v, e := token(32)
	if e != nil {
		return Pending{}, e
	}
	h := sha256.Sum256([]byte(v))
	challenge := base64.RawURLEncoding.EncodeToString(h[:])
	old, returning := g.profiles[label]
	client := initialClient
	subject := ""
	if returning {
		client = old.ClientID
		subject = old.Identity
		if client == "" || subject == "" {
			return Pending{}, ErrInvalid
		}
	}
	exp := g.cfg.Now().Add(g.cfg.Lifetime)
	t := &transaction{profile: label, state: state, nonce: nonce, verifier: v, challenge: challenge, clientID: client, hostID: g.cfg.HostID, subject: subject, returning: returning, expires: exp}
	g.pending[label] = t
	u, _ := url.Parse(g.cfg.AuthorizationEndpoint)
	q := u.Query()
	q.Del("agent_name_hint")
	q.Set("response_type", "code")
	q.Set("state", state)
	q.Set("nonce", nonce)
	q.Set("client_id", client)
	q.Set("redirect_uri", g.cfg.CallbackURI)
	q.Set("code_challenge", challenge)
	q.Set("code_challenge_method", "S256")
	q.Set("scope", strings.Join(g.cfg.RequiredScopes, " "))
	q.Set("ext_agent_host_id", g.cfg.HostID)
	if g.cfg.Resource != "" {
		q.Set("resource", g.cfg.Resource)
	}
	if !returning {
		q.Set("agent_name_hint", g.cfg.AgentName)
	}
	u.RawQuery = q.Encode()
	return Pending{Label: label, AuthorizeURL: u.String(), ExpiresAt: exp}, nil
}
func (g *Gateway) Callback(label, state, code, returnedClient, redirect, providerErr string) (Profile, error) {
	g.mu.Lock()
	t := g.pending[label]
	if t == nil {
		g.mu.Unlock()
		return Profile{}, ErrInvalid
	}
	if !g.cfg.Now().Before(t.expires) {
		delete(g.pending, label)
		g.mu.Unlock()
		return Profile{}, ErrExpired
	}
	if t.validating {
		g.mu.Unlock()
		return Profile{}, ErrBusy
	}
	if state != t.state || redirect != g.cfg.CallbackURI {
		g.mu.Unlock()
		return Profile{}, ErrInvalid
	}
	if providerErr != "" {
		delete(g.pending, label)
		g.mu.Unlock()
		return Profile{}, ErrDenied
	}
	client := returnedClient
	if t.returning {
		if client != "" && client != t.clientID {
			g.mu.Unlock()
			return Profile{}, ErrInvalid
		}
		client = t.clientID
	} else if client == "" || client == initialClient {
		g.mu.Unlock()
		return Profile{}, ErrInvalid
	}
	if code == "" {
		g.mu.Unlock()
		return Profile{}, ErrInvalid
	}
	if g.cfg.Exchange == nil || g.cfg.Validator == nil {
		g.mu.Unlock()
		return Profile{}, ErrUnavailable
	}
	t.validating = true
	v := t.verifier
	expected := Expected{Nonce: t.nonce, ClientID: client, HostID: t.hostID, Subject: t.subject, Returning: t.returning}
	g.mu.Unlock()
	raw, e := g.cfg.Exchange.Exchange(code, client, g.cfg.CallbackURI, v)
	var id VerifiedIdentity
	if e == nil {
		if raw.Signed == "" || raw.ClientID != client || !scopesContain(raw.Scopes, g.cfg.RequiredScopes) {
			e = ErrInvalid
		}
	}
	if e == nil {
		id, e = g.cfg.Validator.Verify(raw, expected)
	}
	if e != nil {
		e = ErrDenied
	}
	if e == nil && (id.Subject == "" || id.ClientID != client || id.Nonce != t.nonce || id.HostID != t.hostID || t.returning && id.Subject != t.subject || !scopesContain(id.Scopes, g.cfg.RequiredScopes)) {
		e = ErrDenied
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.pending[label] != t {
		return Profile{}, ErrInvalid
	}
	t.validating = false
	if !g.cfg.Now().Before(t.expires) {
		delete(g.pending, label)
		return Profile{}, ErrExpired
	}
	if e != nil {
		delete(g.pending, label)
		return Profile{}, e
	}
	old := g.profiles[label]
	p := Profile{Label: label, HostID: t.hostID, ClientID: id.ClientID, Identity: id.Subject, Active: old.Active}
	g.profiles[label] = p
	delete(g.pending, label)
	return p, nil
}
func validScope(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		b := s[i]
		if !(b == 0x21 || b >= 0x23 && b <= 0x5b || b >= 0x5d && b <= 0x7e) {
			return false
		}
	}
	return true
}
func scopesContain(got, required []string) bool {
	for _, r := range required {
		found := false
		for _, s := range got {
			if s == r {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}
func (g *Gateway) Select(label string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if _, ok := g.profiles[label]; !ok {
		return ErrInvalid
	}
	for k, p := range g.profiles {
		p.Active = k == label
		g.profiles[k] = p
	}
	return nil
}
func (g *Gateway) Profiles() []Profile {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]Profile, 0, len(g.profiles))
	for _, p := range g.profiles {
		out = append(out, p)
	}
	return out
}
func token(n int) (string, error) {
	b := make([]byte, n)
	if _, e := rand.Read(b); e != nil {
		return "", e
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}
