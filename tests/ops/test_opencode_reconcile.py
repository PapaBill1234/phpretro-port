import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("opencode_reconcile", ROOT / "scripts/opencode-reconcile.py")
reconcile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile)
LEDGER_SPEC = importlib.util.spec_from_file_location("opencode_ledger", ROOT / "scripts/opencode-ledger.py")
ledger = importlib.util.module_from_spec(LEDGER_SPEC)
LEDGER_SPEC.loader.exec_module(ledger)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.main_repo = self.root / "main-repo"
        self.main_repo.mkdir()
        subprocess.run(["git", "-C", str(self.main_repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.main_repo), "config", "user.name", "Synthetic"], check=True)
        subprocess.run(["git", "-C", str(self.main_repo), "config", "user.email", "synthetic@example.invalid"], check=True)
        (self.main_repo / "owned.txt").write_text("original", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.main_repo), "add", "owned.txt"], check=True)
        subprocess.run(["git", "-C", str(self.main_repo), "commit", "-qm", "fixture"], check=True)
        self.worktree = self.root / "linked-worktree"
        subprocess.run(["git", "-C", str(self.main_repo), "worktree", "add", "-q", "-b", "fixture-worker",
                        str(self.worktree), "HEAD"], check=True)
        self.task = "synthetic-task"
        self.claim = ledger.claim(self.state, task_id=self.task, profile="test", branch="build/test",
                                  worktree=self.worktree, owned_paths=["owned.txt"], now=1000,
                                  lease_seconds=500)
        self.db = self.state / ledger.DATABASE_NAME
        self.evidence = self.root / "evidence.json"

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, state="active", sid="session-one", generated=1100, observed=1100):
        value = {"version": 1, "generated_at": stamp(generated), "claims": [{
            "task_id": self.task, "claim_id": self.claim,
            "session": {"state": state, "session_id": sid}, "observed_at": stamp(observed)}]}
        self.evidence.write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_states_never_prove_termination_and_reserved_has_no_session(self):
        for state in ("active", "idle", "missing", "reserved"):
            with self.subTest(state=state):
                sid = None if state == "reserved" else "session-one"
                self.fixture(state, sid)
                result = reconcile.reconcile(self.db, self.evidence, now=1200)["claims"][0]
                self.assertEqual(result["recommendation"], "quarantine_review")
                self.assertNotIn("release", json.dumps(result))

    def test_expired_dirty_and_missing_worktree_are_advisory_only(self):
        self.fixture("idle", generated=1600, observed=1600)
        (self.worktree / "owned.txt").write_text("dirty")
        result = reconcile.reconcile(self.db, self.evidence, now=1600)["claims"][0]
        self.assertEqual(result["lease"], "expired")
        self.assertIn("worktree_dirty", result["reasons"])
        self.assertIn("lease_expired_not_termination", result["reasons"])
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE claims SET worktree=? WHERE claim_id=?", (str(self.root / "gone"), self.claim))
        result = reconcile.reconcile(self.db, self.evidence, now=1600)["claims"][0]
        self.assertIn("worktree_missing", result["reasons"])

    def test_duplicate_mismatched_stale_future_and_malformed_fail_closed(self):
        value = self.fixture()
        for invalid in (
            {**value, "claims": value["claims"] * 2},
            {**value, "claims": [{**value["claims"][0], "claim_id": "other"}]},
            {**value, "generated_at": stamp(800)},
            {**value, "generated_at": stamp(1300)},
            {**value, "claims": [{**value["claims"][0], "session": {"state": "idle", "session_id": None}}]},
        ):
            self.evidence.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(reconcile.ReconcileError):
                reconcile.reconcile(self.db, self.evidence, now=1200)

    def test_symlinked_worktree_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "owned.txt").write_text("private")
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE claims SET worktree=? WHERE claim_id=?", (str(alias), self.claim))
        self.fixture()
        result = reconcile.reconcile(self.db, self.evidence, now=1200)["claims"][0]
        self.assertEqual(result["worktree"], "unavailable")

    def test_repeat_is_identical_and_inputs_worktree_ledger_unchanged(self):
        self.fixture("idle")
        snapshots = snapshot_paths(self.db, self.evidence, self.worktree, self.main_repo)
        first = reconcile.reconcile(self.db, self.evidence, now=1200)
        second = reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertEqual(first, second)
        self.assertEqual(snapshots, snapshot_paths(self.db, self.evidence, self.worktree, self.main_repo))
        self.assertTrue(first["read_only"])

    def test_status_does_not_refresh_gitfile_linked_worktree_index(self):
        target = self.worktree / "owned.txt"
        stat_before = target.stat()
        os.utime(target, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 2_000_000_000))
        gitdir = (self.worktree / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
        index = self.worktree / gitdir / "index"
        before = hashlib.sha256(index.read_bytes()).digest()
        self.fixture("active")
        reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertEqual(before, hashlib.sha256(index.read_bytes()).digest())

    def test_inherited_git_environment_cannot_redirect_status(self):
        self.fixture("active")
        foreign = self.root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "-C", str(foreign), "init", "-q"], check=True)
        prior = {key: os.environ.get(key) for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        try:
            os.environ.update({"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                               "GIT_INDEX_FILE": str(foreign / ".git/index")})
            result = reconcile.reconcile(self.db, self.evidence, now=1200)
            self.assertEqual(result["claims"][0]["worktree"], "clean")
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_repository_local_fsmonitor_hook_is_never_invoked(self):
        self.fixture("active")
        marker = self.root / "fsmonitor-invoked"
        hook = self.root / "fsmonitor-hook"
        hook.write_text(f"#!/bin/sh\nprintf invoked > {marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        subprocess.run(["git", "-C", str(self.main_repo), "config", "core.fsmonitor", str(hook)], check=True)
        result = reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertEqual(result["claims"][0]["worktree"], "clean")
        self.assertFalse(marker.exists())

    def test_owned_pathspec_magic_is_treated_as_literal(self):
        self.fixture("active")
        (self.worktree / "unowned.txt").write_text("outside owned scope", encoding="utf-8")
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE claims SET owned_paths=? WHERE claim_id=?", (json.dumps([":(glob)*"]), self.claim))
        result = reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertEqual(result["claims"][0]["worktree"], "clean")

    def test_distinct_claims_cannot_bind_the_same_session_id(self):
        other = self.root / "other-worktree"
        subprocess.run(["git", "-C", str(self.main_repo), "worktree", "add", "-q", "-b", "other-worker",
                        str(other), "HEAD"], check=True)
        other_claim = ledger.claim(self.state, task_id="other-task", profile="test", branch="build/other",
                                   worktree=other, owned_paths=["different.txt"], now=1000, lease_seconds=500)
        first = {"task_id": self.task, "claim_id": self.claim,
                 "session": {"state": "active", "session_id": "same-session"}, "observed_at": stamp(1100)}
        second = {"task_id": "other-task", "claim_id": other_claim,
                  "session": {"state": "idle", "session_id": "same-session"}, "observed_at": stamp(1100)}
        self.evidence.write_text(json.dumps({"version": 1, "generated_at": stamp(1100),
                                             "claims": [first, second]}), encoding="utf-8")
        with self.assertRaisesRegex(reconcile.ReconcileError, "multiple claims"):
            reconcile.reconcile(self.db, self.evidence, now=1200)

    def test_rejects_nonfinite_negative_now_and_observation_after_generation(self):
        self.fixture()
        for now in (math.nan, math.inf, -math.inf, -1):
            with self.subTest(now=now), self.assertRaisesRegex(reconcile.ReconcileError, "finite non-negative"):
                reconcile.reconcile(self.db, self.evidence, now=now)
        accepted = self.fixture(generated=1101, observed=1100)
        self.assertEqual(reconcile.reconcile(self.db, self.evidence, now=1200)["claims"][0]["session"], "active")
        self.assertEqual(accepted["generated_at"], stamp(1101))
        self.fixture(generated=1100, observed=1101)
        with self.assertRaisesRegex(reconcile.ReconcileError, "postdates"):
            reconcile.reconcile(self.db, self.evidence, now=1200)

    def test_malformed_ledger_payload_returns_sanitized_error(self):
        self.fixture()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE claims SET owned_paths=? WHERE claim_id=?", ("not-json-secret-path", self.claim))
        with self.assertRaisesRegex(reconcile.ReconcileError, "ledger claim data is malformed") as caught:
            reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertNotIn("not-json-secret-path", str(caught.exception))


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot_paths(*paths):
    result = {}
    for root in paths:
        for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
            if path.is_file():
                result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    unittest.main()
