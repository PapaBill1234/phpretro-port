// SPDX-License-Identifier: GPL-3.0-only
package accountgateway

import (
	"errors"
	"testing"
)

func TestCommitFailureDoesNotPublishProfile(t *testing.T) {
	f := setup(t)
	f.g.cfg.Commit = func(Profile, RawToken) error { return errors.New("private-secret") }
	p := start(t, f, "one")
	if _, e := finish(f, "one", p, "issued-one"); e != ErrUnavailable {
		t.Fatal(e)
	}
	if len(f.g.Profiles()) != 0 {
		t.Fatal("failed durable commit published profile")
	}
}
func TestCancelFencesLateCallbackBeforeCommit(t *testing.T) {
	f := setup(t)
	entered, release := make(chan struct{}), make(chan struct{})
	f.x.entered = entered
	f.x.release = release
	commits := 0
	f.g.cfg.Commit = func(Profile, RawToken) error { commits++; return nil }
	p := start(t, f, "one")
	done := make(chan error, 1)
	go func() { _, e := finish(f, "one", p, "issued-one"); done <- e }()
	waitExchange(t, entered, release)
	f.g.Cancel("one")
	close(release)
	if e := <-done; e != ErrInvalid {
		t.Fatal(e)
	}
	if commits != 0 || len(f.g.Profiles()) != 0 {
		t.Fatal("cancelled callback restored profile")
	}
}
func TestRestoreRegistrationAndReturnedSubjectBinding(t *testing.T) {
	f := setup(t)
	if e := f.g.RestoreProfiles([]Profile{{Label: "one", HostID: "stable-host", ClientID: "issued-one", Identity: "original"}}); e != nil {
		t.Fatal(e)
	}
	p := start(t, f, "one")
	if _, e := finish(f, "one", p, "issued-one"); e != ErrDenied {
		t.Fatal("wrong returning subject", e)
	}
	if f.g.Profiles()[0].Identity != "original" {
		t.Fatal("replaced saved subject")
	}
}
