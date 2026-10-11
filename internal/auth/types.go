// SPDX-License-Identifier: GPL-3.0-only
package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"unicode"
	"unicode/utf8"
)

var (
	ErrInvalid         = errors.New("invalid credentials")
	ErrUnavailable     = errors.New("authentication dependency unavailable")
	ErrUnauthenticated = errors.New("session unavailable")
	ErrLimited         = errors.New("login rate limited")
	ErrNotFound        = errors.New("identity not found")
	ErrForbidden       = errors.New("staff authorization unavailable")
)

type User struct {
	ID                            int64
	Username, Password, MachineID string
	CreatedAt                     int64
	TokenVersion                  int64
}

func (u User) Valid() bool {
	return u.ID > 0 && u.ID <= 2147483647 && ValidUsername(u.Username) && u.CreatedAt >= 0 && u.TokenVersion >= 0
}
func ValidUsername(name string) bool {
	if name == "" || !utf8.ValidString(name) || utf8.RuneCountInString(name) > 25 {
		return false
	}
	for _, r := range name {
		if r > 0xffff || unicode.IsControl(r) {
			return false
		}
	}
	return true
}
func (u User) Binding() string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d:%d:%s", u.ID, u.TokenVersion, u.Password)))
	return hex.EncodeToString(sum[:])
}

type Staff struct {
	UserID       int64
	Role         string
	RoleRevision uint64
	MFARevision  string
	Secret       []byte
	LastStep     int64
}
type Proof struct {
	UserID       int64
	RoleRevision uint64
	MFARevision  string
}
type Session struct {
	UserID       int64  `json:"userId"`
	Binding      string `json:"binding"`
	CSRF         string `json:"csrf"`
	ExpiresAt    int64  `json:"expiresAt"`
	RoleRevision uint64 `json:"roleRevision"`
	MFARevision  string `json:"mfaRevision"`
}

func (s Session) Proof() Proof { return Proof{s.UserID, s.RoleRevision, s.MFARevision} }

type Principal struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	Role     string `json:"role"`
}
type View struct {
	Version       string     `json:"version"`
	Authenticated bool       `json:"authenticated"`
	User          *Principal `json:"user,omitempty"`
	CSRFToken     string     `json:"csrfToken,omitempty"`
}

func Guest() View { return View{Version: "session.v1", Authenticated: false} }
func ViewFor(s Session, p Principal) View {
	return View{Version: "session.v1", Authenticated: true, User: &p, CSRFToken: s.CSRF}
}

type Accounts interface {
	Find(context.Context, string) (User, error)
	ByID(context.Context, int64) (User, error)
	Banned(context.Context, User, string, int64) (bool, error)
	Ready(context.Context) error
}
type Roles interface {
	Staff(context.Context, int64) (Staff, error)
	UseStep(context.Context, Staff, int64) (bool, error)
}
