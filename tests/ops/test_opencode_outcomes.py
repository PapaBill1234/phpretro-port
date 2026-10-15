import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/opencode-ledger.py"
SPEC = importlib.util.spec_from_file_location("opencode_ledger_outcomes", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "private-state"
        self.state.mkdir(mode=0o700)
        self.worktree = self.base / "checkout"
        self.worktree.mkdir()
        self.claim = ledger.claim(self.state, task_id="task-a", profile="primary",
                                  branch="build/task-a", worktree=self.worktree,
                                  owned_paths=["src/task.py"])

    def tearDown(self):
        self.temp.cleanup()

    def failure(self, attempt, signature="sig-a"):
        return ledger.record_outcome(
            self.state, task_id="task-a", claim_id=self.claim, attempt_id=attempt,
            kind="acceptance_failure", acceptance_ran=True,
            implementation_attributed=True, evidence_ref=f"acceptance/{attempt}",
            failure_signature=signature)

    def test_real_acceptance_failure_and_success_are_typed(self):
        failed = self.failure("fail-1")
        self.assertEqual(failed["acceptance_failures"], 1)
        passed = ledger.record_outcome(
            self.state, task_id="task-a", claim_id=self.claim, attempt_id="pass-1",
            kind="acceptance_pass", acceptance_ran=True, evidence_ref="acceptance/pass-1")
        self.assertEqual(passed["acceptance_failures"], 1)
        self.assertIsNone(passed["sol_help_intent"])

    def test_excluded_interruptions_and_unresolved_do_not_count(self):
        for index, kind in enumerate(("funding", "quota", "schedule", "infrastructure",
                                      "model_unavailable", "elapsed_time", "turn_budget",
                                      "interrupted", "unresolved")):
            with self.subTest(kind=kind):
                ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                                      attempt_id=f"excluded-{index}", kind=kind)
        status = ledger.get_task_status(self.state, "task-a")
        self.assertEqual(status["acceptance_failures"], 0)
        self.assertFalse(status["routine_retry_blocked"])
        self.assertTrue(status["funding_paused"])

    def test_nonzero_without_attested_acceptance_is_unresolved_not_counted(self):
        receipt = ledger.record_outcome(
            self.state, task_id="task-a", claim_id=self.claim, attempt_id="exit-1", kind="unresolved")
        self.assertEqual(receipt["acceptance_failures"], 0)
        for kwargs in ({}, {"acceptance_ran": True},
                       {"acceptance_ran": True, "implementation_attributed": True,
                        "evidence_ref": "evidence-only"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ledger.LedgerError):
                ledger.record_outcome(
                    self.state, task_id="task-a", claim_id=self.claim, attempt_id="bad",
                    kind="acceptance_failure", **kwargs)

    def test_invalid_enum_and_boolean_numeric_confusion_rejected(self):
        for kind in ("exit_nonzero", "provider_error", "ACCEPTANCE_FAILURE", ""):
            with self.subTest(kind=kind), self.assertRaises(ledger.LedgerError):
                ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                                      attempt_id="bad-kind", kind=kind)
        for value in (1, 0, "true", None):
            with self.subTest(value=value), self.assertRaises(ledger.LedgerError):
                ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                                      attempt_id="bad-bool", kind="unresolved",
                                      acceptance_ran=value)

    def test_attempt_replay_is_immutable_and_conflict_rejected(self):
        first = self.failure("retry-id")
        replay = self.failure("retry-id")
        self.assertEqual(first, replay)
        self.assertEqual(ledger.get_task_status(self.state, "task-a")["acceptance_failures"], 1)
        with self.assertRaisesRegex(ledger.LedgerError, "conflicting"):
            self.failure("retry-id", "different-signature")
        self.assertEqual(ledger.get_task_status(self.state, "task-a")["acceptance_failures"], 1)

    def test_attempts_are_bound_to_existing_task_claim(self):
        other_tree = self.base / "other"
        other_tree.mkdir()
        other_claim = ledger.claim(self.state, task_id="task-b", profile="secondary",
                                   branch="build/task-b", worktree=other_tree,
                                   owned_paths=["src/other.py"])
        with self.assertRaisesRegex(ledger.LedgerError, "does not belong"):
            ledger.record_outcome(self.state, task_id="task-a", claim_id=other_claim,
                                  attempt_id="cross-task", kind="unresolved")
        with self.assertRaisesRegex(ledger.LedgerError, "does not belong"):
            ledger.record_outcome(self.state, task_id="task-b", claim_id=self.claim,
                                  attempt_id="wrong-task", kind="unresolved")

    def test_acceptance_evidence_is_unique_across_attempts_kinds_and_tasks(self):
        self.failure("evidence-first", "signature-a")
        with self.assertRaisesRegex(ledger.LedgerError, "evidence reference was already used"):
            ledger.record_outcome(
                self.state, task_id="task-a", claim_id=self.claim, attempt_id="evidence-second",
                kind="acceptance_failure", acceptance_ran=True, implementation_attributed=True,
                evidence_ref="acceptance/evidence-first", failure_signature="signature-b")
        with self.assertRaisesRegex(ledger.LedgerError, "evidence reference was already used"):
            ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                                  attempt_id="evidence-pass", kind="acceptance_pass",
                                  acceptance_ran=True, evidence_ref="acceptance/evidence-first")
        other_tree = self.base / "other-evidence"
        other_tree.mkdir()
        other_claim = ledger.claim(self.state, task_id="task-b", profile="secondary",
                                   branch="build/task-b", worktree=other_tree,
                                   owned_paths=["src/other.py"])
        with self.assertRaisesRegex(ledger.LedgerError, "evidence reference was already used"):
            ledger.record_outcome(
                self.state, task_id="task-b", claim_id=other_claim, attempt_id="cross-evidence",
                kind="acceptance_failure", acceptance_ran=True, implementation_attributed=True,
                evidence_ref="acceptance/evidence-first", failure_signature="signature-c")
        self.assertEqual(ledger.get_task_status(self.state, "task-a")["acceptance_failures"], 1)
        self.assertEqual(ledger.get_task_status(self.state, "task-b")["acceptance_failures"], 0)

    def test_concurrent_reuse_of_one_acceptance_evidence_counts_once(self):
        worker = """
import importlib.util, sys, time
from pathlib import Path
s=importlib.util.spec_from_file_location('l', sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
ready=Path(sys.argv[5]); ready.touch()
while not ready.parent.joinpath('go').exists(): time.sleep(.005)
try:
 m.record_outcome(Path(sys.argv[2]), task_id='task-a', claim_id=sys.argv[3],
  attempt_id=sys.argv[4], kind='acceptance_failure', acceptance_ran=True,
  implementation_attributed=True, evidence_ref='acceptance/shared-run',
  failure_signature='concurrent-root')
 print('ACCEPTED')
except m.LedgerError:
 print('REJECTED')
"""
        processes = []
        try:
            for i in range(4):
                ready = self.base / f"evidence-race-{i}.ready"
                process = subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT), str(self.state),
                                            self.claim, f"evidence-race-{i}", str(ready)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append((process, ready))
            deadline = time.monotonic() + 10
            while len(list(self.base.glob("evidence-race-*.ready"))) != 4 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(list(self.base.glob("evidence-race-*.ready"))), 4)
            (self.base / "go").touch()
            outputs = []
            for process, _ in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(stdout.strip())
            self.assertEqual(outputs.count("ACCEPTED"), 1, outputs)
            self.assertEqual(outputs.count("REJECTED"), 3, outputs)
        finally:
            for process, _ in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(ledger.get_task_status(self.state, "task-a")["acceptance_failures"], 1)

    def test_second_identical_failure_blocks_unchanged_retry_before_escalation(self):
        self.failure("same-1", "stable-root-cause")
        second = self.failure("same-2", "stable-root-cause")
        self.assertTrue(second["routine_retry_blocked"])
        self.assertEqual(second["acceptance_failures"], 2)
        self.assertIsNone(second["sol_help_intent"])

    def test_three_failures_create_one_durable_escalation_and_block(self):
        self.failure("failure-1")
        self.failure("failure-2", "sig-b")
        third = self.failure("failure-3", "sig-c")
        self.assertTrue(third["routine_retry_blocked"])
        self.assertEqual(third["acceptance_failures"], 3)
        self.assertIsNotNone(third["sol_help_intent"])
        self.failure("failure-4", "sig-d")
        status = ledger.get_task_status(self.state, "task-a")
        self.assertEqual(status["acceptance_failures"], 4)
        self.assertEqual(status["sol_help_intent"]["created_by_attempt"], "failure-3")
        self.assertTrue(status["routine_retry_blocked"])

    def test_funding_pauses_dispatch_without_failure_count(self):
        receipt = ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                                        attempt_id="funding-1", kind="funding")
        self.assertTrue(receipt["funding_paused"])
        self.assertEqual(receipt["acceptance_failures"], 0)

    def test_status_survives_new_process(self):
        self.failure("durable-1")
        child = subprocess.run([sys.executable, "-c", """
import importlib.util, json, sys
from pathlib import Path
s=importlib.util.spec_from_file_location('l', sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(json.dumps(m.get_task_status(Path(sys.argv[2]), 'task-a'), sort_keys=True))
""", str(SCRIPT), str(self.state)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(child.stdout)["acceptance_failures"], 1)

    def test_task_status_uses_one_snapshot_during_concurrent_escalation(self):
        self.failure("snapshot-1", "snapshot-a")
        self.failure("snapshot-2", "snapshot-b")
        original_database = ledger._database
        reader = original_database(self.state)
        used_reader = False
        inserted = False

        def interleaved_database(root):
            nonlocal used_reader
            if not used_reader:
                used_reader = True
                return reader
            return original_database(root)

        def commit_third_failure_before_status_select(sql):
            nonlocal inserted
            if not inserted and "SELECT 1 FROM outcomes" in sql:
                inserted = True
                ledger._database = original_database
                ledger.record_outcome(
                    self.state, task_id="task-a", claim_id=self.claim, attempt_id="snapshot-3",
                    kind="acceptance_failure", acceptance_ran=True,
                    implementation_attributed=True, evidence_ref="snapshot/three",
                    failure_signature="snapshot-c")

        ledger._database = interleaved_database
        reader.set_trace_callback(commit_third_failure_before_status_select)
        try:
            status = ledger.get_task_status(self.state, "task-a")
        finally:
            ledger._database = original_database
        self.assertTrue(inserted)
        self.assertEqual(status["acceptance_failures"], 3)
        self.assertTrue(status["routine_retry_blocked"])
        self.assertIsNotNone(status["sol_help_intent"])

    def test_concurrent_process_outcomes_and_duplicate_replay_are_atomic(self):
        worker = """
import importlib.util, sys, time
from pathlib import Path
s=importlib.util.spec_from_file_location('l', sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
ready=Path(sys.argv[5]); ready.touch()
while not ready.parent.joinpath('go').exists(): time.sleep(.005)
m.record_outcome(Path(sys.argv[2]), task_id='task-a', claim_id=sys.argv[3], attempt_id=sys.argv[4],
 kind='acceptance_failure', acceptance_ran=True, implementation_attributed=True,
 evidence_ref='acceptance/' + sys.argv[4], failure_signature='concurrent-root')
"""
        processes = []
        try:
            for i in range(6):
                ready = self.base / f"worker-{i}.ready"
                process = subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT), str(self.state),
                                            self.claim, f"concurrent-{i}", str(ready)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append((process, ready))
            deadline = time.monotonic() + 10
            while len(list(self.base.glob("worker-*.ready"))) != 6 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(list(self.base.glob("worker-*.ready"))), 6)
            (self.base / "go").touch()
            for process, _ in processes:
                _, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
        finally:
            for process, _ in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        status = ledger.get_task_status(self.state, "task-a")
        self.assertEqual(status["acceptance_failures"], 6)
        self.assertIsNotNone(status["sol_help_intent"])
        (self.base / "go").unlink()
        replayers = []
        try:
            for i in range(2):
                ready = self.base / f"replay-{i}.ready"
                process = subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT), str(self.state),
                                            self.claim, "concurrent-0", str(ready)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                replayers.append((process, ready))
            deadline = time.monotonic() + 10
            while len(list(self.base.glob("replay-*.ready"))) != 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(list(self.base.glob("replay-*.ready"))), 2)
            (self.base / "go").touch()
            for process, _ in replayers:
                _, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
        finally:
            for process, _ in replayers:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(ledger.get_task_status(self.state, "task-a"), status)
        before = status
        ledger.record_outcome(self.state, task_id="task-a", claim_id=self.claim,
                              attempt_id="concurrent-0", kind="acceptance_failure",
                              acceptance_ran=True, implementation_attributed=True,
                              evidence_ref="acceptance/concurrent-0",
                              failure_signature="concurrent-root")
        self.assertEqual(ledger.get_task_status(self.state, "task-a"), before)


if __name__ == "__main__":
    unittest.main()
