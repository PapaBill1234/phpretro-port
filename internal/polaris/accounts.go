// SPDX-License-Identifier: GPL-3.0-only
// Read-only contract: Polaris0101ce955069af1bcf1dd4299b03250679d9b1e1.
package polaris

import (
	"context"
	"database/sql"
	"errors"

	"github.com/PapaBill1234/phpretro-port/internal/auth"
)

type Accounts struct{ DB *sql.DB }

const userColumns = "id, username, password, account_created, access_token_version, machine_id"

func scan(row *sql.Row) (auth.User, error) {
	var u auth.User
	err := row.Scan(&u.ID, &u.Username, &u.Password, &u.CreatedAt, &u.TokenVersion, &u.MachineID)
	if errors.Is(err, sql.ErrNoRows) {
		return u, auth.ErrNotFound
	}
	return u, err
}
func (a *Accounts) Find(ctx context.Context, name string) (auth.User, error) {
	rows, err := a.DB.QueryContext(ctx, "SELECT "+userColumns+" FROM users WHERE username = ? LIMIT 2", name)
	if err != nil {
		return auth.User{}, err
	}
	defer rows.Close()
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return auth.User{}, err
		}
		return auth.User{}, auth.ErrNotFound
	}
	var u auth.User
	if err := rows.Scan(&u.ID, &u.Username, &u.Password, &u.CreatedAt, &u.TokenVersion, &u.MachineID); err != nil {
		return u, err
	}
	if rows.Next() {
		return auth.User{}, auth.ErrNotFound
	}
	if err := rows.Err(); err != nil {
		return auth.User{}, err
	}
	return u, nil
}
func (a *Accounts) ByID(ctx context.Context, id int64) (auth.User, error) {
	return scan(a.DB.QueryRowContext(ctx, "SELECT "+userColumns+" FROM users WHERE id = ?", id))
}
func (a *Accounts) Banned(ctx context.Context, u auth.User, ip string, now int64) (bool, error) {
	var count int
	err := a.DB.QueryRowContext(ctx, `SELECT COUNT(*) FROM bans WHERE ban_expire >= ? AND ((user_id = ? AND type IN ('account','super')) OR (ip = ? AND ip <> '' AND type IN ('ip','super')) OR (machine_id = ? AND machine_id <> '' AND type IN ('machine','super')))`, now, u.ID, ip, u.MachineID).Scan(&count)
	return count > 0, err
}
func (a *Accounts) Ready(ctx context.Context) error {
	rows, err := a.DB.QueryContext(ctx, "SELECT "+userColumns+" FROM users LIMIT 0")
	if err != nil {
		return err
	}
	rows.Close()
	rows, err = a.DB.QueryContext(ctx, "SELECT user_id,ip,machine_id,type,ban_expire FROM bans LIMIT 0")
	if err != nil {
		return err
	}
	return rows.Close()
}
