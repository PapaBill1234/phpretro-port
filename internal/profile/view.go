// SPDX-License-Identifier: GPL-3.0-only
// Adapted from PapaBill1234/phpretro-preservation at af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7.
// Source file: internal/profile/view.go. See docs/backend-reuse.md for scope and provenance.

package profile

import (
	"strconv"
	"strings"
)

// Tab is the selected profile presentation tab, constrained to the range
// [TabMin, TabMax] by the original profile.php:28-42 read behavior and the
// upstream planning contract retained at the pinned source revision.
type Tab int

const (
	// TabMin and TabMax bound the selectable presentation tabs, matching the
	// original "below 1 or above 5" range check.
	TabMin = Tab(1)
	TabMax = Tab(5)
	// TabDefault is the tab used when no valid selection is present, matching
	// the original "absent tab becomes the string 1" default.
	TabDefault = Tab(1)
)

// NormalizeTab maps a raw tab selection onto a valid Tab.
//
// It trims surrounding whitespace, then accepts only an integer in
// [TabMin, TabMax]. An empty/absent value, a non-integer value, or an
// out-of-range integer all fall back to TabDefault. This preserves the
// original profile.php:28-42 range/default intent without reproducing PHP
// coercion, Location headers, or HTTP status behavior; see also the planning
// contract recorded in docs/backend-reuse.md.
func NormalizeTab(raw string) Tab {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return TabDefault
	}
	value, err := strconv.Atoi(trimmed)
	if err != nil {
		return TabDefault
	}
	tab := Tab(value)
	if tab < TabMin || tab > TabMax {
		return TabDefault
	}
	return tab
}

// View is the read-only profile presentation model: the accepted six-field
// Profile projection plus the selected presentation Tab. It adds no
// principal, auth, session, or privacy field. The projection is defined in
// the upstream source revision recorded in docs/backend-reuse.md.
type View struct {
	Profile Profile
	Tab     Tab
}

// NewView builds a View from a profile and a raw tab selection, normalizing
// the selection through NormalizeTab so absent or invalid input becomes
// TabDefault.
func NewView(p Profile, rawTab string) View {
	return View{Profile: p, Tab: NormalizeTab(rawTab)}
}
