import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_SCRIPT = ROOT / "scripts/opencode-acceptance-outcome.py"
RUNNER_SCRIPT = ROOT / "scripts/opencode-acceptance.py"
LEDGER_SCRIPT = ROOT / "scripts/opencode-ledger.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load("opencode_acceptance_outcome_test", ADAPTER_SCRIPT)
runner = load("opencode_acceptance_outcome_runner_test", RUNNER_SCRIPT)
ledger = load("opencode_acceptance_outcome_ledger_test", LEDGER_SCRIPT)


class ReviewedOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "private-state"
        self.state.mkdir(mode=0o700)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir()
        self.task = "OC2-M06-A09b-test"
        self.claim = ledger.claim(
            self.state, task_id=self.task, profile="primary", branch="build/test",
            worktree=self.worktree, owned_paths=["fixture.py"])

    def tearDown(self):
        self.temp.cleanup()

    def child(self, code):
        return [sys.executable, "-c", code]

    def run_child(self, run_id, code, timeout=3):
        return runner.run(self.state, self.task, self.claim, self.worktree,
                          run_id, self.child(code), timeout)

    def decide(self, run_id, attempt, verdict, **extra):
        return adapter.record(
            self.state, run_id=run_id, task_id=self.task, claim_id=self.claim,
            attempt_id=attempt, verdict=verdict, review_confirmed=True, **extra)

    def count(self):
        return ledger.get_task_status(self.state, self.task)["acceptance_failures"]

    def test_zero_exit_requires_review_and_records_pass(self):
        self.assertEqual(self.run_child("pass-run", "print('ok')")["status"],
                         "command_completed_zero")
        receipt = self.decide("pass-run", "pass-attempt", "pass")
        self.assertEqual(receipt["kind"], "acceptance_pass")
        self.assertEqual(self.count(), 0)

    def test_nonzero_requires_explicit_implementation_attribution(self):
        self.assertEqual(self.run_child("fail-run", "raise SystemExit(7)")["status"],
                         "unclassified_nonzero")
        receipt = self.decide("fail-run", "failure-attempt", "failure",
                              implementation_attributed=True,
                              failure_signature="test.failure")
        self.assertEqual(receipt["acceptance_failures"], 1)

    def test_wrong_verdicts_and_missing_or_wrong_typed_assertions_rejected(self):
        self.run_child("nonzero", "raise SystemExit(2)")
        self.run_child("zero", "pass")
        attempts = [
            ("nonzero", "np", "pass", {"review_confirmed": True}),
            ("zero", "zf", "failure", {"review_confirmed": True,
                                          "implementation_attributed": True,
                                          "failure_signature": "test.failure"}),
            ("zero", "missing", "pass", {"review_confirmed": False}),
            ("zero", "numeric", "pass", {"review_confirmed": 1}),
            ("zero", "string", "pass", {"review_confirmed": "true"}),
            ("nonzero", "no-attribution", "failure", {"review_confirmed": True,
                                                          "implementation_attributed": False,
                                                          "failure_signature": "test.failure"}),
            ("nonzero", "bad-attribution", "failure", {"review_confirmed": True,
                                                           "implementation_attributed": 1,
                                                           "failure_signature": "test.failure"}),
            ("nonzero", "bad-signature", "failure", {"review_confirmed": True,
                                                         "implementation_attributed": True,
                                                         "failure_signature": "free form!"}),
        ]
        for run_id, attempt, verdict, kwargs in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(
                    (adapter.OutcomeError, adapter.ledger.LedgerError)):
                adapter.record(self.state, run_id=run_id, task_id=self.task,
                               claim_id=self.claim, attempt_id=attempt,
                               verdict=verdict, **kwargs)
        self.assertEqual(self.count(), 0)

    def test_all_verified_unresolved_statuses_are_ineligible(self):
        cases = [
            ("timeout", lambda: self.run_child("timeout", "import time; time.sleep(2)", 0.05)),
            ("output", self._output_limited_run),
            ("infrastructure", lambda: runner.run(
                self.state, self.task, self.claim, self.worktree, "infrastructure",
                [str(self.base / "missing-executable")], 2)),
            ("incomplete", self._incomplete_run),
            ("capture", lambda: self._verified_status_fixture("capture", "unresolved_capture")),
            ("interrupted", lambda: self._verified_status_fixture("interrupted", "unresolved_interrupted")),
        ]
        for name, make in cases:
            with self.subTest(status=name):
                receipt = make()
                self.assertNotIn(receipt["status"], ("command_completed_zero", "unclassified_nonzero"))
                with self.assertRaises(adapter.OutcomeError):
                    self.decide(name, f"attempt-{name}", "failure",
                                implementation_attributed=True,
                                failure_signature="test.failure")
        self.assertEqual(self.count(), 0)

    def _output_limited_run(self):
        old = runner.MAX_OUTPUT
        runner.MAX_OUTPUT = 32
        try:
            return self.run_child("output", "print('x'*1000)")
        finally:
            runner.MAX_OUTPUT = old

    def _incomplete_run(self):
        root = ledger._canonical_private_root(self.state)
        runs = root / "acceptance-runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        run_dir = runs / "incomplete"
        run_dir.mkdir(mode=0o700)
        intent = {"schema": 2, "run_id": "incomplete"}
        (run_dir / "intent.json").write_text(json.dumps(intent))
        (run_dir / "intent.json").chmod(0o600)
        return runner.verify(self.state, "incomplete")

    def _verified_status_fixture(self, run_id, status):
        """Construct a schema-valid local receipt fixture for hard-to-induce states."""
        receipt = self.run_child(run_id, "pass")
        path = self.state / "acceptance-runs" / run_id / "receipt.json"
        value = json.loads(path.read_text())
        value["status"] = status
        value["returncode"] = None
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return runner.verify(self.state, run_id)

    def test_run_id_evidence_unique_across_attempts_and_verdicts(self):
        self.run_child("single-run", "print('ok')")
        self.decide("single-run", "attempt-one", "pass")
        with self.assertRaisesRegex(adapter.ledger.LedgerError, "evidence reference was already used"):
            self.decide("single-run", "attempt-two", "pass")
        self.run_child("single-failure", "raise SystemExit(1)")
        self.decide("single-failure", "failure-one", "failure",
                    implementation_attributed=True, failure_signature="test.failure")
        with self.assertRaisesRegex(adapter.ledger.LedgerError, "evidence reference was already used"):
            self.decide("single-failure", "failure-two", "failure",
                        implementation_attributed=True, failure_signature="other.signature")
        # A different attempt cannot relabel the same execution as a pass either.
        with self.assertRaises(adapter.OutcomeError):
            self.decide("single-failure", "failure-pass", "pass")

    def test_exact_attempt_replay_is_idempotent_and_conflict_rejected(self):
        self.run_child("replay", "raise SystemExit(3)")
        first = self.decide("replay", "same-attempt", "failure",
                            implementation_attributed=True, failure_signature="test.failure")
        replay = self.decide("replay", "same-attempt", "failure",
                             implementation_attributed=True, failure_signature="test.failure")
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(adapter.ledger.LedgerError, "conflicting"):
            self.decide("replay", "same-attempt", "failure",
                        implementation_attributed=True, failure_signature="other.failure")
        self.assertEqual(self.count(), 1)

    def test_replay_still_reverifies_tampered_logs_before_ledger_idempotency(self):
        self.run_child("tamper-replay", "raise SystemExit(1)")
        args = dict(run_id="tamper-replay", task_id=self.task, claim_id=self.claim,
                    attempt_id="tamper-attempt", verdict="failure", review_confirmed=True,
                    implementation_attributed=True, failure_signature="test.failure")
        adapter.record(self.state, **args)
        log = self.state / "acceptance-runs/tamper-replay/stderr.log"
        log.write_text("changed")
        log.chmod(0o600)
        with self.assertRaises(adapter.acceptance.AcceptanceError):
            adapter.record(self.state, **args)
        self.assertEqual(self.count(), 1)

    def test_three_distinct_reviewed_failures_make_one_durable_sol_intent(self):
        for index in range(3):
            run_id = f"three-{index}"
            self.run_child(run_id, f"raise SystemExit({index + 1})")
            result = self.decide(run_id, f"attempt-{index}", "failure",
                                 implementation_attributed=True,
                                 failure_signature=f"root.{index}")
        self.assertEqual(result["acceptance_failures"], 3)
        self.assertTrue(result["routine_retry_blocked"])
        self.assertIsNotNone(result["sol_help_intent"])
        self.assertEqual(result["sol_help_intent"]["created_by_attempt"], "attempt-2")
        # The existing identical-signature guard blocks at two, before the third-failure intent.
        for index in range(2):
            run_id = f"same-{index}"
            self.run_child(run_id, "raise SystemExit(8)")
            result = self.decide(run_id, f"same-attempt-{index}", "failure",
                                 implementation_attributed=True,
                                 failure_signature="same.root")
        self.assertEqual(result["identical_failure_count"], 2)
        self.assertTrue(result["routine_retry_blocked"])

    def test_claim_binding_mismatch_rejected_without_outcome_mutation(self):
        receipt = self.run_child("bound", "pass")
        wrong_tree = self.base / "wrong-tree"
        wrong_tree.mkdir()
        wrong_claim = ledger.claim(self.state, task_id="other-task", profile="secondary",
                                   branch="other-branch", worktree=wrong_tree,
                                   owned_paths=["other.py"])
        for task, claim in (("other-task", self.claim), (self.task, wrong_claim)):
            with self.subTest(task=task), self.assertRaises(adapter.OutcomeError):
                adapter.record(self.state, run_id="bound", task_id=task,
                               claim_id=claim, attempt_id=f"bad-{task}", verdict="pass",
                               review_confirmed=True)
        self.assertEqual(self.count(), 0)
        self.assertEqual(receipt["worktree"], str(self.worktree.resolve()))

    def test_original_released_expired_claim_remains_reviewable(self):
        self.run_child("historical", "print('done')")
        ledger.release(self.state, self.claim, termination_evidence={
            "verified_terminated": True, "verifier": "local-test",
            "evidence_ref": "test/termination", "terminated_at": "now"})
        result = self.decide("historical", "historical-attempt", "pass")
        self.assertEqual(result["kind"], "acceptance_pass")

        # Expiration after a valid run also does not erase its original binding.
        expired_tree = self.base / "second-tree"
        expired_tree.mkdir()
        expiring_claim = ledger.claim(
            self.state, task_id="expired-after-run", profile="secondary",
            branch="build/expired-after-run", worktree=expired_tree,
            owned_paths=["second.py"])
        runner.run(self.state, "expired-after-run", expiring_claim, expired_tree,
                   "expired-after-run", self.child("pass"), 3)
        connection = ledger._database(self.state)
        try:
            connection.execute("UPDATE claims SET expires_at=? WHERE claim_id=?",
                               (1, expiring_claim))
        finally:
            connection.close()
        result = adapter.record(
            self.state, run_id="expired-after-run", task_id="expired-after-run",
            claim_id=expiring_claim, attempt_id="expired-attempt", verdict="pass",
            review_confirmed=True)
        self.assertEqual(result["kind"], "acceptance_pass")

    def test_receipt_tampering_rejected_before_any_ledger_write(self):
        self.run_child("tampered", "raise SystemExit(4)")
        path = self.state / "acceptance-runs/tampered/receipt.json"
        receipt = json.loads(path.read_text())
        receipt["task_id"] = "forged-task"
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        with self.assertRaises(adapter.acceptance.AcceptanceError):
            self.decide("tampered", "no-write", "failure",
                        implementation_attributed=True, failure_signature="test.failure")
        self.assertEqual(self.count(), 0)

    def test_cli_redacts_ids_and_paths_and_sanitizes_rejection(self):
        self.run_child("cli-run", "print('PRIVATE-CHILD-OUTPUT')")
        command = [sys.executable, str(ADAPTER_SCRIPT), "--state-root", str(self.state),
                   "--run-id", "cli-run", "--task", self.task, "--claim", self.claim,
                   "--attempt-id", "cli-attempt", "--verdict", "pass", "--review-confirmed"]
        ok = subprocess.run(command, text=True, capture_output=True, check=True)
        for private in (self.task, self.claim, str(self.worktree), "cli-run", "PRIVATE-CHILD-OUTPUT"):
            self.assertNotIn(private, ok.stdout + ok.stderr)
        public = json.loads(ok.stdout)
        self.assertEqual(public["kind"], "acceptance_pass")
        self.assertIs(public["sol_help_intent"], False)
        denied = subprocess.run(command[:-1], text=True, capture_output=True)
        self.assertEqual(denied.returncode, 2)
        self.assertNotIn(self.claim, denied.stderr)
        self.assertNotIn(str(self.worktree), denied.stderr)

    def test_cli_redacts_sol_help_attempt_id_after_three_real_reviewed_failures(self):
        private_attempts = ["PRIVATE-SOL-TRIGGER-ATTEMPT-1",
                            "PRIVATE-SOL-TRIGGER-ATTEMPT-2",
                            "PRIVATE-SOL-TRIGGER-ATTEMPT-3"]
        secret_log = "PRIVATE-FAILURE-LOG-CONTENT"
        outputs = []
        for index, attempt in enumerate(private_attempts):
            run_id = f"private-run-{index}"
            self.run_child(run_id, f"print({secret_log!r}); raise SystemExit(7)")
            command = [sys.executable, str(ADAPTER_SCRIPT), "--state-root", str(self.state),
                       "--run-id", run_id, "--task", self.task, "--claim", self.claim,
                       "--attempt-id", attempt, "--verdict", "failure", "--review-confirmed",
                       "--implementation-attributed", "--failure-signature", f"test.failure.{index}"]
            result = subprocess.run(command, text=True, capture_output=True, check=True)
            outputs.append(json.loads(result.stdout))
            for private in (self.task, self.claim, run_id, attempt, str(self.worktree), secret_log):
                self.assertNotIn(private, result.stdout + result.stderr)

        self.assertIs(outputs[0]["sol_help_intent"], False)
        self.assertIs(outputs[1]["sol_help_intent"], False)
        self.assertIs(outputs[2]["sol_help_intent"], True)
        self.assertEqual(outputs[2]["acceptance_failures"], 3)
        self.assertEqual(set(outputs[2]), {"kind", "recorded", "acceptance_failures",
                                           "routine_retry_blocked", "sol_help_intent"})

    def test_rejected_cli_decision_does_not_mutate_ledger(self):
        self.run_child("cli-nonzero", "raise SystemExit(2)")
        command = [sys.executable, str(ADAPTER_SCRIPT), "--state-root", str(self.state),
                   "--run-id", "cli-nonzero", "--task", self.task, "--claim", self.claim,
                   "--attempt-id", "cli-rejected", "--verdict", "failure",
                   "--review-confirmed", "--implementation-attributed"]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.count(), 0)

    def test_fresh_process_can_read_persisted_reviewed_outcome(self):
        self.run_child("persisted", "raise SystemExit(9)")
        self.decide("persisted", "persisted-attempt", "failure",
                    implementation_attributed=True, failure_signature="test.failure")
        code = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('l',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
            "print(m.get_task_status(__import__('pathlib').Path(sys.argv[2]),sys.argv[3])['acceptance_failures'])"
        )
        result = subprocess.run([sys.executable, "-c", code, str(LEDGER_SCRIPT),
                                 str(self.state), self.task], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
