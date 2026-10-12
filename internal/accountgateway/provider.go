// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

type Endpoints struct{ Issuer, Discovery, Authorization, Token, JWKS, Revocation string }

func OpenAIEndpoints() Endpoints {
	return Endpoints{Issuer: "https://auth.openai.com", Discovery: "https://auth.openai.com/.well-known/openid-configuration", Authorization: "https://auth.openai.com/api/accounts/authorize", Token: "https://auth.openai.com/api/accounts/oauth/token", JWKS: "https://auth.openai.com/.well-known/jwks.json", Revocation: "https://auth.openai.com/api/accounts/oauth/revoke"}
}

// Provider fetches only caller-trusted endpoints; token headers never choose a URL.
type Provider struct {
	*HTTPExchange
	ep                      Endpoints
	mu                      sync.Mutex
	validator               *JWTValidator
	fetched, unknownRefresh time.Time
}

func NewProvider(ep Endpoints, client *http.Client) (*Provider, error) {
	x, e := NewHTTPExchange(ep.Token, "https://api.openai.com/v1", client, nil)
	if e != nil {
		return nil, e
	}
	for _, s := range []string{ep.Issuer, ep.Discovery, ep.Authorization, ep.JWKS, ep.Revocation} {
		u, e := url.Parse(s)
		issuer, ie := url.Parse(ep.Issuer)
		if e != nil || ie != nil || u.Scheme != "https" || u.Host == "" || u.Host != issuer.Host || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
			return nil, ErrInvalid
		}
	}
	p := &Provider{HTTPExchange: x, ep: ep}
	b, e := p.fetch(ep.Discovery)
	if e != nil {
		return nil, e
	}
	if rejectDuplicateJSON(b) != nil || canonicalMembers(b, "issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint") != nil {
		return nil, ErrDenied
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(b, &fields) != nil {
		return nil, ErrDenied
	}
	for k, want := range map[string]string{"issuer": ep.Issuer, "authorization_endpoint": ep.Authorization, "token_endpoint": ep.Token, "jwks_uri": ep.JWKS, "revocation_endpoint": ep.Revocation} {
		var got string
		if json.Unmarshal(fields[k], &got) != nil || got != want {
			return nil, ErrDenied
		}
	}
	if e = p.refresh(); e != nil {
		return nil, e
	}
	return p, nil
}
func (p *Provider) fetch(endpoint string) ([]byte, error) {
	req, e := http.NewRequest(http.MethodGet, endpoint, nil)
	if e != nil {
		return nil, ErrUnavailable
	}
	resp, e := p.client.Do(req)
	if e != nil {
		return nil, ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, ErrUnavailable
	}
	b, e := io.ReadAll(io.LimitReader(resp.Body, maxJWKS+1))
	if e != nil || len(b) > maxJWKS {
		return nil, ErrUnavailable
	}
	return b, nil
}
func (p *Provider) refresh() error {
	b, e := p.fetch(p.ep.JWKS)
	if e != nil {
		return e
	}
	v, e := NewJWTValidator(p.ep.Issuer, b, p.now)
	if e != nil {
		return ErrDenied
	}
	p.validator = v
	p.fetched = p.now()
	return nil
}
func (p *Provider) Verify(raw RawToken, expected Expected) (VerifiedIdentity, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.validator == nil || p.now().Sub(p.fetched) >= 5*time.Minute {
		if e := p.refresh(); e != nil {
			return VerifiedIdentity{}, ErrDenied
		}
	}
	// This untrusted lookup only triggers a bounded fetch of the fixed JWKS URL.
	if len(raw.Signed) <= maxJWT {
		parts := strings.Split(raw.Signed, ".")
		if len(parts) == 3 {
			b, e := base64.RawURLEncoding.Strict().DecodeString(parts[0])
			var h struct {
				Kid string `json:"kid"`
			}
			if e == nil && json.Unmarshal(b, &h) == nil && h.Kid != "" {
				if _, known := p.validator.keys[h.Kid]; !known && (p.unknownRefresh.IsZero() || p.now().Sub(p.unknownRefresh) >= time.Minute) {
					p.unknownRefresh = p.now()
					if e := p.refresh(); e != nil {
						return VerifiedIdentity{}, ErrDenied
					}
				}
			}
		}
	}
	return p.validator.Verify(raw, expected)
}
func (p *Provider) Revoke(refresh, clientID string) error {
	if refresh == "" || clientID == "" || clientID == initialClient {
		return ErrInvalid
	}
	form := url.Values{"token": {refresh}, "token_type_hint": {"refresh_token"}, "client_id": {clientID}}
	req, e := http.NewRequest(http.MethodPost, p.ep.Revocation, strings.NewReader(form.Encode()))
	if e != nil {
		return ErrUnavailable
	}
	req.GetBody = nil
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, e := p.client.Do(req)
	if e != nil {
		return ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return ErrUnavailable
	}
	b, e := io.ReadAll(io.LimitReader(resp.Body, 1025))
	if e != nil || len(b) != 0 {
		return ErrUnavailable
	}
	return nil
}
