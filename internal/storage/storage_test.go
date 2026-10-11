package storage

import (
	"context"
	"os"
	"testing"
	"time"
)

func TestValidateSite(t *testing.T) {
	valid := Site{SiteName: "PHPRetro Port", ActiveTheme: "modern", DefaultLocale: "en", EnabledLocales: []string{"en", "nl"}, SettingsRevision: 1}
	if ValidateSite(valid) != nil {
		t.Fatal("valid settings rejected")
	}
	for _, bad := range []Site{
		{SiteName: "x", ActiveTheme: "other", DefaultLocale: "en", EnabledLocales: []string{"en"}, SettingsRevision: 1},
		{SiteName: "x", ActiveTheme: "modern", DefaultLocale: "nl", EnabledLocales: []string{"en"}, SettingsRevision: 1},
		{SiteName: "x", ActiveTheme: "legacy", DefaultLocale: "en", EnabledLocales: []string{"en", "en"}, SettingsRevision: 1},
	} {
		if ValidateSite(bad) == nil {
			t.Fatal("invalid settings accepted")
		}
	}
}

// Explicit opt-in: use only a new disposable schema with migration privileges.
// This test creates additive tables; it never drops a schema or deletes rows.
func TestDisposableIntegration(t *testing.T) {
	dsn := os.Getenv("PHPRETRO_TEST_DISPOSABLE_DSN")
	if dsn == "" {
		t.Skip("set disposable database and Redis test configuration")
	}
	store, err := Open(dsn, os.Getenv("PHPRETRO_TEST_REDIS_ADDR"), os.Getenv("PHPRETRO_TEST_REDIS_PASSWORD"))
	if err != nil {
		t.Fatal("test configuration invalid")
	}
	defer store.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if store.DatabaseReady(ctx) != nil {
		t.Fatal("database probe failed")
	}
	if store.RedisReady(ctx) != nil {
		t.Fatal("Redis probe failed")
	}
	if Migrate(ctx, store.DB) != nil {
		t.Fatal("initial migration failed")
	}
	initial, err := store.Site(ctx)
	if err != nil {
		t.Fatal("settings read failed")
	}
	if initial.ActiveTheme != "modern" || initial.DefaultLocale != "en" || initial.SettingsRevision != 1 {
		t.Fatal("seed mismatch")
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE cms_settings SET site_name = 'Persistence verified', revision = 2 WHERE id = 1"); err != nil {
		t.Fatal("settings update failed")
	}
	if Migrate(ctx, store.DB) != nil {
		t.Fatal("repeat migration failed")
	}
	updated, err := store.Site(ctx)
	if err != nil || updated.SiteName != "Persistence verified" || updated.SettingsRevision != 2 {
		t.Fatal("repeat migration changed persisted settings")
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE cms_schema_migrations SET checksum = REPEAT('0',64) WHERE version = 1"); err != nil {
		t.Fatal("checksum setup failed")
	}
	if Migrate(ctx, store.DB) == nil {
		t.Fatal("checksum mismatch accepted")
	}
}
