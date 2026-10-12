// SPDX-License-Identifier: GPL-3.0-only
package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"github.com/PapaBill1234/phpretro-port/internal/auth"
)

func (s *Store) Staff(ctx context.Context, id int64) (auth.Staff, error) {
	var staff auth.Staff
	err := s.DB.QueryRowContext(ctx, "SELECT user_id,role,role_revision,mfa_revision,mfa_secret,last_totp_step FROM cms_staff_roles WHERE user_id = ?", id).Scan(&staff.UserID, &staff.Role, &staff.RoleRevision, &staff.MFARevision, &staff.Secret, &staff.LastStep)
	if errors.Is(err, sql.ErrNoRows) {
		return staff, auth.ErrNotFound
	}
	return staff, err
}
func (s *Store) UseStep(ctx context.Context, staff auth.Staff, step int64) (bool, error) {
	result, err := s.DB.ExecContext(ctx, "UPDATE cms_staff_roles SET last_totp_step = ? WHERE user_id = ? AND role = 'admin' AND role_revision = ? AND mfa_revision = ? AND last_totp_step < ?", step, staff.UserID, staff.RoleRevision, staff.MFARevision, step)
	if err != nil {
		return false, err
	}
	n, err := result.RowsAffected()
	return n == 1, err
}

var ErrConflict = errors.New("settings revision conflict")

func (s *Store) UpdateSite(ctx context.Context, proof auth.Proof, site Site) (Site, error) {
	if ValidateSite(site) != nil {
		return Site{}, errors.New("invalid settings")
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return Site{}, err
	}
	defer tx.Rollback()
	// Linearize staff revocation/enrollment changes before the settings mutation.
	var role, mfa string
	var revision uint64
	err = tx.QueryRowContext(ctx, "SELECT role,role_revision,mfa_revision FROM cms_staff_roles WHERE user_id = ? FOR UPDATE", proof.UserID).Scan(&role, &revision, &mfa)
	if errors.Is(err, sql.ErrNoRows) {
		return Site{}, auth.ErrForbidden
	}
	if err != nil {
		return Site{}, err
	}
	if role != "admin" || revision == 0 || revision != proof.RoleRevision || !auth.ValidToken(proof.MFARevision) || !auth.Equal(mfa, proof.MFARevision) {
		return Site{}, auth.ErrForbidden
	}
	before := Site{Version: "site.v1", Health: "ok"}
	var raw []byte
	err = tx.QueryRowContext(ctx, "SELECT site_name,active_theme,default_locale,enabled_locales,revision FROM cms_settings WHERE id = 1 FOR UPDATE").Scan(&before.SiteName, &before.ActiveTheme, &before.DefaultLocale, &raw, &before.SettingsRevision)
	if err != nil {
		return Site{}, err
	}
	if json.Unmarshal(raw, &before.EnabledLocales) != nil || ValidateSite(before) != nil {
		return Site{}, errors.New("invalid persisted settings")
	}
	if before.SettingsRevision != site.SettingsRevision {
		return Site{}, ErrConflict
	}
	if before.SettingsRevision >= 9007199254740991 {
		return Site{}, errors.New("settings revision exhausted")
	}
	site.Version = "site.v1"
	site.Health = "ok"
	site.SettingsRevision++
	locales, _ := json.Marshal(site.EnabledLocales)
	if _, err = tx.ExecContext(ctx, "UPDATE cms_settings SET site_name=?,active_theme=?,default_locale=?,enabled_locales=?,revision=? WHERE id=1", site.SiteName, site.ActiveTheme, site.DefaultLocale, locales, site.SettingsRevision); err != nil {
		return Site{}, err
	}
	beforeJSON, _ := json.Marshal(before)
	afterJSON, _ := json.Marshal(site)
	if _, err = tx.ExecContext(ctx, "INSERT INTO cms_audit_events (actor_user_id,action,before_json,after_json) VALUES (?,'site.settings',?,?)", proof.UserID, beforeJSON, afterJSON); err != nil {
		return Site{}, err
	}
	if err = tx.Commit(); err != nil {
		return Site{}, err
	}
	return site, nil
}

// GrantAdmin is operator-only and uses the migration identity, never the web principal.
func GrantAdmin(ctx context.Context, db *sql.DB, staff auth.Staff) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var id int64
	err = tx.QueryRowContext(ctx, "SELECT user_id FROM cms_staff_roles WHERE user_id=? FOR UPDATE", staff.UserID).Scan(&id)
	if err == nil {
		return errors.New("staff already enrolled")
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if _, err = tx.ExecContext(ctx, "INSERT INTO cms_staff_roles (user_id,role,role_revision,mfa_revision,mfa_secret) VALUES (?,'admin',1,?,?)", staff.UserID, staff.MFARevision, staff.Secret); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO cms_audit_events(actor_user_id,action,before_json,after_json) VALUES (0,'staff.enroll','{}',JSON_OBJECT('userId',?))`, staff.UserID); err != nil {
		return err
	}
	return tx.Commit()
}
func RevokeAdmin(ctx context.Context, db *sql.DB, id int64) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var found int64
	err = tx.QueryRowContext(ctx, "SELECT user_id FROM cms_staff_roles WHERE user_id=? FOR UPDATE", id).Scan(&found)
	if errors.Is(err, sql.ErrNoRows) {
		return auth.ErrNotFound
	}
	if err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM cms_staff_roles WHERE user_id=?", id); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO cms_audit_events(actor_user_id,action,before_json,after_json) VALUES (0,'staff.revoke',JSON_OBJECT('userId',?),'{}')`, id); err != nil {
		return err
	}
	return tx.Commit()
}
