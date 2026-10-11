package storage

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	migrations "github.com/PapaBill1234/phpretro-port/migrations/cms"
)

// Migrate uses a dedicated principal. MariaDB DDL commits implicitly; the
// additive DDL is idempotent, while the seed and version record commit together.
func Migrate(ctx context.Context, db *sql.DB) error {
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked int
	if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK('phpretro_cms_migrations', 10)").Scan(&locked); err != nil {
		return err
	}
	if locked != 1 {
		return errors.New("migration lock unavailable")
	}
	defer func() {
		release, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = conn.ExecContext(release, "DO RELEASE_LOCK('phpretro_cms_migrations')")
	}()
	if _, err := conn.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS cms_schema_migrations (version INT UNSIGNED NOT NULL PRIMARY KEY, checksum CHAR(64) NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"); err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(migrations.SiteSettingsSQL))
	checksum := hex.EncodeToString(sum[:])
	rows, err := conn.QueryContext(ctx, "SELECT version, checksum FROM cms_schema_migrations")
	if err != nil {
		return err
	}
	applied := false
	for rows.Next() {
		var version int
		var recorded string
		if err := rows.Scan(&version, &recorded); err != nil {
			rows.Close()
			return err
		}
		if version != 1 || recorded != checksum {
			rows.Close()
			return errors.New("migration version or checksum mismatch")
		}
		applied = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if applied {
		return nil
	}
	parts := strings.Split(migrations.SiteSettingsSQL, "-- seed-transaction\n")
	if len(parts) != 2 {
		return errors.New("invalid embedded migration")
	}
	if _, err := conn.ExecContext(ctx, strings.TrimSpace(parts[0])); err != nil {
		return err
	}
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, strings.TrimSpace(parts[1])); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO cms_schema_migrations (version, checksum) VALUES (1, ?)", checksum); err != nil {
		return err
	}
	return tx.Commit()
}
