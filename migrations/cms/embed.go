// Package cms contains the CMS-owned, additive schema migration.
package cms

import _ "embed"

//go:embed 001_site_settings.sql
var SiteSettingsSQL string
