// SPDX-License-Identifier: GPL-3.0-only
package auth

import (
	"encoding/json"
	"os"
	"testing"
	"time"

	"golang.org/x/crypto/bcrypt"
)

func TestRFC6238SHA1SixDigitVectors(t *testing.T) {
	// RFC6238 Appendix B supplies eight-digit SHA1 results; use their six-digit suffixes.
	for _, tc := range []struct {
		unix int64
		want string
	}{{59, "287082"}, {1111111109, "081804"}, {1111111111, "050471"}, {1234567890, "005924"}, {2000000000, "279037"}, {20000000000, "353130"}} {
		if got := TOTP([]byte("12345678901234567890"), tc.unix/30); got != tc.want {
			t.Fatalf("time%d: got%s want%s", tc.unix, got, tc.want)
		}
	}
}
func TestMFAWindowAndAADBinding(t *testing.T) {
	key := make([]byte, 32)
	secret := []byte("12345678901234567890")
	revision, _ := RandomToken()
	sealed, err := SealSecret(key, 21, revision, secret)
	if err != nil {
		t.Fatal(err)
	}
	staff := Staff{UserID: 21, MFARevision: revision, Secret: sealed}
	decoded, err := OpenSecret(key, staff)
	if err != nil || string(decoded) != string(secret) {
		t.Fatal("roundtrip failed")
	}
	for _, changed := range []Staff{{UserID: 22, MFARevision: revision, Secret: sealed}, {UserID: 21, MFARevision: string(make([]byte, 64)), Secret: sealed}} {
		if _, err = OpenSecret(key, changed); err == nil {
			t.Fatal("relocated MFA secret accepted")
		}
	}
	altered := append([]byte(nil), sealed...)
	altered[len(altered)-1] ^= 1
	staff.Secret = altered
	if _, err = OpenSecret(key, staff); err == nil {
		t.Fatal("tampered MFA accepted")
	}
	now := time.Unix(1234567890, 0)
	step := now.Unix() / 30
	for _, delta := range []int64{-1, 0, 1} {
		matched, ok := MatchStep(secret, TOTP(secret, step+delta), now)
		if !ok || matched != step+delta {
			t.Fatal("allowed step rejected")
		}
	}
	for _, code := range []string{"12345", "1234567", "abcdef", TOTP(secret, step-2), TOTP(secret, step+2)} {
		if _, ok := MatchStep(secret, code, now); ok {
			t.Fatal("outside-window or malformed code accepted")
		}
	}
}
func TestPolarisJavaBcryptInteroperability(t *testing.T) {
	raw, err := os.ReadFile("../../tests/integration/java-bcrypt-fixtures.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Vectors []struct{ ID, Password, Hash string }
	}
	if json.Unmarshal(raw, &fixture) != nil || len(fixture.Vectors) != 4 {
		t.Fatal("fixture invalid")
	}
	for _, vector := range fixture.Vectors {
		t.Run(vector.ID, func(t *testing.T) {
			if err := bcrypt.CompareHashAndPassword([]byte(vector.Hash), []byte(vector.Password)); err != nil {
				t.Fatal("Java hash incompatible")
			}
			if bcrypt.CompareHashAndPassword([]byte(vector.Hash), []byte("wrong-fixture-password")) == nil {
				t.Fatal("wrong password accepted")
			}
		})
	}
	if _, err = bcrypt.GenerateFromPassword([]byte(string(make([]byte, 73))), 12); err == nil {
		t.Fatal("oversized password accepted")
	}
}
func TestCredentialBindingAndOpaqueTokens(t *testing.T) {
	u := User{ID: 2, Username: "fixture", Password: "hash-one", TokenVersion: 0, CreatedAt: 100}
	bound := u.Binding()
	u.Password = "hash-two"
	if Equal(bound, u.Binding()) {
		t.Fatal("password change did not revoke binding")
	}
	u.Password = "hash-one"
	u.TokenVersion++
	if Equal(bound, u.Binding()) {
		t.Fatal("version change did not revoke binding")
	}
	a, _ := RandomToken()
	b, _ := RandomToken()
	if !ValidToken(a) || !ValidToken(b) || Equal(a, b) || ValidToken(a+"x") || ValidToken("not-a-token") {
		t.Fatal("token contract failed")
	}
}
