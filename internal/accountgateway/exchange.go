// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const exchangeBodyLimit = 64 << 10
const exchangeTokenLimit = 16 << 10

var (
	ErrExchangeInvalid     = errors.New("invalid token exchange")
	ErrExchangeDenied      = errors.New("token exchange denied")
	ErrExchangeUnavailable = errors.New("token exchange unavailable")
)

type Credentials struct {
	AccessToken       string    `json:"-"`
	RefreshToken      string    `json:"-"`
	ExpiresAt         time.Time `json:"-"`
	EarliestRefreshAt []byte    `json:"-"`
}

func (Credentials) String() string   { return "[REDACTED]" }
func (Credentials) GoString() string { return "[REDACTED]" }

type HTTPExchange struct {
	endpoint, resource string
	client             *http.Client
	now                func() time.Time
}

func NewHTTPExchange(endpoint, resource string, client *http.Client, now func() time.Time) (*HTTPExchange, error) {
	validHTTPS := func(s string) bool {
		u, e := url.Parse(s)
		return e == nil && u.Scheme == "https" && u.Host != "" && u.User == nil && u.RawQuery == "" && !u.ForceQuery && u.Fragment == ""
	}
	if !validHTTPS(endpoint) || !validHTTPS(resource) {
		return nil, ErrExchangeInvalid
	}
	if client == nil {
		client = http.DefaultClient
	}
	transport := client.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}
	tr, ok := transport.(*http.Transport)
	if !ok || tr == nil {
		return nil, ErrExchangeInvalid
	}
	if tr.DialTLS != nil || tr.DialTLSContext != nil {
		return nil, ErrExchangeInvalid
	}
	if tr.TLSClientConfig != nil && tr.TLSClientConfig.InsecureSkipVerify {
		return nil, ErrExchangeInvalid
	}
	cloned := *client
	tc := tr.Clone()
	if tr.TLSClientConfig != nil {
		tc.TLSClientConfig = tr.TLSClientConfig.Clone()
	}
	cloned.Transport = tc
	cloned.Jar = nil
	if cloned.Timeout <= 0 || cloned.Timeout > 5*time.Second {
		cloned.Timeout = 5 * time.Second
	}
	cloned.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	if now == nil {
		now = time.Now
	}
	return &HTTPExchange{endpoint: endpoint, resource: resource, client: &cloned, now: now}, nil
}
func NewOpenAIExchange(client *http.Client) (*HTTPExchange, error) {
	return NewHTTPExchange("https://auth.openai.com/api/accounts/oauth/token", "https://api.openai.com/v1", client, nil)
}
func (x *HTTPExchange) Exchange(code, clientID, redirectURI, verifier string) (RawToken, error) {
	return x.ExchangeContext(context.Background(), code, clientID, redirectURI, verifier)
}
func validCallback(s string) bool {
	u, e := url.Parse(s)
	if e != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "/auth/callback" || u.RawPath != "" || u.ForceQuery {
		return false
	}
	p, e := strconv.Atoi(u.Port())
	return e == nil && p > 0 && p <= 65535 && s == "http://127.0.0.1:"+strconv.Itoa(p)+"/auth/callback"
}
func validVerifier(s string) bool {
	if len(s) < 43 || len(s) > 128 {
		return false
	}
	for _, b := range []byte(s) {
		if !(b >= 'A' && b <= 'Z' || b >= 'a' && b <= 'z' || b >= '0' && b <= '9' || strings.ContainsRune("-._~", rune(b))) {
			return false
		}
	}
	return true
}
func (x *HTTPExchange) ExchangeContext(ctx context.Context, code, clientID, redirectURI, verifier string) (RawToken, error) {
	fail := func() (RawToken, error) { return RawToken{}, ErrExchangeInvalid }
	if x == nil || x.client == nil || x.now == nil || ctx == nil || len(code) == 0 || len(code) > exchangeTokenLimit || strings.TrimSpace(code) != code || clientID == "" || clientID == initialClient || strings.TrimSpace(clientID) != clientID || !validCallback(redirectURI) || !validVerifier(verifier) {
		return fail()
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	form := url.Values{"grant_type": {"authorization_code"}, "code": {code}, "client_id": {clientID}, "redirect_uri": {redirectURI}, "code_verifier": {verifier}, "resource": {x.resource}}
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, x.endpoint, strings.NewReader(form.Encode()))
	if e != nil {
		return fail()
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.GetBody = nil
	resp, e := x.client.Do(req)
	if e != nil {
		return RawToken{}, ErrExchangeUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return RawToken{}, ErrExchangeDenied
	}
	b, e := io.ReadAll(io.LimitReader(resp.Body, exchangeBodyLimit+1))
	if e != nil || len(b) > exchangeBodyLimit {
		return fail()
	}
	if rejectDuplicateJSON(b) != nil || canonicalMembers(b, "id_token", "access_token", "refresh_token", "token_type", "expires_in", "scope", "earliest_refresh_at", "client_id") != nil {
		return fail()
	}
	var m map[string]json.RawMessage
	if json.Unmarshal(b, &m) != nil || m == nil {
		return fail()
	}
	for _, k := range []string{"id_token", "access_token", "refresh_token", "token_type", "expires_in", "scope"} {
		if _, ok := m[k]; !ok {
			return fail()
		}
	}
	get := func(k string) (string, bool) { var s string; e := json.Unmarshal(m[k], &s); return s, e == nil }
	id, ok1 := get("id_token")
	access, ok2 := get("access_token")
	refresh, ok3 := get("refresh_token")
	typ, ok4 := get("token_type")
	scope, ok5 := get("scope")
	if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || len(id) == 0 || len(id) > exchangeTokenLimit || len(access) == 0 || len(access) > exchangeTokenLimit || len(refresh) == 0 || len(refresh) > exchangeTokenLimit || !strings.EqualFold(typ, "Bearer") {
		return fail()
	}
	lex := string(m["expires_in"])
	if lex == "" {
		return fail()
	}
	for _, c := range lex {
		if c < '0' || c > '9' {
			return fail()
		}
	}
	seconds, e := strconv.ParseInt(lex, 10, 64)
	if e != nil || seconds <= 0 || seconds > 86400 {
		return fail()
	}
	if _, present := m["client_id"]; present {
		cid, ok := get("client_id")
		if !ok || cid != clientID {
			return fail()
		}
	}
	parts := strings.Split(scope, " ")
	scopes := append([]string(nil), parts...)
	if !scopesValid(scopes) {
		return fail()
	}
	var earliest json.RawMessage
	if raw, ok := m["earliest_refresh_at"]; ok {
		if !json.Valid(raw) {
			return fail()
		}
		earliest = append([]byte(nil), raw...)
	}
	return RawToken{Signed: id, ClientID: clientID, Scopes: scopes, Credentials: &Credentials{AccessToken: access, RefreshToken: refresh, ExpiresAt: x.now().Add(time.Duration(seconds) * time.Second), EarliestRefreshAt: earliest}}, nil
}
