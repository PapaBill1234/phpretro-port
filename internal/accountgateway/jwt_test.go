// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"
)

type jwtFixture struct {
	key       *rsa.PrivateKey
	now       time.Time
	jwk       map[string]any
	validator *JWTValidator
}

func newJWTFixture(t *testing.T) jwtFixture {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	f := jwtFixture{key: key, now: time.Unix(1800000000, 0).UTC()}
	f.jwk = map[string]any{"kty": "RSA", "kid": "trusted", "alg": "RS256", "use": "sig", "n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()), "e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())}
	f.validator, err = NewJWTValidator("https://issuer.example", f.document(t), func() time.Time { return f.now })
	if err != nil {
		t.Fatal(err)
	}
	return f
}
func (f jwtFixture) document(t *testing.T) []byte {
	t.Helper()
	b, e := json.Marshal(map[string]any{"keys": []any{f.jwk}})
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func (f jwtFixture) claims() jwt.MapClaims {
	return jwt.MapClaims{"iss": "https://issuer.example", "aud": "issued-client", "sub": "account-one", "nonce": "expected-nonce", "iat": f.now.Unix(), "exp": f.now.Add(time.Minute).Unix()}
}
func (f jwtFixture) sign(t *testing.T, c jwt.MapClaims, header map[string]any) string {
	t.Helper()
	token := jwt.NewWithClaims(jwt.SigningMethodRS256, c)
	token.Header["kid"] = "trusted"
	for k, v := range header {
		token.Header[k] = v
	}
	signed, e := token.SignedString(f.key)
	if e != nil {
		t.Fatal(e)
	}
	return signed
}
func (f jwtFixture) raw(t *testing.T, c jwt.MapClaims) RawToken {
	return RawToken{Signed: f.sign(t, c, nil), ClientID: "issued-client", Scopes: []string{"openid", "chatgpt.tokens.use.direct"}}
}
func jwtExpected() Expected {
	return Expected{ClientID: "issued-client", Nonce: "expected-nonce", HostID: "local-host"}
}
func signedJSON(t *testing.T, f jwtFixture, header, payload string) string {
	t.Helper()
	input := base64.RawURLEncoding.EncodeToString([]byte(header)) + "." + base64.RawURLEncoding.EncodeToString([]byte(payload))
	sig, e := jwt.SigningMethodRS256.Sign(input, f.key)
	if e != nil {
		t.Fatal(e)
	}
	return input + "." + base64.RawURLEncoding.EncodeToString(sig)
}
func assertDeniedJWT(t *testing.T, v *JWTValidator, raw RawToken, expected Expected) {
	t.Helper()
	id, e := v.Verify(raw, expected)
	if !errors.Is(e, ErrDenied) || id.Subject != "" || id.ClientID != "" || len(id.Scopes) != 0 {
		t.Fatalf("rejected token must return no identity, got error %v", e)
	}
}

func TestJWTValidatorActualSignedClaims(t *testing.T) {
	f := newJWTFixture(t)
	raw := f.raw(t, f.claims())
	expected := jwtExpected()
	got, e := f.validator.Verify(raw, expected)
	if e != nil || got.Subject != "account-one" || got.ClientID != expected.ClientID || got.Nonce != expected.Nonce || got.HostID != expected.HostID {
		t.Fatal("valid signed token rejected", e)
	}
	got.Scopes[0] = "modified"
	if raw.Scopes[0] != "openid" {
		t.Fatal("scope output aliases response")
	}
	cases := []struct {
		name   string
		mutate func(jwt.MapClaims)
	}{
		{"issuer", func(c jwt.MapClaims) { c["iss"] = "https://other.example" }},
		{"missing-issuer", func(c jwt.MapClaims) { delete(c, "iss") }},
		{"audience", func(c jwt.MapClaims) { c["aud"] = "other-client" }},
		{"missing-audience", func(c jwt.MapClaims) { delete(c, "aud") }},
		{"nonce", func(c jwt.MapClaims) { c["nonce"] = "other-nonce" }},
		{"missing-nonce", func(c jwt.MapClaims) { delete(c, "nonce") }},
		{"empty-subject", func(c jwt.MapClaims) { c["sub"] = "" }},
		{"blank-subject", func(c jwt.MapClaims) { c["sub"] = " " }},
		{"missing-subject", func(c jwt.MapClaims) { delete(c, "sub") }},
		{"expired", func(c jwt.MapClaims) { c["exp"] = f.now.Add(-time.Second).Unix() }},
		{"exact-expiry", func(c jwt.MapClaims) { c["exp"] = f.now.Unix() }},
		{"missing-expiry", func(c jwt.MapClaims) { delete(c, "exp") }},
		{"null-expiry", func(c jwt.MapClaims) { c["exp"] = nil }},
		{"missing-issued-at", func(c jwt.MapClaims) { delete(c, "iat") }},
		{"future-issued-at", func(c jwt.MapClaims) { c["iat"] = f.now.Add(time.Second).Unix() }},
		{"future-not-before", func(c jwt.MapClaims) { c["nbf"] = f.now.Add(time.Second).Unix() }},
		{"wrong-azp", func(c jwt.MapClaims) { c["azp"] = "other-client" }},
		{"empty-azp", func(c jwt.MapClaims) { c["azp"] = "" }},
		{"null-azp", func(c jwt.MapClaims) { c["azp"] = nil }},
		{"null-issued-at", func(c jwt.MapClaims) { c["iat"] = nil }},
		{"multi-audience-without-azp", func(c jwt.MapClaims) { c["aud"] = []string{"issued-client", "other-client"} }},
		{"multi-audience-wrong-azp", func(c jwt.MapClaims) { c["aud"] = []string{"issued-client", "other-client"}; c["azp"] = "other-client" }},
		{"case-alias-exp", func(c jwt.MapClaims) { delete(c, "exp"); c["EXP"] = f.now.Add(time.Minute).Unix() }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := f.claims()
			tc.mutate(c)
			assertDeniedJWT(t, f.validator, f.raw(t, c), expected)
		})
	}
	c := f.claims()
	c["aud"] = []string{"issued-client", "other-client"}
	c["azp"] = "issued-client"
	if _, e = f.validator.Verify(f.raw(t, c), expected); e != nil {
		t.Fatal("valid azp rejected", e)
	}
	c = f.claims()
	c["nbf"] = f.now.Unix()
	if _, e = f.validator.Verify(f.raw(t, c), expected); e != nil {
		t.Fatal("current nbf rejected", e)
	}
	expected.Returning = true
	expected.Subject = "account-one"
	if _, e = f.validator.Verify(raw, expected); e != nil {
		t.Fatal(e)
	}
	expected.Subject = "account-two"
	assertDeniedJWT(t, f.validator, raw, expected)
	for _, mutate := range []func(*RawToken, *Expected){
		func(r *RawToken, e *Expected) { r.ClientID = "wrong" },
		func(r *RawToken, e *Expected) { e.Nonce = "" },
		func(r *RawToken, e *Expected) { e.HostID = "" },
		func(r *RawToken, e *Expected) { r.Scopes = []string{"openid"} },
		func(r *RawToken, e *Expected) {
			r.Scopes = []string{"openid", "chatgpt.tokens.use.direct", "bad scope"}
		},
		func(r *RawToken, e *Expected) { r.Scopes = []string{"openid", "openid", "chatgpt.tokens.use.direct"} },
	} {
		r := f.raw(t, f.claims())
		x := jwtExpected()
		mutate(&r, &x)
		assertDeniedJWT(t, f.validator, r, x)
	}
	assertDeniedJWT(t, new(JWTValidator), raw, jwtExpected())
}

func TestJWTValidatorSignatureAndParserBoundaries(t *testing.T) {
	f := newJWTFixture(t)
	expected := jwtExpected()
	raw := f.raw(t, f.claims())
	parts := strings.Split(raw.Signed, ".")
	parts[2] = base64.RawURLEncoding.EncodeToString([]byte("invalid-signature"))
	raw.Signed = strings.Join(parts, ".")
	assertDeniedJWT(t, f.validator, raw, expected)
	// These are actual correctly constructed alternative-algorithm tokens, not signing errors.
	hs := jwt.NewWithClaims(jwt.SigningMethodHS256, f.claims())
	hs.Header["kid"] = "trusted"
	token, e := hs.SignedString([]byte("synthetic-hmac-key"))
	if e != nil {
		t.Fatal(e)
	}
	raw.Signed = token
	assertDeniedJWT(t, f.validator, raw, expected)
	none := jwt.NewWithClaims(jwt.SigningMethodNone, f.claims())
	none.Header["kid"] = "trusted"
	token, e = none.SignedString(jwt.UnsafeAllowNoneSignatureType)
	if e != nil {
		t.Fatal(e)
	}
	raw.Signed = token
	assertDeniedJWT(t, f.validator, raw, expected)
	for _, header := range []map[string]any{{"kid": "unknown"}, {"crit": []string{"unsupported"}}, {"crit": []string{}}, {"b64": true}, {"ALG": "RS256"}, {"KID": "trusted"}} {
		raw.Signed = f.sign(t, f.claims(), header)
		assertDeniedJWT(t, f.validator, raw, expected)
	}
	// Token-supplied discovery URLs never select keys or initiate a fetch.
	raw.Signed = f.sign(t, f.claims(), map[string]any{"jku": "https://untrusted.invalid/keys", "x5u": "https://untrusted.invalid/cert"})
	if _, e = f.validator.Verify(raw, expected); e != nil {
		t.Fatal("fixed trusted key should ignore token discovery URLs", e)
	}
	base, _ := json.Marshal(f.claims())
	for _, payload := range []string{
		strings.TrimSuffix(string(base), "}") + `,"nonce":"second"}`,
		strings.TrimSuffix(string(base), "}") + `,"NONCE":"second"}`,
		strings.TrimSuffix(string(base), "}") + `,"custom":` + strings.Repeat("[", 70) + "0" + strings.Repeat("]", 70) + "}",
		strings.TrimSuffix(string(base), "}") + `,"custom":{"a":1,"a":2}}`,
	} {
		raw.Signed = signedJSON(t, f, `{"alg":"RS256","kid":"trusted"}`, payload)
		assertDeniedJWT(t, f.validator, raw, expected)
	}
	for _, header := range []string{`{"alg":"RS256","kid":"trusted","kid":"other"}`, `{"alg":"RS256","kid":"trusted","KID":"other"}`} {
		raw.Signed = signedJSON(t, f, header, string(base))
		assertDeniedJWT(t, f.validator, raw, expected)
	}
	raw.Signed = strings.Repeat("a", maxJWT+1)
	assertDeniedJWT(t, f.validator, raw, expected)
	raw = f.raw(t, f.claims())
	parts = strings.Split(raw.Signed, ".")
	parts[0] += "="
	raw.Signed = strings.Join(parts, ".")
	assertDeniedJWT(t, f.validator, raw, expected)
	if _, e := decodePart("YQ\n"); e == nil {
		t.Fatal("noncanonical base64 accepted")
	}
}

func TestJWKSRejectsUnsafeOrAmbiguousKeys(t *testing.T) {
	f := newJWTFixture(t)
	for _, tc := range []struct {
		name  string
		field string
		value any
	}{
		{"wrong-type", "kty", "EC"},
		{"empty-alg", "alg", ""}, {"null-use", "use", nil},
		{"excessive-modulus", "n", base64.RawURLEncoding.EncodeToString(append([]byte{1}, make([]byte, 1024)...))}, {"wrong-alg", "alg", "HS256"}, {"wrong-use", "use", "enc"},
		{"weak-modulus", "n", base64.RawURLEncoding.EncodeToString([]byte{17})},
		{"malformed-modulus", "n", "!"}, {"empty-exponent", "e", ""}, {"even-exponent", "e", base64.RawURLEncoding.EncodeToString([]byte{4})},
		{"zero-leading-exponent", "e", base64.RawURLEncoding.EncodeToString([]byte{0, 3})},
		{"excessive-exponent", "e", base64.RawURLEncoding.EncodeToString([]byte{1, 0, 0, 3})},
		{"private-empty", "d", ""}, {"private-null", "p", nil}, {"private-other", "oth", []any{}},
		{"key-ops-sign", "key_ops", []string{"sign"}}, {"key-ops-empty", "key_ops", []string{}}, {"key-ops-null", "key_ops", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			key := map[string]any{}
			for k, v := range f.jwk {
				key[k] = v
			}
			key[tc.field] = tc.value
			b, _ := json.Marshal(map[string]any{"keys": []any{key}})
			if _, e := NewJWTValidator("https://issuer.example", b, nil); e == nil {
				t.Fatal("unsafe JWK accepted")
			}
		})
	}
	good := f.document(t)
	jwk, _ := json.Marshal(f.jwk)
	for _, b := range [][]byte{
		[]byte(`{"keys":[` + string(jwk) + "," + string(jwk) + `]}`),
		[]byte(`{"keys":[` + strings.TrimSuffix(string(jwk), "}") + `,"KID":"other"}]}`),
		[]byte(`{"keys":[` + string(jwk) + `],"Keys":[]}`),
		[]byte(`{"Keys":[` + string(jwk) + `]}`),
		[]byte(`{"keys":[]}`), []byte("null"), []byte(`{"keys":[null]}`),
		[]byte(`{"keys":[` + strings.TrimSuffix(string(jwk), "}") + `,"kid":"other"}]}`),
		[]byte(strings.Repeat(" ", maxJWKS+1)),
	} {
		if _, e := NewJWTValidator("https://issuer.example", b, nil); e == nil {
			t.Fatal("ambiguous/bounded JWKS accepted")
		}
	}
	for _, issuer := range []string{"", "http://issuer.example", "https://user@issuer.example", "https://issuer.example?query=1", "https://issuer.example#fragment"} {
		if _, e := NewJWTValidator(issuer, good, nil); e == nil {
			t.Fatal("untrusted issuer configuration accepted")
		}
	}
	// Too many distinct keys must fail rather than grow an unbounded key cache.
	var many []any
	for i := 0; i < maxKeys+1; i++ {
		key := map[string]any{}
		for k, v := range f.jwk {
			key[k] = v
		}
		key["kid"] = strings.Repeat("k", i+1)
		many = append(many, key)
	}
	oversized, _ := json.Marshal(map[string]any{"keys": many})
	if _, e := NewJWTValidator("https://issuer.example", oversized, nil); e == nil {
		t.Fatal("too many keys accepted")
	}
	f.jwk["key_ops"] = []string{"verify"}
	if _, e := NewJWTValidator("https://issuer.example", f.document(t), nil); e != nil {
		t.Fatal("verify-only key rejected", e)
	}

	// Snapshot does not retain caller bytes.
	v, e := NewJWTValidator("https://issuer.example", good, func() time.Time { return f.now })
	if e != nil {
		t.Fatal(e)
	}
	for i := range good {
		good[i] = 0
	}
	if _, e = v.Verify(f.raw(t, f.claims()), jwtExpected()); e != nil {
		t.Fatal("snapshot aliases caller data", e)
	}
	var group sync.WaitGroup
	for i := 0; i < 16; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			if _, e := v.Verify(f.raw(t, f.claims()), jwtExpected()); e != nil {
				t.Error("concurrent verification", e)
			}
		}()
	}
	group.Wait()
}

type jwtExchange struct{ raw RawToken }

func (x *jwtExchange) Exchange(code, clientID, redirect, verifier string) (RawToken, error) {
	return x.raw, nil
}
func TestGatewayCallbackUsesActualJWTValidator(t *testing.T) {
	f := newJWTFixture(t)
	x := &jwtExchange{}
	cfg := NewOpenAIConfig("http://127.0.0.1:49152/auth/callback", "urn:uuid:550e8400-e29b-41d4-a716-446655440000", "PHPRetro", x, f.validator)
	cfg.Now = func() time.Time { return f.now }
	g := New(cfg)
	callback := func(label, subject, client string, valid bool) error {
		p, e := g.Begin(label)
		if e != nil {
			return e
		}
		u, _ := url.Parse(p.AuthorizeURL)
		q := u.Query()
		c := f.claims()
		c["nonce"] = q.Get("nonce")
		c["sub"] = subject
		c["aud"] = client
		if !valid {
			c["nonce"] = "wrong"
		}
		x.raw = RawToken{Signed: f.sign(t, c, nil), ClientID: client, Scopes: append([]string(nil), cfg.RequiredScopes...)}
		_, e = g.Callback(label, q.Get("state"), "synthetic-code", client, cfg.CallbackURI, "")
		return e
	}
	if e := callback("one", "account-one", "issued-one", true); e != nil {
		t.Fatal(e)
	}
	if e := g.Select("one"); e != nil {
		t.Fatal(e)
	}
	if e := callback("two", "account-two", "issued-two", true); e != nil {
		t.Fatal(e)
	}
	if e := callback("one", "account-two", "issued-one", true); !errors.Is(e, ErrDenied) {
		t.Fatal("replacement identity accepted", e)
	}
	if e := callback("two", "account-two", "issued-two", false); !errors.Is(e, ErrDenied) {
		t.Fatal("nonce mismatch accepted", e)
	}
	profiles := g.Profiles()
	if len(profiles) != 2 {
		t.Fatal("profiles lost")
	}
	for _, p := range profiles {
		if p.Label == "one" && (p.Identity != "account-one" || !p.Active) {
			t.Fatal("active account replaced after denial")
		}
		if p.Label == "two" && (p.Identity != "account-two" || p.Active) {
			t.Fatal("inactive account corrupted")
		}
	}
}
