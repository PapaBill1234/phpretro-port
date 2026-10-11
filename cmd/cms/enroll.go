// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"context"
	"crypto/rand"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strconv"

	"github.com/PapaBill1234/phpretro-port/internal/auth"
	"github.com/PapaBill1234/phpretro-port/internal/polaris"
	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

func staffKey() ([]byte, error) {
	key, err := hex.DecodeString(os.Getenv("CMS_STAFF_ENCRYPTION_KEY"))
	if err != nil || len(key) != 32 {
		return nil, errors.New("staff encryption configuration unavailable")
	}
	return key, nil
}
func enrollment(ctx context.Context, id int64, path string) error {
	if id <= 0 || id > 2147483647 || path == "" {
		return errors.New("enrollment arguments invalid")
	}
	db, err := storage.OpenDB(os.Getenv("CMS_MIGRATION_DSN"))
	if err != nil {
		return err
	}
	defer db.Close()
	game, err := storage.OpenDB(os.Getenv("POLARIS_DATABASE_DSN"))
	if err != nil {
		return err
	}
	defer game.Close()
	user, err := (&polaris.Accounts{DB: game}).ByID(ctx, id)
	if err != nil || !user.Valid() {
		return errors.New("game identity unavailable")
	}
	key, err := staffKey()
	if err != nil {
		return err
	}
	revision, err := auth.RandomToken()
	if err != nil {
		return err
	}
	secret := make([]byte, 20)
	if _, err = rand.Read(secret); err != nil {
		return err
	}
	sealed, err := auth.SealSecret(key, id, revision, secret)
	if err != nil {
		return err
	}
	encoded := base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(secret)
	values := url.Values{"secret": {encoded}, "issuer": {"PHPRetro Port"}, "algorithm": {"SHA1"}, "digits": {"6"}, "period": {"30"}}
	uri := (&url.URL{Scheme: "otpauth", Host: "totp", Path: "/PHPRetro Port:" + user.Username, RawQuery: values.Encode()}).String()
	data, err := json.MarshalIndent(map[string]any{"version": "staff-enrollment.v1", "userId": id, "username": user.Username, "secret": encoded, "otpauthUri": uri}, "", "  ")
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return errors.New("enrollment file must be new and private")
	}
	success := false
	defer func() {
		file.Close()
		if !success {
			_ = os.Remove(path)
		}
	}()
	if _, err = file.Write(append(data, '\n')); err != nil {
		return err
	}
	if err = file.Sync(); err != nil {
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	if err = storage.GrantAdmin(ctx, db, auth.Staff{UserID: id, Role: "admin", RoleRevision: 1, MFARevision: revision, Secret: sealed, LastStep: -1}); err != nil {
		return err
	}
	success = true
	return nil
}
func revoke(ctx context.Context, id int64) error {
	if id <= 0 || id > 2147483647 {
		return errors.New("invalid identity")
	}
	db, err := storage.OpenDB(os.Getenv("CMS_MIGRATION_DSN"))
	if err != nil {
		return err
	}
	defer db.Close()
	return storage.RevokeAdmin(ctx, db, id)
}
func secureCookies() (bool, error) {
	value := os.Getenv("CMS_COOKIE_SECURE")
	if value == "" {
		return true, nil
	}
	return strconv.ParseBool(value)
}
