#!/usr/bin/env python3
"""Private, reproducible native text-host source/toolchain bootstrap and build.

This is a BOOTSTRAP/BUILD helper only, never runtime admission.  It builds the
pinned OpenAI Codex CLI from source into an isolated prefix under
``.builder/native-host``.  It owns only ``scripts/native-host.py``,
``tests/ops/test_native_host_setup.py`` and ``docs/eliza-native-host.md`` and
never edits the public ``builder/eliza/native-host`` patch tree, CMS/theme/port
trees, the tool guard or another worker's files.

Guarantees:
  * the codex source is verified at the exact pin and reused, never ``git
    reset``; any tracked modification or wrong revision is refused;
  * the reviewed public patch is verified by digest before it is applied to a
    private build checkout, so the trusted clone stays clean;
  * the official Rust toolchain is installed from
    ``https://static.rust-lang.org`` with SHA-256 verification into a private
    prefix, never globally and never into ``~/.cargo``/``~/.rustup``;
  * the build is one low-priority (``nice 19``) release job with no debuginfo,
    ``--locked`` dependencies, a private Cargo cache and a filtered environment
    that carries no provider credentials and copies no account/OAuth material;
  * every expensive stage is gated by the canonical off-peak coding window and
    runs in its own supervised process group bounded by the shared cutoff;
  * a successful build is build provenance only.  It does NOT qualify the model
    or toolset, does not run the host, a model, a turn or any service.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, nullcontext
from pathlib import Path

# --- pinned, non-overridable native-host identity ---------------------------
CODEX_REPO = "https://github.com/openai/codex.git"
CODEX_PIN = "c1382380de69521303b416720a52f42d51af6248"  # exact source revision
PACKAGE = "codex-cli"
BIN = "codex"
RUST_VERSION = "1.95.0"
RUST_TRIPLE = "x86_64-unknown-linux-gnu"
RUST_MANIFEST_URL = "https://static.rust-lang.org/dist/channel-rust-%s.toml" % RUST_VERSION
RUST_MANIFEST_SHA256_URL = RUST_MANIFEST_URL + ".sha256"
ALLOWED_HOSTS = frozenset({"static.rust-lang.org"})
PATCH_DIR_REL = Path("builder") / "eliza" / "native-host"
PATCH_MANIFEST = "provenance.json"
NATIVE_REL = Path(".builder") / "native-host"
JOBS = 1  # one low-priority build job, never a parallel herd
DISK_MIN_FREE_BYTES = 12 * 1024 ** 3
RAM_MIN_AVAILABLE_BYTES = 3 * 1024 ** 3
LOG_LIMIT_BYTES = 4 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 30  # bounded read-only git probes outside a gated stage
STOP_MARGIN_SECONDS = 5  # reserve room for cleanup before the immutable cutoff
CLEANUP_GRACE_SECONDS = 3  # TERM-to-KILL window, never allowed past the cutoff
MANIFEST_MAX_BYTES = 8 * 1024 * 1024  # bounded official manifest / sidecar download
ARCHIVE_MAX_DOWNLOAD_BYTES = 2 * 1024 ** 3  # bounded toolchain archive download
ARCHIVE_MAX_MEMBERS = 200000
ARCHIVE_MAX_TOTAL_BYTES = 6 * 1024 ** 3  # bounded total unpacked size
ARCHIVE_MAX_MEMBER_BYTES = 2 * 1024 ** 3
ROOT = Path(__file__).resolve().parents[1]
_CODING_WINDOW = None


class SetupError(Exception):
    """A fail-closed refusal; carries the process exit code to return."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --- private layout (never global, never ancestor) --------------------------
def private_root(root):
    return Path(root) / NATIVE_REL


def source_dir(root):
    return private_root(root) / "source"


def build_dir(root):
    return private_root(root) / "build"


def toolchain_dir(root):
    return private_root(root) / "toolchain"


def toolchain_bin_dir(root):
    return toolchain_dir(root) / "bin"


def toolchain_cargo(root):
    return toolchain_bin_dir(root) / "cargo"


def cargo_home(root):
    return private_root(root) / "cargo"


def target_dir(root):
    return private_root(root) / "target"


def cache_dir(root):
    return private_root(root) / "cache"


def logs_dir(root):
    return private_root(root) / "logs"


def home_dir(root):
    return private_root(root) / "home"


def provenance_path(root):
    return private_root(root) / "provenance.json"


def host_manifest_path(root):
    return Path(root) / PATCH_DIR_REL / PATCH_MANIFEST


# --- ownership containment (no symlink ancestors) ---------------------------
def _reject_symlink_chain(root, target):
    """Refuse if any component from ``root`` down to ``target`` is a symlink."""
    root = Path(root)
    target = Path(target)
    if not target.is_absolute():
        target = root / target
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise SetupError("path outside root: %s" % target, code=1)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise SetupError("unsafe symlink in path: %s" % current, code=1)
    return target


def assert_anchor(root, action="provision"):
    """Reject a symlinked .builder/native-host tree before any write or delete."""
    _reject_symlink_chain(root, private_root(root))
    return Path(private_root(root))


def assert_private(root, target, action):
    """Refuse a delete/install path that escapes, or crosses, a private symlink.

    A ``..`` component is rejected lexically *before* any normalization, so it
    can never be laundered into an inside-looking path, and every ancestor from
    ``.builder`` down to the target is symlink-checked before a write or delete.
    """
    base = assert_anchor(root, action)
    target = Path(target)
    if any(part == ".." for part in target.parts):
        raise SetupError("dotdot in private path: %s" % target, code=1)
    if not target.is_absolute():
        target = base / target
    try:
        target.relative_to(base)
    except ValueError:
        raise SetupError("path outside private tree: %s" % target, code=1)
    _reject_symlink_chain(root, target)
    return target


# --- off-peak gate (canonical scripts/coding-window.py) ---------------------
def coding_window(root=None):
    global _CODING_WINDOW
    if _CODING_WINDOW is not None:
        return _CODING_WINDOW
    path = (Path(root) if root else ROOT) / "scripts" / "coding-window.py"
    if not path.is_file():
        raise SetupError("canonical coding-window.py not found; no off-peak decision", code=75)
    spec = importlib.util.spec_from_file_location("phpretro_coding_window", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CODING_WINDOW = module
    return module


def gate_decision(now=None):
    return coding_window().decision(now)


def enforce_gate(now=None):
    decision = gate_decision(now)
    if not decision.get("allowed"):
        raise SetupError("off-peak gate closed (%s); refusing to run" % decision.get("reason"), code=75)
    return decision


def gate_cutoff():
    """The immutable off-peak cutoff every expensive stage shares."""
    decision = enforce_gate()
    deadline = decision.get("stop_at")
    if not deadline:
        raise SetupError("off-peak gate has no verified cutoff; refusing", code=75)
    return deadline


def parse_cutoff(deadline):
    """Epoch seconds for a strict, timezone-aware, finite cutoff; else refuse.

    Every expensive stage (network read or subprocess) must be bounded by a
    cutoff that is present, well formed, timezone-aware and finite.  A missing,
    naive, malformed or non-finite value is refused *before* any subprocess or
    network read starts, so work can never run unbounded or past the window.
    """
    if not isinstance(deadline, str) or not deadline.strip():
        raise SetupError("missing off-peak cutoff; refusing to start work", code=75)
    try:
        parsed = dt.datetime.fromisoformat(deadline)
    except (TypeError, ValueError):
        raise SetupError("malformed off-peak cutoff; refusing to start work", code=75)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SetupError("naive off-peak cutoff; refusing to start work", code=75)
    epoch = parsed.timestamp()
    if not math.isfinite(epoch):
        raise SetupError("non-finite off-peak cutoff; refusing to start work", code=75)
    return epoch


def require_future_cutoff(deadline, margin=1):
    """Return the cutoff epoch only if it is still ahead of ``margin`` seconds."""
    epoch = parse_cutoff(deadline)
    if epoch - time.time() <= margin:
        raise SetupError("off-peak cutoff expired; refusing to start work", code=75)
    return epoch


def past_deadline(deadline):
    """True once the (strictly parsed) cutoff has elapsed; refuses invalid input."""
    return parse_cutoff(deadline) <= time.time()


def time_budget(deadline, cap):
    """Fresh seconds left before the immutable cutoff, capped, expired refuses."""
    seconds = parse_cutoff(deadline) - time.time()
    if seconds <= 1:
        raise SetupError("off-peak deadline reached; refusing to start work", code=75)
    return max(1.0, min(seconds, cap))


# --- integrity helpers ------------------------------------------------------
def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path, expected):
    return sha256_file(path) == expected


def parse_shasums(text, filename):
    """The sha256 recorded for ``filename`` in a ``shasum -a 256`` style file."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].strip().lower()
    return None


def parse_hash(value):
    """Normalise a manifest ``sha256`` hash field; fail closed if not a digest."""
    if not isinstance(value, str):
        raise SetupError("toolchain manifest hash is missing; refusing", code=1)
    for prefix in ("sha256-", "sha256:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.strip().lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SetupError("toolchain manifest hash is not a SHA-256 digest; refusing", code=1)
    return value


# --- bounded, fail-closed extraction ----------------------------------------
def _within(target, base):
    target = os.path.normpath(str(target))
    base = os.path.normpath(str(base))
    return target == base or target.startswith(base + os.sep)


def _deadline_ok(deadline, action):
    if deadline is not None and past_deadline(deadline):
        raise SetupError("off-peak deadline reached %s; refusing" % action, code=75)


def validate_archive_members(archive, dest, deadline=None):
    dest = Path(dest)
    _deadline_ok(deadline, "before archive scan")
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
    _deadline_ok(deadline, "after archive scan")
    if len(members) > ARCHIVE_MAX_MEMBERS:
        raise SetupError("archive has too many members", code=1)
    symlink_dirs = {os.path.normpath(m.name) for m in members if m.issym()}
    seen = set()
    total = 0
    for index, member in enumerate(members):
        if deadline is not None and index % 4096 == 0:
            _deadline_ok(deadline, "during archive scan")
        name = member.name
        if os.path.isabs(name) or any(part == ".." for part in Path(name).parts):
            raise SetupError("archive member escapes root: %s" % name)
        norm = os.path.normpath(name)
        if norm in seen:
            raise SetupError("duplicate archive member: %s" % name, code=1)
        seen.add(norm)
        parts = norm.split(os.sep)
        for index in range(1, len(parts)):
            if os.sep.join(parts[:index]) in symlink_dirs:
                raise SetupError("archive member under a symlink: %s" % name, code=1)
        target = dest / name
        if not _within(target, dest):
            raise SetupError("archive member escapes root: %s" % name)
        if member.issym():
            link = member.linkname
            if os.path.isabs(link) or any(part == ".." for part in Path(link).parts):
                raise SetupError("archive symlink escapes root: %s" % name, code=1)
            if not _within((target.parent / link), dest):
                raise SetupError("archive symlink escapes root: %s" % name, code=1)
        elif member.islnk():
            raise SetupError("archive hardlink not allowed: %s" % name, code=1)
        elif member.isfile():
            if member.size > ARCHIVE_MAX_MEMBER_BYTES:
                raise SetupError("archive member too large: %s" % name, code=1)
            total += member.size
            if total > ARCHIVE_MAX_TOTAL_BYTES:
                raise SetupError("archive unpacks too large", code=1)
        elif not member.isdir():
            raise SetupError("archive member type not allowed: %s" % name, code=1)
    return members


def _extract_member(tar, member, dest):
    # The secure data filter is required; there is no unsafe fallback.
    try:
        tar.extract(member, path=str(dest), filter="data")
    except TypeError:
        raise SetupError("secure tar extraction filter unavailable; refusing", code=1)


def safe_extract(archive, dest, deadline=None):
    """Validate then extract, bounded by ``deadline`` at every phase.

    The cutoff interrupts validation, decompression and every write, and is
    re-checked after extraction so nothing is published past the window.  A
    refused or interrupted run leaves only bounded private partial files, which
    the caller removes from its own private temp directory.
    """
    boundary = absolute_work_deadline(deadline, "archive") if deadline is not None else nullcontext()
    with boundary:
        _deadline_ok(deadline, "before extraction")
        members = validate_archive_members(archive, dest, deadline=deadline)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:*") as tar:
            for member in members:
                _deadline_ok(deadline, "during extraction")
                _extract_member(tar, member, dest)
        _deadline_ok(deadline, "after extraction")


# --- process supervision (bounded groups, orphan cleanup) -------------------
def _read_proc_stat(proc, pid):
    """Return (state, pgid) for ``pid``; None only when ENOENT proves it gone.

    A permission error, any other I/O error or a malformed ``stat`` is a refusal
    rather than "gone": a group is only treated as gone when the kernel states
    the pid does not exist, so an unreadable process is never silently ignored
    or signalled as if it had exited.
    """
    try:
        data = (Path(proc) / str(pid) / "stat").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SetupError("cannot read /proc/%s/stat (%s); refusing to assume gone" % (pid, error), code=1)
    close = data.rfind(b")")
    if close < 0:
        raise SetupError("malformed /proc/%s/stat; refusing to assume gone" % pid, code=1)
    fields = data[close + 2:].split()
    if len(fields) < 3:
        raise SetupError("malformed /proc/%s/stat; refusing to assume gone" % pid, code=1)
    try:
        group = int(fields[2])
    except ValueError:
        raise SetupError("malformed /proc/%s/stat; refusing to assume gone" % pid, code=1)
    return fields[0], group


def _group_alive(pgid, proc="/proc"):
    """True while a live (non-zombie) member remains in process group pgid."""
    try:
        entries = os.listdir(proc)
    except OSError as error:
        raise SetupError("cannot list %s (%s); refusing to assume gone" % (proc, error), code=1)
    for name in sorted(entries):
        if not name.isdigit():
            continue
        stat = _read_proc_stat(proc, int(name))
        if stat is None:
            continue
        state, group = stat
        if state == b"Z":
            continue
        if group == pgid:
            return True
    return False


def _signal_group_raw(pgid, sig):
    """Best-effort signal to a process group; an empty group is not an error."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _leader_exited(pid):
    """True once the leader has exited, WITHOUT reaping it (so its id is safe)."""
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    except OSError as error:
        raise SetupError("cannot poll leader %d (%s); refusing to assume state" % (pid, error), code=1)
    return info is not None


def _wait_group(pgid, deadline):
    """Wait until the group is gone; return True if a /proc observation failed."""
    while time.time() < deadline:
        try:
            if not _group_alive(pgid):
                return False
        except SetupError:
            return True  # unprovable; the caller must still KILL and report failure
        time.sleep(0.05)
    return False


def _kill_group(pgid, cutoff):
    """TERM then SIGKILL the whole group within the immutable cutoff.

    The group is cleaned even when the leader has already exited, so a surviving
    grandchild cannot outlive the step.  The final SIGKILL is always sent — a
    failed /proc observation can never skip it — and an unprovable group state is
    raised as a failure after cleanup, never reported as success.
    """
    grace = min(time.time() + CLEANUP_GRACE_SECONDS, cutoff)
    _signal_group_raw(pgid, signal.SIGTERM)
    unknown = _wait_group(pgid, grace)
    _signal_group_raw(pgid, signal.SIGKILL)
    unknown = _wait_group(pgid, cutoff) or unknown
    if unknown:
        raise SetupError("process group %d state unprovable; reporting failure" % pgid, code=1)


def _reap_bounded(proc, horizon):
    """Reap a finished process within ``horizon``; never blocks past it."""
    remaining = max(0.0, horizon - time.time())
    try:
        proc.wait(timeout=remaining if remaining > 0 else 0.001)
    except subprocess.TimeoutExpired:
        pass


def _pump(fd, handle, limit, state, cutoff):
    """Copy a child fd into the log via selectors; bounded in time and size.

    Never blocks past ``cutoff`` and records a single boolean overflow flag once
    the byte limit is passed, so the parent neither stalls nor grows unbounded.
    """
    written = 0
    selector = selectors.DefaultSelector()
    try:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = cutoff - time.time()
            if remaining <= 0:
                break
            if not selector.select(timeout=min(0.5, remaining)):
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            if written < limit:
                room = limit - written
                handle.write(chunk[:room])
                written += min(len(chunk), room)
                if len(chunk) > room:
                    state["overflow"] = True
            else:
                state["overflow"] = True
    finally:
        selector.close()
        try:
            handle.flush()
        except (OSError, ValueError):
            pass


def _drain(pump, cutoff):
    """Join the log pump within the cutoff; a live daemon is abandoned, not waited."""
    if pump is None:
        return
    remaining = max(0.0, cutoff - time.time())
    pump.join(timeout=min(CLEANUP_GRACE_SECONDS, remaining) if remaining > 0 else 0.001)


def run_supervised(argv, cwd, env, log_path, stop_at, max_log_bytes=LOG_LIMIT_BYTES):
    """Run argv in its own session, bounded in time, log size and group cleanup.

    The cutoff is required and validated before the child is launched, the
    cancellation handlers are installed before launch so a startup signal cannot
    orphan the child, the leader is kept unreaped until its whole group is
    cleaned (so its id cannot be recycled under a killpg), the log is capped
    while the child runs, and every wait and drain is bounded by the original
    cutoff.  Failure or an unprovable group state is never reported as success.
    """
    cutoff = require_future_cutoff(stop_at)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stopped = []
    state = {"overflow": False}
    child = None
    pump = None
    handle = None
    old = {}
    cleaned = False  # set once the group is killed and the leader reaped

    def stop(sig, frame):
        stopped.append(True)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):  # installed before Popen
            old[sig] = signal.signal(sig, stop)
        handle = open(log_path, "wb")
        child = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        pump = threading.Thread(target=_pump, args=(child.stdout.fileno(), handle, max_log_bytes,
                                                    state, cutoff), daemon=True)
        pump.start()
        halted = False
        while not _leader_exited(child.pid):  # WNOWAIT: never reaps the leader
            if stopped or time.time() >= cutoff - STOP_MARGIN_SECONDS:
                halted = True
                break
            time.sleep(0.2)
        # The leader is still unreaped here: clean its whole group first, then
        # reap it once.  ``cleaned`` guarantees the finally block never signals
        # this pgid again after the leader was reaped (a recycled group).
        _kill_group(child.pid, cutoff)
        group_gone = not _group_alive(child.pid)
        _reap_bounded(child, cutoff)
        cleaned = True
        _drain(pump, cutoff)
        code = child.returncode
        if stopped:
            return 143
        if halted:
            return 75
        if not group_gone:
            return 1  # a surviving descendant was not cleaned: not success
        if code is None:
            return 1
        if code < 0:  # killed by a signal: report 128+signum, never success
            return 128 - code
        return code
    finally:
        if child is not None and not cleaned:
            try:
                _kill_group(child.pid, cutoff)
            except SetupError:
                pass  # an unprovable group state already surfaces as failure
            _reap_bounded(child, cutoff)
            cleaned = True
        if child is not None and child.stdout is not None and (pump is None or not pump.is_alive()):
            child.stdout.close()
        _drain(pump, cutoff)
        for sig, handler in old.items():
            signal.signal(sig, handler)
        if handle is not None and (pump is None or not pump.is_alive()):
            handle.close()
        _truncate_log(log_path)
        if state["overflow"]:
            with open(log_path, "ab") as marker:
                marker.write(b"\n...[log truncated at limit]...\n")


def _truncate_log(path, limit=LOG_LIMIT_BYTES):
    try:
        if path.stat().st_size > limit:
            data = path.read_bytes()[-limit:]
            path.write_bytes(b"...[truncated]...\n" + data)
    except OSError:
        pass


# --- absolute interruption and network ------------------------------------
@contextmanager
def absolute_work_deadline(deadline, operation):
    """Interrupt blocking work at the original cutoff, preserving nested alarms."""
    parse_cutoff(deadline)  # refuse a missing/naive/malformed cutoff up front
    remaining = time_budget(deadline, 300)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(signum, frame):
        raise SetupError("off-peak %s deadline reached" % operation, code=75)

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL,
                     min(remaining, previous_timer[0]) if previous_timer[0] else remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                            max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                            previous_timer[1])


@contextmanager
def absolute_download_deadline(deadline):
    """Interrupt a blocking connect/read at the original immutable cutoff."""
    with absolute_work_deadline(deadline, "download"):
        yield


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect rather than following it to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("refused redirect to %s" % newurl, code=1)


def _opener():
    """An opener with no ambient proxy and no redirect following."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _checked_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SetupError("refusing off-host URL: %s" % url, code=1)
    return parsed


def fetch(url, dest, deadline=None, cap=300, max_bytes=None):
    parse_cutoff(deadline)  # refuse invalid/missing/expired cutoff before the network
    _checked_url(url)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "phpretro-native-host"})
    total = 0
    try:
        with absolute_download_deadline(deadline), \
                _opener().open(request, timeout=time_budget(deadline, cap)) as response, \
                open(tmp, "wb") as out:
            _checked_url(response.geturl())  # final URL after any transport step
            while True:
                if past_deadline(deadline):
                    raise SetupError("off-peak deadline reached during download: %s" % url, code=75)
                chunk = response.read(1024 * 1024)
                if past_deadline(deadline):
                    raise SetupError("off-peak deadline reached during download", code=75)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise SetupError("download exceeds %d bytes: %s" % (max_bytes, url), code=1)
                out.write(chunk)
    except SetupError:
        tmp.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as error:
        tmp.unlink(missing_ok=True)
        raise SetupError("download failed: %s (%s)" % (url, error), code=1)
    if past_deadline(deadline):
        tmp.unlink(missing_ok=True)
        raise SetupError("off-peak deadline reached before download publication", code=75)
    os.replace(tmp, dest)
    return dest


def _ensure_manifest(root, url, dest, deadline):
    """Fetch/verify the official channel manifest against its pinned sidecar.

    The sidecar ``<url>.sha256`` is read from the same HTTPS host and is the
    trust root; a cached manifest is only accepted when it matches that freshly
    fetched digest, so a tampered or unverified cache is discarded, never used.
    """
    if root is not None:
        assert_private(root, dest, "download manifest")
    dest = Path(dest)
    if dest.is_symlink():
        raise SetupError("manifest cache is a symlink; refusing", code=1)
    parse_cutoff(deadline)
    sidecar = dest.with_name(dest.name + ".sha256")
    fetch(url + ".sha256", sidecar, deadline=deadline, max_bytes=MANIFEST_MAX_BYTES)
    expected = parse_shasums(sidecar.read_text(), dest.name)
    if not expected:
        raise SetupError("official manifest sidecar has no entry for %s; refusing" % dest.name, code=1)
    if dest.exists() and verify_sha256(dest, expected):
        return dest, expected
    if dest.exists():
        dest.unlink()  # tampered / unverified cache is never accepted
    fetch(url, dest, deadline=deadline, max_bytes=MANIFEST_MAX_BYTES)
    if not verify_sha256(dest, expected):
        raise SetupError("official manifest failed sidecar SHA-256 verification; refusing", code=1)
    return dest, expected


def _ensure_archive(url, dest, sha256=None, deadline=None, root=None, max_bytes=None):
    if root is not None:
        assert_private(root, dest, "download archive")
    dest = Path(dest)
    if dest.exists() and dest.is_symlink():
        raise SetupError("archive cache is a symlink; refusing", code=1)
    if dest.exists() and sha256 and verify_sha256(dest, sha256):
        return dest
    if dest.exists() and sha256:
        dest.unlink()  # unverified / tampered cache is never accepted
    parse_cutoff(deadline)  # refuse invalid/missing/expired cutoff before the network
    if past_deadline(deadline):
        raise SetupError("off-peak deadline reached before download: %s" % url, code=75)
    fetch(url, dest, deadline=deadline, max_bytes=max_bytes)
    if sha256 and not verify_sha256(dest, sha256):
        raise SetupError("download failed SHA256 verification: %s" % url, code=1)
    return dest


# --- platform / small helpers ----------------------------------------------
def require_platform():
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SetupError("unsupported platform; linux-x64 is required", code=1)


class _Result:
    """A bounded subprocess result (returncode/stdout/stderr)."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_bounded(argv, timeout, cwd=None, env=None, horizon=None):
    """Run a bounded helper in its own group; kill the whole group on timeout.

    ``horizon`` is the wall-clock limit the timeout and its cleanup share; when
    omitted it is the bounded ``now + timeout`` window, never unbounded.  A
    timed-out helper is killed as a group and then reaped, still within horizon.
    """
    if timeout is None or timeout <= 0:
        raise SetupError("subprocess requires a positive bounded timeout; refusing", code=1)
    horizon = time.time() + timeout if horizon is None else min(horizon, time.time() + timeout)
    proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, horizon)
        _reap_bounded(proc, horizon)
        raise SetupError("%s timed out" % (argv[0] if argv else "subprocess"), code=75)
    return _Result(proc.returncode, out or "", err or "")


def _git(args, cwd=None, timeout=PROBE_TIMEOUT_SECONDS):
    return _run_bounded(["git", *args], timeout=timeout, cwd=cwd)


def _cargo_version(binary, timeout=PROBE_TIMEOUT_SECONDS):
    try:
        result = _run_bounded([str(binary), "--version"], timeout=timeout)
    except (SetupError, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return parts[1] if len(parts) >= 2 else None


# --- reviewed public patch (read-only dependency) ---------------------------
def reviewed_patch(root):
    """Return (patch path, sha256) for the reviewed public native-host patch.

    The patch and its digest live in the public ``builder/eliza/native-host``
    tree owned by another worker.  This helper only reads and verifies them; it
    never writes there.  A missing manifest, an unknown digest, an escaping or
    symlinked patch path, or a digest mismatch all refuse.
    """
    manifest = host_manifest_path(root)
    if manifest.is_symlink() or not manifest.is_file():
        raise SetupError("reviewed native-host patch provenance missing; refusing", code=1)
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        raise SetupError("reviewed native-host provenance is unreadable; refusing", code=1)
    entry = data.get("patch") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise SetupError("reviewed native-host provenance has no patch entry; refusing", code=1)
    upstream = data.get("upstream") if isinstance(data.get("upstream"), dict) else {}
    if upstream.get("commit") != CODEX_PIN or entry.get("applies_to_commit") != CODEX_PIN:
        raise SetupError("reviewed native-host patch is not bound to the expected source commit; refusing", code=1)
    rel, digest = entry.get("file"), entry.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise SetupError("reviewed native-host patch digest is unknown; refusing", code=1)
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        raise SetupError("reviewed native-host patch path is invalid; refusing", code=1)
    candidate = manifest.parent / rel
    if candidate.is_symlink() or not _within(candidate.resolve(), manifest.parent.resolve()):
        raise SetupError("reviewed patch path escapes its public tree; refusing", code=1)
    if not candidate.is_file():
        raise SetupError("reviewed native-host patch file is missing; refusing", code=1)
    if sha256_file(candidate) != digest.lower():
        raise SetupError("reviewed native-host patch digest mismatch; refusing", code=1)
    return candidate, digest.lower(), data


def target_files(manifest):
    """The reviewed ``patch.target_file`` entries (path/base/patched sha256)."""
    entry = manifest.get("patch") if isinstance(manifest, dict) else None
    declared = entry.get("target_file") if isinstance(entry, dict) else None
    if declared is None:
        return []
    if isinstance(declared, dict):
        declared = [declared]
    if not isinstance(declared, list) or not declared:
        raise SetupError("reviewed native-host target_file is malformed; refusing", code=1)
    files = []
    for item in declared:
        if not isinstance(item, dict):
            raise SetupError("reviewed native-host target_file is malformed; refusing", code=1)
        path = item.get("path")
        if not isinstance(path, str) or not path or os.path.isabs(path) \
                or any(part == ".." for part in Path(path).parts):
            raise SetupError("reviewed target file path is invalid: %r; refusing" % path, code=1)
        for key in ("base_sha256", "patched_sha256"):
            value = item.get(key)
            if not isinstance(value, str) or len(value) != 64 \
                    or any(c not in "0123456789abcdef" for c in value.lower()):
                raise SetupError("reviewed target file %s is unknown; refusing" % key, code=1)
        files.append((path, item["base_sha256"].lower(), item["patched_sha256"].lower()))
    return files


def verify_target_files(build, manifest, patched):
    """Verify the declared target bytes: base before apply, patched after."""
    for path, base_digest, patched_digest in target_files(manifest):
        target = Path(build) / path
        expected = patched_digest if patched else base_digest
        if not _within(target, build) or not target.is_file() or sha256_file(target) != expected:
            raise SetupError("reviewed target file does not match its expected %s bytes: %s"
                             % ("patched" if patched else "base", path), code=1)


# --- pinned source ----------------------------------------------------------
def _probe_timeout(deadline):
    """A mutating-stage probe clamps to the captured budget; read-only uses a cap."""
    return PROBE_TIMEOUT_SECONDS if deadline is None else time_budget(deadline, PROBE_TIMEOUT_SECONDS)


def source_state(root, deadline=None):
    """Truth about the trusted clone.

    Read-only ``status`` passes no deadline and uses a fixed metadata probe cap;
    a mutating verification stage passes the captured cutoff so every probe is
    clamped to the remaining budget.
    """
    src = source_dir(root)
    if not src.exists():
        return {"present": False, "ok": False, "reason": "missing"}
    if src.is_symlink():
        return {"present": True, "ok": False, "reason": "unsafe_path"}
    if not (src / ".git").exists():
        return {"present": True, "ok": False, "reason": "not_a_checkout"}
    head = _git(["rev-parse", "HEAD"], cwd=str(src), timeout=_probe_timeout(deadline))
    if head.returncode != 0:
        return {"present": True, "ok": False, "reason": "git_error"}
    revision = head.stdout.strip()
    status = _git(["status", "--porcelain"], cwd=str(src), timeout=_probe_timeout(deadline))
    if revision != CODEX_PIN:
        return {"present": True, "ok": False, "reason": "wrong_pin", "revision": revision}
    if status.returncode != 0 or status.stdout.strip():
        return {"present": True, "ok": False, "reason": "tracked_modifications", "revision": revision}
    return {"present": True, "ok": True, "reason": "pinned_clean", "revision": revision}


def declared_rust(src):
    """The pinned Rust channel declared by the source ``rust-toolchain.toml``."""
    for candidate in (Path(src) / "rust-toolchain.toml", Path(src) / "codex-rs" / "rust-toolchain.toml"):
        if candidate.is_file():
            try:
                data = tomllib.loads(candidate.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                raise SetupError("source rust-toolchain.toml is unreadable; refusing", code=1)
            channel = data.get("toolchain", {}).get("channel")
            if not isinstance(channel, str) or not channel:
                raise SetupError("source rust-toolchain.toml has no channel; refusing", code=1)
            return channel
    raise SetupError("source rust-toolchain.toml not found; refusing", code=1)


def ensure_source(root, deadline):
    """Provision, then verify and reuse, the pinned clean codex clone.

    The cutoff is validated before any clone/checkout so no network or
    subprocess runs without a live, timezone-aware window.
    """
    parse_cutoff(deadline)
    src = source_dir(root)
    assert_anchor(root, "clone source")
    if not src.exists():
        assert_private(root, src, "clone source")
        src.parent.mkdir(parents=True, exist_ok=True)
        clone = _git(["clone", CODEX_REPO, str(src)], timeout=time_budget(deadline, 900))
        if clone.returncode != 0:
            shutil.rmtree(src, ignore_errors=True)
            raise SetupError("source clone failed: %s" % clone.stderr.strip(), code=1)
        checkout = _git(["checkout", "--detach", CODEX_PIN], cwd=str(src), timeout=time_budget(deadline, 600))
        if checkout.returncode != 0:
            raise SetupError("pin checkout failed: %s" % checkout.stderr.strip(), code=1)
    # An existing checkout is verified and reused, never reset: a dirty or wrong
    # tree is refused rather than repaired, so unknown changes are never built.
    state = source_state(root, deadline=deadline)
    if not state["ok"]:
        raise SetupError("pinned source not clean (%s); refusing without reset" % state["reason"], code=1)
    if declared_rust(src).split("-", 1)[0] != RUST_VERSION:
        raise SetupError("source rust-toolchain.toml does not pin %s; refusing" % RUST_VERSION, code=1)
    return src


# --- official Rust toolchain (private prefix) -------------------------------
def parse_channel_manifest(text, target=RUST_TRIPLE):
    """Extract the (archive URL, sha256) for ``target`` from the Rust channel TOML."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise SetupError("official toolchain manifest is not valid TOML; refusing", code=1)
    entry = data.get("pkg", {}).get("rust", {}).get("target", {}).get(target)
    if not isinstance(entry, dict) or entry.get("available") is False:
        raise SetupError("official manifest has no %s toolchain for rust %s; refusing" % (target, RUST_VERSION), code=1)
    url = entry.get("url")
    if not isinstance(url, str) or urllib.parse.urlparse(url).scheme != "https" \
            or urllib.parse.urlparse(url).hostname not in ALLOWED_HOSTS:
        raise SetupError("manifest toolchain url is not an allowed https host; refusing", code=1)
    return url, parse_hash(entry.get("hash", entry.get("xz_hash", "")))


def verified_toolchain(root):
    """Return the private cargo path only if its bytes match the recorded digest."""
    entry = (load_provenance(root).get("toolchain") or {})
    if entry.get("version") != RUST_VERSION:
        return None
    bins = entry.get("binaries")
    if not isinstance(bins, dict):
        return None
    for name, path in (("cargo", toolchain_cargo(root)), ("rustc", toolchain_bin_dir(root) / "rustc")):
        record = bins.get(name)
        if not isinstance(record, dict) or Path(record.get("path", "")) != path or not path.is_file():
            return None
        try:
            if sha256_file(path) != record.get("sha256"):
                return None
        except OSError:
            return None
    return toolchain_cargo(root)


def ensure_toolchain(root, deadline):
    """Install the pinned Rust toolchain into the private prefix, verified."""
    parse_cutoff(deadline)  # refuse invalid/missing/expired cutoff before any work
    preflight_resources(Path(root))  # free RAM/disk BEFORE download/extract/install
    assert_anchor(root, "install toolchain")
    private_root(root).mkdir(parents=True, exist_ok=True)
    assert_private(root, logs_dir(root) / "toolchain.log", "toolchain log")
    if verified_toolchain(root) is not None:
        return toolchain_cargo(root)
    prefix = assert_private(root, toolchain_dir(root), "install toolchain")
    manifest, manifest_sha = _ensure_manifest(
        root, RUST_MANIFEST_URL, cache_dir(root) / ("channel-rust-%s.toml" % RUST_VERSION), deadline)
    url, expected = parse_channel_manifest(manifest.read_text())
    if past_deadline(deadline):
        raise SetupError("off-peak deadline reached before toolchain install; refusing", code=75)
    archive = _ensure_archive(url, cache_dir(root) / Path(urllib.parse.urlparse(url).path).name,
                              sha256=expected, deadline=deadline, root=root,
                              max_bytes=ARCHIVE_MAX_DOWNLOAD_BYTES)
    tmp = Path(tempfile.mkdtemp(prefix="rust-", dir=str(private_root(root))))
    try:
        safe_extract(archive, tmp, deadline=deadline)
        inner = tmp / ("rust-%s-%s" % (RUST_VERSION, RUST_TRIPLE))
        installer = inner / "install.sh"
        if not installer.is_file():
            raise SetupError("toolchain archive has no install.sh; refusing", code=1)
        argv = ["/bin/sh", str(installer), "--prefix=%s" % prefix,
                "--disable-ldconfig", "--without=rust-docs"]
        code = run_supervised(argv, cwd=inner, env=installer_env(root),
                              log_path=logs_dir(root) / "toolchain.log", stop_at=deadline)
        if code != 0:
            raise SetupError("Rust toolchain install failed (exit %s)" % code, code=1)
        version = _cargo_version(toolchain_cargo(root), timeout=time_budget(deadline, 30))
        if version != RUST_VERSION:
            raise SetupError("installed Rust is %r, not %s; refusing" % (version, RUST_VERSION), code=1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _deadline_ok(deadline, "before recording toolchain provenance")
    _record_toolchain(root, RUST_MANIFEST_URL, manifest_sha, manifest.stat().st_size,
                      url, expected, archive.stat().st_size)
    return toolchain_cargo(root)


def _record_toolchain(root, manifest_url, manifest_sha, manifest_bytes, url, expected, archive_bytes):
    data = load_provenance(root)
    data["toolchain"] = {
        "version": RUST_VERSION,
        "target": RUST_TRIPLE,
        "manifest": {"url": manifest_url, "sha256": manifest_sha, "bytes": manifest_bytes,
                     "sidecar": manifest_url + ".sha256"},
        "archive": {"url": url, "sha256": expected, "bytes": archive_bytes},
        "prefix": str(toolchain_dir(root)),
        "binaries": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in (("cargo", toolchain_cargo(root)),
                               ("rustc", toolchain_bin_dir(root) / "rustc"))
        },
    }
    save_provenance(root, data)


# --- filtered build environment ---------------------------------------------
def _minimal_env(root):
    home = home_dir(root)
    assert_private(root, home, "private home")
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "%s:/usr/bin:/bin" % toolchain_bin_dir(root),
        "HOME": str(home),  # private HOME; never the operator's home
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "TZ": "UTC",
    }


def installer_env(root):
    return _minimal_env(root)


def build_env(root):
    """A from-scratch environment: private caches, no inherited credentials."""
    env = _minimal_env(root)
    env.update({
        "CARGO_HOME": str(cargo_home(root)),
        "CARGO_TARGET_DIR": str(target_dir(root)),
        "CARGO_PROFILE_RELEASE_DEBUG": "0",
        "CARGO_INCREMENTAL": "0",
        "RUSTUP_HOME": str(private_root(root) / "rustup-unused"),
        "SOURCE_DATE_EPOCH": "0",
    })
    return env


def build_argv(cargo, jobs=JOBS, package=PACKAGE, bin_name=BIN):
    """One low-priority release build job, no debuginfo, locked dependencies."""
    return ["nice", "-n", "19", str(cargo), "build", "--release", "--locked",
            "-p", package, "--bin", bin_name, "--jobs", str(jobs)]


# --- resource preflight (protect XRDP) --------------------------------------
def available_ram_bytes(meminfo=Path("/proc/meminfo")):
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def preflight_resources(path, disk_usage=shutil.disk_usage, meminfo=Path("/proc/meminfo")):
    """Refuse work that would exhaust free disk or RAM and threaten XRDP."""
    free = disk_usage(str(path)).free
    if free < DISK_MIN_FREE_BYTES:
        raise SetupError("insufficient free disk (%d MiB < %d MiB); refusing build"
                         % (free // 2 ** 20, DISK_MIN_FREE_BYTES // 2 ** 20), code=1)
    ram = available_ram_bytes(meminfo)
    if ram is None:
        raise SetupError("cannot determine available RAM; refusing build (fail closed)", code=1)
    if ram < RAM_MIN_AVAILABLE_BYTES:
        raise SetupError("insufficient available RAM (%d MiB < %d MiB); refusing build"
                         % (ram // 2 ** 20, RAM_MIN_AVAILABLE_BYTES // 2 ** 20), code=1)
    return {"free_disk_bytes": free, "available_ram_bytes": ram}


# --- private build checkout (patch applied, trusted clone untouched) --------
def prepare_build(root, deadline=None):
    """Clone the trusted source into a private checkout and apply the patch."""
    parse_cutoff(deadline)  # refuse invalid/missing/expired cutoff before clone/apply
    src = ensure_source(root, deadline=deadline)
    patch, digest, manifest = reviewed_patch(root)
    build = build_dir(root)
    if build.exists() or build.is_symlink():
        assert_private(root, build, "replace build checkout")
        shutil.rmtree(build)
    assert_private(root, build, "prepare build checkout")
    clone = _git(["clone", "--shared", str(src), str(build)], timeout=time_budget(deadline, 600))
    if clone.returncode != 0:
        raise SetupError("build checkout clone failed: %s" % clone.stderr.strip(), code=1)
    checkout = _git(["checkout", "--detach", CODEX_PIN], cwd=str(build), timeout=time_budget(deadline, 600))
    if checkout.returncode != 0:
        raise SetupError("build checkout pin failed: %s" % checkout.stderr.strip(), code=1)
    verify_target_files(build, manifest, patched=False)
    check = _git(["apply", "--check", str(patch)], cwd=str(build), timeout=time_budget(deadline, 120))
    if check.returncode != 0:
        raise SetupError("reviewed patch does not apply cleanly; refusing", code=1)
    applied = _git(["apply", str(patch)], cwd=str(build), timeout=time_budget(deadline, 120))
    if applied.returncode != 0:
        raise SetupError("reviewed patch application failed; refusing", code=1)
    verify_target_files(build, manifest, patched=True)
    return build, patch, digest


def build_state(root):
    data = load_provenance(root)
    entry = data.get("build") or {}
    binary = entry.get("binary") or {}
    inputs = entry.get("inputs") or {}
    toolchain = data.get("toolchain") or {}
    bins = toolchain.get("binaries") or {}
    cargo = bins.get("cargo") or {}
    rustc = bins.get("rustc") or {}
    path = target_dir(root) / "release" / BIN
    ok = False
    if isinstance(binary, dict) and Path(binary.get("path", "")) == path and path.is_file():
        try:
            ok = sha256_file(path) == binary.get("sha256")
        except OSError:
            ok = False
    try:
        current_patch = reviewed_patch(root)[1]
    except SetupError:
        current_patch = None
    # Bind the recorded build to the current exact source, production patch and
    # actual toolchain binary digests — a bare version string is not enough.
    fresh = (entry.get("package") == PACKAGE and entry.get("target") == RUST_TRIPLE
             and inputs.get("source") == CODEX_PIN
             and current_patch is not None and inputs.get("patch") == current_patch
             and inputs.get("toolchain") == toolchain.get("version")
             and cargo.get("sha256") is not None
             and inputs.get("cargo_sha256") == cargo.get("sha256")
             and inputs.get("rustc_sha256") == rustc.get("sha256"))
    return {"ok": bool(ok and fresh), "binary": str(path), "package": entry.get("package"),
            "target": entry.get("target"), "stale": not fresh}


def host_state(root):
    source = source_state(root)
    toolchain = {"ok": verified_toolchain(root) is not None, "version": (load_provenance(root).get("toolchain") or {}).get("version"),
                 "expected": RUST_VERSION, "prefix": str(toolchain_dir(root))}
    build = build_state(root)
    ready = bool(source["ok"] and toolchain["ok"] and build["ok"])
    return {"pin": {"expected": CODEX_PIN, **source}, "toolchain": toolchain, "build": build,
            "ready": ready, "qualified": False,
            "note": "build provenance only; a built host does not qualify the model or toolset"}


# --- provenance / lock ------------------------------------------------------
def load_provenance(root):
    try:
        return json.loads(provenance_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_provenance(root, data):
    path = assert_private(root, provenance_path(root), "write provenance")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@contextmanager
def setup_lock(root):
    directory = assert_anchor(root, "lock")  # validate BEFORE creating anything
    path = directory / "native-host.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SetupError("unsafe lock file: %s" % path, code=1)
    directory.mkdir(parents=True, exist_ok=True)
    assert_private(root, path, "lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(str(path), flags, 0o600)
    except OSError as error:
        raise SetupError("cannot open private lock safely (%s); refusing" % error, code=1)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError("private lock is not an owned regular file; refusing", code=1)
        os.fchmod(handle, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError("another native-host step holds the lock", code=1)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


# --- commands ---------------------------------------------------------------
def command_status(root):
    result = host_state(root)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


def command_source(root):
    with setup_lock(root):
        deadline = gate_cutoff()
        src = ensure_source(root, deadline=deadline)
        patch, digest, _manifest = reviewed_patch(root)
        data = load_provenance(root)
        data["codex"] = {"repo": CODEX_REPO, "pin": CODEX_PIN}
        data["source"] = {"path": str(src), "revision": CODEX_PIN, "clean": True,
                          "toolchain": declared_rust(src)}
        data["patch"] = {"file": str(patch), "sha256": digest, "applied": False}
        save_provenance(root, data)
    print(json.dumps({"ok": True, "revision": CODEX_PIN, "patch": digest}, indent=2))
    return 0


def command_toolchain(root):
    require_platform()
    with setup_lock(root):
        ensure_toolchain(root, deadline=gate_cutoff())
    print(json.dumps({"ok": True, "rust": RUST_VERSION, "prefix": str(toolchain_dir(root))}, indent=2))
    return 0


def command_build(root):
    require_platform()
    with setup_lock(root):
        deadline = gate_cutoff()
        cargo = ensure_toolchain(root, deadline=deadline)
        assert_private(root, cargo_home(root), "cargo home")
        assert_private(root, target_dir(root), "cargo target")
        assert_private(root, logs_dir(root) / "build.log", "build log")
        build, patch, digest = prepare_build(root, deadline=deadline)
        resources = preflight_resources(private_root(root))
        argv = build_argv(cargo)
        code = run_supervised(argv, cwd=build, env=build_env(root),
                              log_path=logs_dir(root) / "build.log", stop_at=deadline)
        if code != 0:
            raise SetupError("codex-cli build did not complete (exit %s); checkpoint at build" % code, code=1)
        binary = target_dir(root) / "release" / BIN
        if not binary.is_file():
            raise SetupError("build produced no %s binary; refusing" % BIN, code=1)
        data = load_provenance(root)
        bins = (data.get("toolchain") or {}).get("binaries") or {}
        data["build"] = {
            "package": PACKAGE, "bin": BIN, "target": RUST_TRIPLE,
            "argv": [str(part) for part in argv], "nice": 19, "jobs": JOBS,
            "debuginfo": False, "exit": code,
            "binary": {"path": str(binary), "sha256": sha256_file(binary)},
            "patch": {"file": str(patch), "sha256": digest},
            "inputs": {"source": CODEX_PIN, "patch": digest,
                       "toolchain": (data.get("toolchain") or {}).get("version"),
                       "cargo_sha256": (bins.get("cargo") or {}).get("sha256"),
                       "rustc_sha256": (bins.get("rustc") or {}).get("sha256")},
            "resources": resources,
        }
        data["checks"] = {"source": "pinned_clean", "patch": "digest_verified",
                          "toolchain": RUST_VERSION, "locked": True, "ready": True,
                          "qualified": False}
        save_provenance(root, data)
    print(json.dumps({"ok": True, "binary": str(binary), "target": RUST_TRIPLE,
                      "qualified": False}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["status", "source", "toolchain", "build"])
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return {"status": command_status, "source": command_source,
                "toolchain": command_toolchain, "build": command_build}[args.command](ROOT)
    except SetupError as error:
        print("native-host: %s" % error, file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
