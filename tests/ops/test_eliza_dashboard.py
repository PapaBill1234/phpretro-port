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

# Regression fixture for the generated DeepSeek chat-compat module. Run under
# the available Bun/Node runtimes when present; it never hits the network.
COMPAT_FIXTURE = r'''
import assert from 'node:assert/strict';
import {compatibleFetch} from './deepseek-chat-compat.mjs';
const calls=[]; const response=new Response('data: chunk\n\n');
const original=(...args)=>{calls.push(args);return Promise.resolve(response)};
const send=compatibleFetch(original);const signal=new AbortController().signal;
const init={method:'POST',body:JSON.stringify({model:'deepseek-flash',tool_choice:'required',messages:[]}),headers:{authorization:'Bearer fixture','content-length':'1'},signal};
assert.equal(await send('https://api.deepseek.com/v1/chat/completions',init),response);
let t=calls.at(-1)[1];assert.deepEqual(JSON.parse(t.body).thinking,{type:'disabled'});assert.equal(JSON.parse(t.body).reasoning_effort,'none');assert.equal(JSON.parse(t.body).tool_choice,'required');assert.equal(t.signal,signal);assert.equal(t.headers.get('authorization'),'Bearer fixture');assert.equal(t.headers.has('content-length'),false);assert.equal(init.headers['content-length'],'1');
for(const [u,next] of [['https://example.com/v1/chat/completions',init],['http://api.deepseek.com/v1/chat/completions',init],['https://api.deepseek.com:444/v1/chat/completions',init],['https://api.deepseek.com/v1/chat/completions?x=1',init],['https://api.deepseek.com/v1/embeddings',init],['https://api.deepseek.com/v1/chat/completions',{...init,method:'GET'}],['https://api.deepseek.com/v1/chat/completions',{...init,body:'invalid'}],['https://api.deepseek.com/v1/chat/completions',{...init,body:JSON.stringify({model:'other'})}]]) {await send(u,next);assert.equal(calls.at(-1)[1],next);}
const error=new Error('fixture failure');await assert.rejects(compatibleFetch(()=>Promise.reject(error))('https://api.deepseek.com/v1/chat/completions',init),e=>e===error);
for(const body of ['null','[]','{}']) {const next={...init,body};await send('https://api.deepseek.com/v1/chat/completions',next);assert.equal(calls.at(-1)[1],next);}
const url=new URL('https://api.deepseek.com:443/chat/completions');await send(url,init);assert.equal(JSON.parse(calls.at(-1)[1].body).thinking.type,'disabled');
const request=new Request(url,{method:'POST',headers:{authorization:'Bearer inherited'},body:'untouched'});
await send(request,{method:'POST',body:init.body,redirect:'manual',signal});assert.equal(calls.at(-1)[1].headers.get('authorization'),'Bearer inherited');assert.equal(calls.at(-1)[1].redirect,'manual');assert.equal(request.bodyUsed,false);
const override={method:'GET',body:init.body};await send(request,override);assert.equal(calls.at(-1)[1],override);
const syncError=new Error('sync');assert.throws(()=>compatibleFetch(()=>{throw syncError})(url,init),e=>e===syncError);
let connected=false;const native=()=>Promise.resolve(response);native.preconnect=()=>{connected=true};compatibleFetch(native).preconnect();assert.equal(connected,true);
assert.equal(response.bodyUsed,false);assert.equal(await response.text(),'data: chunk\n\n');
const abort=new AbortController();const aborted=new DOMException('aborted','AbortError');const abortSend=compatibleFetch((input,next)=>new Promise((resolve,reject)=>next.signal.addEventListener('abort',()=>reject(aborted))));const pending=abortSend(url,{...init,signal:abort.signal});abort.abort();await assert.rejects(pending,e=>e===aborted);
console.log('PASS');
'''


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
        for name, value in (("api.token", "apitok"), ("vault.passphrase", "vaultpw-at-least-12")):
            path = self.state / name
            path.write_text(value)
            os.chmod(path, mode)
        envdir = self.root / "secrets"
        envdir.mkdir(mode=0o700, exist_ok=True)
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
        with mock.patch.object(dash, "probe_agent", return_value={"status": "runtime-ready", "ready": True, "canRespond": True}), mock.patch("builtins.print") as output:
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

    def test_stop_missing_identity_spares_actual_process(self):
        child = subprocess.Popen(["python3", "-c", "import time;time.sleep(30)"], start_new_session=True)
        try:
            dash.atomic_json(self.state / "runtime.json", {"apiPid": child.pid})
            with mock.patch("builtins.print"):
                dash.cmd_stop(self.root)
            self.assertIsNone(child.poll())
        finally:
            child.kill(); child.wait()

    def test_profile_symlink_is_rejected_before_chmod(self):
        self._secrets()
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        (self.state / "upstream-state").symlink_to(outside)
        with self.assertRaises(dash.DashboardError):
            dash.load_secrets(self.root, self.state)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_live_processes_alone_do_not_prove_agent_ready(self):
        self._secrets()
        dash.atomic_json(self.state / "runtime.json", {"apiPid": os.getpid(), "uiPid": os.getpid(),
                          "apiStart": dash.pid_identity(os.getpid()), "uiStart": dash.pid_identity(os.getpid()), "phase": "running"})
        with mock.patch.object(dash, "probe_agent", return_value=None), mock.patch("builtins.print") as output:
            dash.cmd_status(self.root)
        self.assertFalse(json.loads(output.call_args.args[0])["ready"])

    def test_one_live_child_is_never_runtime_ready(self):
        self._secrets()
        dash.atomic_json(self.state / "runtime.json", {"apiPid": os.getpid(), "apiStart": dash.pid_identity(os.getpid()), "phase": "running"})
        with mock.patch.object(dash, "probe_agent") as probe, mock.patch("builtins.print") as output:
            dash.cmd_status(self.root)
        probe.assert_not_called()
        self.assertFalse(json.loads(output.call_args.args[0])["ready"])

    def test_agent_probe_actual_http_requires_ready_and_redacts_extra_fields(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading
        self._secrets()
        bodies = [
            {"state": "running", "canRespond": False, "startup": {"phase": "ready"}},
            {"state": "running", "canRespond": True, "startup": {"phase": "booting"}},
            {"state": "running", "canRespond": True, "startup": {"phase": "ready"}, "private": SECRET},
        ]
        auth = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                auth.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(bodies.pop(0)).encode())
        server = HTTPServer(("127.0.0.1", dash.API_PORT), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            self.assertFalse(dash.probe_agent(self.state)["ready"])
            self.assertFalse(dash.probe_agent(self.state)["ready"])
            result = dash.probe_agent(self.state)
            self.assertTrue(result["ready"])
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertEqual(auth, ["Bearer apitok"] * 3)
        finally:
            server.shutdown();thread.join();server.server_close()

    def test_dotenv_duplicate_or_malformed_key_is_rejected(self):
        for text in ["DEEPSEEK_API_KEY=one\nDEEPSEEK_API_KEY=two", "DEEPSEEK_API_KEY='unterminated"]:
            with self.assertRaises(dash.DashboardError):dash.parse_dotenv(text)
        self.assertEqual(dash.parse_dotenv("export DEEPSEEK_API_KEY='safe value' # comment")["DEEPSEEK_API_KEY"], "safe value")

    # -- deepseek chat-compat transport ------------------------------------
    def test_generated_compat_module_matches_embedded_source(self):
        path = dash.write_compat_module(self.state)
        self.assertEqual(path.read_bytes(), dash.COMPAT_MODULE_SOURCE.encode())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(path.is_symlink())
        self.assertEqual(dash.write_compat_module(self.state).read_bytes(),
                         dash.COMPAT_MODULE_SOURCE.encode())  # idempotent preserve

    def test_write_compat_module_refuses_symlink_and_foreign_bytes(self):
        target = self.root / "outside.mjs"
        target.write_text("foreign")
        link = self.state / dash.COMPAT_MODULE_NAME
        link.symlink_to(target)
        with self.assertRaises(dash.DashboardError):
            dash.write_compat_module(self.state)
        link.unlink()
        tampered = self.state / dash.COMPAT_MODULE_NAME
        tampered.write_text("// tampered compat source\n")
        os.chmod(tampered, 0o600)
        with self.assertRaises(dash.DashboardError):
            dash.write_compat_module(self.state)
        # a byte-matching copy is preserved and its mode tightened to 0600
        tampered.write_bytes(dash.COMPAT_MODULE_SOURCE.encode())
        os.chmod(tampered, 0o644)
        self.assertEqual(dash.write_compat_module(self.state).read_bytes(),
                         dash.COMPAT_MODULE_SOURCE.encode())
        self.assertEqual(os.stat(tampered).st_mode & 0o777, 0o600)

    def test_compat_module_is_preloaded_into_api_only(self):
        compat = dash.write_compat_module(self.state)
        api, ui = dash.child_commands(self.source, compat)
        api, ui = list(api), list(ui)
        self.assertIn("--preload", api)
        preload = api[api.index("--preload") + 1]
        self.assertTrue(Path(preload).is_absolute())
        self.assertEqual(Path(preload).resolve(), compat.resolve())
        self.assertLess(api.index("--preload"), api.index("packages/app/src/entry.ts"))
        self.assertNotIn("--preload", ui)
        self.assertNotIn(str(compat.resolve()), " ".join(ui))
        profile = self.root / "prof"
        profile.mkdir()
        secrets = {"api_token": "t", "vault_passphrase": "p",
                   "deepseek_key": KEY, "profile": profile}
        _, ui_env = dash.build_env(self.source, secrets)
        self.assertFalse(any(str(compat.resolve()) in value for value in ui_env.values()))

    def test_check_reports_compat_module_state(self):
        self._gate()
        self._secrets()
        with self._patch_source(), mock.patch("builtins.print") as output:
            self.assertEqual(dash.cmd_check(self.root, self.source), 0)
        self.assertIn('"compat_module": "absent"', output.call_args.args[0])
        tampered = self.state / dash.COMPAT_MODULE_NAME
        tampered.write_text("// tampered\n")
        os.chmod(tampered, 0o600)
        with self._patch_source(), mock.patch("builtins.print") as output:
            self.assertEqual(dash.cmd_check(self.root, self.source), 2)
        self.assertIn("differs from the reviewed source", output.call_args.args[0])

    def test_check_detects_saved_incompatible_routing_without_dumping(self):
        self._gate()
        self._secrets()
        profile = self.state / "upstream-state"
        profile.mkdir(mode=0o700, exist_ok=True)
        marker = "ROUTING-PRIVATE-MARKER"
        (profile / "eliza.json").write_text(json.dumps(
            {"models": {"provider": "deepseek"}, "note": marker}))
        os.chmod(profile / "eliza.json", 0o600)
        with self._patch_source(), mock.patch("builtins.print") as output:
            code = dash.cmd_check(self.root, self.source)
        payload = output.call_args.args[0]
        self.assertEqual(code, 2)
        self.assertIn("non-direct DeepSeek route", payload)
        self.assertNotIn(marker, payload)
        self.assertIn('"routing": "incompatible"', payload)

    def test_direct_openai_route_is_compatible(self):
        profile = self.state / "upstream-state"
        profile.mkdir(mode=0o700, exist_ok=True)
        for config in ({"provider": "openai"},
                       {"models": {"provider": "openai-compatible", "model": "deepseek-flash"}}):
            (profile / "eliza.json").write_text(json.dumps(config))
            os.chmod(profile / "eliza.json", 0o600)
            self.assertFalse(dash.detect_incompatible_routing(self.state))
        for config in ({"provider": "deepseek"},
                       {"models": {"model": "deepseek/deepseek-flash"}}):
            (profile / "eliza.json").write_text(json.dumps(config))
            os.chmod(profile / "eliza.json", 0o600)
            self.assertTrue(dash.detect_incompatible_routing(self.state))

    def test_start_blocks_incompatible_routing_without_dispatch(self):
        self._secrets()
        self._gate()
        profile = self.state / "upstream-state"
        profile.mkdir(mode=0o700, exist_ok=True)
        (profile / "eliza.json").write_text(json.dumps({"catalog": "deepseek"}))
        os.chmod(profile / "eliza.json", 0o600)
        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print"):
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        popen.assert_not_called()

    def test_generated_module_passes_runtime_fixture(self):
        runtimes = [(name, str(dash.CONFIG_SOURCE[name])) for name in ("bun", "node")
                    if Path(dash.CONFIG_SOURCE[name]).is_file()]
        if not runtimes:
            self.skipTest("no Bun/Node runtime available")
        dash.write_compat_module(self.state)
        fixture = self.state / "compat-fixture.mjs"
        fixture.write_text(COMPAT_FIXTURE)
        for name, runtime in runtimes:
            with self.subTest(runtime=name):
                result = subprocess.run([runtime, str(fixture)], cwd=str(self.state),
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
