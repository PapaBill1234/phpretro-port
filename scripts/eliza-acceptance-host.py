#!/usr/bin/env python3
"""Isolated PRODUCTION full-Eliza acceptance host: thin adapter over the
reviewed dashboard supervisor (``scripts/eliza-dashboard.py``).

This does not reimplement supervision.  It registers one immutable
``ACCEPTANCE_PROFILE`` and delegates ``start``/``stop``/``status``/``check`` to
the reviewed ``eliza-dashboard`` helpers, so the OS lock, captured cutoff,
partial-startup group cleanup, PID start-tick signalling and private filesystem
are shared, not duplicated.  It owns only the fixed acceptance trees:
``.builder/eliza-acceptance`` (the retained default) and the fresh run
namespaces ``.builder/eliza-acceptance-runs/<run-id>``.  A run id is validated
against a strict pattern before any filesystem access; the selected namespace is
scoped to one command and restored in ``finally``.

No install, download, build, launch, browser, model, account or credential call
happens here; ``check``/``status`` are read-only and ``start`` is where the root
later runs the real host after review.
"""

import argparse
import contextvars
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = _load("eliza_dashboard", _HERE / "eliza-dashboard.py")
upstream = _load("eliza_upstream", _HERE / "eliza-upstream.py")

DashboardError = dash.DashboardError
UPSTREAM_PIN = upstream.UPSTREAM_PIN
SOURCE_REL = upstream.SOURCE_REL

ROOT = Path(__file__).resolve().parents[1]  # fixed; no arbitrary CLI root
ACCEPTANCE_REL = Path(".builder") / "eliza-acceptance"  # retained default namespace
RUNS_REL = Path(".builder") / "eliza-acceptance-runs"  # fresh run namespaces live here
# One acceptance-wide admission lock, shared by the default tree and every run.
ADMISSION_REL = RUNS_REL / "admission.lock"
# A run id is validated with a full match before any path is built from it.
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# The command-scoped selected namespace.  A ContextVar keeps it isolated per
# thread/context so concurrent callers cannot corrupt one another's selection;
# it is always restored in ``finally``.
_NAMESPACE = contextvars.ContextVar("acceptance_namespace", default=ACCEPTANCE_REL)
PATCH_REL = Path("builder") / "eliza" / "patches" / "new-chat.patch"
PATCH_SHA256 = "79c302be725fbf99e6b9639dc352ca0a9f68e95071f722d1456731fd820ebb63"
ENTRY_REL = Path("packages") / "app" / "src" / "entry.ts"

API_HOST = "127.0.0.1"
API_PORT = 18831
UI_PORT = 18832
ALLOWED_ORIGINS = "http://127.0.0.1:%d,http://127.0.0.1:%d" % (UI_PORT, API_PORT)

# The only paths the reviewed patch may change.  Any other overlay difference
# from the pinned canonical checkout is treated as an unknown change and refused.
EXPECTED_CHANGED = frozenset({
    "packages/ui/src/styles/styles.css",
    "packages/ui/src/components/shell/ChatOverlay.tsx",
})
EXPECTED_ADDED = frozenset({
    "packages/ui/src/components/shell/__e2e__/new-chat.test.tsx",
    "packages/ui/src/components/shell/useNewChatAction.ts",
})

# Physical drop-ins: a REAL package directory under the fresh state's
# plugins/custom, never a symlinked directory (drop-in discovery skips symlink
# dirs).  Each drop-in carries its original package id and a small physical
# index that imports the fixed verified CANONICAL module in place via a file
# URL, so the package's own relative layout (e.g. native-model/transport.mjs
# resolving ``../../../scripts/codex-generate.py``, and the companion root
# layout) stays valid.  The whole module is never copied out of its layout.
DROPINS = {
    "@phpretro/plugin-project-companion": {
        "canonical": Path("builder") / "eliza" / "upstream-plugin",
        "entry": Path("src") / "index.js",
    },
    "@phpretro/eliza-native-model": {
        "canonical": Path("builder") / "eliza" / "native-model",
        "entry": Path("index.mjs"),
    },
}

_DROPIN_INDEX = (
    "// Physical drop-in: re-exports the verified canonical module in place so\n"
    "// the package's own relative layout (e.g. ../../../scripts) stays valid.\n"
    'export * from "%s";\n'
    'import * as canonical from "%s";\n'
    "export { canonical };\n"
    "// The pinned loader requires a capability array even for model-only plugins.\n"
    "export const plugin = { ...canonical.default, providers: canonical.default.providers ?? [] };\n"
    "export default plugin;\n"
)
DEFAULT_WINDOW_S = 3600
SETUP_BUDGET_S = 240

# Verified canonical runtimes (reused from the pinned upstream installer layout).
_NODE = ROOT / ".builder" / "upstream" / "runtimes" / "node-v24.15.0-linux-x64" / "bin" / "node"
_BUN = ROOT / ".builder" / "upstream" / "runtimes" / "bun" / "package" / "bin" / "bun"


class AcceptanceError(DashboardError):
    """A fail-closed refusal safe to print."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --- namespace selection (command-scoped, restored in ``finally``) ----------
class _Namespace:
    """Select one fixed acceptance namespace for the duration of a command."""

    def __init__(self, rel):
        self.rel = Path(rel)
        self.token = None

    def __enter__(self):
        self.token = _NAMESPACE.set(self.rel)
        return self.rel

    def __exit__(self, *exc):
        _NAMESPACE.reset(self.token)
        self.token = None
        return False


def _current_rel():
    return _NAMESPACE.get()


def run_namespace(run_id):
    """Return the fixed namespace for a validated run id, or the default."""
    if run_id is None:
        return ACCEPTANCE_REL
    if not RUN_ID_RE.fullmatch(run_id):
        raise AcceptanceError("run id must match [a-z0-9][a-z0-9-]{0,47}")
    return RUNS_REL / run_id


def _bound(ns, fn):
    """Bind a profile callback to the namespace captured when it was built."""
    def wrapped(*args, **kwargs):
        with _Namespace(ns):
            return fn(*args, **kwargs)
    return wrapped


# --- private layout ---------------------------------------------------------
def acceptance_root(root):
    return Path(root) / _current_rel()


def overlay_dir(root):
    return acceptance_root(root) / "source"


def state_dir(root):
    return acceptance_root(root) / "state"


def config_path(root):
    return state_dir(root) / "runtime" / "eliza.json"


def cutoff_path(root):
    return acceptance_root(root) / "cutoff.json"


def operation_lock_path(root):
    return acceptance_root(root) / "operation.lock"


def provenance_path(root):
    return acceptance_root(root) / "overlay-provenance.json"


def canonical_source(root):
    return Path(root) / SOURCE_REL


def assert_fixed_path(root, target, rel):
    """Containment for a fixed owned path prefix, with symlink ancestor checks.

    The **entire** prefix is compared (not a fixed component count), so sibling
    trees such as ``.builder/eliza-acceptance-runs`` are never mistaken for the
    retained ``.builder/eliza-acceptance`` default.
    """
    root, target = Path(root), Path(target)
    rel = Path(rel)
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise AcceptanceError("acceptance path escapes project")
    prefix = rel.parts
    if (len(relative.parts) < len(prefix) or ".." in relative.parts
            or relative.parts[:len(prefix)] != prefix):
        raise AcceptanceError("acceptance path escapes owned tree")
    if root.is_symlink():
        raise AcceptanceError("symlink in acceptance path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AcceptanceError("symlink in acceptance path")
    return target


def assert_private_path(root, target, rel=None):
    """Contain ``target`` in the currently selected acceptance namespace."""
    return assert_fixed_path(root, target, _current_rel() if rel is None else rel)


def _reject_namespace_symlinks(root):
    """Read-only: reject symlinked ancestors of the selected namespace."""
    root = Path(root)
    if root.is_symlink():
        raise AcceptanceError("project root is a symlink")
    current = root
    for part in _current_rel().parts:
        current = current / part
        if current.is_symlink():
            raise AcceptanceError("symlink in acceptance namespace")
    return current


def _ensure_private_chain(root, target):
    """Create the missing owned 0700 components of ``target`` under ``root``.

    Only directories that do not yet exist are created (mode 0700); an existing
    component must already be an owned ``0700`` directory and is never re-moded
    or repaired.  A symlink, foreign owner, wrong mode or non-directory
    component is refused.
    """
    root, target = Path(root), Path(target)
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise AcceptanceError("acceptance path escapes project")
    current = root
    for part in rel.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AcceptanceError("acceptance namespace parent is unsafe")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AcceptanceError("acceptance namespace parent is not owned 0700")
    return target


def _prevalidate_private_parents(root):
    """Read-only refusal of a pre-existing namespace parent that is not owned 0700.

    Runs *before* ``_Admission`` (which creates/chmods the fixed shared
    ``.builder/eliza-acceptance-runs`` parent and the namespace) and before
    ``dash.ensure_state_dir`` (which chmods the selected namespace), so an unsafe
    or foreign-owned pre-existing parent is refused instead of being silently
    repaired.  Both the selected namespace chain and the fixed shared run parent
    are checked, so the default namespace cannot bypass the shared parent's
    validation.  A parent that does not yet exist is left to the safe creator.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise AcceptanceError("project root is not a safe directory")
    for rel in (Path(_current_rel()), RUNS_REL):
        current = root
        for part in rel.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue  # not created yet; the creator creates it 0700
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AcceptanceError("acceptance namespace parent is unsafe")
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise AcceptanceError("acceptance namespace parent is not owned 0700")
    return root


# --- source / profile -------------------------------------------------------
def _acceptance_source(root, ns=None, captured_stop=None):
    ns = _current_rel() if ns is None else Path(ns)
    return {
        "name": "eliza-acceptance-overlay",
        "managed": False,
        "path": overlay_dir(root),
        "pin": UPSTREAM_PIN,
        "bun": _BUN,
        "node": _NODE,
        "bun_version": "1.4.2",
        "node_version": "v24.15.0",
        "api_entry": ENTRY_REL,
        "vite": Path("node_modules") / "vite" / "bin" / "vite.js",
        "api_cwd": Path("."),
        "ui_cwd": Path("packages") / "app",
        # Registered validator: overlay vs canonical runtimes; never a caller
        # value.  The effective captured deadline bounds the whole validation.
        "validate": _bound(ns, lambda root: validate_overlay_source(root, launch_deadline(root, captured_stop))),
    }


def _verify_runtimes(root):
    for kind, expected in (("node", _NODE), ("bun", _BUN)):
        verified = upstream.verified_runtime(root, kind)
        if verified is None or Path(verified) != expected:
            raise AcceptanceError("registered %s runtime is not the verified canonical binary" % kind)
    return True


def _verify_canonical_source(root, deadline=None):
    _remaining(deadline, "canonical source")
    cutoff = None if deadline is None else (datetime.now(timezone.utc) + timedelta(seconds=deadline - time.monotonic())).isoformat()
    state = upstream.source_state(root, deadline=cutoff)
    _remaining(deadline, "canonical source")
    if not state.get("ok"):
        raise AcceptanceError("canonical source is not pinned and clean")
    return state


def launch_deadline(root, captured_stop=None):
    """Deadline for launch validation.

    Prefers the effective cutoff captured by ``start`` (caller/canonical clamp)
    and falls back to the immutable saved cutoff for other callers.
    """
    stop = captured_stop if captured_stop is not None else read_cutoff(root)
    if stop is None:
        return None
    deadline = time.monotonic() + stop.timestamp() - time.time() - 5
    _remaining(deadline, "launch prerequisites")
    return deadline


def launch_problems(root, captured_stop=None):
    deadline = launch_deadline(root, captured_stop)
    verify_overlay(root, deadline)
    if not _dropin_ready(root, deadline):
        return ["acceptance drop-ins not verified"]
    _remaining(deadline, "launch prerequisite completion")
    return []


def validate_overlay_source(root, deadline=None):
    root = Path(root)
    _verify_canonical_source(root, deadline)
    src = overlay_dir(root)
    if src.is_symlink() or not src.is_dir():
        raise AcceptanceError("acceptance overlay is unavailable")
    if not (src / ENTRY_REL).is_file():
        raise AcceptanceError("acceptance overlay is missing the api entry")
    if not (src / "node_modules" / "vite" / "bin" / "vite.js").is_file():
        raise AcceptanceError("acceptance overlay is missing the vite launcher")
    _remaining(deadline, "runtime prerequisites")
    _verify_runtimes(root)
    _remaining(deadline, "runtime prerequisites")
    verify_overlay(root, deadline)  # strict known-patch-only check
    return {"name": "eliza-acceptance-overlay", "pin": UPSTREAM_PIN, "readiness": True}


def acceptance_profile(root, window_s=DEFAULT_WINDOW_S, captured_stop=None):
    """The single immutable acceptance launch profile.

    Every callback is bound to the namespace selected when the profile was
    built, so a later namespace restore cannot move a running launch.  When
    ``captured_stop`` is given (always from ``start``), it threads the effective
    caller/saved cutoff through validation and the gate.
    """
    ns = _current_rel()
    state = state_dir(root)
    return dash.LaunchProfile(
        name="eliza-acceptance",
        state_relative=ns,
        api_host=API_HOST,
        api_port=API_PORT,
        ui_port=UI_PORT,
        allowed_origins=ALLOWED_ORIGINS,
        allowed_hosts=API_HOST,
        source=_acceptance_source(root, ns, captured_stop),
        run_path=overlay_dir(root),
        secrets=dash.SECRET_FILES,
        dotenv=None,
        compat=False,
        local_auth=True,
        autonomy=False,
        routing=False,
        home=state / "home",
        window_seconds=window_s,
        gate=_bound(ns, lambda r: acceptance_gate(r, window_s, captured_stop)),
        load_secrets=_bound(ns, lambda r, s: _acceptance_secrets(r)),
        verify=_bound(ns, lambda r, s: launch_problems(r, captured_stop)),
    )


# Persisted vault state is bound to the existing passphrase: the PGlite vault
# directory and the legacy vault.json both invalidate a regenerated passphrase,
# so a missing passphrase must refuse rather than mint a new key.
VAULT_STATE_REL = Path(".vault-pglite")
LEGACY_VAULT_REL = Path("vault.json")


def _private_runtime_dirs(root):
    """Every intermediate and leaf private directory that holds credentials or
    runtime state (never the regular-copy ``source`` overlay, whose copied files
    keep their canonical modes)."""
    state = state_dir(root)
    runtime = state / "runtime"
    return (state, runtime, runtime / "db", runtime / "workspace", state / "home")


def vault_state_present(runtime) -> bool:
    """True when persisted vault state binds values to the existing passphrase."""
    runtime = Path(runtime)
    return (runtime / VAULT_STATE_REL).exists() or (runtime / LEGACY_VAULT_REL).exists()


def _acceptance_setup_state(root, deadline=None):
    """Setup: create the private directories once and the canonical credentials.

    Existing directories are validated (owned, 0700, no symlink), never
    re-moded; a missing directory is created 0700.  Credentials are created once
    and reused unchanged; a corrupted or unsafe file is refused, and a missing
    passphrase is refused when persisted vault state exists (never replaced).
    """
    _remaining(deadline, "private directory")
    state = assert_private_path(root, acceptance_root(root))
    _ensure_private_chain(root, state)
    for directory in _private_runtime_dirs(root):
        _remaining(deadline, "private directory")
        assert_private_path(root, directory)
        _ensure_private_chain(root, directory)
        dash.require_private_dir(directory)
    runtime = state_dir(root) / "runtime"
    if not (state / "vault.passphrase").exists() and vault_state_present(runtime):
        raise AcceptanceError("persisted vault state requires the existing vault passphrase")
    _remaining(deadline, "credentials")
    dash.create_credentials(state)
    return {"profile": runtime}


def _acceptance_secrets(root, deadline=None):
    """Read-only load of the acceptance credentials and private directories.

    Creates and re-modes nothing; a missing or unsafe directory, an unsafe or
    malformed credential, or a missing passphrase with persisted vault state is
    refused before any launch.
    """
    state = assert_private_path(root, acceptance_root(root))
    _prevalidate_private_parents(root)
    for directory in _private_runtime_dirs(root):
        _remaining(deadline, "private directory")
        assert_private_path(root, directory)
        dash.require_private_dir(directory)
    runtime = state_dir(root) / "runtime"
    if not (state / "vault.passphrase").exists() and vault_state_present(runtime):
        raise AcceptanceError("persisted vault state requires the existing vault passphrase")
    try:
        credentials = dash.load_local_credentials(state)
    except FileNotFoundError:
        raise AcceptanceError("acceptance credentials are incomplete") from None
    except DashboardError as exc:
        raise AcceptanceError(str(exc)) from None
    return {"profile": runtime, **credentials}


# --- bounded cutoff (captured once, never extended) -------------------------
def read_cutoff(root):
    """Read the immutable saved cutoff without creating or modifying anything."""
    path = assert_private_path(root, cutoff_path(root))
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    stop = datetime.fromisoformat(str(data["stop_at"]))
    if stop.tzinfo is None:
        raise AcceptanceError("saved cutoff is not timezone-aware")
    return stop


def _operation_stop(root):
    """Return the local maximum cutoff for a provider-free invocation.

    Acceptance is provider-free, so its operation window is local and finite;
    the general dashboard keeps its separate provider-backed schedule gate.
    """
    return datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_WINDOW_S)


def effective_cutoff(root, window_s=DEFAULT_WINDOW_S):
    """Effective stop time, computed in memory only.

    The finite caller window and saved cutoff can only bring the deadline
    forward; the saved cutoff is never rewritten or extended.
    """
    if window_s <= 0:
        raise AcceptanceError("operation window must be positive")
    stop = min(_operation_stop(root), datetime.now(timezone.utc) + timedelta(seconds=window_s))
    saved = read_cutoff(root)
    return min(stop, saved) if saved is not None else stop


def preflight_expired(root, window_s=DEFAULT_WINDOW_S):
    """Read-only expiry refusal, safe to run before any lock or state write."""
    return datetime.now(timezone.utc) >= effective_cutoff(root, window_s)


def _ensure_cutoff(root, stop_at):
    """Create cutoff.json once, exclusively; never rewrite an existing file."""
    path = assert_private_path(root, cutoff_path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = (json.dumps({"stop_at": stop_at.isoformat()}) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def acceptance_gate(root, window_s=DEFAULT_WINDOW_S, captured_stop=None):
    """Gate the run, capturing the effective cutoff once.

    Without ``captured_stop`` this captures and returns the effective stop
    (caller window vs saved), creating ``cutoff.json`` once. With
    ``captured_stop`` (the gate callback invoked by the delegated supervisor) it
    may only tighten against the current operation window and the saved file — it never
    extends the captured deadline and never rewrites the file.
    """
    root = Path(root)
    if captured_stop is None:
        stop_at = effective_cutoff(root, window_s)  # capture, memory-only clamp
    else:
        stop_at = min(captured_stop, effective_cutoff(root, window_s))
        saved = read_cutoff(root)
        if saved is not None:
            stop_at = min(stop_at, saved)
    if datetime.now(timezone.utc) >= stop_at:
        raise AcceptanceError("acceptance cutoff reached; no launch")
    _ensure_cutoff(root, stop_at)  # created once, exclusively, under admission
    return {"stop_at": stop_at.isoformat(), "allowed": True}


def _remaining(deadline, stage):
    if deadline is not None and time.monotonic() >= deadline:
        raise AcceptanceError("acceptance %s exceeded the shared deadline" % stage, code=2)


# --- admission (fixed acceptance-wide lock + per-run operation lock) --------
class _AdmissionBusy(Exception):
    """Another setup/start already holds admission."""


def _acquire_operation_lock(path):
    """Non-blocking exclusive lock on a host lock file.

    O_CLOEXEC keeps the descriptor out of every child, so the delegated
    dashboard supervisor and its children never inherit admission.  On
    contention the descriptor is closed exactly once before raising.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise AcceptanceError("operation lock is unsafe")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise AcceptanceError("operation lock is unsafe")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _AdmissionBusy()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unlock(fd):
    if fd is None:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return None


class _Admission:
    """Admission for setup/start, held through supervision and cleanup.

    Acquires the fixed acceptance-wide lock first (shared by the default tree
    and every run) and then the per-run operation lock; releases in reverse
    order.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.shared_fd = None
        self.fd = None

    def __enter__(self):
        _reject_namespace_symlinks(self.root)
        # 1) fixed acceptance-wide admission lock (never namespace-scoped)
        shared = assert_fixed_path(self.root, self.root / ADMISSION_REL, RUNS_REL)
        _ensure_private_chain(self.root, shared.parent)
        self.shared_fd = _acquire_operation_lock(shared)
        try:
            # 2) per-run operation lock
            root_dir = assert_private_path(self.root, acceptance_root(self.root))
            _ensure_private_chain(self.root, root_dir)
            path = assert_private_path(self.root, operation_lock_path(self.root))
            self.fd = _acquire_operation_lock(path)
        except BaseException:
            self.shared_fd = _unlock(self.shared_fd)
            raise
        return self

    def __exit__(self, *exc):
        self.fd = _unlock(self.fd)
        self.shared_fd = _unlock(self.shared_fd)
        return False


def require_admission(root):
    """Enter admission or raise ``_AdmissionBusy`` (never leaks the fd)."""
    return _Admission(root)


def _namespace_active(root):
    """Read-only: refuse overlap when this namespace already has a live child.

    Reads the dashboard runtime record at the acceptance namespace root (where
    the delegated supervisor writes ``runtime.json``).  An unknown pid identity
    is treated as gone, never as a match; a live but unverified ownership is
    refused rather than assumed safe.
    """
    try:
        runtime = dash.read_runtime(acceptance_root(root))
    except DashboardError:
        return True  # unsafe runtime state is treated as active
    if not runtime:
        return False
    for name in ("api", "ui"):
        pid = runtime.get(f"{name}Pid")
        if pid is None:
            continue
        identity = dash.pid_identity(pid)
        if identity is None:
            continue  # recorded pid no longer exists; not active
        start = runtime.get(f"{name}Start")
        if start is None or str(identity) != str(start):
            return True  # live but unverified ownership; refuse overlap
        return True
    return False


def _reserve_ports():
    """Check both loopback ports are free under admission, holding them briefly."""
    sockets = []
    try:
        for port in (API_PORT, UI_PORT):
            sock = socket.socket()
            sockets.append(sock)  # close even if this bind fails
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((API_HOST, port))
    except OSError:
        raise AcceptanceError("a loopback port is busy")
    finally:
        for sock in sockets:
            sock.close()


def sha256_file(path, deadline=None):
    _remaining(deadline, "hash")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            _remaining(deadline, "hash")
            digest.update(chunk)
    return digest.hexdigest()


def verify_patch(root, deadline=None):
    patch = Path(root) / PATCH_REL
    if not patch.is_file() or sha256_file(patch, deadline) != PATCH_SHA256:
        raise AcceptanceError("reviewed patch missing or digest mismatch")
    return patch


# --- overlay build (regular copies; no shared source inodes) ----------------
def _tracked_paths(source, deadline=None):
    _remaining(deadline, "git inventory")
    timeout = min(30, deadline - time.monotonic()) if deadline is not None else 30
    out = upstream._git(["ls-files", "--stage", "-z"], cwd=source, timeout=timeout) if hasattr(upstream, "_git") else None
    if out is None:
        import subprocess
        out = subprocess.run(["git", "-C", str(source), "ls-files", "--stage", "-z"],
                             capture_output=True, text=True, check=True, timeout=timeout)
    paths = []
    for row in out.stdout.split("\0"):
        if not row:
            continue
        metadata, rel = row.split("\t", 1)
        mode = metadata.split()[0]
        if mode == "160000":  # optional native-inference submodule, not initialized here
            continue
        if mode not in ("100644", "100755", "120000"):
            raise AcceptanceError("unsupported canonical Git entry")
        paths.append(rel)
    _remaining(deadline, "git inventory completion")
    return paths


# The pinned children are launched with only ``--conditions=eliza-source``
# (``scripts/eliza-dashboard.py``); no other source condition is requested, so a
# bare ``"source"`` or ``"bun"`` key never qualifies a member as source-complete.
_EXPORT_CONDITION_ORDER = ("eliza-source", "import", "default")


def _resolve_export_target(value):
    """The pinned resolution target of one ``exports`` value.

    Mirrors the pinned app's condition preference over the only source condition
    the children request: ``eliza-source`` first, then ``import``/``default``.
    Array fallbacks and nested condition maps are followed.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for entry in value:
            target = _resolve_export_target(entry)
            if target is not None:
                return target
        return None
    if isinstance(value, dict):
        for condition in _EXPORT_CONDITION_ORDER:
            if condition in value:
                target = _resolve_export_target(value[condition])
                if target is not None:
                    return target
    return None


def _exports_resolve_to_built_output(exports):
    """Whether any export key resolves through built ``dist/`` output.

    Accepts the ``exports`` shapes Node allows: a plain string target, an array
    of fallback targets, or a key map whose values are strings, arrays or nested
    condition maps.  Not covered (and absent upstream today): a member with no
    ``exports`` at all, whose ``main`` alone points at built output.
    """
    if isinstance(exports, str):
        return exports.startswith("./dist/")
    if isinstance(exports, list):
        return any(_exports_resolve_to_built_output(entry) for entry in exports)
    if isinstance(exports, dict):
        for value in exports.values():
            if isinstance(value, (str, list)):
                if _exports_resolve_to_built_output(value):
                    return True
            else:
                target = _resolve_export_target(value)
                if target is not None and target.startswith("./dist/"):
                    return True
    return False


def _workspace_package_needs_built_output(package_dir):
    """Whether a workspace package resolves only through its built ``dist/``.

    The overlay copies pinned *tracked* source only, so it never contains the
    build output a package's ``exports`` may resolve to.  When the pinned
    ``eliza-source`` condition set cannot serve the package (some export key
    pins ``dist/*.js``), the member must keep its complete verified canonical
    package binding; otherwise the source-only overlay copy drops that built
    target and Vite's import analysis fails for the app's plugin-registration
    seam (``@elizaos/plugin-native-settings/register``,
    ``@elizaos/plugin-trajectory-logger/register``).
    """
    package_dir = Path(package_dir)
    try:
        manifest = json.loads((package_dir / "package.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or not (package_dir / "dist").is_dir():
        return False
    return "exports" in manifest and _exports_resolve_to_built_output(manifest["exports"])


def _workspace_package_is_overlay_required(relative):
    """The reviewed patch's own package must always resolve from the overlay."""
    relative = str(relative)
    prefix = relative.rstrip("/") + "/"
    return any(rel == relative or rel.startswith(prefix)
               for rel in EXPECTED_CHANGED | EXPECTED_ADDED)


def dependency_bindings(source, overlay, deadline=None):
    """Exact installed dependency locations; workspace links resolve in the overlay.

    A member the pinned ``eliza-source`` conditions can serve from source binds
    to the private overlay copy, as does the reviewed patch's own package.  A
    member that resolves through built ``dist/`` output (the overlay never
    carries build output) keeps its complete verified canonical package binding,
    because the source-only overlay would otherwise drop that built subpath.
    """
    source, overlay = Path(source).resolve(), Path(overlay)
    bindings = {}
    for directory, names, _files in os.walk(source):
        _remaining(deadline, "dependency inventory")
        names[:] = [name for name in names if name != ".git"]
        if "node_modules" not in names:
            continue
        names.remove("node_modules")
        location = Path(directory) / "node_modules"
        rel = location.relative_to(source)
        entries = {}
        for entry in sorted(location.iterdir()):
            _remaining(deadline, "dependency inventory")
            if entry.name == "@phpretro":
                continue
            members = sorted(entry.iterdir()) if entry.name.startswith("@") and entry.is_dir() else [entry]
            for member in members:
                name = str(member.relative_to(location))
                resolved = member.resolve()
                target = member
                if _under(resolved, source):
                    relative = resolved.relative_to(source)
                    if "node_modules" not in relative.parts and (
                            _workspace_package_is_overlay_required(relative)
                            or not _workspace_package_needs_built_output(resolved)):
                        target = overlay / relative
                entries[name] = str(target)
        bindings[str(rel)] = entries
    return bindings


def link_dependencies(root, overlay, deadline=None):
    source = canonical_source(root)
    for rel, entries in dependency_bindings(source, overlay, deadline).items():
        location = assert_private_path(root, overlay / rel)
        location.mkdir(parents=True, exist_ok=True)
        for name, target in entries.items():
            _remaining(deadline, "dependency links")
            destination = location / name
            if destination.parent != location:
                parent = destination.parent
                assert_private_path(root, parent.parent)
                if parent.is_symlink():
                    if os.readlink(parent) != str(Path(source).resolve() / rel / parent.name):
                        raise AcceptanceError("unknown existing dependency scope")
                    parent.unlink()  # replace only the known legacy canonical scope link
                assert_private_path(root, parent)
                parent.mkdir(exist_ok=True)
            if destination.is_symlink():
                if os.readlink(destination) == target:
                    continue
                if os.readlink(destination) != str(Path(source).resolve() / rel / name):
                    raise AcceptanceError("unknown existing dependency binding")
                destination.unlink()  # replace only the known old canonical workspace binding
            elif destination.exists():
                raise AcceptanceError("unexpected regular dependency entry")
            os.symlink(target, destination)


def verify_dependencies(root, overlay, deadline=None):
    bindings = dependency_bindings(canonical_source(root), overlay, deadline)
    for rel, entries in bindings.items():
        _remaining(deadline, "dependency verification")
        location = overlay / rel
        if location.is_symlink() or not location.is_dir():
            raise AcceptanceError("installed dependency location missing or symlinked")
        if {item.name for item in location.iterdir()} != {name.split("/")[0] for name in entries}:
            raise AcceptanceError("unexpected installed dependency scope")
        found = set()
        for item in location.iterdir():
            if item.name.startswith("@") and not item.is_symlink() and item.is_dir():
                found.update(str(member.relative_to(location)) for member in item.iterdir())
            else:
                found.add(item.name)
        if found != set(entries):
            raise AcceptanceError("unexpected installed dependency entry")
        for name, target in entries.items():
            entry = location / name
            if not entry.is_symlink() or os.readlink(entry) != target:
                raise AcceptanceError("installed dependency binding differs from canonical layout")
    return set(bindings)


def build_overlay(root, deadline=None):
    root = Path(root)
    _verify_canonical_source(root, deadline)
    source = canonical_source(root)
    overlay = assert_private_path(root, overlay_dir(root))
    if overlay.exists():
        if not _overlay_provenance_matches(root):
            raise AcceptanceError("existing overlay has unknown provenance; refusing to overwrite")
        link_dependencies(root, overlay, deadline)
        verify_overlay(root, deadline)
        return overlay
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.mkdir(mode=0o700)
    made = True
    try:
        _remaining(deadline, "copy")
        for rel in _tracked_paths(source, deadline):
            _remaining(deadline, "copy")
            dst = overlay / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            original = source / rel
            if original.is_symlink():
                link = os.readlink(original)
                if os.path.isabs(link) or not _under(original.resolve(), source):
                    raise AcceptanceError("canonical symlink escapes source")
                os.symlink(link, dst)  # preserve only pinned internal aliases
            else:
                shutil.copy2(original, dst)  # regular copy, no shared inode
        _remaining(deadline, "node_modules")
        link_dependencies(root, overlay, deadline)
        _remaining(deadline, "patch")
        patch = verify_patch(root, deadline)
        _apply_patch(overlay, patch, deadline)
        _remaining(deadline, "provenance")
        _write_provenance(root, patch, deadline)
        verify_overlay(root, deadline)
    except BaseException:
        if made and overlay.is_dir() and _under(overlay, acceptance_root(root)):
            shutil.rmtree(overlay, ignore_errors=True)
        raise
    return overlay


def _apply_patch(overlay, patch, deadline=None):
    import subprocess
    for args in (["--check"], [], ["--check", "--reverse"]):
        _remaining(deadline, "patch")
        timeout = min(30, deadline - time.monotonic()) if deadline is not None else 30
        proc = subprocess.run(["git", "apply", *args, str(patch)], cwd=str(overlay),
                              capture_output=True, text=True, timeout=timeout,
                              env={"PATH": "/usr/bin:/bin", "GIT_CEILING_DIRECTORIES": str(overlay.parent.resolve()),
                                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        if proc.returncode != 0:
            raise AcceptanceError("overlay patch step failed: %s" % proc.stderr.strip())


def _under(path, base):
    try:
        p, b = Path(path).resolve(), Path(base).resolve()
    except OSError:
        return False
    return p == b or b in p.parents


def verify_overlay(root, deadline=None):
    """Strict provenance: only the reviewed paths may differ; reverse-apply alone
    is not sufficient, so every tracked file is compared by hash."""
    root = Path(root)
    source = canonical_source(root)
    overlay = assert_private_path(root, overlay_dir(root))
    dependency_locations = verify_dependencies(root, overlay, deadline)
    tracked = set(_tracked_paths(source, deadline))
    changed, added = set(), set()
    source_links = {rel for rel in tracked if (source / rel).is_symlink()}
    for rel in tracked:
        _remaining(deadline, "overlay verification")
        o = overlay / rel
        if rel in source_links:
            if not o.is_symlink() or os.readlink(o) != os.readlink(source / rel) or not _under(o.resolve(), overlay):
                raise AcceptanceError("overlay alias differs from pinned internal source link")
            continue
        if o.is_symlink():
            raise AcceptanceError("symlinked tracked overlay file: " + rel)
        if not o.is_file():
            changed.add(rel)
            continue
        if sha256_file(o, deadline) != sha256_file(source / rel, deadline):
            changed.add(rel)
    # Unknown additions: any overlay file (outside node_modules) that is not a
    # tracked canonical path.  Reverse-apply alone cannot exclude these.
    for dirpath, dirnames, filenames in os.walk(overlay):
        _remaining(deadline, "overlay inventory")
        for name in dirnames:
            directory = Path(dirpath) / name
            rel = str(directory.relative_to(overlay))
            if directory.is_symlink() and rel not in source_links:
                raise AcceptanceError("unexpected overlay symlink directory")
        dirnames[:] = [d for d in dirnames if str((Path(dirpath) / d).relative_to(overlay)) not in dependency_locations]
        for name in filenames:
            rel = str(Path(dirpath).relative_to(overlay) / name)
            if rel not in tracked:
                added.add(rel)
    unknown = (changed - EXPECTED_CHANGED) | (added - EXPECTED_ADDED)
    if unknown:
        raise AcceptanceError("unknown overlay change(s): %s" % ", ".join(sorted(unknown)))
    if changed != EXPECTED_CHANGED or added != EXPECTED_ADDED:
        raise AcceptanceError("overlay does not contain exactly the reviewed patch diff")
    for rel, expected in expected_patch_bytes(root, deadline).items():
        _remaining(deadline, "patch byte verification")
        if (overlay / rel).is_symlink() or (overlay / rel).read_bytes() != expected:
            raise AcceptanceError("overlay output differs from reviewed patch: " + rel)
    # the overlay UI must be the patched copy, not the canonical workspace link
    ui = overlay / "node_modules" / "@elizaos" / "ui"
    if not ui.is_symlink() or ui.resolve() != (overlay / "packages" / "ui").resolve():
        raise AcceptanceError("@elizaos/ui does not resolve to the overlay copy")
    if not (state_dir(root) / "db").parent.is_dir():
        pass
    return True


def expected_patch_bytes(root, deadline=None):
    """Reconstruct reviewed outputs from canonical bytes, independent of Git cwd."""
    outputs = {}
    patch = verify_patch(root, deadline).read_text()
    for section in patch.split("diff --git ")[1:]:
        _remaining(deadline, "patch reconstruction")
        rel = section.splitlines()[0].split(" b/", 1)[1]
        if rel not in EXPECTED_CHANGED | EXPECTED_ADDED or rel in outputs:
            raise AcceptanceError("unexpected reviewed patch path")
        base = canonical_source(root) / rel
        rows = base.read_text().splitlines(keepends=True) if rel in EXPECTED_CHANGED else []
        offset = 0
        for hunk in re.finditer(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n((?:[ +\-][^\n]*\n)*)", section, re.M):
            body = hunk.group(2).splitlines(keepends=True)
            old = [line[1:] for line in body if line[0] in " -"]
            new = [line[1:] for line in body if line[0] in " +"]
            start = max(0, int(hunk.group(1)) - 1) + offset
            if rows[start:start + len(old)] != old:
                raise AcceptanceError("reviewed patch context differs from canonical source")
            rows[start:start + len(old)] = new
            offset += len(new) - len(old)
        outputs[rel] = "".join(rows).encode()
    if set(outputs) != EXPECTED_CHANGED | EXPECTED_ADDED:
        raise AcceptanceError("reviewed patch scope differs from expected files")
    return outputs


def _is_git(path):
    return (Path(path) / ".git").exists()


def _overlay_ok(root):
    try:
        return verify_overlay(root)
    except AcceptanceError:
        return False
    except Exception:
        return False


def _write_provenance(root, patch, deadline=None):
    _remaining(deadline, "provenance")
    path = assert_private_path(root, provenance_path(root))
    data = {
        "pin": UPSTREAM_PIN,
        "patch": str(PATCH_REL),
        "patch_sha256": sha256_file(patch, deadline),
        "changed": sorted(EXPECTED_CHANGED),
        "entry": str(ENTRY_REL),
        "dropins": dropin_provenance(root, deadline),
    }
    _remaining(deadline, "provenance write")
    path.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)


def _overlay_provenance_matches(root):
    try:
        data = json.loads(assert_private_path(root, provenance_path(root)).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("pin") == UPSTREAM_PIN and data.get("patch_sha256") == PATCH_SHA256


# --- physical drop-ins (real dirs; canonical import; verified) ---------------
def plugins_custom_dir(root):
    return state_dir(root) / "runtime" / "plugins" / "custom"


def dropin_dir(root, package):
    return plugins_custom_dir(root) / package.split("/")[-1]


def canonical_entry(root, package):
    spec = DROPINS[package]
    return Path(root) / spec["canonical"] / spec["entry"]


def install_dropins(root, deadline=None):
    """Create REAL drop-in package dirs (never symlinks) under the fresh state."""
    _remaining(deadline, "drop-in installation")
    root = Path(root)
    custom = assert_private_path(root, plugins_custom_dir(root))
    custom.mkdir(parents=True, exist_ok=True)
    os.chmod(custom, 0o700)
    created = []
    for package, spec in DROPINS.items():
        _remaining(deadline, "drop-in installation")
        target = assert_private_path(root, dropin_dir(root, package))
        if target.is_symlink():
            raise AcceptanceError("drop-in directory must not be a symlink: %s" % target)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        assert_private_path(root, target / "package.json")
        assert_private_path(root, target / "index.mjs")
        (target / "package.json").write_text(json.dumps({
            "name": package,
            "type": "module",
            "main": "./index.mjs",
            "exports": {".": {"import": "./index.mjs", "default": "./index.mjs"}},
        }, indent=2) + "\n")
        url = canonical_entry(root, package).resolve().as_uri()
        (target / "index.mjs").write_text(_DROPIN_INDEX % (url, url))
        created.append(str(target))
    return created


def dropin_provenance(root, deadline=None):
    """Content-hash provenance binding each drop-in to its canonical entry."""
    root = Path(root)
    data = {}
    for package in DROPINS:
        entry = canonical_entry(root, package)
        if not entry.is_file():
            raise AcceptanceError("canonical drop-in entry missing: %s" % package)
        data[package] = {"canonical": str(entry), "entry_sha256": sha256_file(entry, deadline)}
    return data


def _dropin_ready(root, deadline=None):
    _remaining(deadline, "drop-in verification")
    """Readiness: real dir, original package id, canonical file-URL import."""
    root = Path(root)
    prov = _overlay_data(root).get("dropins", {})
    for package in DROPINS:
        _remaining(deadline, "drop-in verification")
        target = assert_private_path(root, dropin_dir(root, package))
        pkg_json = assert_private_path(root, target / "package.json")
        index = assert_private_path(root, target / "index.mjs")
        if target.is_symlink() or not pkg_json.is_file() or not index.is_file():
            return False
        try:
            if json.loads(pkg_json.read_text()).get("name") != package:
                return False
        except (OSError, json.JSONDecodeError):
            return False
        url = canonical_entry(root, package).resolve().as_uri()
        if index.is_symlink() or index.read_text() != _DROPIN_INDEX % (url, url):
            return False
        recorded = prov.get(package, {}).get("entry_sha256")
        if recorded != sha256_file(canonical_entry(root, package), deadline):
            return False
    return True


def _overlay_data(root):
    try:
        return json.loads(assert_private_path(root, provenance_path(root)).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# --- config (verified schema only; no invented fields) ----------------------
def acceptance_config(root):
    """``plugins.entries`` per the pinned zod schema
    (packages/agent/src/config/zod-schema.ts): values allow only ``enabled`` and
    ``config``.  Provider routing and the generation guard are the plugin's, not
    config fields, so none are fabricated here."""
    return {"plugins": {"entries": {name: {"enabled": True} for name in DROPINS}},
            "agents": {"defaults": {"workspace": str(state_dir(root) / "runtime" / "workspace")}}}


def write_config(root, deadline=None):
    _remaining(deadline, "config write")
    path = assert_private_path(root, config_path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acceptance_config(root), indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


# --- commands ---------------------------------------------------------------
def cmd_check(root, _argv=None):
    """Read prerequisites without creating state, locks or a captured cutoff."""
    root = Path(root)
    problems = []
    overlay_ready = False
    try:
        _reject_namespace_symlinks(root)
        validate_overlay_source(root)
        overlay_ready = True
        if preflight_expired(root):
            raise AcceptanceError("acceptance cutoff reached; no launch")
        for port in (API_PORT, UI_PORT):
            dash.port_free(port, API_HOST)
        lock = assert_private_path(root, acceptance_root(root) / "dashboard.lock")
        if lock.exists() or lock.is_symlink():
            dash.read_private_text(lock)
    except (DashboardError, upstream.SetupError, OSError) as exc:
        problems.append(str(exc))
    try:
        dropins_ready = _dropin_ready(root)
    except (DashboardError, OSError):
        dropins_ready = False
    report = {
        "command": "check",
        "prerequisites": {"ok": not problems, "problems": problems},
        "overlay_ready": overlay_ready,
        "dropins_ready": dropins_ready,
        "host": "pending",
        "browser": "pending",
    }
    print(json.dumps(report, indent=2))
    return 0 if not problems and overlay_ready and dropins_ready else 1


def operation_budget(args, cap):
    seconds = getattr(args, "deadline", None)
    if seconds is None:
        return cap
    if seconds <= 0:
        raise AcceptanceError("operation deadline must be positive")
    return min(seconds, cap)


def cmd_setup(root, _argv=None):
    root = Path(root)
    budget = operation_budget(_argv, SETUP_BUDGET_S)
    # Read-only preflight: refuse an unsafe pre-existing parent and an expired
    # run before any lock/state write.
    _prevalidate_private_parents(root)
    if preflight_expired(root, DEFAULT_WINDOW_S):
        raise AcceptanceError("acceptance cutoff reached; no launch")
    try:
        admission = require_admission(root)
        admission.__enter__()
    except _AdmissionBusy:
        print(json.dumps({"command": "setup", "status": "busy"}))
        return 1
    try:
        # Revalidate under serialization before the first write.
        if preflight_expired(root, DEFAULT_WINDOW_S):
            raise AcceptanceError("acceptance cutoff reached; no launch")
        if _namespace_active(root):
            print(json.dumps({"command": "setup", "status": "busy",
                              "reason": "namespace active"}))
            return 1
        gate = acceptance_gate(root)  # creates cutoff once, under admission
        seconds = datetime.fromisoformat(gate["stop_at"]).timestamp() - time.time() - 5
        if seconds <= 0:
            raise AcceptanceError("acceptance setup has no cleanup budget")
        deadline = time.monotonic() + min(budget, seconds)
        build_overlay(root, deadline)
        _acceptance_setup_state(root, deadline)
        install_dropins(root, deadline)
        write_config(root, deadline)
        _remaining(deadline, "setup completion")
        print(json.dumps({"overlay": str(overlay_dir(root)),
                          "plugins": [str(dropin_dir(root, p)) for p in DROPINS],
                          "patch_sha256": PATCH_SHA256, "ready": True}, indent=2))
        return 0
    finally:
        admission.__exit__(None, None, None)


def cmd_start(root, _argv=None):
    root = Path(root)
    window_s = operation_budget(_argv, DEFAULT_WINDOW_S)
    # Read-only preflight: refuse an unsafe pre-existing parent and an expired
    # run before lock/state creation.
    _prevalidate_private_parents(root)
    if preflight_expired(root, window_s):
        raise AcceptanceError("acceptance cutoff reached; no launch")
    try:
        admission = require_admission(root)
        admission.__enter__()
    except _AdmissionBusy:
        print(json.dumps({"command": "start", "status": "busy"}))
        return 1
    try:
        # Revalidate under serialization, then refuse overlap with an active run.
        if preflight_expired(root, window_s):
            raise AcceptanceError("acceptance cutoff reached; no launch")
        if _namespace_active(root):
            print(json.dumps({"command": "start", "status": "busy",
                              "reason": "namespace active"}))
            return 1
        _reserve_ports()  # both ports checked under admission
        # Capture the effective caller/saved cutoff once, under admission,
        # and thread it through validation so slow work cannot overrun it.
        captured = acceptance_gate(root, window_s)
        captured_stop = datetime.fromisoformat(captured["stop_at"])
        profile = acceptance_profile(root, window_s, captured_stop)
        return dash.cmd_start(root, profile.source, profile)
    finally:
        admission.__exit__(None, None, None)


def cmd_stop(root, _argv=None):
    root = Path(root)
    _reject_namespace_symlinks(root)
    return dash.cmd_stop(root, acceptance_profile(root))


def cmd_status(root, _argv=None):
    root = Path(root)
    _reject_namespace_symlinks(root)
    return dash.cmd_status(root, acceptance_profile(root))


COMMANDS = {"check": cmd_check, "setup": cmd_setup, "start": cmd_start,
            "stop": cmd_stop, "status": cmd_status}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="check", choices=list(COMMANDS))
    parser.add_argument("--yes", action="store_true", help="confirm a gated action")
    parser.add_argument("--deadline", type=int, default=None, help="maximum operation seconds (setup capped at 240, start at 3600)")
    parser.add_argument("--run-id", default=None,
                        help="select an isolated run namespace under .builder/eliza-acceptance-runs/<id>")
    args = parser.parse_args(argv)
    try:
        rel = run_namespace(args.run_id)  # strict full-match before any filesystem access
    except AcceptanceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.command in ("setup", "start") and not args.yes:
        print("error: %s is gated; pass --yes" % args.command, file=sys.stderr)
        return 1
    try:
        with _Namespace(rel):
            return COMMANDS[args.command](ROOT, args)
    except subprocess.TimeoutExpired:
        print("error: acceptance subprocess exceeded the shared deadline", file=sys.stderr)
        return 2
    except DashboardError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return getattr(exc, "code", 1) if isinstance(exc, AcceptanceError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
