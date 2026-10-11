// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.
// Source file: internal/profile/profile.go. See docs/backend-reuse.md for scope and provenance.

// Package profile contains the read-only public profile boundary.
package profile

import (
	"errors"
	"strings"
	"time"
)

var (
	ErrNotFound         = errors.New("profile not found")
	ErrStoreUnavailable = errors.New("profile store unavailable")
	ErrInvalidGender    = errors.New("invalid profile gender")
)

type Profile struct {
	ID             int64
	Username       string
	Motto          string
	Look           string
	Gender         string
	AccountCreated time.Time
}

type Store interface {
	FindByID(id int64) (Profile, error)
	FindByUsername(username string) (Profile, error)
}

type Service struct{ Store Store }

func (s Service) ByID(id int64) (Profile, error) {
	if s.Store == nil || id <= 0 {
		return Profile{}, ErrStoreUnavailable
	}
	return validate(s.Store.FindByID(id))
}

func (s Service) ByUsername(username string) (Profile, error) {
	username = strings.TrimSpace(username)
	if s.Store == nil || username == "" {
		return Profile{}, ErrStoreUnavailable
	}
	return validate(s.Store.FindByUsername(username))
}

func validate(p Profile, err error) (Profile, error) {
	if err != nil {
		return Profile{}, err
	}
	if p.ID <= 0 || p.Username == "" {
		return Profile{}, ErrNotFound
	}
	if p.Gender != "M" && p.Gender != "F" {
		return Profile{}, ErrInvalidGender
	}
	return p, nil
}
