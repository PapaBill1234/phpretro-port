package accountvault

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"golang.org/x/sys/unix"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testDir(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "private-vault")
}

func openTestVault(t *testing.T) (*Vault, string) {
	t.Helper()
	dir := testDir(t)
	v, err := Open(dir)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return v, dir
}

func account(label string) Account {
	return Account{
		Label: label, ClientID: "issued-client", Subject: "subject-" + label,
		IDToken: "id-secret-" + label, AccessToken: "access-secret-" + label,
		RefreshToken: "refresh-secret-" + label, ExpiresAt: time.Now().UTC().Add(time.Hour),
		Scopes: []string{"openid", "profile"},
	}
}

func TestOpenCreatesPrivateEncryptedVaultAndReopens(t *testing.T) {
	v, dir := openTestVault(t)
	initial := v.State()
	if !validHostID(initial.HostID) || initial.Active != "" || len(initial.Accounts) != 0 {
		t.Fatalf("invalid initial state: %#v", initial)
	}
	if err := v.Close(); err != nil {
		t.Fatal(err)
	}
	for name, wantMode := range map[string]os.FileMode{"": 0700, keyName: 0600, lockName: 0600, stateName: 0600} {
		info, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != wantMode {
			t.Fatalf("%s mode = %o, want %o", name, info.Mode().Perm(), wantMode)
		}
	}
	key, err := os.ReadFile(filepath.Join(dir, keyName))
	if err != nil || len(key) != 32 {
		t.Fatalf("key length=%d err=%v", len(key), err)
	}
	ciphertext, err := os.ReadFile(filepath.Join(dir, stateName))
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"id-secret", "access-secret", "refresh-secret"} {
		if strings.Contains(string(ciphertext), secret) {
			t.Fatalf("ciphertext contains %q", secret)
		}
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatal(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatal(err)
	}
	plain, err := gcm.Open(nil, ciphertext[:gcm.NonceSize()], ciphertext[gcm.NonceSize():], stateAAD)
	if err != nil {
		t.Fatalf("state authentication failed: %v", err)
	}
	defer clear(plain)
	var envelope map[string]any
	if err := json.Unmarshal(plain, &envelope); err != nil || envelope["schema_version"] != float64(schemaVersion) {
		t.Fatalf("schema envelope invalid: %v", err)
	}
	v, err = Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer v.Close()
	if v.State().HostID != initial.HostID {
		t.Fatal("host ID changed on reopen")
	}
}

func TestTwoAccountSelectionAndPendingLogoutSurviveRestart(t *testing.T) {
	v, dir := openTestVault(t)
	state := v.State()
	first := account("account-1")
	first.PendingOperation = "logout"
	second := account("account-2")
	state.Accounts = []Account{first, second}
	state.Active = "account-2"
	if err := v.Save(state); err != nil {
		t.Fatal(err)
	}
	if err := v.Close(); err != nil {
		t.Fatal(err)
	}
	v, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer v.Close()
	restored := v.State()
	if restored.HostID != state.HostID || restored.Active != "account-2" || len(restored.Accounts) != 2 {
		t.Fatalf("unexpected restored selection: %#v", restored)
	}
	if restored.Accounts[0].PendingOperation != "logout" || restored.Accounts[0].RefreshToken != first.RefreshToken || restored.Accounts[1].Label != "account-2" {
		t.Fatal("pending logout or account tokens were not retained")
	}
}

func TestStateDeepCopyAndInvalidSaveKeepsPriorState(t *testing.T) {
	v, _ := openTestVault(t)
	defer v.Close()
	state := v.State()
	state.Accounts = []Account{account("account-1")}
	state.Active = "account-1"
	if err := v.Save(state); err != nil {
		t.Fatal(err)
	}
	copyState := v.State()
	copyState.Accounts[0].Scopes[0] = "mutated"
	if v.State().Accounts[0].Scopes[0] != "openid" {
		t.Fatal("State exposed mutable internal scopes")
	}
	before := v.State()
	bad := cloneState(before)
	bad.Accounts[0].ClientID = "dynamic_agent_client"
	if err := v.Save(bad); err == nil || strings.Contains(err.Error(), "dynamic_agent_client") {
		t.Fatalf("invalid Save error was not fixed/redacted: %v", err)
	}
	if got := v.State(); got.Accounts[0].ClientID != before.Accounts[0].ClientID {
		t.Fatal("failed validation changed in-memory state")
	}
}

func TestFailedAtomicSavePreservesMemoryAndPriorFile(t *testing.T) {
	v, dir := openTestVault(t)
	state := v.State()
	state.Accounts = []Account{account("account-1")}
	state.Active = "account-1"
	if err := v.Save(state); err != nil {
		t.Fatal(err)
	}
	before := v.State()
	state.Active = ""
	state.Accounts[0].AccessToken = "new-access-secret"
	path := filepath.Join(dir, stateName)
	backup := filepath.Join(dir, "state.backup")
	if err := os.Rename(path, backup); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(backup, path); err != nil {
		t.Fatal(err)
	}
	if err := v.Save(state); err == nil || strings.Contains(err.Error(), "new-access-secret") {
		t.Fatalf("Save accepted symlink target or leaked secret: %v", err)
	}
	if got := v.State(); got.Active != before.Active || got.Accounts[0].AccessToken != before.Accounts[0].AccessToken {
		t.Fatal("failed write changed in-memory state")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(backup, path); err != nil {
		t.Fatal(err)
	}
	if err := v.Close(); err != nil {
		t.Fatal(err)
	}
	v, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer v.Close()
	if got := v.State(); got.Active != before.Active || got.Accounts[0].AccessToken != before.Accounts[0].AccessToken {
		t.Fatal("failed atomic save damaged prior persisted state")
	}
}

func TestOpenLockSameAndSeparateProcess(t *testing.T) {
	v, dir := openTestVault(t)
	defer v.Close()
	if _, err := Open(dir); err == nil {
		t.Fatal("same-process second opener acquired lock")
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestLockChild$")
	cmd.Env = append(os.Environ(), "ACCOUNT_VAULT_LOCK_CHILD="+dir)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("lock subprocess: %v: %s", err, out)
	}
}

func TestLockChild(t *testing.T) {
	dir := os.Getenv("ACCOUNT_VAULT_LOCK_CHILD")
	if dir == "" {
		return
	}
	v, err := Open(dir)
	if err == nil {
		_ = v.Close()
		t.Fatal("separate process acquired existing vault lock")
	}
}

func TestTamperingMalformedJSONAndMissingKeyRejectWithoutRepair(t *testing.T) {
	t.Run("ciphertext tamper", func(t *testing.T) {
		v, dir := openTestVault(t)
		if err := v.Close(); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(dir, stateName)
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		data[len(data)-1] ^= 1
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
		if reopened, err := Open(dir); err == nil {
			reopened.Close()
			t.Fatal("tampered state was accepted")
		}
	})
	for name, payload := range map[string]string{
		"duplicate field":       `{"schema_version":1,"schema_version":1,"host_id":"%s","active":"","accounts":[]}`,
		"case alias":            `{"schema_version":1,"host_id":"%s","active":"","accounts":[],"Host_ID":"alias"}`,
		"trailing value":        `{"schema_version":1,"host_id":"%s","active":"","accounts":[]} {}`,
		"duplicate account key": `{"schema_version":1,"host_id":"%s","active":"","accounts":[{"label":"account-1","label":"account-2","client_id":"c","subject":"s","id_token":"i","access_token":"a","refresh_token":"r","expires_at":"2026-10-12T00:00:00Z","scopes":[],"needs_sign_in":false,"revocation_unconfirmed":false,"pending_operation":""}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			v, dir := openTestVault(t)
			hostID := v.State().HostID
			if err := v.Close(); err != nil {
				t.Fatal(err)
			}
			writeEncryptedForTest(t, dir, fmt.Sprintf(payload, hostID))
			if reopened, err := Open(dir); err == nil {
				reopened.Close()
				t.Fatal("malformed JSON accepted")
			}
		})
	}
	t.Run("missing key not regenerated", func(t *testing.T) {
		v, dir := openTestVault(t)
		if err := v.Close(); err != nil {
			t.Fatal(err)
		}
		keyPath := filepath.Join(dir, keyName)
		if err := os.Remove(keyPath); err != nil {
			t.Fatal(err)
		}
		if reopened, err := Open(dir); err == nil {
			reopened.Close()
			t.Fatal("state without key was repaired")
		}
		if _, err := os.Stat(keyPath); !os.IsNotExist(err) {
			t.Fatal("missing key was regenerated")
		}
	})
}

func writeEncryptedForTest(t *testing.T, dir, plain string) {
	t.Helper()
	key, err := os.ReadFile(filepath.Join(dir, keyName))
	if err != nil {
		t.Fatal(err)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatal(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatal(err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		t.Fatal(err)
	}
	encoded := gcm.Seal(nonce, nonce, []byte(plain), stateAAD)
	if err := os.WriteFile(filepath.Join(dir, stateName), encoded, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestRejectsSymlinkAndInsecureDirectoriesOrFiles(t *testing.T) {
	t.Run("symlink directory", func(t *testing.T) {
		real := testDir(t)
		if err := os.Mkdir(real, 0700); err != nil {
			t.Fatal(err)
		}
		link := filepath.Join(t.TempDir(), "link")
		if err := os.Symlink(real, link); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(link); err == nil {
			t.Fatal("symlink directory accepted")
		}
	})
	t.Run("insecure directory mode", func(t *testing.T) {
		v, dir := openTestVault(t)
		if err := v.Close(); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(dir, 0755); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(dir); err == nil {
			t.Fatal("insecure directory accepted")
		}
	})
	t.Run("symlink state file", func(t *testing.T) {
		v, dir := openTestVault(t)
		if err := v.Close(); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(dir, stateName)
		backup := filepath.Join(dir, "state.backup")
		if err := os.Rename(path, backup); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(backup, path); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(dir); err == nil {
			t.Fatal("symlink state accepted")
		}
	})
}

func TestValidationRedactionAndJSONSecrecy(t *testing.T) {
	v, _ := openTestVault(t)
	defer v.Close()
	a := account("account-1")
	s := v.State()
	s.Accounts = []Account{a}
	s.Active = a.Label
	formatted := fmt.Sprintf("%v %+v %#v %v %+v %#v", a, &a, a, s, &s, s)
	encodedA, err := json.Marshal(a)
	if err != nil {
		t.Fatal(err)
	}
	encodedS, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	for _, output := range []string{formatted, string(encodedA), string(encodedS)} {
		for _, secret := range []string{a.ClientID, a.Subject, a.IDToken, a.AccessToken, a.RefreshToken} {
			if strings.Contains(output, secret) {
				t.Fatalf("credential leaked through formatter/JSON: %s", output)
			}
		}
	}
	cases := []State{
		{HostID: s.HostID, Accounts: []Account{a, a}},
		{HostID: s.HostID, Active: "account-1", Accounts: []Account{func() Account { x := a; x.PendingOperation = "logout"; return x }()}},
		{HostID: s.HostID, Accounts: []Account{func() Account { x := a; x.Scopes = []string{"openid", "openid"}; return x }()}},
		{HostID: "urn:uuid:00000000-0000-4000-8000-000000000000", Accounts: []Account{func() Account { x := a; x.ClientID = "dynamic_agent_client"; return x }()}},
	}
	for _, invalid := range cases {
		if err := v.Save(invalid); err == nil {
			t.Fatal("illegal state accepted")
		}
	}
	invalidHost := s
	invalidHost.HostID = "urn:uuid:00000000-0000-4000-8000-000000000000"
	if err := v.Save(invalidHost); err == nil {
		t.Fatal("host ID replacement accepted")
	}
	needsSignIn := Account{Label: "account-1", ClientID: "issued-client", Subject: "saved-subject", NeedsSignIn: true}
	s.Accounts = []Account{needsSignIn}
	s.Active = ""
	if err := v.Save(s); err != nil {
		t.Fatalf("empty needs-sign-in record rejected: %v", err)
	}
	needsSignIn.AccessToken = "retained"
	s.Accounts = []Account{needsSignIn}
	if err := v.Save(s); err == nil {
		t.Fatal("needs-sign-in record retained credential")
	}
}

func TestSavedSecretsEncryptedAndSignedOutMappingReopens(t *testing.T) {
	v, dir := openTestVault(t)
	state := v.State()
	a := account("account-1")
	state.Accounts = []Account{a}
	if e := v.Save(state); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(dir, stateName))
	if e != nil {
		t.Fatal(e)
	}
	for _, secret := range []string{a.IDToken, a.AccessToken, a.RefreshToken, a.ClientID, a.Subject} {
		if strings.Contains(string(b), secret) {
			t.Fatal("encrypted file contains plaintext")
		}
	}
	state.Accounts[0].IDToken = ""
	state.Accounts[0].AccessToken = ""
	state.Accounts[0].RefreshToken = ""
	state.Accounts[0].ExpiresAt = time.Time{}
	state.Accounts[0].Scopes = nil
	state.Accounts[0].NeedsSignIn = true
	if e = v.Save(state); e != nil {
		t.Fatal(e)
	}
	v.Close()
	v, e = Open(dir)
	if e != nil {
		t.Fatal(e)
	}
	defer v.Close()
	if got := v.State().Accounts[0]; got.ClientID != a.ClientID || got.Subject != a.Subject || !got.NeedsSignIn || got.AccessToken != "" {
		t.Fatal("signed-out registration lost")
	}
}
func TestHardlinkedKeyAndTruncatedStateRejected(t *testing.T) {
	for _, mode := range []string{"hardlink", "truncated"} {
		t.Run(mode, func(t *testing.T) {
			v, dir := openTestVault(t)
			v.Close()
			if mode == "hardlink" {
				if e := os.Link(filepath.Join(dir, keyName), filepath.Join(dir, "linked-key")); e != nil {
					t.Fatal(e)
				}
			} else {
				if e := os.WriteFile(filepath.Join(dir, stateName), []byte("short"), 0600); e != nil {
					t.Fatal(e)
				}
			}
			if opened, e := Open(dir); e == nil {
				opened.Close()
				t.Fatal("invalid private file accepted")
			}
		})
	}
}

func TestDurabilityFailureBeforeAndAfterRename(t *testing.T) {
	for _, stage := range []string{"before", "after"} {
		t.Run(stage, func(t *testing.T) {
			v, dir := openTestVault(t)
			oldBytes, _ := os.ReadFile(filepath.Join(dir, stateName))
			old := v.State()
			next := v.State()
			next.Accounts = []Account{account("account-1")}
			next.Active = "account-1"
			if stage == "before" {
				v.syncFile = func(int) error { return unix.EIO }
			} else {
				v.syncDir = func(int) error { return unix.EIO }
			}
			if e := v.Save(next); e == nil {
				t.Fatal("sync failure reported success")
			}
			if len(v.State().Accounts) != len(old.Accounts) {
				t.Fatal("failed save published memory")
			}
			if e := v.Save(next); e == nil {
				t.Fatal("uncertain vault remained writable")
			}
			newBytes, _ := os.ReadFile(filepath.Join(dir, stateName))
			if stage == "before" && !bytes.Equal(newBytes, oldBytes) {
				t.Fatal("pre-rename failure changed disk")
			}
			v.Close()
			v, e := Open(dir)
			if e != nil {
				t.Fatal(e)
			}
			defer v.Close()
			if stage == "before" && len(v.State().Accounts) != 0 {
				t.Fatal("pre-rename failure restored candidate")
			}
			if stage == "after" && v.State().Active != "account-1" {
				t.Fatal("post-rename outcome not recoverable")
			}
		})
	}
}
func TestMissingStatePreservesHostAndInsecureFilesFail(t *testing.T) {
	for _, file := range []string{keyName, lockName, stateName} {
		t.Run(file, func(t *testing.T) {
			v, dir := openTestVault(t)
			v.Close()
			if e := os.Chmod(filepath.Join(dir, file), 0644); e != nil {
				t.Fatal(e)
			}
			if opened, e := Open(dir); e == nil {
				opened.Close()
				t.Fatal("readable private file accepted")
			}
		})
	}
	v, dir := openTestVault(t)
	v.Close()
	keyBefore, _ := os.ReadFile(filepath.Join(dir, keyName))
	os.Remove(filepath.Join(dir, stateName))
	if opened, e := Open(dir); e == nil {
		opened.Close()
		t.Fatal("lost state silently generated host")
	}
	keyAfter, _ := os.ReadFile(filepath.Join(dir, keyName))
	if !bytes.Equal(keyBefore, keyAfter) {
		t.Fatal("key changed")
	}
}
func TestAllPointerAndValueFormatsRedacted(t *testing.T) {
	a := account("account-1")
	s := State{Accounts: []Account{a}}
	for _, value := range []any{a, &a, s, &s} {
		for _, format := range []string{"%v", "%+v", "%#v"} {
			if strings.Contains(fmt.Sprintf(format, value), "secret") {
				t.Fatal("format leak")
			}
		}
	}
}
