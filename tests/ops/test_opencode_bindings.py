import importlib.util
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("opencode_ledger_bindings", ROOT / "scripts/opencode-ledger.py")
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.worktree = self.base / "checkout"
        self.worktree.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add(self, task="task-a", profile="primary", now=10, lease=100):
        worktree = self.base / f"checkout-{task}"
        worktree.mkdir()
        return ledger.claim(self.state, task_id=task, profile=profile,
                            branch=f"branch/{task}", worktree=worktree,
                            owned_paths=[f"src/{task}.py"], now=now, lease_seconds=lease)

    def bind(self, task, claim, **kw):
        return ledger.bind_session(self.state, task_id=task, claim_id=claim,
                                   session_id=kw.pop("session_id", "session-a"),
                                   project_id=kw.pop("project_id", "project-a"),
                                   profile=kw.pop("profile", "primary"),
                                   server_scope=kw.pop("server_scope", "scope-a"),
                                   now=kw.pop("now", 11), **kw)

    def test_durable_exact_binding_replay_and_private_read(self):
        claim = self.add()
        receipt = self.bind("task-a", claim, now=11)
        # A replay returns the historical receipt; it is not new admission and
        # does not revalidate a lease that has since expired.
        self.assertEqual(self.bind("task-a", claim, now=111), receipt)
        path = self.state / ledger.DATABASE_NAME
        digest_before = hashlib.sha256(path.read_bytes()).digest()
        entries_before = sorted(item.name for item in self.state.iterdir())
        self.assertEqual(ledger.get_session_binding(self.state, task_id="task-a", claim_id=claim), receipt)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest_before)
        self.assertEqual(sorted(item.name for item in self.state.iterdir()), entries_before)
        with sqlite3.connect(self.state / ledger.DATABASE_NAME) as db:
            self.assertEqual(db.execute("SELECT created_at FROM session_bindings").fetchone()[0], 11)
        self.assertEqual(ledger.get_session_binding(self.state, task_id="missing", claim_id=claim), None)

    def test_active_matching_profile_and_exact_claim_required(self):
        claim = self.add()
        self.assertEqual(self.bind("task-a", claim)["profile"], "primary")
        other = self.add("task-b", profile="secondary")
        for args in (("wrong-task", claim, "primary"), ("task-b", claim, "primary"),
                     ("task-b", other, "primary")):
            with self.subTest(args=args), self.assertRaises(ledger.LedgerError):
                ledger.bind_session(self.state, task_id=args[0], claim_id=args[1],
                    session_id="s", project_id="p", profile=args[2], server_scope="scope", now=11)

    def test_released_quarantined_and_expired_claims_cannot_bind(self):
        claim = self.add(now=10, lease=1)
        with self.assertRaisesRegex(ledger.LedgerError, "active unexpired"):
            self.bind("task-a", claim, now=11)
        with self.assertRaisesRegex(ledger.LedgerError, "cannot be revived"):
            ledger.heartbeat(self.state, claim, now=11)
        with self.assertRaisesRegex(ledger.LedgerError, "active unexpired"):
            self.bind("task-a", claim, now=10.5)
        claim2 = self.add("task-b", now=10)
        ledger.release(self.state, claim2, termination_evidence={"verified_terminated": True,
            "verifier": "operator", "evidence_ref": "ev1", "terminated_at": "then"})
        with self.assertRaisesRegex(ledger.LedgerError, "active unexpired"):
            self.bind("task-b", claim2, now=11)

    def test_conflicting_rebind_and_historical_scope_session_collision_forbidden(self):
        a = self.add("task-a")
        original = self.bind("task-a", a, now=11)
        for changes in ({"project_id": "other"}, {"session_id": "other"}, {"server_scope": "other"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ledger.LedgerError, "conflicting"):
                self.bind("task-a", a, now=12, **changes)
        ledger.release(self.state, a, termination_evidence={"verified_terminated": True,
            "verifier": "operator", "evidence_ref": "ev1", "terminated_at": "then"})
        b = self.add("task-b", profile="secondary")
        with self.assertRaisesRegex(ledger.LedgerError, "conflicting"):
            self.bind("task-b", b, profile="secondary", now=11, session_id=original["session_id"])
        self.assertEqual(self.bind("task-b", b, profile="secondary", now=11, session_id="same", server_scope="different")["session_id"], "same")

    def test_bad_identifier_types_lengths_and_clocks_rejected(self):
        claim = self.add()
        base = dict(task_id="task-a", claim_id=claim, session_id="s", project_id="p",
                    profile="primary", server_scope="scope")
        for key, value in (("session_id", None), ("project_id", "x" * 129),
                           ("server_scope", "https://user:pass@example.test"),
                           ("profile", "bad profile")):
            with self.subTest(key=key), self.assertRaises(ledger.LedgerError):
                ledger.bind_session(self.state, **(base | {key: value}), now=11)
        for clock in (float("nan"), float("inf"), -1, "nope", True, False, "11"):
            with self.subTest(clock=clock), self.assertRaises(ledger.LedgerError):
                ledger.bind_session(self.state, **base, now=clock)

    def test_legacy_schema_migrates_without_changing_existing_claims_or_outcomes(self):
        claim = self.add()
        ledger.record_outcome(self.state, task_id="task-a", claim_id=claim,
                              attempt_id="attempt-a", kind="infrastructure")
        path = self.state / ledger.DATABASE_NAME
        with sqlite3.connect(path) as db:
            before = db.execute("SELECT * FROM claims").fetchall()
            outcomes = db.execute("SELECT * FROM outcomes").fetchall()
            db.execute("DROP TABLE session_bindings")  # emulate pre-binding ledger
        digest_before = hashlib.sha256(path.read_bytes()).digest()
        entries_before = sorted(item.name for item in self.state.iterdir())
        self.assertIsNone(ledger.get_session_binding(self.state, task_id="task-a", claim_id=claim))
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest_before)
        self.assertEqual(sorted(item.name for item in self.state.iterdir()), entries_before)
        # The write path owns migration; the reader above must leave the legacy DB intact.
        self.assertEqual(self.bind("task-a", claim, now=11)["task_id"], "task-a")
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT * FROM claims").fetchall(), before)
            self.assertEqual(db.execute("SELECT * FROM outcomes").fetchall(), outcomes)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_bindings").fetchone()[0], 1)

    def test_read_of_empty_root_is_unknown_and_creates_nothing(self):
        before = sorted(item.name for item in self.state.iterdir())
        self.assertIsNone(ledger.get_session_binding(self.state, task_id="task", claim_id="claim"))
        self.assertEqual(sorted(item.name for item in self.state.iterdir()), before)
        self.assertFalse((self.state / ledger.DATABASE_NAME).exists())

    def test_read_rejects_unsafe_database_file(self):
        claim = self.add()
        path = self.state / ledger.DATABASE_NAME
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ledger.LedgerError, "private"):
            ledger.get_session_binding(self.state, task_id="task-a", claim_id=claim)
        os.chmod(path, 0o600)

    def test_binding_clock_cannot_predate_claim_creation(self):
        claim = self.add(now=20, lease=100)
        with self.assertRaisesRegex(ledger.LedgerError, "predates"):
            self.bind("task-a", claim, now=19)
        self.assertIsNone(ledger.get_session_binding(self.state, task_id="task-a", claim_id=claim))
        self.assertEqual(self.bind("task-a", claim, now=21)["created_at"], 21)

    def test_concurrent_same_session_binding_has_one_winner(self):
        a = self.add("task-a")
        b = self.add("task-b", profile="secondary")
        barrier = threading.Barrier(2)
        results = []
        def worker(task, claim):
            barrier.wait()
            try:
                results.append(("ok", self.bind(task, claim, profile="primary" if task == "task-a" else "secondary",
                                                 session_id="race", now=11)))
            except ledger.LedgerError:
                results.append(("reject", None))
        threads = [threading.Thread(target=worker, args=("task-a", a)),
                   threading.Thread(target=worker, args=("task-b", b))]
        [thread.start() for thread in threads]
        [thread.join(10) for thread in threads]
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result[0] for result in results), ["ok", "reject"])
        with sqlite3.connect(self.state / ledger.DATABASE_NAME) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_bindings").fetchone()[0], 1)

    def test_insert_failure_rolls_back_without_partial_binding(self):
        claim = self.add()
        with sqlite3.connect(self.state / ledger.DATABASE_NAME) as db:
            db.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON session_bindings "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaisesRegex(ledger.LedgerError, "conflicting"):
            self.bind("task-a", claim)
        self.assertIsNone(ledger.get_session_binding(self.state, task_id="task-a", claim_id=claim))
        with sqlite3.connect(self.state / ledger.DATABASE_NAME) as db:
            db.execute("DROP TRIGGER reject_binding")
        self.assertEqual(self.bind("task-a", claim)["claim_id"], claim)


if __name__ == "__main__":
    unittest.main()
