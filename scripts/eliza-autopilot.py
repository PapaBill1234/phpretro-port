#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Trusted LOCAL off-peak autopilot for the existing real Eliza companion.

A bounded, host-owned *local queue* around the companion's fixed loopback
builder API (127.0.0.1:18791, bearer). It never starts a model, spawns a worker
directly, uses an ambient proxy, logs into an account, changes quota/ownership
state on a provider, touches the CMS/theme/game port, or pushes/merges a branch.
The default empty queue dispatches nothing; the owner maintains reviewed
declarative task definitions in a private manifest.

Controls:
  check   dry-run decision, no local or remote mutation (read-only network)
  run     bounded foreground daemon (single process via a stable OS lock)
  stop    cooperative private checkpoint flag (set, or --clear to remove)

This is a local builder queue, not full Eliza UI autonomous planning, and it
never applies a Sol plan automatically: a passed acceptance still requires an
independent review before any human-driven integration.
"""
import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# Private state lives under .builder; the env override exists only so an owner
# (or a controlled test) can redirect it without moving the trusted default.
BUILDER_STATE = Path(os.environ.get("ELIZA_AUTOPILOT_HOME") or (ROOT / ".builder"))
DEFAULT_STATE = BUILDER_STATE / "eliza-autopilot"
DEFAULT_COMPANION = BUILDER_STATE / "eliza"
CAPACITY = 4
MAX_TASK_SCAN = 200
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 18791
MAX_RESPONSE_BYTES = 1 << 20
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TOKEN_BYTES = 4096
MAX_TASKS = 20
DEFAULT_GRACE_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 20
MAX_STRING = 2000

# Only Eliza-companion-owned paths may ever be declared or changed. Anything
# else -- the CMS/theme/game port, the game DB, `.env`/`.git` or a traversal --
# fails closed before a task is imported or a worker acceptance is taken.
ALLOW_SCOPE = ("scripts/eliza-", "builder/eliza/", "docs/eliza-", "tests/ops/test_eliza_")
DENY_PREFIX = ("apps/", "cmd/", "internal/", "migrations/", "themes/", "assets/",
               "vendor/", "node_modules/", "config/", "infra/", "packages/")
DENY_SUFFIX = re.compile(r"\.(?:sql|db|sqlite3?|pem|key|p12|pfx|env)(?:\.|$)", re.I)
SECRET_RE = re.compile(
    r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|Bearer\s+\S+|"
    r"ANTHROPIC_AUTH_TOKEN\s*[=:]|api[_-]?key\s*[=:])", re.I)
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MUTATING = {"task-create", "task-import", "task-claim", "task-dispatch",
            "task-check", "fail", "task-cancel", "help"}


class AutopilotError(RuntimeError):
    pass


class ManifestError(AutopilotError):
    pass


class ApiError(AutopilotError):
    pass


class ApiUnavailable(ApiError):
    pass


class BoundedResponse(ApiError):
    pass


class ControlRejected(ApiError):
    pass


class AmbiguousDispatch(ApiError):
    """A mutating control request was sent but no response was observed."""


class SingletonBusy(AutopilotError):
    pass


class IdentityConflict(AutopilotError):
    """A boardId already belongs to a task whose trusted identity differs."""


def _load_coding_window():
    """Import the SINGLE canonical coding-window module; never reimplement it."""
    path = ROOT / "scripts" / "coding-window.py"
    spec = importlib.util.spec_from_file_location("coding_window", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coding_window = _load_coding_window()


def redact(text):
    return SECRET_RE.sub("[redacted]", str(text))


def reject_secrets(value):
    if SECRET_RE.search(json.dumps(value) if not isinstance(value, str) else value):
        raise ManifestError("Secret-like input rejected")


def _parts(path):
    return [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]


def scope_allowed(path):
    """True only for an Eliza-owned, non-traversing, non-CMS relative path."""
    if not isinstance(path, str) or not path or path.startswith("/") or path.startswith("~"):
        return False
    norm = path.replace("\\", "/")
    parts = _parts(norm)
    if not parts:
        return False
    if any(p == ".." for p in parts):
        return False
    if any(p == ".git" or p.startswith(".env") for p in parts):
        return False
    if DENY_SUFFIX.search(norm):
        return False
    if any(norm.startswith(pre) for pre in DENY_PREFIX):
        return False
    return any(norm.startswith(pre) for pre in ALLOW_SCOPE)


def _declared_matches(changed, declared):
    entry = declared.rstrip("*").rstrip("/")
    if not entry:
        return False
    return changed == entry or changed.startswith(entry + "/") or changed.startswith(entry)


def paths_within_scope(changed, declared):
    if not isinstance(changed, list) or not isinstance(declared, list) or not declared:
        return False
    for path in changed:
        if not scope_allowed(path):
            return False
        if not any(_declared_matches(path, d) for d in declared):
            return False
    return True


def _assert_no_symlink_ancestors(path, stop):
    stop = Path(stop)
    cur = Path(path)
    while True:
        if cur == stop:
            return
        if cur.is_symlink():
            raise ManifestError("path component is a symlink")
        if cur.parent == cur:
            return
        cur = cur.parent


def _private_file(path, limit):
    path = Path(path)
    if not str(path.resolve()).startswith(str(BUILDER_STATE)):
        raise ManifestError("path must be private project .builder state")
    _assert_no_symlink_ancestors(path, BUILDER_STATE)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode):
        raise ManifestError("path must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError("path must be a regular file")
    if info.st_uid != os.getuid():
        raise ManifestError("file must be owned by the runtime user")
    if info.st_mode & 0o077:
        raise ManifestError("file permissions must be 0600")
    if info.st_size > limit:
        raise ManifestError("file exceeds the private size bound")
    return path


def load_manifest(path):
    """Validate a private 0700/0600 declarative manifest; return bounded tasks."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "tasks": []}
    _private_file(path, MAX_MANIFEST_BYTES)
    raw = path.read_text()
    reject_secrets(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ManifestError("manifest is not valid JSON")
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ManifestError("manifest version must be 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ManifestError("manifest tasks must be a list")
    if len(tasks) > MAX_TASKS:
        raise ManifestError("manifest exceeds the bounded task count")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ManifestError("each task must be an object")
        board = task.get("boardId")
        if not isinstance(board, str) or not BOARD_ID_RE.match(board) or board in seen:
            raise ManifestError("task needs a unique boardId")
        seen.add(board)
        for key in ("goal", "title"):
            value = task.get(key, "")
            if not isinstance(value, str) or len(value) > MAX_STRING:
                raise ManifestError(f"task {board} {key} must be a bounded string")
        if not task.get("goal"):
            raise ManifestError(f"task {board} needs a goal")
        scope = task.get("scope")
        if not isinstance(scope, list) or not scope:
            raise ManifestError(f"task {board} needs a scope")
        for entry in scope:
            if not scope_allowed(entry):
                raise ManifestError(f"task {board} scope entry denied: {entry}")
        criteria = task.get("acceptanceCriteria")
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c for c in criteria):
            raise ManifestError(f"task {board} needs acceptanceCriteria")
        command = task.get("acceptanceCommand")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a and "\0" not in a for a in command):
            raise ManifestError(f"task {board} needs acceptanceCommand argv")
        depends = task.get("depends", [])
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise ManifestError(f"task {board} depends must be a string list")
    return data


def _ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    _ensure_private_dir(path.parent)
    tmp = path.parent / (path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, mode)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_private(path, limit):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(str(path))
    _private_file(path, limit)
    return path.read_text()


class Singleton:
    """Stable-inode OS advisory (fcntl) lock so two daemons can never both run."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def _validate(self, fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SingletonBusy("lock target is not a regular file")
        if info.st_uid != os.getuid():
            raise SingletonBusy("lock must be owned by the runtime user")
        if info.st_mode & 0o077:
            raise SingletonBusy("lock permissions must be 0600")

    def acquire(self):
        _ensure_private_dir(self.path.parent)
        try:
            if self.path.is_symlink():
                raise SingletonBusy("lock path must not be a symlink")
        except OSError:
            pass
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            self._validate(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except SingletonBusy:
            os.close(fd)
            raise
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise SingletonBusy("another autopilot already holds the private lock")
            raise
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


class QState:
    """Private queue state: pause, ownership and waiting-reserve each block dispatch."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "qstate.json"
        self.data = {"version": 1, "paused": False, "pauseReason": None, "owner": None,
                     "waitingReserve": False, "quotaReason": None, "remainingPercent": None,
                     "observedAt": None, "completed": [], "reviewNeeded": [], "recoveryRequired": []}

    def load(self):
        try:
            stored = json.loads(read_private(self.path, MAX_MANIFEST_BYTES))
        except FileNotFoundError:
            return self
        if isinstance(stored, dict):
            self.data.update(stored)
        return self

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=2) + "\n")

    def blocking(self):
        """Return a reason string while any qstate field blocks a new dispatch."""
        if self.data.get("recoveryRequired"):
            return "recovery_required"
        if self.data.get("paused"):
            return self.data.get("pauseReason") or "paused"
        return None


class Attempts:
    """Durable attempt markers written BEFORE every mutating control request."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir) / "attempts"

    def _path(self, key):
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def begin(self, key, action, payload=None):
        _ensure_private_dir(self.dir)
        marker = {"key": key, "action": action, "state": "pending",
                  "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "payload": payload or {}}
        atomic_write(self._path(key), json.dumps(marker) + "\n")
        return marker

    def finish(self, key, state, detail=None):
        marker = {"key": key, "state": state,
                  "finishedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "detail": detail or {}}
        atomic_write(self._path(key), json.dumps(marker) + "\n")

    def unresolved(self):
        if not self.dir.exists():
            return []
        rows = []
        for item in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(item.read_text())
            except (OSError, json.JSONDecodeError):
                rows.append({"key": item.name, "state": "unreadable"})
                continue
            if data.get("state") in ("pending", "ambiguous"):
                rows.append(data)
        return rows


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalApi:
    """Fixed 127.0.0.1 bearer client: no ambient proxy, no redirects, bounded."""

    def __init__(self, companion_state, *, timeout=60, max_bytes=MAX_RESPONSE_BYTES, opener=None):
        state = Path(companion_state)
        try:
            server = json.loads(read_private(state / "server.json", MAX_MANIFEST_BYTES))
        except FileNotFoundError:
            raise ApiUnavailable("companion server descriptor is not present")
        host, port = server.get("host"), server.get("port")
        if host != LOOPBACK_HOST:
            raise ApiUnavailable("companion server must be fixed to loopback")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ApiUnavailable("companion server port is invalid")
        token = read_private(state / "control.token", MAX_TOKEN_BYTES).strip()
        if not token or any(c.isspace() for c in token) or len(token) > MAX_TOKEN_BYTES:
            raise ApiUnavailable("companion control token is malformed")
        self.base = f"http://{LOOPBACK_HOST}:{port}"
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def _read(self, stream):
        data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise BoundedResponse("companion response exceeds the bounded size")
        return data

    def _open(self, request, timeout):
        # An explicit empty ProxyHandler ignores ambient proxy configuration and
        # the redirect handler refuses every 3xx so a loopback call cannot be
        # forwarded off-host.
        return self._opener.open(request, timeout=timeout)

    def _request(self, path, payload, *, mutating):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self._open(request, self.timeout) as response:
                body = self._read(response)
        except urllib.error.HTTPError as error:
            try:
                detail = redact(error.read(self.max_bytes))
            except Exception:
                detail = ""
            raise ControlRejected(f"control rejected ({error.code}): {detail}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            if mutating:
                raise AmbiguousDispatch(
                    f"control request outcome unknown: {redact(error)}")
            raise ApiUnavailable(f"companion unavailable: {redact(error)}")
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            raise ApiError("companion returned malformed JSON")
        return value

    def status(self):
        value = self._request("/status", None, mutating=False)
        if not isinstance(value, dict):
            raise ApiError("companion status envelope is not an object")
        return value

    def tasks(self):
        # The canonical control API returns a bare JSON array from GET /tasks.
        value = self._request("/tasks", None, mutating=False)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("tasks"), list):
            return value["tasks"]
        raise ApiError("companion tasks envelope is not a list")

    def control(self, command, **fields):
        payload = {"command": command}
        payload.update({k: v for k, v in fields.items() if v is not None})
        value = self._request("/control", payload, mutating=command in MUTATING)
        if not isinstance(value, dict):
            raise ApiError("companion control envelope is not an object")
        return value


def changed_paths(workdir, timeout=20):
    """Tracked + staged + untracked changes in an isolated claim worktree."""
    wd = Path(workdir)
    if not wd.is_dir():
        return None
    env = {k: os.environ.get(k) for k in ("PATH", "HOME", "LANG") if os.environ.get(k)}
    out = []
    for args in (["diff", "--name-only", "-z", "HEAD"],
                 ["diff", "--name-only", "-z", "--cached", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard", "-z"]):
        try:
            result = subprocess.run(["git", "-C", str(wd), *args], capture_output=True,
                                    timeout=timeout, env=env, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        out.extend(p for p in result.stdout.decode().split("\0") if p)
    return sorted(set(out))


class Autopilot:
    """Bounded, single-process local queue over the companion's loopback API."""

    def __init__(self, *, state_dir=DEFAULT_STATE, companion_state=DEFAULT_COMPANION,
                 manifest_path=None, clock=None, grace=DEFAULT_GRACE_SECONDS,
                 interval=DEFAULT_INTERVAL_SECONDS, once=False, api=None, log=None):
        self.state_dir = Path(state_dir)
        self.companion_state = Path(companion_state)
        self.manifest_path = Path(manifest_path) if manifest_path else self.state_dir / "manifest.json"
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.grace = grace
        self.interval = interval
        self.once = once
        self._api = api
        self._log = log or (lambda message: None)
        self.singleton = Singleton(self.state_dir / "autopilot.lock")
        self.attempts = Attempts(self.state_dir)
        self.qstate = QState(self.state_dir).load()
        self.manifest = load_manifest(self.manifest_path)
        self.tracked = {}
        try:
            self.tracked = json.loads(read_private(self.state_dir / "tracked.json", MAX_MANIFEST_BYTES))
        except FileNotFoundError:
            self.tracked = {}

    # -- injected collaborators ------------------------------------------------
    @property
    def api(self):
        if self._api is None:
            self._api = LocalApi(self.companion_state)
        return self._api

    def _save_tracked(self):
        if self.tracked:
            atomic_write(self.state_dir / "tracked.json", json.dumps(self.tracked) + "\n")
        elif (self.state_dir / "tracked.json").exists():
            (self.state_dir / "tracked.json").unlink()

    def stop_flag(self):
        return (self.state_dir / "STOP").exists()

    # -- decisions -------------------------------------------------------------
    def decide(self, now, *, for_run):
        if not self.manifest["tasks"]:
            return {"action": "idle_empty", "reason": "empty_queue"}
        window = coding_window.decision(now)
        if not window["allowed"]:
            return {"action": "stay_idle", "reason": window["reason"],
                    "next_wake": window["next_allowed_at"]}
        stop_at = dt.datetime.fromisoformat(window["stop_at"])
        if for_run and (stop_at - now).total_seconds() <= self.grace:
            return {"action": "winddown", "stop_at": window["stop_at"]}
        try:
            status = self.api.status()
        except ApiError:
            return {"action": "await_api", "reason": "companion_unavailable"}
        quota = status.get("quota") or {}
        if not quota.get("allowed"):
            return {"action": "await_quota", "reason": quota.get("reason", "quota_unknown"),
                    "quota": quota}
        if len(status.get("activeWorkers") or []) >= CAPACITY:
            return {"action": "await_capacity", "reason": "four_worker_cap"}
        if self.attempts.unresolved():
            return {"action": "recovery_required", "markers": self.attempts.unresolved()}
        blocking = self.qstate.blocking()
        if blocking:
            return {"action": "paused", "reason": blocking}
        task = self._next_task(status)
        if task is None:
            return {"action": "idle_empty", "reason": "no_pending_task"}
        return {"action": "dispatch", "task": task}

    def _next_task(self, status):
        done = set(self.qstate.data.get("completed") or [])
        review = self.qstate.data.get("reviewNeeded") or []
        review_boards = {r.get("boardId") for r in review}
        for task in self.manifest["tasks"]:
            board_id = task["boardId"]
            if board_id in done or board_id in review_boards:
                continue
            if any(entry.get("boardId") == board_id for entry in self.tracked.values()):
                continue
            if any(dep not in done for dep in task.get("depends", [])):
                continue
            return task
        return None

    # -- mutations -------------------------------------------------------------
    def _create_task(self, task):
        key = f"create:{task['boardId']}"
        self.attempts.begin(key, "task-create", {"boardId": task["boardId"]})
        try:
            result = self.api.control("task-create", task={
                "boardId": task["boardId"], "title": task.get("title", task["boardId"]),
                "goal": task["goal"], "scope": task["scope"],
                "acceptanceCriteria": task["acceptanceCriteria"],
                "acceptanceCommand": task["acceptanceCommand"],
                "depends": task.get("depends", [])})
        except AmbiguousDispatch:
            self.attempts.finish(key, "ambiguous")
            self.qstate.data["recoveryRequired"] = [{"key": key, "taskId": None}]
            self.qstate.save()
            raise
        self.attempts.finish(key, "done", {"id": result.get("id")})
        return result.get("id")

    def _task_ids(self):
        # GET /tasks is treated as candidate ids only. The companion itself
        # resolves a row's authoritative metadata through task-status, so the
        # client mirrors that instead of trusting the list envelope.
        ids = []
        for row in self.api.tasks():
            tid = row.get("id") if isinstance(row, dict) else None
            if tid:
                ids.append(tid)
        return ids[:MAX_TASK_SCAN]

    def _identity_matches(self, detail, task):
        meta = detail.get("metadata") or {}
        return (meta.get("boardId") == task["boardId"]
                and meta.get("scope") == list(task["scope"])
                and detail.get("goal") == task["goal"]
                and meta.get("acceptanceCommand") == list(task["acceptanceCommand"]))

    def _find_existing_task(self, task):
        for tid in self._task_ids():
            try:
                detail = self.api.control("task-status", id=tid)
            except ApiError:
                continue
            if (detail.get("metadata") or {}).get("boardId") != task["boardId"]:
                continue
            if not self._identity_matches(detail, task):
                raise IdentityConflict(
                    f"boardId {task['boardId']} already belongs to an existing task "
                    "whose scope/goal/acceptance do not match the trusted manifest")
            return tid
        return None

    def dispatch(self, task):
        try:
            task_id = self._find_existing_task(task)
        except IdentityConflict as error:
            # Fail closed: never mutate, rewrite or dispatch the foreign task.
            self._mark_review(None, task["boardId"], "identity_conflict", handoff=redact(error))
            return {"action": "review_required", "reason": "identity_conflict",
                    "task": task["boardId"]}
        if task_id is None:
            task_id = self._create_task(task)
        key = f"dispatch:{task['boardId']}"
        self.attempts.begin(key, "task-dispatch", {"boardId": task["boardId"]})
        try:
            detail = self.api.control("task-dispatch", id=task_id)
        except AmbiguousDispatch:
            self.attempts.finish(key, "ambiguous")
            self.qstate.data["recoveryRequired"] = [{"key": key, "taskId": task_id}]
            self.qstate.save()
            return {"action": "recovery_required", "task": task_id}
        except ControlRejected as error:
            self.attempts.finish(key, "rejected", {"error": redact(error)})
            return {"action": "await_quota", "reason": "dispatch_rejected", "task": task_id}
        self.attempts.finish(key, "done", {"attemptId": detail.get("attemptId")})
        self.tracked[task_id] = {"attemptId": detail.get("attemptId"),
                                 "sessionId": detail.get("sessionId"),
                                 "boardId": task["boardId"]}
        self._save_tracked()
        return {"action": "dispatch", "task": task_id}

    def cancel_tracked(self):
        cancelled = []
        for task_id in list(self.tracked):
            try:
                self.api.control("task-cancel", id=task_id)
                cancelled.append(task_id)
            except ApiError:
                continue
            self.tracked.pop(task_id, None)
        self._save_tracked()
        return cancelled

    # -- reconciliation --------------------------------------------------------
    def _drop_tracked(self, task_id):
        if self.tracked.pop(task_id, None) is not None:
            self._save_tracked()

    def reconcile(self, now):
        """Complete/verify tracked workers; safe and idempotent across restarts."""
        for task_id, entry in list(self.tracked.items()):
            try:
                state = self.api.control("task-status", id=task_id)
            except ApiError:
                continue
            if not isinstance(state, dict):
                continue
            meta = state.get("metadata") or {}
            board_id = meta.get("boardId") or entry.get("boardId")
            worker = next((s for s in state.get("sessions", [])
                           if s.get("sessionId") == entry.get("sessionId")), None)
            status = worker.get("status") if worker else None
            if status in ("errored", "cancelled", "stopped"):
                # An interrupted process is infrastructure, not a code failure.
                self._drop_tracked(task_id)
                continue
            if status != "completed":
                continue
            workdir = meta.get("workdir")
            changed = changed_paths(workdir) if workdir else None
            if changed is None or not paths_within_scope(changed, meta.get("scope") or []):
                self._drop_tracked(task_id)
                self._mark_review(task_id, board_id, "scope_violation")
                continue
            try:
                result = self.api.control("task-check", id=task_id)
            except ControlRejected as error:
                message = str(error)
                if "worker attempt" in message or "not completed" in message or "No unchecked" in message:
                    # A prior cycle or crash already resolved this attempt; derive
                    # the outcome idempotently instead of re-checking forever.
                    self._drop_tracked(task_id)
                    kind = self._report_kind(task_id)
                    if kind:
                        self._record_result(task_id, board_id, {"kind": kind})
                    continue
                self._drop_tracked(task_id)
                continue
            except ApiError:
                continue
            self._drop_tracked(task_id)
            self._record_result(task_id, board_id, result)

    def _report_kind(self, task_id):
        try:
            report = self.api.control("report", id=task_id)
        except ApiError:
            return None
        terminal = [r.get("kind") for r in report.get("receipts", [])
                    if r.get("kind") in ("acceptance_passed", "acceptance_failed", "attempt_interrupted")]
        return terminal[-1] if terminal else None

    def _record_result(self, task_id, board_id, result):
        kind = result.get("kind")
        if kind == "acceptance_passed":
            completed = set(self.qstate.data.get("completed") or [])
            completed.add(board_id)
            self.qstate.data["completed"] = sorted(completed)
            self.qstate.save()
        elif kind == "acceptance_failed":
            # Never auto-retry a real code failure; retain review-needed. The
            # existing Builder already asks Sol for help at the third failure.
            failures = self._count_failures(task_id)
            if failures >= 3:
                self._mark_review(task_id, board_id, "third_failure",
                                  handoff="existing Builder already auto-helps Sol; "
                                          "stop further dispatch, retain review-needed")
            else:
                self._mark_review(task_id, board_id, "acceptance_failed")

    def _count_failures(self, task_id):
        try:
            report = self.api.control("report", id=task_id)
        except ApiError:
            return 0
        return sum(1 for r in report.get("receipts", []) if r.get("kind") == "acceptance_failed")

    def _mark_review(self, task_id, board_id, reason, handoff=None):
        rows = self.qstate.data.setdefault("reviewNeeded", [])
        if any(r.get("boardId") == board_id or (task_id and r.get("taskId") == task_id) for r in rows):
            return
        rows.append({"taskId": task_id, "boardId": board_id, "reason": reason,
                     "handoff": handoff})
        self.qstate.save()

    # -- control flow ----------------------------------------------------------
    def step(self, now):
        # Reconcile during an open window so completed workers are accepted
        # (with scope verification and third-failure handoff). Never during a
        # blocked window, and never while an ambiguity needs recovery.
        if coding_window.decision(now)["allowed"] and not self.attempts.unresolved():
            self.reconcile(now)
        decision = self.decide(now, for_run=True)
        action = decision["action"]
        if action == "dispatch":
            return self.dispatch(decision["task"])
        if action == "winddown":
            decision["cancelled"] = self.cancel_tracked()
            after = coding_window.decision(dt.datetime.fromisoformat(decision["stop_at"]))
            decision["next_wake"] = after["next_allowed_at"]
            return decision
        if action == "await_quota":
            self.qstate.data.update(paused=True, pauseReason=decision["reason"],
                                   waitingReserve=decision["reason"] == "quota_reserve",
                                   quotaReason=decision["reason"],
                                   remainingPercent=(decision.get("quota") or {}).get("remainingPercent"),
                                   observedAt=now.isoformat())
            self.qstate.save()
            return decision
        if action in ("stay_idle", "recovery_required", "paused"):
            return decision
        return decision

    def run(self):
        if self.stop_flag():
            return {"action": "stopped", "reason": "private STOP flag present"}
        self.singleton.acquire()
        self.qstate.data["owner"] = f"eliza-autopilot:{os.getpid()}"
        self.qstate.save()
        deadline = time.time() + self.max_seconds
        try:
            while True:
                if self.stop_flag():
                    return {"action": "stopped", "reason": "private STOP flag set"}
                now = self.clock()
                decision = self.step(now)
                if decision["action"] in ("dispatch", "recovery_required"):
                    if self.once:
                        return decision
                if self.once:
                    return decision
                if time.time() >= deadline:
                    return decision
                sleep_for = self.interval
                if decision.get("next_wake"):
                    until = (dt.datetime.fromisoformat(decision["next_wake"]) - now).total_seconds()
                    sleep_for = max(1.0, min(until, self.max_seconds)) if until > 0 else 1.0
                while sleep_for > 0 and not self.stop_flag() and time.time() < deadline:
                    chunk = min(sleep_for, 1.0)
                    time.sleep(chunk)
                    sleep_for -= chunk
        finally:
            self.singleton.release()

    max_seconds = 900


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "run", "stop"])
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--companion-state", type=Path, default=DEFAULT_COMPANION)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--now", help="fixed UTC clock instant (ISO), for bounded checks")
    parser.add_argument("--once", action="store_true", help="single bounded step then exit")
    parser.add_argument("--clear", action="store_true", help="stop: remove the private flag")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    os.umask(0o077)
    if args.command == "stop":
        flag = Path(args.state_dir) / "STOP"
        if args.clear:
            if flag.exists():
                flag.unlink()
            print(json.dumps({"stop": False, "path": str(flag)}))
        else:
            atomic_write(flag, datetime_now())
            print(json.dumps({"stop": True, "path": str(flag)}))
        return 0
    clock = None
    if args.now:
        fixed = dt.datetime.fromisoformat(args.now)
        if fixed.tzinfo is None:
            raise SystemExit("--now requires a UTC-aware instant")
        clock = lambda: fixed.astimezone(dt.timezone.utc)
    try:
        pilot = Autopilot(state_dir=args.state_dir, companion_state=args.companion_state,
                          manifest_path=args.manifest, clock=clock, grace=args.grace_seconds,
                          interval=args.interval, once=args.once or args.command == "check")
        pilot.max_seconds = args.max_seconds
        if args.command == "check":
            decision = pilot.decide(clock() if clock else pilot.clock(), for_run=False)
        else:
            decision = pilot.run()
    except SingletonBusy:
        print(json.dumps({"action": "busy", "reason": "singleton"}))
        return 3
    except AutopilotError as error:
        print(json.dumps({"action": "error", "reason": redact(error)}))
        return 1
    print(json.dumps(decision))
    return _exit_code(decision.get("action"))


def datetime_now():
    return json.dumps({"set": dt.datetime.now(dt.timezone.utc).isoformat()})


def _exit_code(action):
    # dispatch/idle/stopped = normal; busy = singleton; error = failure; every
    # other bounded state (gate, quota, capacity, recovery, winddown) = 75.
    return {"dispatch": 0, "idle_empty": 0, "stopped": 0, "busy": 3, "error": 1}.get(action, 75)


if __name__ == "__main__":
    raise SystemExit(main())
