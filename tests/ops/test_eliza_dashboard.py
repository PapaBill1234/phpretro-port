import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "eliza-dashboard.py"
spec = importlib.util.spec_from_file_location("eliza_dashboard", SCRIPT)
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

SECRET = "SUPERSECRETVALUE-1234567890"
KEY = "sk-test-deepseek-key"
DIRECT_CONFIG = {"agents": {"defaults": {"model": {"primary": "openai/deepseek-flash"}}}}
DIRECT_ROUTE = {"serviceRouting": {"llmText": {"backend": "openai", "transport": "direct", "primaryModel": "deepseek-flash"}}}
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

    # -- managed (integration) source fixtures ------------------------------
    def _managed_source(self):
        """A registered source whose paths live only under the isolated root."""
        private = self.root / ".builder" / "upstream" / "runtimes"
        return {
            "name": "eliza-upstream",
            "managed": True,
            "path": self.root / "builder" / "eliza" / "upstream",
            "pin": REPO_PIN,
            "bun": private / "bun" / "package" / "bin" / "bun",
            "bun_version": BUN_VERSION,
            "node": private / "node-v24.15.0-linux-x64" / "bin" / "node",
            "node_version": NODE_VERSION,
            "api_entry": Path("packages/app/src/entry.ts"),
            "vite": Path("node_modules/vite/bin/vite.js"),
            "api_cwd": Path("."),
            "ui_cwd": Path("packages/app"),
        }

    def _managed_tree(self, source):
        entry = source["path"] / source["api_entry"]
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// entry")
        vite = source["path"] / source["vite"]
        vite.parent.mkdir(parents=True, exist_ok=True)
        vite.write_text("// vite")

    def _managed_state(self, source, pin_ok=True, runtime_ok=True,
                       deps_ok=True, node_version=None, bun_version=None):
        return {
            "pin": {"ok": pin_ok, "revision": REPO_PIN if pin_ok else "b" * 40},
            "runtime": {"ok": runtime_ok,
                        "node": NODE_VERSION if node_version is None else node_version,
                        "bun": BUN_VERSION if bun_version is None else bun_version},
            "dependencies": {"ok": deps_ok},
            "readiness": pin_ok and runtime_ok and deps_ok,
        }

    def _installer(self, state, verified):
        """Write a fake reviewed installer exposing check/verified_runtime."""
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        body = ("import json\nSTATE = json.loads(%r)\nVERIFIED = json.loads(%r)\n"
                "def check(root):\n    return STATE\n\n"
                "def verified_runtime(root, kind):\n    return VERIFIED.get(kind)\n"
                % (json.dumps(state), json.dumps({k: str(v) for k, v in verified.items()})))
        (scripts / "eliza-upstream.py").write_text(body)

    def _verified(self, source):
        return {"node": source["node"], "bun": source["bun"]}

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

    # -- default registered source (integration ROOT/installer) -------------
    def test_default_source_uses_root_relative_integration_paths(self):
        installer = dash.load_eliza_upstream(dash.ROOT)
        self.assertEqual(dash.CONFIG_SOURCE["node"], installer.node_bin(dash.ROOT))
        self.assertEqual(dash.CONFIG_SOURCE["bun"], installer.bun_bin(dash.ROOT))
        self.assertTrue(dash.CONFIG_SOURCE["managed"])
        self.assertEqual(dash.CONFIG_SOURCE["path"], dash.ROOT / dash.UPSTREAM_SOURCE_REL)
        self.assertEqual(dash.CONFIG_SOURCE["path"], dash.ROOT / "builder" / "eliza" / "upstream")
        self.assertEqual(dash.CONFIG_SOURCE["node"],
                         dash.ROOT / ".builder" / "upstream" / "runtimes"
                         / "node-v24.15.0-linux-x64" / "bin" / "node")
        self.assertEqual(dash.CONFIG_SOURCE["bun"],
                         dash.ROOT / ".builder" / "upstream" / "runtimes" / "bun"
                         / "package" / "bin" / "bun")
        # the absolute research checkout and its private runtime tree are gone
        for value in dash.CONFIG_SOURCE.values():
            self.assertNotIn("work/research/eliza", str(value))
            self.assertNotIn(".phpretro-builder", str(value))

    def test_managed_source_validated_through_installer_not_direct_probes(self):
        source = self._managed_source()
        self._managed_tree(source)
        self._installer(self._managed_state(source), self._verified(source))
        with mock.patch.object(dash.subprocess, "check_output") as probe, \
                mock.patch.object(dash, "load_eliza_upstream",
                                  wraps=dash.load_eliza_upstream) as loader:
            result = dash.validate_source(source, self.root)
        probe.assert_not_called()  # no raw version probe on an unverified path
        loader.assert_called_once()
        self.assertEqual(result["pin"], REPO_PIN)
        self.assertEqual(result["node"], NODE_VERSION)
        self.assertEqual(result["bun"], BUN_VERSION)
        self.assertTrue(result["readiness"])

    def test_managed_source_fails_closed_without_installer(self):
        source = self._managed_source()
        self._managed_tree(source)
        with self.assertRaises(dash.DashboardError):
            dash.validate_source(source, self.root)  # no scripts/eliza-upstream.py

    def test_managed_source_rejects_missing_runtime_pin_or_dependencies(self):
        source = self._managed_source()
        self._managed_tree(source)
        cases = {
            "pin": self._managed_state(source, pin_ok=False),
            "runtime": self._managed_state(source, runtime_ok=False),
            "dependencies": self._managed_state(source, deps_ok=False),
            "node version": self._managed_state(source, node_version="v0.0.0"),
            "bun version": self._managed_state(source, bun_version="0.0.0"),
        }
        for label, state in cases.items():
            with self.subTest(case=label):
                self._installer(state, self._verified(source))
                with self.assertRaises(dash.DashboardError):
                    dash.validate_source(source, self.root)

    def test_managed_source_rejects_unverified_runtime_bytes(self):
        source = self._managed_source()
        self._managed_tree(source)
        # installer check passes, but verified_runtime returns None (bytes do
        # not match the recorded digest) -> the binary must never be trusted
        self._installer(self._managed_state(source), {})
        with self.assertRaises(dash.DashboardError) as ctx:
            dash.validate_source(source, self.root)
        self.assertIn("verified", str(ctx.exception))

    def test_managed_source_rejects_non_canonical_runtime_path(self):
        source = self._managed_source()
        self._managed_tree(source)
        elsewhere = self.root / "elsewhere-bun"
        self._installer(self._managed_state(source),
                        {"node": source["node"], "bun": elsewhere})
        with self.assertRaises(dash.DashboardError) as ctx:
            dash.validate_source(source, self.root)
        self.assertIn("canonical", str(ctx.exception))

    def test_managed_source_rejects_foreign_source_path(self):
        source = self._managed_source()
        self._managed_tree(source)
        source["path"] = self.root / "somewhere" / "else"
        self._installer(self._managed_state(source), self._verified(source))
        with self.assertRaises(dash.DashboardError):
            dash.validate_source(source, self.root)

    def test_injected_source_stays_isolated_from_installer(self):
        # a managed installer present in the tree must never be consulted for an
        # injected fixture; the fixture keeps its own explicit paths
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts" / "eliza-upstream.py").write_text(
            "def check(root):\n    raise AssertionError('consulted')\n")
        with self._patch_source(), \
                mock.patch.object(dash, "load_eliza_upstream") as loader:
            result = dash.validate_source(self.source, self.root)
        loader.assert_not_called()
        self.assertEqual(result["pin"], REPO_PIN)
        self.assertEqual(result["bun"], BUN_VERSION)

    def test_check_truthfully_requires_setup_prereqs_for_managed_source(self):
        source = self._managed_source()
        self._managed_tree(source)
        self._gate()
        self._secrets()
        # missing setup dependencies -> check reports not-ok and does not launch
        self._installer(self._managed_state(source, deps_ok=False), self._verified(source))
        with mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print") as output:
            code = dash.cmd_check(self.root, source)
        self.assertEqual(code, 2)
        popen.assert_not_called()
        self.assertIn("dependencies", output.call_args.args[0])

    def test_check_ok_for_fully_set_up_managed_source(self):
        source = self._managed_source()
        self._managed_tree(source)
        self._gate()
        self._secrets()
        self._installer(self._managed_state(source), self._verified(source))
        with mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print") as output:
            code = dash.cmd_check(self.root, source)
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertIn('"onboarding": "ready"', output.call_args.args[0])

    def test_start_blocks_when_managed_prereqs_are_missing(self):
        source = self._managed_source()
        self._managed_tree(source)
        self._gate()
        self._secrets()
        self._installer(self._managed_state(source, runtime_ok=False), self._verified(source))
        with mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, source)
        self.assertEqual(code, 2)
        popen.assert_not_called()
        self.assertIn('"status": "blocked"', output.call_args.args[0])

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
        executions = []

        class RunningChild:
            pid = 54321
            returncode = None
            def poll(self):
                return None
            def wait(self, timeout=None):
                return 0

        def popen(argv, **kwargs):
            executions.append((argv, kwargs["cwd"]))
            if not calls:
                calls.append("spawned")
                return RunningChild()
            raise OSError("second child failed")

        with self._patch_source(), mock.patch.object(dash.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(dash.os, "killpg", side_effect=lambda pid, sig: calls.append(sig)), \
                mock.patch("builtins.print"):
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        self.assertEqual(Path(executions[0][1]), self.source_dir / self.source["api_cwd"])
        self.assertEqual(Path(executions[1][1]), self.source_dir / self.source["ui_cwd"])
        self.assertIn(str(self.source_dir / self.source["vite"]), executions[1][0])
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
        outside.chmod(0o755)
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
        for config in (DIRECT_CONFIG,
                       DIRECT_ROUTE):
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

    # -- reviewed-defect regressions ---------------------------------------
    def test_bounded_grace_clamps_to_cutoff(self):
        now = time.time()
        self.assertEqual(dash.bounded_grace(3.0, None), 3.0)
        self.assertEqual(dash.bounded_grace(3.0, now + 100), 3.0)
        self.assertAlmostEqual(dash.bounded_grace(3.0, now + 1.0), 1.0, delta=0.3)
        self.assertEqual(dash.bounded_grace(3.0, now - 5.0), 0.0)

    def test_open_private_log_rejects_fifo_without_reader_promptly(self):
        fifo = self.state / "api.log"
        os.mkfifo(fifo, 0o640)
        fifo.chmod(0o640)
        self.assertTrue(stat.S_ISFIFO(os.stat(fifo).st_mode))
        started = time.monotonic()
        with self.assertRaises(dash.DashboardError):
            dash.open_private_log(fifo)
        self.assertLess(time.monotonic() - started, 5.0)
        # mode of the unsafe FIFO is never changed
        self.assertEqual(os.stat(fifo).st_mode & 0o777, 0o640)
        # and the rejection cannot hang: bounded subprocess watch-dog
        code = ("import importlib.util,sys\n"
                "spec=importlib.util.spec_from_file_location('d',%r)\n"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                "from pathlib import Path\n"
                "try:\n"
                "    m.open_private_log(Path(%r));sys.exit(9)\n"
                "except m.DashboardError:\n"
                "    sys.exit(0)\n" % (str(SCRIPT), str(fifo)))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.stat(fifo).st_mode & 0o777, 0o640)

    def test_routing_fails_closed_for_invalid_or_unsafe_config(self):
        profile = self.state / "upstream-state"
        profile.mkdir(mode=0o700, exist_ok=True)
        cfg = profile / "eliza.json"
        # absence is the only legitimate first-run compatible case
        self.assertFalse(dash.detect_incompatible_routing(self.state))
        # empty / non-object / malformed existing configs fail closed
        for body in ("{}", "null", "[]", '""', "123", "{not json"):
            cfg.write_text(body)
            os.chmod(cfg, 0o600)
            self.assertTrue(dash.detect_incompatible_routing(self.state), body)
        # an object with no selection at all also fails closed
        cfg.write_text(json.dumps({"note": "x"}))
        os.chmod(cfg, 0o600)
        self.assertTrue(dash.detect_incompatible_routing(self.state))
        # unsafe: non-private mode, symlink, directory, unreadable existing file
        cfg.write_text(json.dumps(DIRECT_CONFIG))
        os.chmod(cfg, 0o644)
        self.assertTrue(dash.detect_incompatible_routing(self.state))
        os.chmod(cfg, 0o600)
        target = self.root / "outside.json"
        target.write_text(json.dumps(DIRECT_CONFIG))
        cfg.unlink(); cfg.symlink_to(target)
        self.assertTrue(dash.detect_incompatible_routing(self.state))
        cfg.unlink(); cfg.mkdir()
        self.assertTrue(dash.detect_incompatible_routing(self.state))
        cfg.rmdir(); cfg.write_text(json.dumps(DIRECT_CONFIG))
        os.chmod(cfg, 0o000)
        try:
            self.assertTrue(dash.detect_incompatible_routing(self.state))
        finally:
            os.chmod(cfg, 0o600)
        # native DeepSeek and unknown selections fail closed
        for body in ({"serviceRouting": {"llmText": {"backend": "openai", "transport": "direct", "primaryModel": []}}},
                     {"provider": "openai"}, {"models": {"provider": "openai", "model": "deepseek-flash"}},
                     {"plugins": {"entries": {"unused": {"config": {"provider": "openai"}}}}},
                     {"provider": "deepseek"},
                     {"models": {"model": "deepseek/deepseek-flash"}},
                     {"provider": "some-unknown-provider"}):
            cfg.write_text(json.dumps(body))
            os.chmod(cfg, 0o600)
            self.assertTrue(dash.detect_incompatible_routing(self.state), body)
        # an explicit supported direct selection is compatible
        for body in (DIRECT_CONFIG,
                     DIRECT_ROUTE):
            cfg.write_text(json.dumps(body))
            os.chmod(cfg, 0o600)
            self.assertFalse(dash.detect_incompatible_routing(self.state), body)

    def test_start_rechecks_gate_after_slow_validation(self):
        self._secrets()
        self._gate()
        calls = {"n": 0}
        def fake_window(root):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"allowed": True, "stop_at": "2999-01-01T00:00:00+00:00"}
            raise dash.DashboardError("coding window is closed")
        with self._patch_source(), \
                mock.patch.object(dash, "require_window", side_effect=fake_window), \
                mock.patch.object(dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        popen.assert_not_called()
        self.assertGreaterEqual(calls["n"], 2)  # gate re-checked after validation
        self.assertIn('"status": "blocked"', output.call_args.args[0])

    def test_start_preserves_original_cutoff_over_later_gate_window(self):
        self._secrets()
        self._gate()
        early = "2998-01-01T00:00:00+00:00"
        late = "2999-01-01T00:00:00+00:00"
        seq = [{"allowed": True, "stop_at": early}, {"allowed": True, "stop_at": late}]
        def fake_window(root):
            return seq.pop(0) if seq else {"allowed": True, "stop_at": late}
        class ExitedChild:
            pid = 71000
            returncode = 0
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
        with self._patch_source(), \
                mock.patch.object(dash, "require_window", side_effect=fake_window), \
                mock.patch.object(dash.subprocess, "Popen", return_value=ExitedChild()), \
                mock.patch.object(dash.os, "killpg", side_effect=lambda pid, sig: None), \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 0)
        record = json.loads((self.state / "runtime.json").read_text())
        self.assertEqual(record["stopAt"], early)  # later window cannot extend it
        self.assertIn(early, output.call_args.args[0])

    def test_start_second_spawn_blocked_when_clock_crosses_cutoff(self):
        from datetime import datetime
        self._secrets()
        stop_at = "2970-01-01T00:00:00+00:00"
        self._gate(stop_at=stop_at)
        cutoff = datetime.fromisoformat(stop_at).timestamp()
        clock = {"t": cutoff - 50.0}
        spawned = []
        class RunningChild:
            pid = 72000
            returncode = None
            def poll(self):
                return None
            def wait(self, timeout=None):
                return 0
        def popen(argv, **kwargs):
            spawned.append(argv)
            clock["t"] = cutoff - 2.0  # cross into the 5s no-launch margin
            return RunningChild()
        captured = {}
        real_cleanup = dash.cleanup_children
        def spy(children, term_grace=3.0):
            captured["grace"] = term_grace
            captured["now"] = clock["t"]
            return real_cleanup(children, term_grace)
        with self._patch_source(), \
                mock.patch.object(dash.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(dash.time, "time", lambda: clock["t"]), \
                mock.patch.object(dash.time, "sleep",
                                  lambda sec: clock.__setitem__("t", clock["t"] + sec)), \
                mock.patch.object(dash, "cleanup_children", side_effect=spy), \
                mock.patch.object(dash.os, "killpg", side_effect=lambda pid, sig: None), \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        self.assertEqual(len(spawned), 1)  # only the first child launched
        self.assertIn('"status": "blocked"', output.call_args.args[0])
        # cleanup deadline never extends past the original cutoff
        self.assertLessEqual(captured["now"] + captured["grace"], cutoff + 1e-6)
        self.assertLess(captured["grace"], 3.0)

    def test_start_exception_cleanup_bounded_by_cutoff(self):
        from datetime import datetime
        self._secrets()
        stop_at = "2971-01-01T00:00:00+00:00"
        self._gate(stop_at=stop_at)
        cutoff = datetime.fromisoformat(stop_at).timestamp()
        clock = {"t": cutoff - 10.0}
        captured = {}
        real_cleanup = dash.cleanup_children
        def spy(children, term_grace=3.0):
            captured["grace"] = term_grace
            captured["now"] = clock["t"]
            return real_cleanup(children, term_grace)
        with self._patch_source(), \
                mock.patch.object(dash.subprocess, "Popen",
                                  side_effect=OSError("spawn failed")), \
                mock.patch.object(dash.time, "time", lambda: clock["t"]), \
                mock.patch.object(dash, "cleanup_children", side_effect=spy), \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(code, 2)
        self.assertIn('"status": "failed"', output.call_args.args[0])
        record = json.loads((self.state / "runtime.json").read_text())
        self.assertEqual(record["phase"], "partial-startup-failed")
        self.assertLessEqual(captured["now"] + captured["grace"], cutoff + 1e-6)

    def test_start_signal_cannot_allow_second_spawn(self):
        self._secrets()
        self._gate()
        spawned = []
        class RunningChild:
            pid = 73000
            returncode = None
            def poll(self):
                return None
            def wait(self, timeout=None):
                return 0
        def popen(argv, **kwargs):
            spawned.append(argv)
            # deliver a stop request right after the first launch
            os.kill(os.getpid(), signal.SIGTERM)
            return RunningChild()
        with self._patch_source(), \
                mock.patch.object(dash.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(dash.os, "killpg", side_effect=lambda pid, sig: None), \
                mock.patch("builtins.print") as output:
            code = dash.cmd_start(self.root, self.source)
        self.assertEqual(len(spawned), 1)
        self.assertIn('"status": "blocked"', output.call_args.args[0])

    def test_stop_cleanup_bounded_by_recorded_cutoff(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"],
            start_new_session=True)
        try:
            runtime = {"apiPid": child.pid, "apiStart": dash.pid_identity(child.pid),
                       "phase": "running", "stopAt": "2000-01-01T00:00:00+00:00"}
            dash.atomic_json(self.state / "runtime.json", runtime)
            captured = {}
            real = dash.terminate_pid_group
            def spy(pid, expected, grace):
                captured["grace"] = grace
                return real(pid, expected, grace)
            with mock.patch.object(dash, "terminate_pid_group", side_effect=spy), \
                    mock.patch("builtins.print"):
                self.assertEqual(dash.cmd_stop(self.root), 0)
            # cutoff already long past -> bounded grace collapses to 0 (immediate KILL)
            self.assertEqual(captured["grace"], 0.0)
            child.wait(timeout=10)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_terminate_pid_group_never_kills_replaced_pid(self):
        signals = []
        identities = ["111", "111", "222"]  # stable snapshot, changes after TERM
        def fake_identity(pid):
            return identities.pop(0) if identities else "222"
        with mock.patch.object(dash, "pid_identity", side_effect=fake_identity), \
                mock.patch.object(dash, "_group_members", return_value={}), \
                mock.patch.object(dash.os, "killpg",
                                  side_effect=lambda pid, sig: signals.append(sig)), \
                mock.patch.object(dash.os, "kill",
                                  side_effect=lambda pid, sig: signals.append((pid, sig))), \
                mock.patch.object(dash.time, "time", return_value=0):
            self.assertTrue(dash.terminate_pid_group(4321, "111", 3.0))
        self.assertIn(signal.SIGTERM, signals)
        self.assertNotIn(signal.SIGKILL, signals)  # replacement PID is never KILLed
        self.assertNotIn((4321, signal.SIGKILL), signals)

    def test_terminate_pid_group_reclaims_known_orphan_after_leader_reaped(self):
        events = []
        leader = {"seq": ["555", "555", None]}  # stable snapshot, then reaped
        orphan = {"seq": ["777", "777", "777"]}
        def fake_identity(pid):
            if pid == 5000:
                return leader["seq"].pop(0) if leader["seq"] else None
            if pid == 6000:
                return orphan["seq"].pop(0) if orphan["seq"] else "777"
            return None
        members = {5000: "555", 6000: "777"}
        with mock.patch.object(dash, "pid_identity", side_effect=fake_identity), \
                mock.patch.object(dash, "_group_members", return_value=dict(members)), \
                mock.patch.object(dash.os, "killpg",
                                  side_effect=lambda pid, sig: events.append(("group", sig))), \
                mock.patch.object(dash.os, "kill",
                                  side_effect=lambda pid, sig: events.append(("pid", pid, sig))), \
                mock.patch.object(dash.time, "time", return_value=0):
            self.assertTrue(dash.terminate_pid_group(5000, "555", 3.0))
        self.assertIn(("group", signal.SIGTERM), events)
        self.assertNotIn(("group", signal.SIGKILL), events)  # reaped leader: no group KILL
        self.assertIn(("pid", 6000, signal.SIGKILL), events)  # verified orphan reclaimed
        self.assertNotIn(("pid", 5000, signal.SIGKILL), events)

    def test_terminate_pid_group_discards_snapshot_after_leader_replacement(self):
        with mock.patch.object(dash, "pid_identity", side_effect=["111", "222"]), \
                mock.patch.object(dash, "_group_members", return_value={4321: "222", 5500: "333"}), \
                mock.patch.object(dash, "_killpg") as group_signal, \
                mock.patch.object(dash, "_kill_verified") as member_signal:
            self.assertFalse(dash.terminate_pid_group(4321, "111", 3.0))
        group_signal.assert_not_called()
        member_signal.assert_not_called()

    def test_terminate_pid_group_reclaims_real_descendants(self):
        pid_file = self.root / "gc.pid"
        leader_code = ("import subprocess,signal,time;"
                       "g=subprocess.Popen(['python3','-c',%r]);open(%r,'w').write(str(g.pid));"
                       "time.sleep(0.1)"
                       % ("import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)",
                          str(pid_file)))
        proc = subprocess.Popen(["python3", "-c", leader_code], start_new_session=True)
        try:
            for _ in range(150):
                if pid_file.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            grandchild = int(pid_file.read_text())
            start = dash.pid_identity(proc.pid)
            reaper = threading.Thread(target=proc.wait)
            reaper.start()
            self.assertTrue(dash.terminate_pid_group(proc.pid, start, 0.5))
            reaper.join(timeout=2)
            self.assertFalse(reaper.is_alive())
            self.assertIsNone(dash.pid_identity(proc.pid))
            for _ in range(200):
                if proc_state(grandchild) in ("Z", "gone"):
                    break
                time.sleep(0.02)
            self.assertIn(proc_state(grandchild), ("Z", "gone"))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    # -- local-auth credentials (create once, load read-only) ---------------
    def _noncompat_profile(self, local_auth):
        return dash.LaunchProfile(
            name="noncompat", compat=False, local_auth=local_auth,
            api_host="127.0.0.1", api_port=1, ui_port=2,
            allowed_origins="o", allowed_hosts="h",
            home=None, state_relative=Path(".x"))

    def test_noncompat_local_auth_injects_vault_env_only_when_enabled(self):
        secrets = {"profile": Path("/tmp/runtime"),
                   "api_token": "A" * 43, "vault_passphrase": "B" * 43}
        api_env, ui_env = dash.build_env(self.source, secrets, self._noncompat_profile(True))
        self.assertEqual(api_env["ELIZA_API_TOKEN"], "A" * 43)
        self.assertEqual(api_env["ELIZA_VAULT_PASSPHRASE"], "B" * 43)
        self.assertEqual(api_env["ELIZA_VAULT_DISABLE_KEYCHAIN"], "1")
        for name in ("ELIZA_API_TOKEN", "ELIZA_VAULT_PASSPHRASE",
                     "ELIZA_VAULT_DISABLE_KEYCHAIN"):
            self.assertNotIn(name, ui_env)  # UI never receives a secret

    def test_noncompat_without_local_auth_injects_no_vault_env(self):
        secrets = {"profile": Path("/tmp/runtime")}
        api_env, ui_env = dash.build_env(self.source, secrets, self._noncompat_profile(False))
        for name in ("ELIZA_API_TOKEN", "ELIZA_VAULT_PASSPHRASE",
                     "ELIZA_VAULT_DISABLE_KEYCHAIN"):
            self.assertNotIn(name, api_env)
            self.assertNotIn(name, ui_env)

    def test_create_and_load_credentials_are_canonical_and_strict(self):
        dash.create_credentials(self.state)
        creds = dash.load_local_credentials(self.state)
        for name in dash.SECRET_FILES:
            self.assertEqual(stat.S_IMODE((self.state / name).lstat().st_mode), 0o600)
            self.assertEqual(len(creds[name.replace(".", "_")]), 43)
        before = {n: (self.state / n).read_bytes() for n in dash.SECRET_FILES}
        dash.create_credentials(self.state)  # reuse unchanged, never rotate
        self.assertEqual(before, {n: (self.state / n).read_bytes() for n in dash.SECRET_FILES})
        (self.state / "api.token").write_bytes(b"x" * 43 + b"\n")  # trailing newline
        os.chmod(self.state / "api.token", 0o600)
        with self.assertRaises(dash.DashboardError) as caught:
            dash.load_local_credentials(self.state)
        self.assertNotIn("x" * 43, str(caught.exception))  # value-free error

    def test_create_credentials_never_repairs_an_unsafe_dir_or_file(self):
        os.chmod(self.state, 0o755)
        with self.assertRaises(dash.DashboardError):
            dash.create_credentials(self.state)
        self.assertEqual(stat.S_IMODE(self.state.lstat().st_mode), 0o755)
        os.chmod(self.state, 0o700)
        dash.create_credentials(self.state)
        (self.state / "vault.passphrase").chmod(0o644)
        with self.assertRaises(dash.DashboardError):
            dash.create_credentials(self.state)
        self.assertEqual(stat.S_IMODE((self.state / "vault.passphrase").lstat().st_mode), 0o644)

    def test_load_credential_rejects_fifo_without_blocking(self):
        fifo = self.state / "api.token"
        os.mkfifo(fifo, 0o600)
        self.assertTrue(stat.S_ISFIFO(os.stat(fifo).st_mode))
        # O_NONBLOCK must make the open return promptly so fstat can refuse it;
        # a bounded subprocess watch-dog proves the rejection cannot hang.
        code = ("import importlib.util,sys\n"
                "spec=importlib.util.spec_from_file_location('d',%r)\n"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                "from pathlib import Path\n"
                "try:\n"
                "    m.load_credential(Path(%r),'api.token');sys.exit(9)\n"
                "except m.DashboardError:\n"
                "    sys.exit(0)\n" % (str(SCRIPT), str(fifo)))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(fifo).st_mode), 0o600)  # untouched


if __name__ == "__main__":
    unittest.main()
