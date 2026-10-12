// SPDX-License-Identifier: GPL-3.0-only
package auth

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"crypto/subtle"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"strconv"
	"time"
)

func RandomToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}
func ValidToken(v string) bool {
	if len(v) != 64 {
		return false
	}
	for _, r := range v {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}
func Equal(a, b string) bool {
	return len(a) == len(b) && subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
func staffAAD(id int64, revision string) []byte {
	return []byte("phpretro-staff-mfa:v1:user:" + strconv.FormatInt(id, 10) + ":credential:" + revision)
}
func aead(key []byte) (cipher.AEAD, error) {
	if len(key) != 32 {
		return nil, ErrUnavailable
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, ErrUnavailable
	}
	return cipher.NewGCM(block)
}
func SealSecret(key []byte, id int64, revision string, secret []byte) ([]byte, error) {
	if id <= 0 || !ValidToken(revision) || len(secret) != 20 {
		return nil, ErrInvalid
	}
	c, err := aead(key)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, c.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	out := append([]byte{1}, nonce...)
	return c.Seal(out, nonce, secret, staffAAD(id, revision)), nil
}
func OpenSecret(key []byte, staff Staff) ([]byte, error) {
	c, err := aead(key)
	if err != nil {
		return nil, err
	}
	raw := staff.Secret
	if staff.UserID <= 0 || !ValidToken(staff.MFARevision) || len(raw) != 1+c.NonceSize()+20+c.Overhead() || raw[0] != 1 {
		return nil, ErrUnavailable
	}
	secret, err := c.Open(nil, raw[1:1+c.NonceSize()], raw[1+c.NonceSize():], staffAAD(staff.UserID, staff.MFARevision))
	if err != nil {
		return nil, ErrUnavailable
	}
	return secret, nil
}

// RFC6238/HOTP dynamic truncation, SHA1, six digits and a thirty-second step.
func TOTP(secret []byte, step int64) string {
	var counter [8]byte
	binary.BigEndian.PutUint64(counter[:], uint64(step))
	h := hmac.New(sha1.New, secret)
	_, _ = h.Write(counter[:])
	sum := h.Sum(nil)
	off := sum[len(sum)-1] & 15
	n := (binary.BigEndian.Uint32(sum[off:off+4]) & 0x7fffffff) % 1000000
	return fmt.Sprintf("%06d", n)
}
func MatchStep(secret []byte, code string, now time.Time) (int64, bool) {
	if len(code) != 6 {
		return 0, false
	}
	for _, r := range code {
		if r < '0' || r > '9' {
			return 0, false
		}
	}
	step := now.Unix() / 30
	for _, offset := range []int64{0, -1, 1} {
		if step+offset >= 0 && Equal(TOTP(secret, step+offset), code) {
			return step + offset, true
		}
	}
	return 0, false
}
