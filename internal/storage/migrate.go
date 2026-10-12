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
	checksums := map[int]string{}
	for _, migration := range migrations.All() {
		sum := sha256.Sum256([]byte(migration.SQL))
		checksums[migration.Version] = hex.EncodeToString(sum[:])
	}
	rows, err := conn.QueryContext(ctx, "SELECT version, checksum FROM cms_schema_migrations")
	if err != nil {
		return err
	}
	applied := map[int]bool{}
	for rows.Next() {
		var version int
		var recorded string
		if err := rows.Scan(&version, &recorded); err != nil {
			rows.Close()
			return err
		}
		if expected, known := checksums[version]; !known || recorded != expected {
			rows.Close()
			return errors.New("migration version or checksum mismatch")
		}
		applied[version] = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, migration := range migrations.All() {
		if applied[migration.Version] {
			continue
		}
		parts := strings.Split(migration.SQL, "-- seed-transaction\n")
		if len(parts) > 2 {
			return errors.New("invalid embedded migration")
		}
		for _, statement := range strings.Split(parts[0], "-- next-statement\n") {
			if _, err := conn.ExecContext(ctx, strings.TrimSpace(statement)); err != nil {
				return err
			}
		}
		tx, err := conn.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		if len(parts) == 2 {
			if _, err = tx.ExecContext(ctx, strings.TrimSpace(parts[1])); err != nil {
				tx.Rollback()
				return err
			}
		}
		if _, err = tx.ExecContext(ctx, "INSERT INTO cms_schema_migrations (version,checksum) VALUES (?,?)", migration.Version, checksums[migration.Version]); err != nil {
			tx.Rollback()
			return err
		}
		if err = tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}
