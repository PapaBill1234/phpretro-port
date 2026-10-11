// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.
// Source file: internal/accountview/accountview_test.go. See docs/backend-reuse.md for scope and provenance.

package accountview

import (
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/PapaBill1234/phpretro-port/internal/profile"
)

type syntheticSource struct {
	view AccountView
	err  error
}

func (s syntheticSource) DashboardForPrincipal(int64) (AccountView, error) {
	return s.view, s.err
}

func syntheticPrincipal() Principal { return Principal{ID: 7} }

func syntheticProfile() profile.Profile {
	return profile.Profile{ID: 7, Username: "synthetic-user", Motto: "test motto", Look: "synthetic-look", Gender: "M", AccountCreated: time.Unix(100, 0).UTC()}
}

func TestReadOwnDashboardRendersProfileMottoStatusAndWidgets(t *testing.T) {
	want := AccountView{Profile: syntheticProfile(), Status: "online", Widgets: []Widget{{Name: "feed", Value: "synthetic"}}}
	got, err := (Service{Source: syntheticSource{view: want}}).Read(syntheticPrincipal())
	if err != nil {
		t.Fatalf("Read() error = %v", err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Read() = %#v, want %#v", got, want)
	}
	if got.Profile.Motto != "test motto" {
		t.Fatalf("Read().Profile.Motto = %q, want %q", got.Profile.Motto, "test motto")
	}
}

func TestReadDeniesCrossPrincipalDashboard(t *testing.T) {
	other := syntheticProfile()
	other.ID = 8
	_, err := (Service{Source: syntheticSource{view: AccountView{Profile: other}}}).Read(syntheticPrincipal())
	if !errors.Is(err, ErrPrincipalMismatch) {
		t.Fatalf("Read() error = %v, want %v", err, ErrPrincipalMismatch)
	}
}

func TestReadPreservesAbsentOptionalWidgets(t *testing.T) {
	want := AccountView{Profile: syntheticProfile(), Status: "offline"}
	got, err := (Service{Source: syntheticSource{view: want}}).Read(syntheticPrincipal())
	if err != nil {
		t.Fatalf("Read() error = %v", err)
	}
	if got.Widgets != nil {
		t.Fatalf("Read().Widgets = %#v, want nil absent widgets", got.Widgets)
	}
}

func TestReadRejectsMalformedPrincipal(t *testing.T) {
	_, err := (Service{Source: syntheticSource{view: AccountView{Profile: syntheticProfile()}}}).Read(Principal{})
	if !errors.Is(err, ErrInvalidPrincipal) {
		t.Fatalf("Read() error = %v, want %v", err, ErrInvalidPrincipal)
	}
}

func TestReadRejectsUnavailableSource(t *testing.T) {
	_, err := (Service{}).Read(syntheticPrincipal())
	if !errors.Is(err, ErrSourceUnavailable) {
		t.Fatalf("Read() error = %v, want %v", err, ErrSourceUnavailable)
	}
}
