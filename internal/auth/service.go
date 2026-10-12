// SPDX-License-Identifier: GPL-3.0-only
package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/redis/go-redis/v9"
	"golang.org/x/crypto/bcrypt"
)

type Service struct {
	Accounts Accounts
	Roles    Roles
	Cache    *redis.Client
	Key      []byte
	Now      func() time.Time
	dummy    []byte
}

const SessionTTL = 8 * time.Hour

func New(accounts Accounts, roles Roles, cache *redis.Client, key []byte) (*Service, error) {
	if len(key) != 32 {
		return nil, ErrUnavailable
	}
	random, err := RandomToken()
	if err != nil {
		return nil, err
	}
	dummy, err := bcrypt.GenerateFromPassword([]byte(random), 12)
	if err != nil {
		return nil, err
	}
	return &Service{accounts, roles, cache, append([]byte(nil), key...), time.Now, dummy}, nil
}
func sessionKey(token string) string { return "phpretro:session:" + User{Password: token}.Binding() }
func (a *Service) limit(ctx context.Context, username, ip string) error {
	account := User{Password: strings.ToLower(username)}.Binding()
	peer := User{Password: ip}.Binding()
	// Fixed windows are incremented before verification; outages never bypass limits.
	result, err := a.Cache.Eval(ctx, `local a=redis.call('INCR',KEYS[1]);if a==1 then redis.call('EXPIRE',KEYS[1],300) end;local b=redis.call('INCR',KEYS[2]);if b==1 then redis.call('EXPIRE',KEYS[2],900) end;if a>30 or b>8 then return 0 end;return 1`, []string{"phpretro:login:ip:" + peer, "phpretro:login:account:" + account}).Int()
	if err != nil {
		return ErrUnavailable
	}
	if result != 1 {
		return ErrLimited
	}
	return nil
}
func (a *Service) Login(ctx context.Context, username, password, otp, ip, previous string) (string, Session, Principal, error) {
	username = strings.TrimSpace(username)
	if !ValidUsername(username) || len(password) == 0 || len(password) > 72 || !utf8.ValidString(password) {
		return "", Session{}, Principal{}, ErrInvalid
	}
	if err := a.limit(ctx, username, ip); err != nil {
		return "", Session{}, Principal{}, err
	}
	u, err := a.Accounts.Find(ctx, username)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			_ = bcrypt.CompareHashAndPassword(a.dummy, []byte(password))
			return "", Session{}, Principal{}, ErrInvalid
		}
		return "", Session{}, Principal{}, ErrUnavailable
	}
	if u.Valid() {
		// The database collation folds case and accents: limit its immutable ID.
		count, limitErr := a.Cache.Eval(ctx, `local n=redis.call('INCR',KEYS[1]);if n==1 then redis.call('EXPIRE',KEYS[1],900) end;return n`, []string{fmt.Sprintf("phpretro:login:user:%d", u.ID)}).Int()
		if limitErr != nil {
			return "", Session{}, Principal{}, ErrUnavailable
		}
		if count > 8 {
			return "", Session{}, Principal{}, ErrLimited
		}
	}
	cost, costErr := bcrypt.Cost([]byte(u.Password))
	if !u.Valid() || costErr != nil || cost > 14 || cost < 4 {
		_ = bcrypt.CompareHashAndPassword(a.dummy, []byte(password))
		return "", Session{}, Principal{}, ErrInvalid
	}
	if bcrypt.CompareHashAndPassword([]byte(u.Password), []byte(password)) != nil {
		return "", Session{}, Principal{}, ErrInvalid
	}
	now := a.Now()
	banned, err := a.Accounts.Banned(ctx, u, ip, now.Unix())
	if err != nil {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	if banned {
		return "", Session{}, Principal{}, ErrInvalid
	}
	token, err := RandomToken()
	if err != nil {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	csrf, err := RandomToken()
	if err != nil {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	// Bind only the credential snapshot actually checked, including its token version.
	s := Session{UserID: u.ID, Binding: u.Binding(), CSRF: csrf, ExpiresAt: now.Add(SessionTTL).Unix()}
	p := Principal{ID: u.ID, Username: u.Username, Role: "member"}
	staff, err := a.Roles.Staff(ctx, u.ID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	if err == nil {
		if staff.Role != "admin" || staff.RoleRevision == 0 {
			return "", Session{}, Principal{}, ErrUnavailable
		}
		secret, err := OpenSecret(a.Key, staff)
		if err != nil {
			return "", Session{}, Principal{}, ErrUnavailable
		}
		step, ok := MatchStep(secret, otp, now)
		if !ok {
			return "", Session{}, Principal{}, ErrInvalid
		}
		used, err := a.Roles.UseStep(ctx, staff, step)
		if err != nil {
			return "", Session{}, Principal{}, ErrUnavailable
		}
		if !used {
			return "", Session{}, Principal{}, ErrInvalid
		}
		s.RoleRevision = staff.RoleRevision
		s.MFARevision = staff.MFARevision
		p.Role = "admin"
	}
	raw, err := json.Marshal(s)
	if err != nil {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	old := sessionKey(token)
	if ValidToken(previous) {
		old = sessionKey(previous)
	}
	if _, err = a.Cache.Eval(ctx, `redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]);if KEYS[1]~=KEYS[2] then redis.call('DEL',KEYS[2]) end;return 1`, []string{sessionKey(token), old}, raw, int64(SessionTTL/time.Second)).Result(); err != nil {
		return "", Session{}, Principal{}, ErrUnavailable
	}
	return token, s, p, nil
}
func (a *Service) Current(ctx context.Context, token, ip string) (Session, Principal, error) {
	if !ValidToken(token) {
		return Session{}, Principal{}, ErrUnauthenticated
	}
	raw, err := a.Cache.Get(ctx, sessionKey(token)).Bytes()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return Session{}, Principal{}, ErrUnauthenticated
		}
		return Session{}, Principal{}, ErrUnavailable
	}
	var s Session
	if json.Unmarshal(raw, &s) != nil || s.UserID <= 0 || s.ExpiresAt <= a.Now().Unix() || !ValidToken(s.CSRF) || !ValidToken(s.Binding) {
		return a.invalidate(ctx, token)
	}
	u, err := a.Accounts.ByID(ctx, s.UserID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return a.invalidate(ctx, token)
		}
		return Session{}, Principal{}, ErrUnavailable
	}
	if !u.Valid() || !Equal(s.Binding, u.Binding()) {
		return a.invalidate(ctx, token)
	}
	banned, err := a.Accounts.Banned(ctx, u, ip, a.Now().Unix())
	if err != nil {
		return Session{}, Principal{}, ErrUnavailable
	}
	if banned {
		return a.invalidate(ctx, token)
	}
	p := Principal{ID: u.ID, Username: u.Username, Role: "member"}
	staff, err := a.Roles.Staff(ctx, u.ID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return Session{}, Principal{}, ErrUnavailable
	}
	if s.RoleRevision != 0 || s.MFARevision != "" {
		if err != nil || staff.Role != "admin" || staff.RoleRevision == 0 || s.RoleRevision != staff.RoleRevision || s.MFARevision != staff.MFARevision || !ValidToken(s.MFARevision) {
			return a.invalidate(ctx, token)
		}
		p.Role = "admin"
	}
	return s, p, nil
}
func (a *Service) invalidate(ctx context.Context, token string) (Session, Principal, error) {
	if a.Cache.Del(ctx, sessionKey(token)).Err() != nil {
		return Session{}, Principal{}, ErrUnavailable
	}
	return Session{}, Principal{}, ErrUnauthenticated
}
func (a *Service) Logout(ctx context.Context, token string) error {
	if !ValidToken(token) {
		return ErrUnauthenticated
	}
	if a.Cache.Del(ctx, sessionKey(token)).Err() != nil {
		return ErrUnavailable
	}
	return nil
}
