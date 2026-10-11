import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "continue-project.py"
spec = importlib.util.spec_from_file_location("continue_project", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ContinuationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = runner.ROOT, runner.STATE, runner.READINESS
        runner.ROOT = self.root
        runner.STATE = self.root / ".builder" / "continuation"
        runner.READINESS = self.root / ".builder" / "doctor" / "readiness.json"
        runner.READINESS.parent.mkdir(parents=True)
        runner.STATE.mkdir(parents=True)
        runner.READINESS.write_text('{"ready": true}')

    def tearDown(self):
        runner.ROOT, runner.STATE, runner.READINESS = self.old
        self.temp.cleanup()

    def invoke(self, *args):
        return runner.main(list(args))

    def test_check_does_not_launch(self):
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(self.invoke("--check"), 0)
            popen.assert_not_called()

    def test_not_ready_skips_without_child(self):
        runner.READINESS.write_text('{"ready": false}')
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(self.invoke("--run"), 0)
            popen.assert_not_called()

    def test_malformed_nonregular_and_symlink_readiness_exit2(self):
        for replacement in ("{", None, "fifo"):
            if runner.READINESS.exists() or runner.READINESS.is_symlink():
                runner.READINESS.unlink()
            if replacement == "fifo":
                os.mkfifo(runner.READINESS)
            elif replacement is None:
                runner.READINESS.symlink_to(self.root / "missing")
            else:
                runner.READINESS.write_text(replacement)
            self.assertEqual(self.invoke("--run"), 2)

    def test_run_holds_lock_through_checkpoint_and_logs_are_private(self):
        observed = []
        class Child:
            pid = os.getpid()
            def wait(self, timeout=None):
                observed.append(runner.lock_status() == "busy")
                return 0
        with mock.patch.object(runner.subprocess, "Popen", return_value=Child()) as popen:
            self.assertEqual(self.invoke("--run"), 0)
            command = popen.call_args.args[0]
        self.assertEqual(observed, [True])
        self.assertIn(str(self.root), command[command.index("-p") + 1])
        record = json.loads((runner.STATE / "last-run.json").read_text())
        self.assertEqual(os.stat(runner.STATE / "last-run.json").st_mode & 0o777, 0o600)
        for key in ("stdout_log", "stderr_log"):
            self.assertEqual(os.stat(record[key]).st_mode & 0o777, 0o600)

    def test_timeout_kills_group_after_leader_exits(self):
        calls = []
        class Child:
            pid = 123
            waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("child", timeout)
                return 0
        child = Child()
        def killpg(pid, sig):
            calls.append(sig)
        with mock.patch.object(runner.subprocess, "Popen", return_value=child):
            with mock.patch.object(runner.os, "killpg", side_effect=killpg):
                with mock.patch.object(runner, "terminate_group", wraps=runner.terminate_group):
                    self.assertEqual(self.invoke("--run"), 124)
        self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
        record = json.loads((runner.STATE / "last-run.json").read_text())
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["exit_code"], 124)

    def test_busy_lock_separate_descriptor_skips(self):
        import fcntl
        fd = os.open(runner.STATE / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.invoke("--check"), 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_symlink_lock_rejected(self):
        (runner.STATE / "run.lock").symlink_to(self.root / "target")
        self.assertEqual(self.invoke("--check"), 2)


if __name__ == "__main__":
    unittest.main()
