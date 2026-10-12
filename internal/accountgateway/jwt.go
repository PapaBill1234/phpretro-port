// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"bytes"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"math/big"
	"net/url"
	"strings"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"
)

const (
	maxJWKS = 1 << 20
	maxKeys = 64
	maxJWT  = 16 << 10
)

// JWTValidator verifies ID tokens against an immutable, caller-provided JWKS snapshot.
// It never performs discovery or network access; key refresh is intentionally unfinished.
type JWTValidator struct {
	issuer string
	keys   map[string]*rsa.PublicKey
	now    func() time.Time
}

type jwksDoc struct {
	Keys []json.RawMessage `json:"keys"`
}
type jwkRSA struct {
	Kty    string          `json:"kty"`
	Kid    string          `json:"kid"`
	Alg    string          `json:"alg"`
	Use    string          `json:"use"`
	N      string          `json:"n"`
	E      string          `json:"e"`
	KeyOps []string        `json:"key_ops"`
	D      json.RawMessage `json:"d"`
	P      json.RawMessage `json:"p"`
	Q      json.RawMessage `json:"q"`
	DP     json.RawMessage `json:"dp"`
	DQ     json.RawMessage `json:"dq"`
	QI     json.RawMessage `json:"qi"`
}

func rejectDuplicateJSON(data []byte) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	var walk func(int) error
	walk = func(depth int) error {
		if depth > 64 {
			return ErrInvalid
		}
		t, e := d.Token()
		if e != nil {
			return e
		}
		delim, ok := t.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]bool{}
			for d.More() {
				k, e := d.Token()
				if e != nil {
					return e
				}
				s, ok := k.(string)
				if !ok || seen[strings.ToLower(s)] {
					return ErrInvalid
				}
				seen[strings.ToLower(s)] = true
				if e = walk(depth + 1); e != nil {
					return e
				}
			}
			_, e = d.Token()
			return e
		case '[':
			for d.More() {
				if e := walk(depth + 1); e != nil {
					return e
				}
			}
			_, e = d.Token()
			return e
		}
		return ErrInvalid
	}
	if e := walk(0); e != nil {
		return e
	}
	if _, e := d.Token(); e != io.EOF {
		return ErrInvalid
	}
	return nil
}
func decodePart(s string) ([]byte, error) {
	if s == "" || strings.Contains(s, "=") {
		return nil, ErrInvalid
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(s)
	if err != nil || base64.RawURLEncoding.EncodeToString(decoded) != s {
		return nil, ErrInvalid
	}
	return decoded, nil
}
func NewJWTValidator(issuer string, jwks []byte, now func() time.Time) (*JWTValidator, error) {
	u, e := url.Parse(issuer)
	if e != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || len(jwks) == 0 || len(jwks) > maxJWKS {
		return nil, ErrInvalid
	}
	if now == nil {
		now = time.Now
	}
	if e = rejectDuplicateJSON(jwks); e != nil || canonicalMembers(jwks, "keys") != nil {
		return nil, ErrInvalid
	}
	var doc jwksDoc
	dec := json.NewDecoder(bytes.NewReader(jwks))
	if e = dec.Decode(&doc); e != nil || len(doc.Keys) == 0 || len(doc.Keys) > maxKeys {
		return nil, ErrInvalid
	}
	keys := map[string]*rsa.PublicKey{}
	for _, raw := range doc.Keys {
		if canonicalMembers(raw, "kty", "kid", "alg", "use", "n", "e", "d", "p", "q", "dp", "dq", "qi", "oth", "key_ops") != nil {
			return nil, ErrInvalid
		}
		var members map[string]json.RawMessage
		if json.Unmarshal(raw, &members) != nil {
			return nil, ErrInvalid
		}
		for _, name := range []string{"d", "p", "q", "dp", "dq", "qi", "oth"} {
			if _, present := members[name]; present {
				return nil, ErrInvalid
			}
		}
		var k jwkRSA
		if e = json.Unmarshal(raw, &k); e != nil || k.Kty != "RSA" || k.Kid == "" || k.N == "" || k.E == "" || len(k.D) > 0 || len(k.P) > 0 || len(k.Q) > 0 || len(k.DP) > 0 || len(k.DQ) > 0 || len(k.QI) > 0 || k.Alg != "" && k.Alg != "RS256" || k.Use != "" && k.Use != "sig" || len(k.KeyOps) > 0 && (len(k.KeyOps) != 1 || k.KeyOps[0] != "verify") {
			return nil, ErrInvalid
		}
		if _, present := members["alg"]; present && k.Alg != "RS256" {
			return nil, ErrInvalid
		}
		if _, present := members["use"]; present && k.Use != "sig" {
			return nil, ErrInvalid
		}
		if _, present := members["key_ops"]; present && (len(k.KeyOps) != 1 || k.KeyOps[0] != "verify") {
			return nil, ErrInvalid
		}
		if _, ok := keys[k.Kid]; ok {
			return nil, ErrInvalid
		}
		nb, er := decodePart(k.N)
		if er != nil {
			return nil, ErrInvalid
		}
		eb, er := decodePart(k.E)
		if er != nil || len(eb) > 4 || len(nb) == 0 || nb[0] == 0 || eb[0] == 0 {
			return nil, ErrInvalid
		}
		n := new(big.Int).SetBytes(nb)
		ex := new(big.Int).SetBytes(eb)
		if n.Bit(0) == 0 || n.BitLen() < 2048 || n.BitLen() > 8192 || !ex.IsInt64() || ex.Int64() < 3 || ex.Int64() > 65537 || ex.Bit(0) == 0 {
			return nil, ErrInvalid
		}
		keys[k.Kid] = &rsa.PublicKey{N: n, E: int(ex.Int64())}
	}
	return &JWTValidator{issuer: issuer, keys: keys, now: now}, nil
}

type idClaims struct {
	Nonce string `json:"nonce"`
	Azp   string `json:"azp"`
	jwt.RegisteredClaims
}

func (v *JWTValidator) Verify(raw RawToken, expected Expected) (VerifiedIdentity, error) {
	fail := func() (VerifiedIdentity, error) { return VerifiedIdentity{}, ErrDenied }
	if v == nil || v.now == nil || len(v.keys) == 0 || v.issuer == "" {
		return fail()
	}
	now := v.now()
	if len(raw.Signed) == 0 || len(raw.Signed) > maxJWT || raw.ClientID == "" || raw.ClientID != expected.ClientID || expected.ClientID == initialClient || expected.HostID == "" || expected.Nonce == "" || !scopesValid(raw.Scopes) || !scopesContain(raw.Scopes, []string{"openid", "chatgpt.tokens.use.direct"}) {
		return fail()
	}
	parts := strings.Split(raw.Signed, ".")
	if len(parts) != 3 {
		return fail()
	}
	head, e := decodePart(parts[0])
	if e != nil {
		return fail()
	}
	payload, e := decodePart(parts[1])
	if e != nil {
		return fail()
	}
	sig, e := decodePart(parts[2])
	if e != nil || len(sig) == 0 || rejectDuplicateJSON(head) != nil || rejectDuplicateJSON(payload) != nil {
		return fail()
	}
	var h struct {
		Alg string `json:"alg"`
		Kid string `json:"kid"`
	}
	if canonicalMembers(head, "alg", "kid", "crit", "b64") != nil || json.Unmarshal(head, &h) != nil || h.Alg != "RS256" || h.Kid == "" {
		return fail()
	}
	var headerMembers map[string]json.RawMessage
	if json.Unmarshal(head, &headerMembers) != nil {
		return fail()
	}
	if _, present := headerMembers["crit"]; present {
		return fail()
	}
	if _, present := headerMembers["b64"]; present {
		return fail()
	}
	if canonicalMembers(payload, "iss", "sub", "aud", "exp", "iat", "nbf", "nonce", "azp") != nil {
		return fail()
	}
	key := v.keys[h.Kid]
	if key == nil {
		return fail()
	}
	claims := &idClaims{}
	parser := jwt.NewParser(jwt.WithValidMethods([]string{"RS256"}), jwt.WithIssuer(v.issuer), jwt.WithAudience(expected.ClientID), jwt.WithExpirationRequired(), jwt.WithIssuedAt(), jwt.WithTimeFunc(func() time.Time { return now }), jwt.WithStrictDecoding())
	token, e := parser.ParseWithClaims(raw.Signed, claims, func(t *jwt.Token) (any, error) {
		if t.Method.Alg() != "RS256" {
			return nil, ErrDenied
		}
		return key, nil
	})
	if e != nil || token == nil || !token.Valid {
		return fail()
	}
	if strings.TrimSpace(claims.Subject) == "" || claims.IssuedAt == nil || claims.ExpiresAt == nil || claims.Nonce != expected.Nonce || claims.IssuedAt.Time.After(now) || !now.Before(claims.ExpiresAt.Time) || claims.NotBefore != nil && claims.NotBefore.Time.After(now) {
		return fail()
	}
	if len(claims.Audience) > 1 && (len(claims.Azp) == 0 || claims.Azp != expected.ClientID) {
		return fail()
	}
	var payloadMembers map[string]json.RawMessage
	if json.Unmarshal(payload, &payloadMembers) != nil {
		return fail()
	}
	if _, present := payloadMembers["azp"]; present && claims.Azp != expected.ClientID {
		return fail()
	}
	if expected.Returning && claims.Subject != expected.Subject {
		return fail()
	}
	return VerifiedIdentity{Subject: claims.Subject, ClientID: expected.ClientID, Nonce: claims.Nonce, HostID: expected.HostID, Scopes: append([]string(nil), raw.Scopes...)}, nil
}
func scopesValid(s []string) bool {
	if len(s) == 0 {
		return false
	}
	seen := map[string]bool{}
	for _, x := range s {
		if !validScope(x) || seen[x] {
			return false
		}
		seen[x] = true
	}
	return true
}

var _ Validator = (*JWTValidator)(nil)

// encoding/json matches struct field names without regard to case. Security
// members use exact standard spellings so aliases cannot select another value.
func canonicalMembers(data []byte, names ...string) error {
	var members map[string]json.RawMessage
	if err := json.Unmarshal(data, &members); err != nil || members == nil {
		return ErrInvalid
	}
	for key := range members {
		for _, name := range names {
			if strings.EqualFold(key, name) && key != name {
				return ErrInvalid
			}
		}
	}
	return nil
}
