import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eliza-dashboard.py"
spec = importlib.util.spec_from_file_location("eliza_dashboard", SCRIPT)
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

SECRET = "SUPERSECRETVALUE-1234567890"
KEY = "sk-test-deepseek-key"
REPO_PIN = "a" * 40
BUN_VERSION = "1.4.2"
NODE_VERSION = "v24.15.0"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def proc_state(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return "gone"
    return text.split(") ", 1)[1].split()[0]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_ports = dash.API_PORT, dash.UI_PORT
        dash.API_PORT, dash.UI_PORT = free_port(), free_port()
        self.state = self.root / ".builder" / "eliza-dashboard"
        self.state.mkdir(parents=True)
        os.chmod(self.root / ".builder", 0o700)
        os.chmod(self.state, 0o700)
        self.source_dir = self._make_source()
        self.source = {
            "name": "test",
            "path": self.source_dir,
            "pin": REPO_PIN,
            "bun": self.root / "fake-bun",
            "bun_version": BUN_VERSION,
            "node": self.root / "fake-node",
            "node_version": NODE_VERSION,
            "api_entry": Path("packages/app/src/entry.ts"),
            "vite": Path("node_modules/vite/bin/vite.js"),
            "api_cwd": Path("."),
            "ui_cwd": Path("packages/app"),
        }

    def tearDown(self):
        dash.API_PORT, dash.UI_PORT = self.old_ports
        self.temp.cleanup()

    # -- fixtures -----------------------------------------------------------
    def _make_source(self):
        src = self.root / "eliza-src"
        (src / "packages/app/src").mkdir(parents=True)
        (src / "packages/app/src/entry.ts").write_text("// entry")
        (src / "node_modules/vite/bin").mkdir(parents=True)
        (src / "node_modules/vite/bin/vite.js").write_text("// vite")
        return src

    def _check_output(self, cmd, *args, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse" in joined:
            return REPO_PIN + "\n"
        if str(self.source["bun"]) in joined:
            return BUN_VERSION + "\n"
        if str(self.source["node"]) in joined:
            return NODE_VERSION + "\n"
        raise AssertionError("unexpected command: " + joined)

    def _patch_source(self):
        return mock.patch.object(dash.subprocess, "check_output", side_effect=self._check_output)

    def _gate(self, allowed=True, stop_at="2999-01-01T00:00:00+00:00"):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        body = ("def require_allowed():\n    return {'allowed': True, 'stop_at': %r}\n"
                % stop_at) if allowed else \
               "def require_allowed():\n    raise ValueError('closed')\n"
        (scripts / "coding-window.py").write_text(body)

    def _secrets(self, mode=0o600):
        for name, value in (("api.token", "apitok"), ("vault.passphrase", "vaultpw")):
            path = self.state / name
            path.write_text(value)
            os.chmod(path, mode)
        envdir = self.root / "secrets"
        envdir.mkdir(exist_ok=True)
        env = envdir / "deepseek.env"
        env.write_text(f"# comment\nDEEPSEEK_API_KEY={KEY}\nOTHER=ignored\n")
        os.chmod(env, mode)

    # -- registered config source ------------------------------------------
    def test_validate_source_reports_pin_and_versions(self):
        with self._patch_source():
            result = dash.validate_source(self.source)
        self.assertEqual(result["pin"], self.source["pin"])
        self.assertEqual(result["bun"], "1.4.2")
        self.assertEqual(result["node"], "v24.15.0")

    def test_validate_source_rejects_wrong_pin(self):
        self.source["pin"] = "b" * 40
        with self._patch_source():
            with self.assertRaises(dash.DashboardError) as ctx:
                dash.validate_source(self.source)
        self.assertIn("pin", str(ctx.exception))

    def test_validate_source_rejects_missing_pin_checkout(self):
        with mock.patch.object(dash.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(128, "git")):
            with self.assertRaises(dash.DashboardError):
                dash.validate_source(self.source)

    def test_validate_source_rejects_wrong_runtime_version(self):
        self.source["node_version"] = "v24.14.0"
        with self._patch_source():
            with self.assertRaises(dash.DashboardError) as ctx:
                dash.validate_source(self.source)
        self.assertIn("node", str(ctx.exception))

    def test_validate_source_rejects_missing_prereq_file(self):
        (self.source_dir / "packages/app/src/entry.ts").unlink()
        with self._patch_source():
            with self.assertRaises(dash.DashboardError):
                dash.validate_source(self.source)

    # -- coding gate --------------------------------------------------------
    def test_gate_missing_fails_closed(self):
        with self.assertRaises(dash.DashboardError):
            dash.require_window(self.root)

    def test_gate_denied_blocks_start_without_dispatch(self):
        self._secrets()
        self._gate(allowed=False)
        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen") as popen:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        popen.assert_not_called()
        lock = dash.acquire_lock(self.state)
        self.assertEqual(lock[0], "free")  # lock released after refusal
        dash.release_lock(lock[1])

    # -- secrets and private files -----------------------------------------
    def test_read_private_text_rejects_symlink_and_bad_mode(self):
        target = self.state / "api.token"
        target.write_text("x")
        os.chmod(target, 0o600)
        link = self.state / "link.token"
        link.symlink_to(target)
        with self.assertRaises(dash.DashboardError):
            dash.read_private_text(link)
        os.chmod(target, 0o644)
        with self.assertRaises(dash.DashboardError):
            dash.read_private_text(target)

    def test_parse_dotenv_is_inert_data(self):
        data = dash.parse_dotenv("A=1\n# c\nB = 'quoted'\nbadline\nC=\"dq\"\n")
        self.assertEqual(data, {"A": "1", "B": "quoted", "C": "dq"})

    def test_unsafe_secret_fails_start(self):
        self._secrets(mode=0o600)
        os.chmod(self.state / "api.token", 0o644)
        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen") as popen:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        popen.assert_not_called()

    def test_ensure_state_dir_rejects_symlinked_ancestor(self):
        (self.root / ".builder" / "eliza-dashboard").rmdir()
        (self.root / ".builder" / "eliza-dashboard").symlink_to(self.root / "elsewhere",
                                                                target_is_directory=True)
        with self.assertRaises(dash.DashboardError):
            dash.ensure_state_dir(self.root)

    # -- child environment --------------------------------------------------
    def test_build_env_isolates_provider_and_vault_secrets_from_ui(self):
        profile = self.root / "prof"
        profile.mkdir()
        secrets = {"api_token": "apitok", "vault_passphrase": "vaultpw",
                   "deepseek_key": KEY, "profile": profile}
        api_env, ui_env = dash.build_env(self.source, secrets)
        for env in (api_env, ui_env):
            self.assertEqual(env["ELIZA_ALLOWED_ORIGINS"], "https://phpretro.work.gd")
            self.assertEqual(env["ELIZA_ALLOWED_HOSTS"], "phpretro.work.gd")
            self.assertEqual(env["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"], "phpretro.work.gd")
            self.assertEqual(env["ELIZA_API_BIND"], "127.0.0.1")
            self.assertEqual(env["ELIZA_DISABLE_LOCAL_EMBEDDINGS"], "1")
            self.assertEqual(env["ELIZA_SKIP_LOCAL_VOICE_WARMUP"], "1")
        for name in ("OPENAI_API_KEY", "ELIZA_VAULT_PASSPHRASE", "ELIZA_API_TOKEN"):
            self.assertIn(name, api_env)
            self.assertNotIn(name, ui_env)
        self.assertEqual(api_env["OPENAI_BASE_URL"], "https://api.deepseek.com/v1")
        self.assertEqual(api_env["ENABLE_AUTONOMY"], "false")
        self.assertEqual(api_env["ELIZA_AGENT_ORCHESTRATOR"], "0")
        for tier in dash.TEXT_TIERS:
            self.assertEqual(api_env[f"OPENAI_{tier}_MODEL"], "deepseek-flash")

    # -- check / status (no inference, no launch) ---------------------------
    def test_check_does_not_launch_or_print_secrets(self):
        self._gate()
        self._secrets()
        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print") as output:
            code = dash.cmd_check(self.root, self.source)
        self.assertEqual(code, 0)
        popen.assert_not_called()
        payload = output.call_args.args[0]
        self.assertNotIn(SECRET, payload)
        self.assertNotIn(KEY, payload)
        self.assertIn('"onboarding": "ready"', payload)

    def test_check_reports_busy_loopback_port(self):
        self._gate()
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", dash.API_PORT))
        holder.listen(1)
        try:
            with self._patch_source(), mock.patch("builtins.print") as output:
                code = dash.cmd_check(self.root, self.source)
            self.assertEqual(code, 2)
            self.assertIn("busy", output.call_args.args[0])
        finally:
            holder.close()

    def test_status_awaiting_onboarding_vs_runtime_ready(self):
        with mock.patch("builtins.print") as output:
            self.assertEqual(dash.cmd_status(self.root), 0)
        self.assertIn("awaiting-onboarding", output.call_args.args[0])
        self._secrets()
        runtime = {"supervisorPid": os.getpid(), "supervisorStart": dash.pid_identity(os.getpid()),
                   "apiPid": os.getpid(), "apiStart": dash.pid_identity(os.getpid()),
                   "uiPid": os.getpid(), "uiStart": dash.pid_identity(os.getpid()),
                   "phase": "running", "stopAt": "2999-01-01T00:00:00+00:00"}
        dash.atomic_json(self.state / "runtime.json", runtime)
        with mock.patch("builtins.print") as output:
            self.assertEqual(dash.cmd_status(self.root), 0)
        self.assertIn("runtime-ready", output.call_args.args[0])

    # -- lock ---------------------------------------------------------------
    def test_duplicate_start_busy_in_separate_process(self):
        lock = self.state / "dashboard.lock"
        lock.touch()
        marker = self.root / "holder.ready"
        code = ("import fcntl,pathlib,time;fd=open(%r,'r+');fcntl.flock(fd,fcntl.LOCK_EX);"
                "pathlib.Path(%r).write_text('ready');time.sleep(3)") % (str(lock), str(marker))
        holder = subprocess.Popen([sys.executable, "-c", code])
        try:
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            with mock.patch.object(dash.subprocess, "Popen") as popen, \
                    mock.patch("builtins.print") as output:
                code = dash.cmd_start(self.root, self.source)
            self.assertEqual(code, 1)
            popen.assert_not_called()
            self.assertIn('"status": "busy"', output.call_args.args[0])
        finally:
            holder.terminate()
            holder.wait()

    # -- partial startup ----------------------------------------------------
    def test_partial_startup_cleans_first_child_group(self):
        self._secrets()
        self._gate()
        calls = []

        class RunningChild:
            pid = 54321
            returncode = None
            def poll(self):
                return None
            def wait(self, timeout=None):
                return 0

        def popen(argv, **kwargs):
            if not calls:
                calls.append("spawned")
                return RunningChild()
            raise OSError("second child failed")

        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(dash.os, "killpg", side_effect=lambda pid, sig: calls.append(sig)), \
                mock.patch("builtins.print"):
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        self.assertIn(signal.SIGTERM, calls)
        self.assertIn(signal.SIGKILL, calls)
        record = json.loads((self.state / "runtime.json").read_text())
        self.assertEqual(record["phase"], "partial-startup-failed")

    # -- process-group lifecycle (real processes) ---------------------------
    def test_terminate_group_stops_leader_and_grandchild(self):
        pid_file = self.root / "grandchild.pid"
        code = ("import subprocess,signal,time;"
                "g=subprocess.Popen(['python3','-c',%r]);open(%r,'w').write(str(g.pid));"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
                % ("import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)",
                   str(pid_file)))
        proc = subprocess.Popen(["python3", "-c", code], start_new_session=True)
        try:
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            grandchild = int(pid_file.read_text())
            dash.terminate_group(proc, 0.05)
            self.assertIsNotNone(proc.poll())
            for _ in range(100):
                if proc_state(grandchild) in ("Z", "gone"):
                    break
                time.sleep(0.02)
            self.assertIn(proc_state(grandchild), ("Z", "gone"))
        finally:
            if proc.poll() is None:
                dash.terminate_group(proc, 0.05)

    def test_stop_verifies_start_ticks_and_spares_reused_pid(self):
        child = subprocess.Popen(["python3", "-c", "import time;time.sleep(30)"],
                                 start_new_session=True)
        try:
            runtime = {"apiPid": child.pid, "apiStart": "1",  # mismatched -> reused PID
                       "uiPort": dash.UI_PORT, "phase": "running"}
            dash.atomic_json(self.state / "runtime.json", runtime)
            with mock.patch("builtins.print"):
                self.assertEqual(dash.cmd_stop(self.root), 0)
            time.sleep(0.1)
            self.assertIsNone(child.poll())  # identity mismatch -> not killed
        finally:
            child.kill()
            child.wait()

    def test_stop_kills_identity_verified_group(self):
        child = subprocess.Popen(["python3", "-c", "import time;time.sleep(30)"],
                                 start_new_session=True)
        runtime = {"apiPid": child.pid, "apiStart": dash.pid_identity(child.pid),
                   "phase": "running"}
        dash.atomic_json(self.state / "runtime.json", runtime)
        with mock.patch("builtins.print"):
            self.assertEqual(dash.cmd_stop(self.root), 0)
        child.wait(timeout=10)
        self.assertIsNotNone(child.poll())

    def test_stop_without_runtime_is_clean(self):
        with mock.patch("builtins.print") as output:
            self.assertEqual(dash.cmd_stop(self.root), 0)
        self.assertIn("no-runtime", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
