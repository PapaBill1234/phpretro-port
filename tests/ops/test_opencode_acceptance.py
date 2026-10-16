import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/opencode-acceptance.py"
LEDGER_SCRIPT = ROOT / "scripts/opencode-ledger.py"
SPEC = importlib.util.spec_from_file_location("opencode_acceptance_test", SCRIPT)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)
LEDGER_SPEC = importlib.util.spec_from_file_location("opencode_acceptance_test_ledger", LEDGER_SCRIPT)
ledger = importlib.util.module_from_spec(LEDGER_SPEC)
LEDGER_SPEC.loader.exec_module(ledger)


class AcceptanceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "private-state"
        self.state.mkdir(mode=0o700)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir()
        self.task = "OC2-M06-A09a"
        self.claim = ledger.claim(self.state, task_id=self.task, profile="primary",
                                  branch="build/task-a09", worktree=self.worktree,
                                  owned_paths=["fixture.txt"])

    def tearDown(self):
        self.temp.cleanup()

    def command(self, code):
        return [sys.executable, "-c", code]

    def run_once(self, run_id, argv, timeout=5):
        return acceptance.run(self.state, self.task, self.claim, self.worktree,
                              run_id, argv, timeout)

    def verify(self, run_id):
        return acceptance.verify(self.state, run_id)

    def test_zero_nonzero_and_restart_verification_are_unclassified(self):
        zero = self.run_once("zero-1", self.command("print('ok')"))
        nonzero = self.run_once("nonzero-1", self.command("raise SystemExit(7)"))
        self.assertEqual(zero["status"], "command_completed_zero")
        self.assertEqual(nonzero["status"], "unclassified_nonzero")
        self.assertEqual(nonzero["returncode"], 7)
        self.assertEqual(self.verify("zero-1"), zero)
        self.assertEqual(self.verify("nonzero-1"), nonzero)
        self.assertEqual((self.state / "acceptance-runs/zero-1/stdout.log").read_text(), "ok\n")
        self.assertEqual((self.state / "acceptance-runs/zero-1/stdout.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual(ledger.get_task_status(self.state, self.task)["acceptance_failures"], 0)

    def test_hashes_stable_and_duplicate_run_id_never_reexecutes(self):
        marker = self.base / "launch-count"
        code = f"open({str(marker)!r}, 'a').write('x')"
        receipt = self.run_once("once", self.command(code))
        self.assertEqual(self.verify("once")["stdout_sha256"], receipt["stdout_sha256"])
        with self.assertRaises(FileExistsError):
            self.run_once("once", self.command(code))
        self.assertEqual(marker.read_text(), "x")

    def test_racing_same_id_launches_exactly_one_child(self):
        marker = self.base / "race-count"
        command = self.command(f"open({str(marker)!r}, 'a').write('x')")
        barrier = threading.Barrier(3)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                outcomes.append(self.run_once("race", command)["status"])
            except FileExistsError:
                outcomes.append("duplicate")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["command_completed_zero", "duplicate"])
        self.assertEqual(marker.read_text(), "x")

    def test_wrong_task_claim_or_worktree_denied_before_child(self):
        marker = self.base / "should-not-exist"
        command = self.command(f"open({str(marker)!r}, 'w').write('bad')")
        other = self.base / "other"
        other.mkdir()
        for task, claim_id, worktree in (("wrong-task", self.claim, self.worktree),
                                          (self.task, "unknown-claim", self.worktree),
                                          (self.task, self.claim, other)):
            with self.subTest(task=task, claim=claim_id, worktree=worktree), self.assertRaises(acceptance.AcceptanceError):
                acceptance.run(self.state, task, claim_id, worktree, f"deny-{len(task)}-{len(claim_id)}-{len(str(worktree))}", command, 2)
        self.assertFalse(marker.exists())

    def test_expired_and_released_claims_denied_before_child(self):
        expired_tree = self.base / "expired"
        expired_tree.mkdir()
        expired = ledger.claim(self.state, task_id="expired-task", profile="primary", branch="expired-branch",
                               worktree=expired_tree, owned_paths=["x"], lease_seconds=1, now=time.time() - 5)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "active"):
            acceptance.run(self.state, "expired-task", expired, expired_tree, "expired-run", self.command("pass"), 2)
        ledger.release(self.state, self.claim, termination_evidence={"verified_terminated": True,
                       "verifier": "test", "evidence_ref": "synthetic", "terminated_at": "now"})
        with self.assertRaisesRegex(acceptance.AcceptanceError, "active"):
            acceptance.run(self.state, self.task, self.claim, self.worktree, "released-run", self.command("pass"), 2)

    def test_invalid_inputs_include_bool_nan_and_unsafe_ids(self):
        for run_id, timeout, command in (("../escape", 1, self.command("pass")),
                                         ("nan", float("nan"), self.command("pass")),
                                         ("bool", True, self.command("pass")),
                                         ("nul", 1, ["bad\x00arg"])):
            with self.subTest(run_id=run_id), self.assertRaises(acceptance.AcceptanceError):
                self.run_once(run_id, command, timeout)

    def test_cli_parse_errors_do_not_echo_private_argument_values(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--state-root", str(self.state), "run",
                                 "--task", "private-task", "--claim", self.claim,
                                 "--worktree", str(self.worktree), "--run-id", "private-run",
                                 "--timeout", "PRIVATE-INVALID", "--", *self.command("pass")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PRIVATE-INVALID", result.stderr)
        self.assertNotIn(str(self.worktree), result.stderr)

    def test_symlink_or_log_tamper_rejected(self):
        self.run_once("tamper", self.command("print('safe')"))
        log = self.state / "acceptance-runs/tamper/stdout.log"
        log.unlink()
        log.symlink_to(self.base / "victim")
        with self.assertRaises(acceptance.AcceptanceError):
            self.verify("tamper")
        log.unlink()
        log.write_text("altered")
        log.chmod(0o600)
        with self.assertRaises(acceptance.AcceptanceError):
            self.verify("tamper")

    def test_receipt_symlink_rejected(self):
        self.run_once("receipt-link", self.command("pass"))
        receipt = self.state / "acceptance-runs/receipt-link/receipt.json"
        receipt.unlink()
        receipt.symlink_to(self.base / "victim")
        with self.assertRaises(acceptance.AcceptanceError):
            self.verify("receipt-link")

    def test_timeout_kills_in_group_descendant_and_is_unresolved(self):
        pidfile = self.base / "descendant.pid"
        code = ("import subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)")
        receipt = self.run_once("timeout", self.command(code), timeout=0.25)
        self.assertEqual(receipt["status"], "unresolved_timeout")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            # A zombie may briefly remain; it is not running and will be reaped by init.
            with open(f"/proc/{pid}/stat") as process_stat:
                self.assertIn(process_stat.read().split()[2], ("Z",))
        self.assertEqual(self.verify("timeout")["status"], "unresolved_timeout")

    def test_output_cap_leaves_unresolved_receipt(self):
        previous = acceptance.MAX_OUTPUT
        acceptance.MAX_OUTPUT = 1024
        try:
            receipt = self.run_once("output-cap", self.command("import sys; sys.stdout.write('x'*10000000)"), 5)
        finally:
            acceptance.MAX_OUTPUT = previous
        self.assertEqual(receipt["status"], "unresolved_output_limit")
        self.assertLessEqual(receipt["stdout_size"] + receipt["stderr_size"], 1024)

    def test_missing_executable_is_infrastructure_unresolved(self):
        receipt = self.run_once("missing-executable", [str(self.base / "does-not-exist")])
        self.assertEqual(receipt["status"], "unresolved_infrastructure")
        self.assertEqual(self.verify("missing-executable")["status"], "unresolved_infrastructure")

    def test_killed_runner_leaves_intent_and_never_replays(self):
        pidfile = self.base / "child.pid"
        code = f"import os,time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
        command = [sys.executable, str(SCRIPT), "--state-root", str(self.state), "run",
                   "--task", self.task, "--claim", self.claim, "--worktree", str(self.worktree),
                   "--run-id", "killed-run", "--timeout", "20", "--", *self.command(code)]
        runner = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        intent = self.state / "acceptance-runs/killed-run/intent.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not intent.exists() or not pidfile.exists()):
            time.sleep(0.01)
        self.assertTrue(intent.exists())
        self.assertTrue(pidfile.exists())
        child_pid = int(pidfile.read_text())
        runner.kill()
        runner.wait(timeout=5)
        # The child was intentionally terminated here; SIGKILL of the runner
        # itself provides no cleanup guarantee.
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.assertEqual(self.verify("killed-run")["status"], "unresolved_incomplete_intent")
        with self.assertRaises(FileExistsError):
            self.run_once("killed-run", self.command("pass"))

    def test_preflight_is_read_only_for_outcomes(self):
        db = self.state / ledger.DATABASE_NAME
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        before = connection.execute("select count(*) from outcomes").fetchone()[0]
        before_claims = connection.execute("select count(*) from claims").fetchone()[0]
        connection.close()
        self.run_once("readonly", self.command("pass"))
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        after = connection.execute("select count(*) from outcomes").fetchone()[0]
        after_claims = connection.execute("select count(*) from claims").fetchone()[0]
        connection.close()
        self.assertEqual(before, after)
        self.assertEqual(before_claims, after_claims)

    def test_inherited_git_routing_environment_is_removed(self):
        previous = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = str(self.base / "foreign-git-dir")
        try:
            receipt = self.run_once("git-env", self.command("import os; print(os.environ.get('GIT_DIR', 'absent'))"))
        finally:
            if previous is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = previous
        self.assertEqual(receipt["status"], "command_completed_zero")
        self.assertEqual((self.state / "acceptance-runs/git-env/stdout.log").read_text(), "absent\n")


if __name__ == "__main__":
    unittest.main()
