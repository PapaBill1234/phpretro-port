import hashlib
import importlib.util
import json
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
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "-C", str(self.worktree), "init", "-q"], check=True)
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
        snapshots = snapshot_paths(self.db, self.evidence, self.worktree)
        first = reconcile.reconcile(self.db, self.evidence, now=1200)
        second = reconcile.reconcile(self.db, self.evidence, now=1200)
        self.assertEqual(first, second)
        self.assertEqual(snapshots, snapshot_paths(self.db, self.evidence, self.worktree))
        self.assertTrue(first["read_only"])


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
