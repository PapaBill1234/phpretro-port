// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestHTTPExchangeSyntheticTLSFormAndRedaction(t *testing.T) {
	var hits int
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		if r.Method != "POST" || r.Header.Get("Authorization") != "" || r.Header.Get("Content-Type") != "application/x-www-form-urlencoded" {
			t.Errorf("unexpected request")
		}
		if err := r.ParseForm(); err != nil {
			t.Error(err)
		}
		for k, v := range map[string]string{"grant_type": "authorization_code", "code": "synthetic-code", "client_id": "issued-client", "redirect_uri": "http://127.0.0.1:49152/auth/callback", "code_verifier": strings.Repeat("v", 43), "resource": "https://api.example/v1"} {
			if r.Form.Get(k) != v {
				t.Errorf("form %s mismatch", k)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"id_token":"id-secret","access_token":"access-secret","refresh_token":"refresh-secret","token_type":"Bearer","expires_in":3600,"scope":"openid profile","earliest_refresh_at":{"opaque":"metadata"},"client_id":"issued-client"}`)
	}))
	defer srv.Close()
	client := srv.Client()
	old := client.Transport.(*http.Transport)
	cloned := old.Clone()
	cloned.TLSClientConfig = &tls.Config{RootCAs: old.TLSClientConfig.RootCAs}
	client.Transport = cloned
	x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", client, func() time.Time { return time.Unix(100, 0) })
	if e != nil {
		t.Fatal(e)
	}
	raw, e := x.ExchangeContext(context.Background(), "synthetic-code", "issued-client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e != nil {
		t.Fatal(e)
	}
	if hits != 1 || raw.Signed != "id-secret" || raw.Credentials == nil || raw.Credentials.ExpiresAt.Unix() != 3700 {
		t.Fatalf("bad result %#v", raw)
	}
	for _, secret := range []string{"id-secret", "access-secret", "refresh-secret", "opaque"} {
		if strings.Contains(fmt.Sprintf("%v %+v %#v", raw, raw, raw), secret) || strings.Contains(fmt.Sprintf("%v %+v %#v", raw.Credentials, raw.Credentials, raw.Credentials), secret) {
			t.Fatal("format leaked secret")
		}
	}
	b, _ := json.Marshal(raw)
	if strings.Contains(string(b), "secret") || strings.Contains(string(b), "opaque") {
		t.Fatal("JSON leaked secret")
	}
}
func TestHTTPExchangeRejectsRedirectAndDenialWithoutLeak(t *testing.T) {
	var hits int
	dest := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hits++ }))
	defer dest.Close()
	src := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		http.Redirect(w, r, dest.URL, http.StatusTemporaryRedirect)
	}))
	defer src.Close()
	x, e := NewHTTPExchange(src.URL, "https://api.example/v1", src.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	_, e = x.Exchange("code-secret", "client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e != ErrExchangeDenied || hits != 1 {
		t.Fatalf("redirect: %v hits=%d", e, hits)
	}
	denial := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(400)
		fmt.Fprint(w, "code-secret access-secret refresh-secret")
	}))
	defer denial.Close()
	x, e = NewHTTPExchange(denial.URL, "https://api.example/v1", denial.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	_, e = x.Exchange("code-secret", "client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e != ErrExchangeDenied || strings.Contains(e.Error(), "secret") {
		t.Fatalf("unsafe denial %v", e)
	}
}
func TestHTTPExchangeRejectsBadConfigAndResponse(t *testing.T) {
	if _, e := NewHTTPExchange("https://user@example.test/token", "https://api.example/v1", nil, nil); e == nil {
		t.Fatal("userinfo accepted")
	}
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"id_token":"id","access_token":"a","refresh_token":"r","token_type":"Bearer","expires_in":1.5,"scope":"openid"}`)
	}))
	defer srv.Close()
	x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	_, e = x.Exchange("code", "client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e != ErrExchangeInvalid {
		t.Fatalf("fractional expiry accepted: %v", e)
	}
}

func TestExchangeStrictResponses(t *testing.T) {
	valid := map[string]any{"id_token": "id-secret", "access_token": "access-secret", "refresh_token": "refresh-secret", "token_type": "Bearer", "expires_in": 3600, "scope": "openid chatgpt.tokens.use.direct"}
	body := func(k string, v any) string {
		m := map[string]any{}
		for a, b := range valid {
			m[a] = b
		}
		m[k] = v
		b, _ := json.Marshal(m)
		return string(b)
	}
	good, _ := json.Marshal(valid)
	cases := map[string]string{
		"quoted expiry": body("expires_in", "3600"), "null expiry": body("expires_in", nil), "zero": body("expires_in", 0), "negative": body("expires_in", -1), "large expiry": body("expires_in", 86401),
		"wrong token type": body("token_type", "MAC"), "null token": body("access_token", nil), "empty refresh": body("refresh_token", ""), "large token": body("id_token", strings.Repeat("x", exchangeTokenLimit+1)),
		"scope duplicate": body("scope", "openid openid"), "scope space": body("scope", "openid  profile"), "scope tab": body("scope", "openid\tprofile"), "scope null": body("scope", nil),
		"wrong client": body("client_id", "other"), "null client": body("client_id", nil),
		"duplicate":  strings.TrimSuffix(string(good), "}") + `,"scope":"openid"}`,
		"case alias": strings.TrimSuffix(string(good), "}") + `,"Scope":"openid"}`,
		"trailing":   string(good) + "{}", "body bound": string(good) + strings.Repeat(" ", exchangeBodyLimit),
		"deep": strings.TrimSuffix(string(good), "}") + `,"extra":` + strings.Repeat("[", 65) + "0" + strings.Repeat("]", 65) + "}",
	}
	for name, b := range cases {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, b) }))
			defer srv.Close()
			x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
			if e != nil {
				t.Fatal(e)
			}
			raw, e := x.Exchange("code", "issued-client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
			if e != ErrExchangeInvalid || raw.Credentials != nil || raw.Signed != "" {
				t.Fatalf("accepted invalid response: %v", e)
			}
		})
	}
}
func TestExchangeCallbackAndConfig(t *testing.T) {
	if _, e := NewOpenAIExchange(nil); e != nil {
		t.Fatal("default transport", e)
	}
	for _, u := range []string{"http://example.test", "https://example.test/?", "https://example.test/#fragment"} {
		if _, e := NewHTTPExchange(u, "https://api.example/v1", nil, nil); e == nil {
			t.Fatalf("bad endpoint %s", u)
		}
	}
	var hits atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer srv.Close()
	x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	for _, u := range []string{"http://localhost:49152/auth/callback", "http://127.0.0.1:049152/auth/callback", "http://127.0.0.1:49152/auth/%63allback", "http://127.0.0.1:49152/auth/callback?", "http://127.0.0.1:49152/auth/callback#x", "http://127.0.0.1:0/auth/callback"} {
		if _, e := x.Exchange("code", "issued-client", u, strings.Repeat("v", 43)); e != ErrExchangeInvalid {
			t.Fatalf("callback accepted %s", u)
		}
	}
	if hits.Load() != 0 {
		t.Fatal("invalid input reached provider")
	}
	tr := srv.Client().Transport.(*http.Transport).Clone()
	tr.TLSClientConfig = tr.TLSClientConfig.Clone()
	tr.TLSClientConfig.InsecureSkipVerify = true
	if _, e := NewHTTPExchange(srv.URL, "https://api.example/v1", &http.Client{Transport: tr}, nil); e == nil {
		t.Fatal("insecure TLS accepted")
	}
}
func TestExchangeContextBoundsBody(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer srv.Close()
	x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, e = x.ExchangeContext(ctx, "code", "issued-client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e == nil || time.Since(start) > time.Second {
		t.Fatal("body outlived caller deadline")
	}
}
func TestExchangeAllRedirectsAndStatuses(t *testing.T) {
	for _, status := range []int{201, 301, 302, 303, 307, 308} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			var destHits atomic.Int32
			dest := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { destHits.Add(1) }))
			defer dest.Close()
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Location", dest.URL)
				w.WriteHeader(status)
			}))
			defer srv.Close()
			x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
			if e != nil {
				t.Fatal(e)
			}
			_, e = x.Exchange("code", "issued-client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
			if e != ErrExchangeDenied || destHits.Load() != 0 {
				t.Fatal("followed redirect or accepted status", e)
			}
		})
	}
}
func TestExchangeBundleRedaction(t *testing.T) {
	c := Credentials{AccessToken: "access-secret", RefreshToken: "refresh-secret", EarliestRefreshAt: []byte("opaque-secret")}
	r := RawToken{Signed: "id-secret", Credentials: &c}
	for _, v := range []any{c, &c, r, &r} {
		for _, format := range []string{"%v", "%+v", "%#v"} {
			if strings.Contains(fmt.Sprintf(format, v), "secret") {
				t.Fatal("format leak")
			}
		}
		b, e := json.Marshal(v)
		if e != nil || strings.Contains(string(b), "secret") {
			t.Fatal("JSON leak")
		}
	}
}

func TestExchangeConnectionFailureDoesNotRetry(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		conn.Close()
	}))
	defer srv.Close()
	x, e := NewHTTPExchange(srv.URL, "https://api.example/v1", srv.Client(), nil)
	if e != nil {
		t.Fatal(e)
	}
	_, e = x.Exchange("code-secret", "issued-client", "http://127.0.0.1:49152/auth/callback", strings.Repeat("v", 43))
	if e != ErrExchangeUnavailable || hits.Load() != 1 {
		t.Fatal("connection failure retried or leaked", e, hits.Load())
	}
}

func TestExchangeRejectsCustomTLSDialer(t *testing.T) {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.DialTLS = func(string, string) (net.Conn, error) {
		t.Fatal("custom TLS invoked")
		return nil, fmt.Errorf("unexpected")
	}
	if _, e := NewHTTPExchange("https://provider.example/token", "https://api.example/v1", &http.Client{Transport: tr}, nil); e != ErrExchangeInvalid {
		t.Fatal("custom TLS accepted", e)
	}
	tr.DialTLS = nil
	tr.DialTLSContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("custom TLS context invoked")
		return nil, fmt.Errorf("unexpected")
	}
	if _, e := NewHTTPExchange("https://provider.example/token", "https://api.example/v1", &http.Client{Transport: tr}, nil); e != ErrExchangeInvalid {
		t.Fatal("custom TLS context accepted", e)
	}
}
