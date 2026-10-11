package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/redis/go-redis/v9"
)

type Site struct {
	Version          string   `json:"version"`
	SiteName         string   `json:"siteName"`
	ActiveTheme      string   `json:"activeTheme"`
	DefaultLocale    string   `json:"defaultLocale"`
	EnabledLocales   []string `json:"enabledLocales"`
	SettingsRevision uint64   `json:"settingsRevision"`
	Health           string   `json:"health"`
}

type Store struct {
	DB    *sql.DB
	Cache *redis.Client
}

func Open(dsn, redisAddr, redisPassword string) (*Store, error) {
	db, err := OpenDB(dsn)
	if err != nil {
		return nil, err
	}
	if redisAddr == "" {
		redisAddr = "redis:6379"
	}
	return &Store{DB: db, Cache: redis.NewClient(&redis.Options{Addr: redisAddr, Password: redisPassword, DialTimeout: 2 * time.Second, ReadTimeout: 2 * time.Second, WriteTimeout: 2 * time.Second, MaxRetries: -1})}, nil
}

func OpenDB(dsn string) (*sql.DB, error) {
	if dsn == "" {
		return nil, errors.New("database configuration is required")
	}
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil {
		return nil, errors.New("invalid database configuration")
	}
	cfg.Timeout, cfg.ReadTimeout, cfg.WriteTimeout = 3*time.Second, 3*time.Second, 3*time.Second
	cfg.MultiStatements = false
	connector, err := mysql.NewConnector(cfg)
	if err != nil {
		return nil, errors.New("invalid database configuration")
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(3 * time.Minute)
	return db, nil
}

func (s *Store) Close()                                  { _ = s.DB.Close(); _ = s.Cache.Close() }
func (s *Store) DatabaseReady(ctx context.Context) error { return s.DB.PingContext(ctx) }
func (s *Store) RedisReady(ctx context.Context) error    { return s.Cache.Ping(ctx).Err() }
func (s *Store) Site(ctx context.Context) (Site, error) {
	v := Site{Version: "site.v1", Health: "ok"}
	var locales []byte
	err := s.DB.QueryRowContext(ctx, "SELECT site_name, active_theme, default_locale, enabled_locales, revision FROM cms_settings WHERE id = 1").Scan(&v.SiteName, &v.ActiveTheme, &v.DefaultLocale, &locales, &v.SettingsRevision)
	if err != nil {
		return Site{}, err
	}
	if err := json.Unmarshal(locales, &v.EnabledLocales); err != nil {
		return Site{}, errors.New("invalid site settings")
	}
	if err := ValidateSite(v); err != nil {
		return Site{}, err
	}
	return v, nil
}

func ValidateSite(v Site) error {
	if v.SiteName == "" || v.SettingsRevision == 0 || (v.ActiveTheme != "modern" && v.ActiveTheme != "legacy") || len(v.EnabledLocales) == 0 {
		return errors.New("invalid site settings")
	}
	found, seen := false, map[string]bool{}
	for _, locale := range v.EnabledLocales {
		if (locale != "en" && locale != "nl") || seen[locale] {
			return errors.New("invalid site settings")
		}
		seen[locale] = true
		found = found || locale == v.DefaultLocale
	}
	if !found {
		return errors.New("invalid site settings")
	}
	return nil
}
