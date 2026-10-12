// SPDX-License-Identifier: GPL-3.0-only
// Package accountgateway contains the provider-neutral authorization state machine.
package accountgateway

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
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

type Profile struct {
	Label, HostID, ClientID, Identity string
	Active                            bool
}
type Pending struct {
	Label, AuthorizeURL string
	ExpiresAt           time.Time
}
type RawToken struct {
	Signed   string
	ClientID string
	Scopes   []string
}
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
	openid, grant := false, false
	for _, scope := range c.RequiredScopes {
		if !validScope(scope) {
			return ErrInvalid
		}
		if scope == "openid" {
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
	for pendingLabel, t := range g.pending {
		if t.validating {
			continue
		}
		if !g.cfg.Now().Before(t.expires) {
			delete(g.pending, pendingLabel)
		}
	}
	if t := g.pending[label]; t != nil {
		return Pending{}, ErrBusy
	}
	if _, hasProfile := g.profiles[label]; !hasProfile {
		labels := make(map[string]struct{}, len(g.pending)+len(g.profiles))
		for pendingLabel := range g.pending {
			labels[pendingLabel] = struct{}{}
		}
		for profileLabel := range g.profiles {
			labels[profileLabel] = struct{}{}
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
	q.Set("response_type", "code")
	q.Set("state", state)
	q.Set("nonce", nonce)
	q.Set("client_id", client)
	q.Set("redirect_uri", g.cfg.CallbackURI)
	q.Set("code_challenge", challenge)
	q.Set("code_challenge_method", "S256")
	q.Set("scope", strings.Join(g.cfg.RequiredScopes, " "))
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
	if providerErr != "" {
		g.mu.Unlock()
		return Profile{}, ErrDenied
	}
	if state != t.state || code == "" || returnedClient == "" || returnedClient == initialClient || returnedClient != t.clientID && t.returning || redirect != g.cfg.CallbackURI {
		g.mu.Unlock()
		return Profile{}, ErrInvalid
	}
	if g.cfg.Exchange == nil || g.cfg.Validator == nil {
		g.mu.Unlock()
		return Profile{}, ErrUnavailable
	}
	t.validating = true
	v := t.verifier
	client := returnedClient
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
func validScope(scope string) bool {
	if scope == "" {
		return false
	}
	for i := 0; i < len(scope); i++ {
		b := scope[i]
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
