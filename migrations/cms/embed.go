// Package cms contains the CMS-owned, additive schema migration.
package cms

import _ "embed"

//go:embed 001_site_settings.sql
var SiteSettingsSQL string

//go:embed 002_staff_and_audit.sql
var StaffSQL string

type Migration struct {
	Version int
	SQL     string
}

func All() []Migration { return []Migration{{1, SiteSettingsSQL}, {2, StaffSQL}} }
