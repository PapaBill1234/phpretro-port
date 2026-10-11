// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.
// Source file: internal/profile/profile_test.go. See docs/backend-reuse.md for scope and provenance.

package profile

import (
	"errors"
	"testing"
	"time"
)

type syntheticStore struct {
	profile Profile
	err     error
}

func (s syntheticStore) FindByID(int64) (Profile, error)        { return s.profile, s.err }
func (s syntheticStore) FindByUsername(string) (Profile, error) { return s.profile, s.err }

func syntheticProfile() Profile {
	return Profile{ID: 7, Username: "synthetic-user", Motto: "test", Look: "synthetic-look", Gender: "M", AccountCreated: time.Unix(100, 0).UTC()}
}

func TestProfileReadsByID(t *testing.T) {
	p, err := (Service{Store: syntheticStore{profile: syntheticProfile()}}).ByID(7)
	if err != nil || p.Username != "synthetic-user" || p.Motto != "test" {
		t.Fatalf("profile = %#v, err = %v", p, err)
	}
}

func TestProfileReadsByUsernameTrimsInput(t *testing.T) {
	p, err := (Service{Store: syntheticStore{profile: syntheticProfile()}}).ByUsername(" synthetic-user ")
	if err != nil || p.ID != 7 {
		t.Fatalf("profile = %#v, err = %v", p, err)
	}
}

func TestProfileRejectsMalformedGender(t *testing.T) {
	p := syntheticProfile()
	p.Gender = "X"
	_, err := (Service{Store: syntheticStore{profile: p}}).ByID(7)
	if !errors.Is(err, ErrInvalidGender) {
		t.Fatalf("error = %v", err)
	}
}

func TestProfilePropagatesNotFound(t *testing.T) {
	_, err := (Service{Store: syntheticStore{err: ErrNotFound}}).ByID(7)
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v", err)
	}
}
