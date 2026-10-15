import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/opencode-ledger.py"
SPEC = importlib.util.spec_from_file_location("opencode_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "private-state"
        self.state.mkdir(mode=0o700)
        self.worktree = self.base / "checkout"
        self.worktree.mkdir()
        self.other_worktree = self.base / "worker-checkout"
        self.other_worktree.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def add(self, task="task-1", paths=None, **kwargs):
        return ledger.claim(self.state, task_id=task, profile=kwargs.pop("profile", "primary"),
                            branch=f"build/{task}", worktree=kwargs.pop("worktree", self.worktree),
                            owned_paths=paths or [f"src/{task}.py"], **kwargs)

    def rows(self):
        with sqlite3.connect(self.state / ledger.DATABASE_NAME) as db:
            return db.execute("SELECT task_id, state, owned_paths FROM claims ORDER BY task_id").fetchall()

    def test_real_process_race_admits_exactly_four_and_denies_fifth(self):
        child_code = """
import importlib.util, sys, time
from pathlib import Path
s = importlib.util.spec_from_file_location('ledger', sys.argv[1])
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
Path(sys.argv[4]).touch()
while not Path(sys.argv[4]).parent.joinpath('ready-release').exists():
    time.sleep(.005)
try:
    claim_id = m.claim(Path(sys.argv[2]), task_id=sys.argv[3], profile='synthetic',
                       branch='b/' + sys.argv[3], worktree=Path(sys.argv[5]),
                       owned_paths=['src/' + sys.argv[3]])
    print('ADMITTED ' + claim_id, flush=True)
except m.LedgerError as error:
    print('DENIED ' + str(error), flush=True)
"""
        children = []
        try:
            for i in range(8):
                ready = self.base / f"ready-{i}"
                child = subprocess.Popen([sys.executable, "-c", child_code, str(SCRIPT), str(self.state),
                                          f"race-{i}", str(ready), str(self.worktree)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append((child, ready))
            # Parent creates the common release marker only after every process is waiting.
            import time
            end = time.monotonic() + 10
            while len(list(self.base.glob("ready-*"))) < 8 and time.monotonic() < end:
                time.sleep(.01)
            self.assertEqual(len(list(self.base.glob("ready-*"))), 8)
            # The wait loop is released by a marker visible to all participants.
            (self.base / "ready-release").touch()
            outputs = []
            for child, _ in children:
                out, err = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, err)
                outputs.append(out.strip())
            self.assertEqual(sum(line.startswith("ADMITTED ") for line in outputs), 4, outputs)
            self.assertEqual(sum(line.startswith("DENIED ") for line in outputs), 4, outputs)
            self.assertEqual(sum(state != "released" for _, state, _ in self.rows()), 4)
        finally:
            for child, _ in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_duplicate_task_and_parent_child_path_conflicts_rejected(self):
        self.add("duplicate", ["src/one.py"])
        with self.assertRaisesRegex(ledger.LedgerError, "duplicate task"):
            self.add("duplicate", ["src/two.py"], profile="secondary")
        with self.assertRaisesRegex(ledger.LedgerError, "overlap"):
            self.add("child", ["src/one.py/child.py"], profile="secondary")
        with self.assertRaisesRegex(ledger.LedgerError, "overlap"):
            self.add("parent", ["src"], profile="secondary")

    def test_traversal_absolute_and_symlink_owned_paths_rejected(self):
        for path in ("../escape", "src/../escape", "/tmp/absolute"):
            with self.subTest(path=path), self.assertRaises(ledger.LedgerError):
                self.add(f"bad-{path}", [path])
        (self.worktree / "linked").symlink_to(self.other_worktree, target_is_directory=True)
        with self.assertRaisesRegex(ledger.LedgerError, "symlink"):
            self.add("symlink-path", ["linked/file.py"])
        alias = self.base / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ledger.LedgerError, "symlink"):
            ledger.claim(alias, task_id="alias", profile="p", branch="b",
                         worktree=self.worktree, owned_paths=["x"])

    def test_canonical_ledger_is_shared_across_synthetic_profiles_and_worktrees(self):
        self.add("primary-task", ["modules/a.py"], profile="primary")
        self.add("secondary-task", ["modules/b.py"], profile="secondary", worktree=self.other_worktree)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        with self.assertRaisesRegex(ledger.LedgerError, "duplicate task"):
            ledger.claim(self.state, task_id="primary-task", profile="secondary", branch="other",
                         worktree=self.other_worktree, owned_paths=["elsewhere"])

    def test_failed_insert_rolls_back_transaction_without_partial_claim(self):
        child_code = """
import importlib.util, os, sys
from pathlib import Path
s = importlib.util.spec_from_file_location('ledger', sys.argv[1])
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
insert = m._insert_claim
def insert_then_crash(connection, values):
    insert(connection, values)
    os._exit(73)
m._insert_claim = insert_then_crash
m.claim(Path(sys.argv[2]), task_id='rollback', profile='p', branch='b',
       worktree=Path(sys.argv[3]), owned_paths=['src/rollback.py'])
"""
        crashed = subprocess.run([sys.executable, "-c", child_code, str(SCRIPT), str(self.state),
                                  str(self.worktree)], check=False, capture_output=True, text=True)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        self.assertEqual(self.rows(), [])
        self.add("rollback", ["src/rollback.py"])
        self.assertEqual(len(self.rows()), 1)

    def test_expired_claim_is_quarantined_and_consumes_slot_until_evidence_release(self):
        expired = self.add("expired", ["src/expired.py"], now=10, lease_seconds=1)
        self.add("two", ["src/two.py"], now=10, lease_seconds=100)
        self.add("three", ["src/three.py"], now=10, lease_seconds=100)
        self.add("four", ["src/four.py"], now=10, lease_seconds=100)
        with self.assertRaisesRegex(ledger.LedgerError, "four-slot"):
            self.add("fifth", ["src/fifth.py"], now=12)
        self.assertEqual(dict((task, state) for task, state, _ in self.rows())["expired"], "quarantined")
        with self.assertRaisesRegex(ledger.LedgerError, "cannot be revived"):
            ledger.heartbeat(self.state, expired, now=12)
        with self.assertRaisesRegex(ledger.LedgerError, "explicit verified-termination"):
            ledger.release(self.state, expired, termination_evidence={})
        evidence = {"verified_terminated": True, "verifier": "operator", "evidence_ref": "local-check-1",
                    "terminated_at": "2026-10-15T20:00:00Z"}
        ledger.release(self.state, expired, termination_evidence=evidence)
        self.add("fifth", ["src/fifth.py"], now=12)
        self.assertEqual(dict((task, state) for task, state, _ in self.rows())["expired"], "released")

    def test_database_and_root_are_private_and_symlinked_database_rejected(self):
        self.add("private", ["src/private.py"])
        db_path = self.state / ledger.DATABASE_NAME
        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        os.chmod(self.state, 0o755)
        with self.assertRaisesRegex(ledger.LedgerError, "private"):
            ledger.claim(self.state, task_id="exposed", profile="p", branch="b",
                         worktree=self.worktree, owned_paths=["exposed"])
        os.chmod(self.state, 0o700)
        db_path.unlink()
        target = self.base / "outside-db"
        target.write_text("untouched")
        db_path.symlink_to(target)
        with self.assertRaisesRegex(ledger.LedgerError, "regular file"):
            ledger.claim(self.state, task_id="symlink-db", profile="p", branch="b",
                         worktree=self.worktree, owned_paths=["safe"])
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
