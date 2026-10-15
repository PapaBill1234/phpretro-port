import datetime as dt
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "eliza-autopilot.py"
spec = importlib.util.spec_from_file_location("eliza_autopilot", SCRIPT)
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

OPEN = "2026-10-13T04:30:00+00:00"      # allowed, ~60 min of room
WINDDOWN = "2026-10-13T05:28:00+00:00"  # allowed, inside the 5 min stop buffer
PEAK = "2026-10-13T02:00:00+00:00"      # blocked peak/buffer

SNIPPET = (
    "import importlib.util,sys,time\n"
    "spec=importlib.util.spec_from_file_location('ap', sys.argv[1])\n"
    "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "lock=m.Singleton(sys.argv[2]); lock.acquire()\n"
    "print('locked', flush=True)\n"
    "time.sleep(float(sys.argv[3]))\n"
)


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def log_error(self, *a):
        pass

    def _respond(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        fixture = self.server.fixture
        if self.headers.get("Authorization") != "Bearer " + fixture.token:
            self._respond(401, {"error": "auth"})
            return False
        return True

    def do_GET(self):
        fixture = self.server.fixture
        if not self._auth_ok():
            return
        if self.path == "/status":
            fixture.log.append(("GET", "/status", None))
            return self._respond(200, fixture.status)
        if self.path == "/tasks":
            fixture.log.append(("GET", "/tasks", None))
            return self._respond(200, fixture.tasks)
        self._respond(404, {"error": "unknown route"})

    def do_POST(self):
        fixture = self.server.fixture
        if not self._auth_ok():
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        command = payload.get("command")
        fixture.log.append(("POST", command, payload))
        if command in fixture.drop:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if command in fixture.error_bodies:
            body = fixture.error_bodies[command].encode()
            self.send_response(409)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        handler = fixture.control.get(command)
        result = handler(payload) if callable(handler) else (handler or {})
        if result is None:
            return
        self._respond(200, result)


class Fixture:
    def __init__(self):
        self.token = "test-token-0123456789abcdef"
        self.status = {"quota": {"allowed": True, "reason": "quota_available"}, "sessions": []}
        self.tasks = []
        self.control = {}
        self.drop = set()
        self.error_bodies = {}
        self.log = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server.fixture = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self, command):
        return [row for row in self.log if row[0] == "POST" and row[1] == command]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.build = self.root / ".builder"
        self._old = ap.BUILDER_STATE
        ap.BUILDER_STATE = self.build
        self.state = self.build / "eliza-autopilot"
        self.companion = self.build / "eliza"
        self.manifest = self.state / "manifest.json"
        self.fixture = None

    def tearDown(self):
        if self.fixture:
            self.fixture.close()
        ap.BUILDER_STATE = self._old
        self.temp.cleanup()

    # helpers
    def env(self):
        return {**os.environ, "ELIZA_AUTOPILOT_HOME": str(self.build)}

    def task(self, board="AUTO-1", scope=None, depends=None):
        return {"boardId": board, "title": board, "goal": "bounded local change",
                "scope": scope or ["scripts/eliza-"], "acceptanceCriteria": ["passes"],
                "acceptanceCommand": ["python3", "-c", "pass"], "depends": depends or []}

    def write_manifest(self, tasks, mode=0o600):
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.manifest.write_text(json.dumps({"version": 1, "tasks": tasks}))
        os.chmod(self.manifest, mode)

    def start_fixture(self):
        self.fixture = Fixture()
        self.companion.mkdir(parents=True, exist_ok=True)
        os.chmod(self.companion, 0o700)
        ap.atomic_write(self.companion / "server.json",
                        json.dumps({"pid": os.getpid(), "port": self.fixture.port, "host": "127.0.0.1"}))
        ap.atomic_write(self.companion / "control.token", self.fixture.token + "\n")
        return self.fixture

    def pilot(self, now, **kw):
        clock = (lambda: dt.datetime.fromisoformat(now)) if now else None
        return ap.Autopilot(state_dir=self.state, companion_state=self.companion,
                            manifest_path=self.manifest, clock=clock, once=True, **kw)


class ManifestTests(Base):
    def test_missing_manifest_is_empty_and_spends_nothing(self):
        decision = self.pilot(OPEN).decide(dt.datetime.fromisoformat(OPEN), for_run=False)
        self.assertEqual(decision, {"action": "idle_empty", "reason": "empty_queue"})

    def test_scope_denies_cms_db_env_git_and_traversal(self):
        denied = ["apps/web/x.tsx", "cmd/main.go", "internal/x.go", "migrations/a.sql",
                  "themes/x", "assets/x.swf", ".env", ".git/config", "../etc/passwd",
                  "builder/eliza/../cmd/y", "config/site.json"]
        for path in denied:
            with self.subTest(path=path):
                self.assertFalse(ap.scope_allowed(path))
        for path in ["scripts/eliza-autopilot.py", "builder/eliza/src/x.mjs",
                     "docs/eliza-autopilot.md", "tests/ops/test_eliza_autopilot.py"]:
            with self.subTest(path=path):
                self.assertTrue(ap.scope_allowed(path))

    def test_manifest_rejects_denied_scope(self):
        self.write_manifest([self.task(scope=["apps/web/index.tsx"])])
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)

    def test_manifest_rejects_symlink_world_readable_oversized_and_secret(self):
        self.write_manifest([self.task()])
        link = self.state / "link.json"
        link.symlink_to(self.manifest)
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(link)
        self.write_manifest([self.task()], mode=0o644)
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)
        self.manifest.write_text(json.dumps({"version": 1, "tasks": [self.task()]}) + " " * (300 * 1024))
        os.chmod(self.manifest, 0o600)
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)
        secret = self.task()
        secret["goal"] = "use api_key=abcdefghijklmnopqrstuvwxyz to connect"
        self.write_manifest([secret])
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)

    def test_manifest_rejects_malformed_and_missing_fields(self):
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.manifest.write_text("{not json")
        os.chmod(self.manifest, 0o600)
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)
        bad = self.task()
        del bad["acceptanceCommand"]
        self.write_manifest([bad])
        with self.assertRaises(ap.ManifestError):
            ap.load_manifest(self.manifest)


class SingletonTests(Base):
    def test_real_subprocess_singleton_and_restart(self):
        lock = self.state / "autopilot.lock"
        with subprocess.Popen([sys.executable, "-c", SNIPPET, str(SCRIPT), str(lock), "15"],
                              stdout=subprocess.PIPE, text=True) as proc:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            held = ap.Singleton(lock)
            with self.assertRaises(ap.SingletonBusy):
                held.acquire()
            proc.terminate()
        # Restart: the OS lock is gone once the holder exits.
        again = ap.Singleton(lock)
        self.assertTrue(again.acquire())
        again.release()

    def test_cli_run_is_singleton_and_reacquires_after_stop(self):
        with subprocess.Popen([sys.executable, str(SCRIPT), "run", "--state-dir", str(self.state),
                               "--interval", "0.2", "--max-seconds", "6"],
                              stdout=subprocess.PIPE, text=True, env=self.env()) as proc:
            time.sleep(1.0)
            second = subprocess.run([sys.executable, str(SCRIPT), "run", "--state-dir", str(self.state),
                                     "--once", "--now", PEAK], capture_output=True, text=True,
                                    timeout=30, env=self.env())
            self.assertEqual(second.returncode, 3)
            self.assertEqual(json.loads(second.stdout)["action"], "busy")
            proc.terminate()
        third = subprocess.run([sys.executable, str(SCRIPT), "run", "--state-dir", str(self.state),
                                "--once", "--now", PEAK], capture_output=True, text=True,
                               timeout=30, env=self.env())
        self.assertEqual(third.returncode, 0)


class ControlFlowTests(Base):
    def test_nested_private_scope_is_rejected(self):
        for path in ["builder/eliza/.builder/control.token", "builder/eliza/private/report.json",
                     "builder/eliza/node_modules/package/index.js", "builder/eliza/secrets/provider.json"]:
            with self.subTest(path=path):
                self.assertFalse(ap.scope_allowed(path))

    def test_reserve_and_foreign_owner_block_before_reconciliation(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        pilot = self.pilot(OPEN)
        for flags, reason in [({"waitingReserve": True}, "quota_reserve"),
                              ({"owner": "claude-code"}, "ownership_changed")]:
            pilot.qstate.data.update(waitingReserve=False, owner=None, paused=False)
            pilot.qstate.data.update(flags)
            with mock.patch.object(pilot, "reconcile") as reconcile:
                result = pilot.step(dt.datetime.fromisoformat(OPEN))
            self.assertEqual(result["action"], "paused")
            self.assertEqual(result["reason"], reason)
            reconcile.assert_not_called()
            self.assertEqual(self.fixture.posts("task-dispatch"), [])

    def test_canonical_handoff_blocks_without_changing_owner(self):
        self.write_manifest([self.task()])
        pilot = self.pilot(OPEN)
        with mock.patch.object(ap, "ROOT", self.root):
            directory = self.root / ".builder"
            directory.mkdir(mode=0o700, exist_ok=True)
            marker = directory / "quota-state.json"
            state = {"coordinatorOwner": "claude-code", "status": "active-eliza-only",
                     "reportedWindows": [{"usedPercent": 10}]}
            marker.write_text(json.dumps(state))
            marker.chmod(0o600)
            self.assertEqual(pilot.coordination_blocking(), "ownership_changed")
            state["coordinatorOwner"] = "codex-eliza"
            state["reportedWindows"][0]["usedPercent"] = 90
            marker.write_text(json.dumps(state))
            self.assertEqual(pilot.coordination_blocking(), "quota_reserve")
            state["reportedWindows"] = []
            marker.write_text(json.dumps(state))
            self.assertEqual(pilot.coordination_blocking(), "coordination_quota_unknown")
            self.assertEqual(json.loads(marker.read_text()), state)

    def test_foreign_owner_cannot_cancel_at_winddown(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        pilot = self.pilot(WINDDOWN)
        pilot.qstate.data["owner"] = "claude-code"
        pilot.tracked = {"t-1": {"attemptId": "a1", "boardId": "AUTO-1"}}
        result = pilot.step(dt.datetime.fromisoformat(WINDDOWN))
        self.assertEqual(result["reason"], "ownership_changed")
        self.assertEqual(self.fixture.posts("task-cancel"), [])

    def test_peak_idles_until_next_allowed_at_without_dispatch(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        decision = self.pilot(PEAK).decide(dt.datetime.fromisoformat(PEAK), for_run=True)
        self.assertEqual(decision["action"], "stay_idle")
        self.assertEqual(decision["next_wake"], "2026-10-13T04:05:00+00:00")
        self.assertEqual(self.fixture.posts("task-dispatch"), [])

    def test_check_cli_reports_next_wake(self):
        self.write_manifest([self.task()])
        result = subprocess.run([sys.executable, str(SCRIPT), "check", "--state-dir", str(self.state),
                                 "--now", PEAK], capture_output=True, text=True, timeout=30, env=self.env())
        value = json.loads(result.stdout)
        self.assertEqual(value["next_wake"], "2026-10-13T04:05:00+00:00")
        self.assertEqual(result.returncode, 75)

    def test_cutoff_cancels_workers_before_stop_at(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        self.fixture.control["task-cancel"] = {"cancelled": True}
        pilot = self.pilot(WINDDOWN)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        decision = pilot.step(dt.datetime.fromisoformat(WINDDOWN))
        self.assertEqual(decision["action"], "winddown")
        self.assertEqual(decision["cancelled"], ["t-1"])
        self.assertEqual(len(self.fixture.posts("task-cancel")), 1)
        self.assertEqual(self.fixture.posts("task-dispatch"), [])
        self.assertEqual(decision["next_wake"], "2026-10-13T10:05:00+00:00")

    def test_quota_reserve_pauses_qstate_without_dispatch(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        self.fixture.status = {"quota": {"allowed": False, "reason": "quota_reserve",
                                         "remainingPercent": 4}, "sessions": []}
        pilot = self.pilot(OPEN)
        decision = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(decision["action"], "await_quota")
        self.assertTrue(pilot.qstate.data["paused"])
        self.assertTrue(pilot.qstate.data["waitingReserve"])
        self.assertEqual(self.fixture.posts("task-dispatch"), [])

    def test_unknown_quota_is_not_a_spend(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        self.fixture.status = {"quota": {"allowed": False, "reason": "quota_unknown"}, "sessions": []}
        decision = self.pilot(OPEN).decide(dt.datetime.fromisoformat(OPEN), for_run=True)
        self.assertEqual(decision["reason"], "quota_unknown")


class DispatchTests(Base):
    def _ready_fixture(self):
        fixture = self.start_fixture()
        # /tasks rows are candidate ids only; the authoritative identity is read
        # through task-status, mirroring the companion's own resolution.
        fixture.tasks = [{"id": "t-1"}]
        fixture.control["task-status"] = {
            "id": "t-1", "goal": "bounded local change",
            "metadata": {"boardId": "AUTO-1", "scope": ["scripts/eliza-"],
                         "acceptanceCommand": ["python3", "-c", "pass"],
                         "workdir": str(self.build / "worktrees" / "AUTO-1")},
            "sessions": []}
        fixture.control["task-dispatch"] = {"attemptId": "a1", "sessionId": "s1", "task": {}}
        return fixture

    def test_open_window_dispatches_once(self):
        self.write_manifest([self.task()])
        fixture = self._ready_fixture()
        pilot = self.pilot(OPEN)
        decision = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["task"], "t-1")
        self.assertEqual(len(fixture.posts("task-dispatch")), 1)
        self.assertEqual(fixture.posts("task-dispatch")[0][2]["id"], "t-1")

    def test_ambiguous_dispatch_requires_recovery_and_never_blind_retries(self):
        self.write_manifest([self.task()])
        fixture = self._ready_fixture()
        fixture.drop.add("task-dispatch")
        pilot = self.pilot(OPEN)
        first = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(first["action"], "recovery_required")
        self.assertEqual(len(fixture.posts("task-dispatch")), 1)
        self.assertTrue(pilot.qstate.data["recoveryRequired"])
        # A second bounded step must not re-POST the same mutation.
        second = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(second["action"], "recovery_required")
        self.assertEqual(len(fixture.posts("task-dispatch")), 1)

    def test_conflicting_boardid_with_foreign_scope_fails_closed(self):
        self.write_manifest([self.task()])  # trusted AUTO-1: scripts/eliza-
        fixture = self.start_fixture()
        fixture.tasks = [{"id": "t-9"}]
        fixture.control["task-status"] = {
            "id": "t-9", "goal": "cms/private work",
            "metadata": {"boardId": "AUTO-1", "scope": ["apps/web"],
                         "acceptanceCommand": ["echo", "x"]},
            "sessions": []}
        pilot = self.pilot(OPEN)
        decision = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(decision["action"], "review_required")
        self.assertEqual(decision["reason"], "identity_conflict")
        # Fail closed: never create, dispatch, or rewrite the foreign task.
        self.assertEqual(fixture.posts("task-create"), [])
        self.assertEqual(fixture.posts("task-dispatch"), [])
        self.assertEqual(pilot.qstate.data["reviewNeeded"][0]["reason"], "identity_conflict")


class ReconcileTests(Base):
    def _worktree(self, changed):
        wd = self.build / "worktrees" / "AUTO-1"
        wd.mkdir(parents=True)
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
        subprocess.run(["git", "init", "-q"], cwd=wd, env=env, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=wd, env=env, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=wd, env=env, check=True)
        (wd / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=wd, env=env, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=wd, env=env, check=True)
        target = wd / changed
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("change\n")
        return wd

    def _fixture_with_task(self, workdir):
        fixture = self.start_fixture()
        fixture.control["task-status"] = {"metadata": {"boardId": "AUTO-1", "scope": ["scripts/eliza-"],
                                                        "workdir": str(workdir)},
                                           "sessions": [{"sessionId": "s1", "status": "completed"}]}
        return fixture

    def test_out_of_scope_change_blocks_acceptance_and_flags_review(self):
        self.write_manifest([self.task()])
        wd = self._worktree("internal/secret.go")
        fixture = self._fixture_with_task(wd)
        pilot = self.pilot(OPEN)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        pilot.reconcile(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(fixture.posts("task-check"), [])
        self.assertEqual(pilot.qstate.data["reviewNeeded"][0]["reason"], "scope_violation")

    def test_in_scope_change_runs_acceptance_and_records_pass(self):
        self.write_manifest([self.task()])
        wd = self._worktree("scripts/eliza-newhelper.py")
        fixture = self._fixture_with_task(wd)
        fixture.control["task-check"] = {"kind": "acceptance_passed", "receipt": {"id": "r1"},
                                         "independentReviewRequired": True}
        pilot = self.pilot(OPEN)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        pilot.reconcile(dt.datetime.fromisoformat(OPEN))
        self.assertEqual(len(fixture.posts("task-check")), 1)
        self.assertIn("AUTO-1", pilot.qstate.data["completed"])

    def test_third_failure_hands_off_and_blocks_further_dispatch(self):
        self.write_manifest([self.task()])
        wd = self._worktree("scripts/eliza-newhelper.py")
        fixture = self._fixture_with_task(wd)
        fixture.control["task-check"] = {"kind": "acceptance_failed", "receipt": {"id": "r1"}}
        fixture.control["report"] = {"receipts": [{"kind": "acceptance_failed"}] * 3}
        pilot = self.pilot(OPEN)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        pilot.reconcile(dt.datetime.fromisoformat(OPEN))
        reviews = pilot.qstate.data["reviewNeeded"]
        self.assertEqual(reviews[0]["reason"], "third_failure")
        self.assertIn("stop further dispatch", reviews[0]["handoff"])
        self.assertEqual(pilot.decide(dt.datetime.fromisoformat(OPEN), for_run=True)["action"], "idle_empty")

    def test_step_reconciles_and_finishes_a_tracked_task(self):
        # The production step/run path must itself reconcile and complete a
        # worker, not only a manual reconcile() call.
        self.write_manifest([self.task()])
        wd = self._worktree("scripts/eliza-newhelper.py")
        fixture = self._fixture_with_task(wd)
        fixture.control["task-check"] = {"kind": "acceptance_passed", "receipt": {"id": "r1"},
                                         "independentReviewRequired": True}
        pilot = self.pilot(OPEN)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        decision = pilot.step(dt.datetime.fromisoformat(OPEN))
        self.assertIn("AUTO-1", pilot.qstate.data["completed"])
        self.assertEqual(len(fixture.posts("task-check")), 1)
        self.assertEqual(decision["action"], "idle_empty")

    def test_step_does_not_reconcile_during_peak(self):
        self.write_manifest([self.task()])
        wd = self._worktree("scripts/eliza-newhelper.py")
        fixture = self._fixture_with_task(wd)
        pilot = self.pilot(PEAK)
        pilot.tracked = {"t-1": {"attemptId": "a1", "sessionId": "s1", "boardId": "AUTO-1"}}
        decision = pilot.step(dt.datetime.fromisoformat(PEAK))
        self.assertEqual(decision["action"], "stay_idle")
        self.assertEqual(fixture.posts("task-check"), [])


class HttpBoundaryTests(Base):
    def _api(self, **kw):
        self.start_fixture()
        return ap.LocalApi(self.companion, timeout=10, **kw)

    def test_oversized_response_is_bounded(self):
        self.start_fixture()
        self.fixture.status = {"quota": {"allowed": True}, "padding": "x" * 500}
        api = ap.LocalApi(self.companion, timeout=10, max_bytes=64)
        with self.assertRaises(ap.BoundedResponse):
            api.status()

    def test_error_bodies_are_redacted(self):
        self.start_fixture()
        self.fixture.error_bodies["task-dispatch"] = (
            "denied api_key=abcdefghijklmnopqrstuvwxyz Bearer supersecretvalue")
        api = ap.LocalApi(self.companion, timeout=10)
        with self.assertRaises(ap.ControlRejected) as caught:
            api.control("task-dispatch", id="t-1")
        message = str(caught.exception)
        self.assertNotIn("api_key=abcdefghijklmnopqrstuvwxyz", message)
        self.assertNotIn("supersecretvalue", message)
        self.assertIn("[redacted]", message)

    def test_non_loopback_host_is_refused(self):
        self.start_fixture()
        ap.atomic_write(self.companion / "server.json",
                        json.dumps({"pid": 1, "port": self.fixture.port, "host": "10.0.0.5"}))
        with self.assertRaises(ap.ApiUnavailable):
            ap.LocalApi(self.companion, timeout=10)

    def test_missing_companion_is_unavailable(self):
        with self.assertRaises(ap.ApiUnavailable):
            ap.LocalApi(self.companion, timeout=10)

    def test_tasks_envelope_matches_bare_array_contract(self):
        self.start_fixture()
        self.fixture.tasks = [{"id": "t-1", "metadata": {"boardId": "AUTO-1"}}]
        api = ap.LocalApi(self.companion, timeout=10)
        self.assertEqual(api.tasks(), [{"id": "t-1", "metadata": {"boardId": "AUTO-1"}}])
        # A bare array is right for /tasks, but not for the object-shaped control API.
        self.fixture.status = ["unexpected"]
        with self.assertRaises(ap.ApiError):
            api.status()

    def test_four_worker_cap_blocks_dispatch(self):
        self.write_manifest([self.task()])
        self.start_fixture()
        self.fixture.status = {"quota": {"allowed": True, "reason": "quota_available"},
                               "activeWorkers": [{"id": str(i)} for i in range(4)], "sessions": []}
        decision = self.pilot(OPEN).decide(dt.datetime.fromisoformat(OPEN), for_run=True)
        self.assertEqual(decision["action"], "await_capacity")
        self.assertEqual(self.fixture.posts("task-dispatch"), [])


class StopControlTests(Base):
    def test_stop_flag_is_set_cleared_and_honored(self):
        subprocess.run([sys.executable, str(SCRIPT), "stop", "--state-dir", str(self.state)],
                       check=True, env=self.env())
        self.assertTrue((self.state / "STOP").exists())
        decision = self.pilot(PEAK).run()
        self.assertEqual(decision["action"], "stopped")
        subprocess.run([sys.executable, str(SCRIPT), "stop", "--state-dir", str(self.state),
                        "--clear"], check=True, env=self.env())
        self.assertFalse((self.state / "STOP").exists())


if __name__ == "__main__":
    unittest.main()
