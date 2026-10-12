import fcntl
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

    def test_symlink_readiness_ancestor_rejected(self):
        doctor = self.root / ".builder" / "doctor"
        runner.READINESS.unlink()
        doctor.rmdir()
        target = self.root / "elsewhere"
        target.mkdir()
        doctor.symlink_to(target, target_is_directory=True)
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(self.invoke("--run"), 2)
            popen.assert_not_called()

    def test_run_holds_lock_through_checkpoint_and_logs_are_private(self):
        observed = []
        class Child:
            pid = os.getpid()
            def wait(self, timeout=None):
                observed.append(runner.lock_status() == "busy")
                return 0
        with mock.patch.object(runner.subprocess, "Popen", return_value=Child()) as popen:
            original_atomic = runner.atomic_json
            def checkpoint(path, data):
                observed.append(runner.lock_status() == "busy")
                original_atomic(path, data)
            with mock.patch.object(runner, "atomic_json", side_effect=checkpoint):
                self.assertEqual(self.invoke("--run"), 0)
            command = popen.call_args.args[0]
        self.assertEqual(observed, [True, True])
        self.assertIn(str(self.root), command[command.index("-p") + 1])
        record = json.loads((runner.STATE / "last-run.json").read_text())
        self.assertEqual(os.stat(runner.STATE / "last-run.json").st_mode & 0o777, 0o600)
        for key in ("stdout_log", "stderr_log"):
            self.assertEqual(os.stat(record[key]).st_mode & 0o777, 0o600)

    @staticmethod
    def _proc_state(grandchild):
        try:
            stat_text = Path(f"/proc/{grandchild}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return "gone"
        return stat_text.split(") ", 1)[1].split()[0]

    def test_proc_state_disappearance_only_accepts_gone_process_errors(self):
        for error in (FileNotFoundError("gone"), ProcessLookupError("gone")):
            with self.subTest(error=type(error).__name__), mock.patch.object(Path, "read_text", side_effect=error) as read_text:
                self.assertEqual(self._proc_state(987654321), "gone")
                read_text.assert_called_once_with()

        for error in (PermissionError("denied"), OSError("I/O failure")):
            with self.subTest(error=type(error).__name__), mock.patch.object(Path, "read_text", side_effect=error):
                with self.assertRaises(type(error)):
                    self._proc_state(987654321)

        with mock.patch.object(Path, "read_text", return_value="malformed"):
            with self.assertRaises(IndexError):
                self._proc_state(987654321)
        with mock.patch.object(Path, "read_text", return_value="123 (worker) S 1 2 3"):
            self.assertEqual(self._proc_state(987654321), "S")

    def test_terminate_group_stops_real_leader_and_grandchild(self):
        pid_file = self.root / "grandchild.pid"
        code = ("import os,signal,subprocess,time; "
                "g=subprocess.Popen(['python3','-c',%r]); open(%r,'w').write(str(g.pid)); "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)" %
                ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)", str(pid_file)))
        proc = subprocess.Popen(["python3", "-c", code], start_new_session=True)
        try:
            for _ in range(50):
                if pid_file.exists():
                    break
                import time
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            grandchild = int(pid_file.read_text())
            runner.terminate_group(proc, 0.05)
            self.assertIsNotNone(proc.poll())
            for _ in range(50):
                state = self._proc_state(grandchild)
                if state in ("Z", "gone"):
                    break
                import time
                time.sleep(0.02)
            state = self._proc_state(grandchild)
            self.assertIn(state, ("Z", "gone"))
        finally:
            if proc.poll() is None:
                runner.terminate_group(proc, 0.05)

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
                self.assertEqual(self.invoke("--run"), 124)
        self.assertEqual(calls, [signal.SIGTERM, signal.SIGKILL])
        record = json.loads((runner.STATE / "last-run.json").read_text())
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["exit_code"], 124)

    def test_busy_lock_separate_process_skips(self):
        import sys
        lock = runner.STATE / "run.lock"
        lock.touch()
        marker = self.root / "holder.ready"
        code = ("import fcntl, pathlib, time; fd=open(%r,'r+'); fcntl.flock(fd, fcntl.LOCK_EX); "
                "pathlib.Path(%r).write_text('ready'); time.sleep(2)") % (str(lock), str(marker))
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            for _ in range(100):
                if marker.exists():
                    break
                import time
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            with mock.patch.object(runner.subprocess, "Popen") as popen:
                with mock.patch("builtins.print") as output:
                    self.assertEqual(self.invoke("--run"), 0)
                popen.assert_not_called()
                self.assertIn('"reason": "busy"', output.call_args.args[0])
        finally:
            proc.terminate()
            proc.wait()

    def test_symlink_lock_rejected(self):
        (runner.STATE / "run.lock").symlink_to(self.root / "target")
        self.assertEqual(self.invoke("--check"), 2)


if __name__ == "__main__":
    unittest.main()
