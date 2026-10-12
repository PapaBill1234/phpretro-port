// Package accountvault provides a small, process-locked encrypted account vault.
package accountvault

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const (
	keyName       = "vault.key"
	lockName      = "vault.lock"
	stateName     = "state.bin"
	maxStateBytes = 1 << 20
	schemaVersion = 1
)

var (
	errOpen    = errors.New("account vault: open failed")
	errSave    = errors.New("account vault: save failed")
	errClosed  = errors.New("account vault: closed")
	errInvalid = errors.New("account vault: invalid state")
	errLock    = errors.New("account vault: already open")
	stateAAD   = []byte("PHPRetro account vault state envelope v1")
)

// Account deliberately excludes credential material from all default JSON output.
type Account struct {
	Label                 string    `json:"-"`
	ClientID              string    `json:"-"`
	Subject               string    `json:"-"`
	IDToken               string    `json:"-"`
	AccessToken           string    `json:"-"`
	RefreshToken          string    `json:"-"`
	ExpiresAt             time.Time `json:"-"`
	Scopes                []string  `json:"-"`
	NeedsSignIn           bool      `json:"-"`
	RevocationUnconfirmed bool      `json:"-"`
	PendingOperation      string    `json:"-"`
}

// State contains the stable host identifier and the currently selected account.
type State struct {
	HostID   string    `json:"-"`
	Active   string    `json:"-"`
	Accounts []Account `json:"-"`
}

func (Account) String() string   { return "Account{redacted}" }
func (Account) GoString() string { return "Account{redacted}" }
func (State) String() string     { return "State{redacted}" }
func (State) GoString() string   { return "State{redacted}" }

type Vault struct {
	mu                sync.Mutex
	dirFD             int
	lockFD            int
	key               [32]byte
	state             State
	closed            bool
	failed            bool
	syncFile, syncDir func(int) error
}

type diskState struct {
	SchemaVersion int           `json:"schema_version"`
	HostID        string        `json:"host_id"`
	Active        string        `json:"active"`
	Accounts      []diskAccount `json:"accounts"`
}

type diskAccount struct {
	Label                 string    `json:"label"`
	ClientID              string    `json:"client_id"`
	Subject               string    `json:"subject"`
	IDToken               string    `json:"id_token"`
	AccessToken           string    `json:"access_token"`
	RefreshToken          string    `json:"refresh_token"`
	ExpiresAt             time.Time `json:"expires_at"`
	Scopes                []string  `json:"scopes"`
	NeedsSignIn           bool      `json:"needs_sign_in"`
	RevocationUnconfirmed bool      `json:"revocation_unconfirmed"`
	PendingOperation      string    `json:"pending_operation"`
}

func Open(dir string) (*Vault, error) {
	if !filepath.IsAbs(dir) || dir == "/" || dir != filepath.Clean(dir) {
		return nil, errOpen
	}
	dirFD, err := openPrivateDir(filepath.Clean(dir))
	if err != nil {
		return nil, errOpen
	}
	v := &Vault{dirFD: dirFD, lockFD: -1, syncFile: unix.Fsync, syncDir: unix.Fsync}
	fail := func(e error) (*Vault, error) {
		if v.lockFD >= 0 {
			_ = unix.Close(v.lockFD)
		}
		_ = unix.Close(v.dirFD)
		clear(v.key[:])
		if errors.Is(e, errLock) {
			return nil, errLock
		}
		return nil, errOpen
	}

	lockFD, err := openSecureFile(dirFD, lockName, unix.O_RDWR|unix.O_CREAT, true)
	if err != nil {
		return fail(err)
	}
	v.lockFD = lockFD
	if err = unix.Flock(lockFD, unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return fail(errLock)
	}

	stateExists, err := fileExistsSecure(dirFD, stateName)
	if err != nil {
		return fail(err)
	}
	if stateExists {
		exists, e := fileExistsSecure(dirFD, keyName)
		if e != nil || !exists {
			return fail(errOpen)
		}
	}
	keyFD, keyCreated, err := openKey(dirFD)
	if err != nil {
		return fail(err)
	}
	if keyCreated && stateExists || !keyCreated && !stateExists {
		_ = unix.Close(keyFD)
		return fail(errOpen)
	}
	keyBytes, err := readExactKey(keyFD)
	_ = unix.Close(keyFD)
	if err != nil {
		return fail(err)
	}
	copy(v.key[:], keyBytes)
	clear(keyBytes)

	if stateExists {
		encoded, readErr := readSecureFile(dirFD, stateName, maxStateBytes+64)
		if readErr != nil {
			return fail(readErr)
		}
		state, decodeErr := v.decryptState(encoded)
		clear(encoded)
		if decodeErr != nil {
			return fail(decodeErr)
		}
		if validateState(state) != nil {
			return fail(errOpen)
		}
		v.state = cloneState(state)
		return v, nil
	}

	hostID, err := newHostID()
	if err != nil {
		return fail(err)
	}
	v.state = State{HostID: hostID, Accounts: []Account{}}
	if err := v.writeState(v.state); err != nil {
		return fail(err)
	}
	return v, nil
}

func (v *Vault) State() State {
	v.mu.Lock()
	defer v.mu.Unlock()
	return cloneState(v.state)
}

func (v *Vault) Save(next State) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.closed || v.failed {
		return errClosed
	}
	if next.HostID != v.state.HostID || validateState(next) != nil {
		return errInvalid
	}
	candidate := cloneState(next)
	if err := v.writeState(candidate); err != nil {
		v.failed = true
		return errSave
	}
	v.state = candidate
	return nil
}

func (v *Vault) Close() error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.closed {
		return nil
	}
	v.closed = true
	v.state = State{}
	clear(v.key[:])
	unlockErr := unix.Flock(v.lockFD, unix.LOCK_UN)
	closeLockErr := unix.Close(v.lockFD)
	closeDirErr := unix.Close(v.dirFD)
	v.lockFD, v.dirFD = -1, -1
	if unlockErr != nil || closeLockErr != nil || closeDirErr != nil {
		return errOpen
	}
	return nil
}

func (v *Vault) writeState(state State) error {
	if _, err := fileExistsSecure(v.dirFD, stateName); err != nil {
		return err
	}
	plain, err := json.Marshal(toDiskState(state))
	if err != nil || len(plain) > maxStateBytes {
		return errSave
	}
	block, err := aes.NewCipher(v.key[:])
	if err != nil {
		return errSave
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return errSave
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return errSave
	}
	sealed := gcm.Seal(nonce, nonce, plain, stateAAD)
	clear(plain)
	defer clear(sealed)
	if err := atomicReplace(v.dirFD, stateName, sealed, v.syncFile, v.syncDir); err != nil {
		return err
	}
	return nil
}

func (v *Vault) decryptState(encoded []byte) (State, error) {
	block, err := aes.NewCipher(v.key[:])
	if err != nil {
		return State{}, errOpen
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil || len(encoded) < gcm.NonceSize()+gcm.Overhead() || len(encoded) > maxStateBytes+gcm.NonceSize()+gcm.Overhead() {
		return State{}, errOpen
	}
	plain, err := gcm.Open(nil, encoded[:gcm.NonceSize()], encoded[gcm.NonceSize():], stateAAD)
	if err != nil || len(plain) > maxStateBytes {
		clear(plain)
		return State{}, errOpen
	}
	defer clear(plain)
	state, err := decodeDiskState(plain)
	if err != nil {
		return State{}, errOpen
	}
	return state, nil
}

func validateState(state State) error {
	if !validHostID(state.HostID) || len(state.Accounts) > 2 {
		return errInvalid
	}
	labels := make(map[string]bool, 2)
	usable := make(map[string]bool, 2)
	for _, account := range state.Accounts {
		if account.Label != "account-1" && account.Label != "account-2" || labels[account.Label] {
			return errInvalid
		}
		labels[account.Label] = true
		if account.ClientID == "" || account.Subject == "" || account.ClientID == "dynamic_agent_client" || len(account.ClientID) > 256 || len(account.Subject) > 256 || len(account.IDToken) > 16384 || len(account.AccessToken) > 16384 || len(account.RefreshToken) > 16384 {
			return errInvalid
		}
		if account.PendingOperation != "" && account.PendingOperation != "logout" {
			return errInvalid
		}
		if account.NeedsSignIn {
			if account.IDToken != "" || account.AccessToken != "" || account.RefreshToken != "" {
				return errInvalid
			}
		} else if account.ClientID == "" || account.Subject == "" || account.IDToken == "" || account.AccessToken == "" || account.RefreshToken == "" || account.ExpiresAt.IsZero() {
			return errInvalid
		}
		if account.PendingOperation == "logout" && state.Active == account.Label {
			return errInvalid
		}
		if !account.NeedsSignIn && account.PendingOperation == "" {
			usable[account.Label] = true
		}
		seenScopes := make(map[string]bool, len(account.Scopes))
		for _, scope := range account.Scopes {
			if !validScope(scope) || seenScopes[scope] {
				return errInvalid
			}
			seenScopes[scope] = true
		}
	}
	if state.Active != "" && !usable[state.Active] {
		return errInvalid
	}
	return nil
}

func validScope(scope string) bool {
	if scope == "" {
		return false
	}
	for i := 0; i < len(scope); i++ {
		c := scope[i]
		if c < 0x21 || c > 0x7e || c == '"' || c == '\\' {
			return false
		}
	}
	return true
}

func validHostID(id string) bool {
	const prefix = "urn:uuid:"
	if !strings.HasPrefix(id, prefix) || len(id) != len(prefix)+36 {
		return false
	}
	value := id[len(prefix):]
	if value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' || value[14] != '4' {
		return false
	}
	if value[19] != '8' && value[19] != '9' && value[19] != 'a' && value[19] != 'b' {
		return false
	}
	compact := strings.ReplaceAll(value, "-", "")
	decoded, err := hex.DecodeString(compact)
	return err == nil && len(decoded) == 16
}

func newHostID() (string, error) {
	var id [16]byte
	if _, err := io.ReadFull(rand.Reader, id[:]); err != nil {
		return "", errOpen
	}
	id[6] = (id[6] & 0x0f) | 0x40
	id[8] = (id[8] & 0x3f) | 0x80
	return fmt.Sprintf("urn:uuid:%08x-%04x-%04x-%04x-%012x", id[0:4], id[4:6], id[6:8], id[8:10], id[10:16]), nil
}

func cloneState(in State) State {
	out := State{HostID: in.HostID, Active: in.Active, Accounts: make([]Account, len(in.Accounts))}
	for i, a := range in.Accounts {
		out.Accounts[i] = a
		out.Accounts[i].Scopes = append([]string(nil), a.Scopes...)
	}
	return out
}

func toDiskState(state State) diskState {
	d := diskState{SchemaVersion: schemaVersion, HostID: state.HostID, Active: state.Active, Accounts: make([]diskAccount, len(state.Accounts))}
	for i, a := range state.Accounts {
		d.Accounts[i] = diskAccount{Label: a.Label, ClientID: a.ClientID, Subject: a.Subject, IDToken: a.IDToken, AccessToken: a.AccessToken, RefreshToken: a.RefreshToken, ExpiresAt: a.ExpiresAt, Scopes: append([]string{}, a.Scopes...), NeedsSignIn: a.NeedsSignIn, RevocationUnconfirmed: a.RevocationUnconfirmed, PendingOperation: a.PendingOperation}
	}
	return d
}

func fromDiskState(d diskState) State {
	s := State{HostID: d.HostID, Active: d.Active, Accounts: make([]Account, len(d.Accounts))}
	for i, a := range d.Accounts {
		s.Accounts[i] = Account{Label: a.Label, ClientID: a.ClientID, Subject: a.Subject, IDToken: a.IDToken, AccessToken: a.AccessToken, RefreshToken: a.RefreshToken, ExpiresAt: a.ExpiresAt, Scopes: append([]string{}, a.Scopes...), NeedsSignIn: a.NeedsSignIn, RevocationUnconfirmed: a.RevocationUnconfirmed, PendingOperation: a.PendingOperation}
	}
	return s
}

// decodeDiskState rejects duplicate keys, case aliases, unknown keys and trailing values.
func decodeDiskState(data []byte) (State, error) {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	value, err := readJSONValue(dec)
	if err != nil || ensureJSONEOF(dec) != nil {
		return State{}, errOpen
	}
	if err := requireObjectKeys(value, map[string]bool{"schema_version": true, "host_id": true, "active": true, "accounts": true}); err != nil {
		return State{}, errOpen
	}
	root := value.(map[string]any)
	for _, required := range []string{"schema_version", "host_id", "active", "accounts"} {
		if _, ok := root[required]; !ok {
			return State{}, errOpen
		}
	}
	if _, ok := root["schema_version"].(json.Number); !ok {
		return State{}, errOpen
	}
	if _, ok := root["host_id"].(string); !ok {
		return State{}, errOpen
	}
	if _, ok := root["active"].(string); !ok {
		return State{}, errOpen
	}
	accounts, ok := root["accounts"].([]any)
	if !ok {
		return State{}, errOpen
	}
	accountKeys := map[string]bool{"label": true, "client_id": true, "subject": true, "id_token": true, "access_token": true, "refresh_token": true, "expires_at": true, "scopes": true, "needs_sign_in": true, "revocation_unconfirmed": true, "pending_operation": true}
	for _, item := range accounts {
		if err := requireObjectKeys(item, accountKeys); err != nil {
			return State{}, errOpen
		}
		obj := item.(map[string]any)
		if len(obj) != len(accountKeys) {
			return State{}, errOpen
		}
		for _, key := range []string{"label", "client_id", "subject", "id_token", "access_token", "refresh_token", "expires_at", "pending_operation"} {
			if _, ok := obj[key].(string); !ok {
				return State{}, errOpen
			}
		}
		for _, key := range []string{"needs_sign_in", "revocation_unconfirmed"} {
			if _, ok := obj[key].(bool); !ok {
				return State{}, errOpen
			}
		}
		scopes, ok := obj["scopes"].([]any)
		if !ok {
			return State{}, errOpen
		}
		for _, scope := range scopes {
			if _, ok := scope.(string); !ok {
				return State{}, errOpen
			}
		}
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return State{}, errOpen
	}
	var d diskState
	dis := json.NewDecoder(strings.NewReader(string(canonical)))
	dis.DisallowUnknownFields()
	if err := dis.Decode(&d); err != nil || d.SchemaVersion != schemaVersion {
		return State{}, errOpen
	}
	if err := ensureJSONEOF(dis); err != nil {
		return State{}, errOpen
	}
	return fromDiskState(d), nil
}

func requireObjectKeys(value any, allowed map[string]bool) error {
	obj, ok := value.(map[string]any)
	if !ok {
		return errOpen
	}
	for key := range obj {
		if !allowed[key] {
			return errOpen
		}
	}
	return nil
}

func readJSONValue(dec *json.Decoder) (any, error) { return readJSONValueDepth(dec, 0) }
func readJSONValueDepth(dec *json.Decoder, depth int) (any, error) {
	if depth > 64 {
		return nil, errOpen
	}
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, isDelim := tok.(json.Delim)
	if !isDelim {
		return tok, nil
	}
	switch delim {
	case '{':
		obj := make(map[string]any)
		for dec.More() {
			keyToken, err := dec.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, errOpen
			}
			if _, exists := obj[key]; exists {
				return nil, errOpen
			}
			value, err := readJSONValueDepth(dec, depth+1)
			if err != nil {
				return nil, err
			}
			obj[key] = value
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim('}') {
			return nil, errOpen
		}
		return obj, nil
	case '[':
		list := []any{}
		for dec.More() {
			value, err := readJSONValueDepth(dec, depth+1)
			if err != nil {
				return nil, err
			}
			list = append(list, value)
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim(']') {
			return nil, errOpen
		}
		return list, nil
	default:
		return nil, errOpen
	}
}

func ensureJSONEOF(dec *json.Decoder) error {
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return errOpen
	}
	return nil
}

func openPrivateDir(path string) (int, error) {
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	if len(parts) == 0 || parts[len(parts)-1] == "" {
		return -1, errOpen
	}
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	for i, part := range parts {
		if part == "" || part == "." || part == ".." {
			_ = unix.Close(fd)
			return -1, errOpen
		}
		next, openErr := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if openErr == unix.ENOENT && i == len(parts)-1 {
			if mkdirErr := unix.Mkdirat(fd, part, 0700); mkdirErr != nil {
				_ = unix.Close(fd)
				return -1, mkdirErr
			}
			next, openErr = unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		}
		_ = unix.Close(fd)
		if openErr != nil {
			return -1, openErr
		}
		fd = next
		var stat unix.Stat_t
		if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR || stat.Uid != 0 && stat.Uid != uint32(unix.Getuid()) {
			_ = unix.Close(fd)
			return -1, errOpen
		}
		if i == len(parts)-1 && (stat.Uid != uint32(unix.Getuid()) || stat.Mode&07777 != 0700) {
			_ = unix.Close(fd)
			return -1, errOpen
		}
	}
	return fd, nil
}

func openSecureFile(dirFD int, name string, flags int, create bool) (int, error) {
	openFlags := flags | unix.O_NOFOLLOW | unix.O_CLOEXEC | unix.O_NONBLOCK
	if create {
		openFlags |= unix.O_CREAT
	}
	fd, err := unix.Openat(dirFD, name, openFlags, 0600)
	if err != nil {
		return -1, err
	}
	if err := verifyFileFD(fd); err != nil {
		_ = unix.Close(fd)
		return -1, err
	}
	return fd, nil
}

func verifyFileFD(fd int) error {
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Uid != uint32(unix.Getuid()) || stat.Nlink != 1 || stat.Mode&07777 != 0600 {
		return errOpen
	}
	return nil
}

func fileExistsSecure(dirFD int, name string) (bool, error) {
	fd, err := unix.Openat(dirFD, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err == unix.ENOENT {
		return false, nil
	}
	if err != nil {
		return false, errOpen
	}
	defer unix.Close(fd)
	if err := verifyFileFD(fd); err != nil {
		return false, err
	}
	return true, nil
}

func openKey(dirFD int) (int, bool, error) {
	fd, err := unix.Openat(dirFD, keyName, unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_CREAT|unix.O_EXCL, 0600)
	if err == nil {
		key := make([]byte, 32)
		if _, readErr := io.ReadFull(rand.Reader, key); readErr != nil {
			clear(key)
			_ = unix.Close(fd)
			return -1, false, errOpen
		}
		if writeErr := writeFD(fd, key); writeErr != nil || unix.Fsync(fd) != nil || unix.Fsync(dirFD) != nil {
			clear(key)
			_ = unix.Close(fd)
			return -1, false, errOpen
		}
		clear(key)
		return fd, true, nil
	}
	if err != unix.EEXIST {
		return -1, false, errOpen
	}
	fd, err = unix.Openat(dirFD, keyName, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err != nil || verifyFileFD(fd) != nil {
		if fd >= 0 {
			_ = unix.Close(fd)
		}
		return -1, false, errOpen
	}
	return fd, false, nil
}

func readExactKey(fd int) ([]byte, error) {
	if err := verifyFileFD(fd); err != nil {
		return nil, errOpen
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Size != 32 {
		return nil, errOpen
	}
	if _, err := unix.Seek(fd, 0, io.SeekStart); err != nil {
		return nil, errOpen
	}
	key := make([]byte, 32)
	if _, err := io.ReadFull(unixFile{fd}, key); err != nil {
		clear(key)
		return nil, errOpen
	}
	return key, nil
}

type unixFile struct{ fd int }

func (f unixFile) Read(p []byte) (int, error) {
	n, e := unix.Read(f.fd, p)
	if n == 0 && e == nil {
		return 0, io.EOF
	}
	return n, e
}

func writeFD(fd int, data []byte) error {
	for len(data) > 0 {
		n, err := unix.Write(fd, data)
		if err == unix.EINTR {
			continue
		}
		if err != nil || n == 0 {
			return errOpen
		}
		data = data[n:]
	}
	return nil
}

func readSecureFile(dirFD int, name string, max int64) ([]byte, error) {
	fd, err := unix.Openat(dirFD, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, errOpen
	}
	defer unix.Close(fd)
	if err := verifyFileFD(fd); err != nil {
		return nil, errOpen
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Size < 0 || stat.Size > max {
		return nil, errOpen
	}
	data := make([]byte, stat.Size)
	if _, err := io.ReadFull(unixFile{fd}, data); err != nil {
		clear(data)
		return nil, errOpen
	}
	var extra [1]byte
	n, err := unix.Read(fd, extra[:])
	if n != 0 || err != nil && err != io.EOF {
		clear(data)
		return nil, errOpen
	}
	return data, nil
}

func atomicReplace(dirFD int, target string, data []byte, syncFile, syncDir func(int) error) error {
	var randomName [12]byte
	if _, err := io.ReadFull(rand.Reader, randomName[:]); err != nil {
		return errSave
	}
	temp := ".vault-tmp-" + hex.EncodeToString(randomName[:])
	fd, err := unix.Openat(dirFD, temp, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if err != nil {
		return errSave
	}
	ok := false
	defer func() {
		_ = unix.Close(fd)
		if !ok {
			_ = unix.Unlinkat(dirFD, temp, 0)
		}
	}()
	if verifyFileFD(fd) != nil || writeFD(fd, data) != nil || syncFile(fd) != nil {
		return errSave
	}
	if _, err := fileExistsSecure(dirFD, target); err != nil {
		return errSave
	}
	if err := unix.Renameat(dirFD, temp, dirFD, target); err != nil {
		return errSave
	}
	if err := syncDir(dirFD); err != nil {
		return errSave
	}
	ok = true
	return nil
}
