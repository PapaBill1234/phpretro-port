// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.
// Source file: internal/accountview/accountview.go. See docs/backend-reuse.md for scope and provenance.

// Package accountview contains the read-only synthetic account dashboard boundary.
package accountview

import (
	"errors"

	"github.com/PapaBill1234/phpretro-port/internal/profile"
)

var (
	ErrInvalidPrincipal  = errors.New("invalid account principal")
	ErrPrincipalMismatch = errors.New("account principal mismatch")
	ErrSourceUnavailable = errors.New("account view source unavailable")
)

// Principal identifies the already-established synthetic caller. Authentication,
// session creation, cookies, and HTTP are intentionally outside this package.
type Principal struct {
	ID int64
}

// Widget is an optional, source-provided dashboard widget. Its semantics are
// intentionally opaque because the legacy data source and response are unknown.
type Widget struct {
	Name  string
	Value string
}

// AccountView is the read-only dashboard projection established by the F20
// planning contract. Optional widgets are nil when the source provides none.
type AccountView struct {
	Profile profile.Profile
	Status  string
	Widgets []Widget
}

// Source supplies the dashboard for the requested synthetic principal. It must
// apply any source-side lookup and return the principal's own profile.
type Source interface {
	DashboardForPrincipal(principalID int64) (AccountView, error)
}

type Service struct {
	Source Source
}

// Read returns only the dashboard belonging to principal. It does not perform
// authentication or establish a session.
func (s Service) Read(principal Principal) (AccountView, error) {
	if principal.ID <= 0 {
		return AccountView{}, ErrInvalidPrincipal
	}
	if s.Source == nil {
		return AccountView{}, ErrSourceUnavailable
	}
	view, err := s.Source.DashboardForPrincipal(principal.ID)
	if err != nil {
		return AccountView{}, err
	}
	if view.Profile.ID != principal.ID {
		return AccountView{}, ErrPrincipalMismatch
	}
	return view, nil
}
