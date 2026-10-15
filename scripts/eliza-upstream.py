#!/usr/bin/env python3
"""Reproducible pinned upstream Eliza source setup and CLI control.

Owns only the private ``.builder/upstream`` tree and the pinned source checkout
at ``builder/eliza/upstream``.  It never touches the CMS/theme/port trees, never
installs into global runtime locations, never injects provider/cloud credentials
and never executes scripts fetched from the network before verifying them.
"""
import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

# --- pinned, non-overridable upstream + runtime identity ---------------------
UPSTREAM_REPO = "https://github.com/elizaOS/eliza.git"
UPSTREAM_PIN = "28fef47c9d9d3acc7792be0fdaa3070020293cc5"  # exact full-source pin
SOURCE_REL = Path("builder") / "eliza" / "upstream"
NODE_VERSION = "24.15.0"
NODE_SHA256 = "472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6"
NODE_ARCH = "linux-x64"
NODE_ARCHIVE = "node-v%s-%s.tar.xz" % (NODE_VERSION, NODE_ARCH)
NODE_URL = "https://nodejs.org/dist/v%s/%s" % (NODE_VERSION, NODE_ARCHIVE)
NODE_SHASUMS_URL = "https://nodejs.org/dist/v%s/SHASUMS256.txt" % NODE_VERSION
BUN_VERSION = "1.4.2"
BUN_ARCHIVE = "bun-%s-%s.tgz" % (NODE_ARCH, BUN_VERSION)
BUN_URL = "https://registry.npmjs.org/@oven/bun-%s/-/%s" % (NODE_ARCH, BUN_ARCHIVE)
# Official npm registry SHA-512 SRI for @oven/bun-linux-x64@1.4.2.
BUN_SRI = "sha512-9/E/UXOTpSo3YsV5g+FhtTd/qTpiWoKuxS12cqtuYA1ssu9fRAoPQnipFgGyck3tWO63iUdxBiygq+kELFawng=="
BUN_SHA256 = "0c75e0b94e9d56cece77abb7d03e9995b47b79506df5dcca3046eeb888db5934"
ALLOWED_HOSTS = frozenset({"nodejs.org", "registry.npmjs.org"})
LOG_LIMIT_BYTES = 4 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
_CODING_WINDOW = None


class SetupError(Exception):
    """A fail-closed refusal; carries the process exit code to return."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --- private layout (never global) ------------------------------------------
def private_root(root):
    return Path(root) / ".builder" / "upstream"


def runtimes_dir(root):
    return private_root(root) / "runtimes"


def node_dir(root):
    return runtimes_dir(root) / ("node-v%s-%s" % (NODE_VERSION, NODE_ARCH))


def node_bin(root):
    return node_dir(root) / "bin" / "node"


def bun_dir(root):
    return runtimes_dir(root) / "bun"


def bun_bin(root):
    return bun_dir(root) / "package" / "bin" / "bun"


def cache_dir(root):
    return private_root(root) / "cache"


def logs_dir(root):
    return private_root(root) / "logs"


def state_dir(root):
    return private_root(root) / "state"


def integrity_path(root):
    return private_root(root) / "runtime-integrity.json"


def source_dir(root):
    return Path(root) / SOURCE_REL


def provenance_path(root):
    return private_root(root) / "provenance.json"


# --- ownership containment (no symlink ancestors) ---------------------------
def assert_anchor(root, action="provision"):
    """Reject a symlinked ``ROOT/.builder`` before any private write or delete."""
    if (Path(root) / ".builder").is_symlink() or private_root(root).is_symlink():
        raise SetupError("private tree root is a symlink; refusing %s" % action, code=1)


def assert_private(root, target, action):
    """Refuse a delete/install path that escapes, or crosses, a private symlink."""
    assert_anchor(root, action)
    base = Path(private_root(root))
    target = Path(target)
    try:
        rel = target.relative_to(base)
    except ValueError:
        raise SetupError("path outside private tree: %s" % target, code=1)
    current = base
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise SetupError("unsafe symlink in private path: %s" % current, code=1)
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


def past_deadline(deadline):
    if not deadline:
        return False
    try:
        return dt.datetime.fromisoformat(deadline).timestamp() <= time.time()
    except (TypeError, ValueError):
        return False


def time_budget(deadline, cap):
    """Fresh seconds left before the immutable cutoff, capped, fail closed past it."""
    if not deadline:
        return cap
    try:
        seconds = dt.datetime.fromisoformat(deadline).timestamp() - time.time()
    except (TypeError, ValueError):
        return cap
    if seconds <= 1:
        raise SetupError("operation deadline reached; refusing to start work", code=75)
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


def verify_sri(path, sri):
    algo, _, want = sri.partition("-")
    if not want:
        return False
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode() == want


def parse_shasums(text, filename):
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0]
    return None


def load_integrity(root):
    try:
        return json.loads(integrity_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def canonical_binary(root, kind):
    return node_bin(root) if kind == "node" else bun_bin(root)


def record_runtime(root, kind, binary):
    """Record the canonical binary's bytes so reuse re-checks before execution."""
    canonical = canonical_binary(root, kind)
    if Path(binary) != Path(canonical):
        raise SetupError("runtime integrity must record the canonical binary", code=1)
    data = load_integrity(root)
    data[kind] = {"path": str(canonical), "sha256": sha256_file(canonical)}
    integrity_path(root).parent.mkdir(parents=True, exist_ok=True)
    integrity_path(root).write_text(json.dumps(data, indent=2) + "\n")


def verified_runtime(root, kind):
    """Return the canonical binary only if its bytes match the recorded digest.

    The manifest is never trusted for *which* path to use: it must name exactly
    the derived canonical binary, whose bytes are then re-hashed. An entry that
    points anywhere else is rejected rather than hashed and returned unchecked.
    """
    entry = load_integrity(root).get(kind)
    if not isinstance(entry, dict) or not entry.get("sha256"):
        return None
    canonical = canonical_binary(root, kind)
    if Path(entry.get("path", "")) != Path(canonical):
        return None
    if not canonical.is_file():
        return None
    try:
        if sha256_file(canonical) != entry["sha256"]:
            return None
    except OSError:
        return None
    return canonical


# --- bounded, fail-closed extraction ----------------------------------------
def _within(target, base):
    target = os.path.normpath(str(target))
    base = os.path.normpath(str(base))
    return target == base or target.startswith(base + os.sep)


def validate_archive_members(archive, dest):
    dest = Path(dest)
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
    if len(members) > 200000:
        raise SetupError("archive has too many members", code=1)
    for member in members:
        name = member.name
        if os.path.isabs(name) or any(part == ".." for part in Path(name).parts):
            raise SetupError("archive member escapes root: %s" % name)
        target = dest / name
        if not _within(target, dest):
            raise SetupError("archive member escapes root: %s" % name)
        if member.issym() or member.islnk():
            link = member.linkname
            if os.path.isabs(link):
                raise SetupError("archive link is absolute: %s" % name)
            if not _within((target.parent / link), dest):
                raise SetupError("archive link escapes root: %s" % name)
        elif not (member.isfile() or member.isdir()):
            raise SetupError("archive member type not allowed: %s" % name)
    return members


def _extract_member(tar, member, dest):
    try:
        tar.extract(member, path=str(dest), filter="data")
    except TypeError:  # Python < 3.12 has no extraction filters
        tar.extract(member, path=str(dest))


def safe_extract(archive, dest):
    members = validate_archive_members(archive, dest)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        for member in members:
            _extract_member(tar, member, dest)


# --- process supervision (bounded groups, orphan cleanup) -------------------
def _group_alive(pgid):
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        close = stat.rfind(b")")
        if close < 0:
            continue
        fields = stat[close + 2:].split()
        if len(fields) < 3 or fields[0] == b"Z":
            continue
        try:
            if int(fields[2]) == pgid:
                return True
        except ValueError:
            continue
    return False


def terminate_group(pgid, deadline=None):
    """TERM then bounded KILL the whole group; ignore an already-empty group."""
    if deadline is None:
        deadline = time.time() + 3
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    while time.time() < deadline and _group_alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_supervised(argv, cwd, env, log_path, stop_at=None):
    """Run argv in its own session, capture a bounded log, clean up on stop."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stopped = []
    child = None
    old = {}
    handle = open(log_path, "wb")
    try:
        child = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=handle,
                                 stderr=subprocess.STDOUT, start_new_session=True)

        def stop(sig, frame):
            stopped.append(True)

        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, stop)
        cutoff = None
        if stop_at:
            try:
                cutoff = dt.datetime.fromisoformat(stop_at).timestamp()
            except (TypeError, ValueError):
                cutoff = None
        halted = False
        while child.poll() is None:
            if stopped or (cutoff is not None and time.time() >= cutoff - 5):
                halted = True
                break
            time.sleep(0.2)
        deadline = time.time() + 3 if cutoff is None else min(time.time() + 3, cutoff)
        terminate_group(child.pid, deadline)
        child.wait()
        if stopped:
            return 143
        if halted:
            return 75
        code = child.returncode
        if code is None:
            return 1
        if code < 0:  # killed by a signal: report 128+signum, never success
            return 128 - code
        return code
    finally:
        if child is not None:
            terminate_group(child.pid, time.time() + 3)
            child.wait()
        for sig, handler in old.items():
            signal.signal(sig, handler)
        handle.close()
        _truncate_log(log_path)


def _truncate_log(path):
    try:
        if path.stat().st_size > LOG_LIMIT_BYTES:
            data = path.read_bytes()[-LOG_LIMIT_BYTES:]
            path.write_bytes(b"...[truncated]...\n" + data)
    except OSError:
        pass


# --- network (allowlisted, verified before use) -----------------------------
@contextmanager
def absolute_download_deadline(deadline):
    """Interrupt a blocking connect/read at the original Linux CLI cutoff."""
    if not deadline:
        yield
        return
    remaining = time_budget(deadline, 300)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(signum, frame):
        raise SetupError("download operation deadline reached", code=75)
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


def fetch(url, dest, deadline=None, cap=300):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SetupError("refusing off-host URL: %s" % url, code=1)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "phpretro-eliza-upstream"})
    try:
        with absolute_download_deadline(deadline), \
                urllib.request.urlopen(request, timeout=time_budget(deadline, cap)) as response, \
                open(tmp, "wb") as out:
            while True:
                if past_deadline(deadline):
                    raise SetupError("operation deadline reached during download: %s" % url, code=75)
                chunk = response.read(1024 * 1024)
                if past_deadline(deadline):
                    raise SetupError("operation deadline reached during download", code=75)
                if not chunk:
                    break
                out.write(chunk)
    except SetupError:
        tmp.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as error:
        tmp.unlink(missing_ok=True)
        raise SetupError("download failed: %s (%s)" % (url, error), code=1)
    if past_deadline(deadline):
        tmp.unlink(missing_ok=True)
        raise SetupError("operation deadline reached before download publication", code=75)
    os.replace(tmp, dest)
    return dest


def _ensure_archive(url, dest, sha256=None, sri=None, shasums=None, deadline=None):
    dest = Path(dest)
    if dest.exists():
        if sha256 and not verify_sha256(dest, sha256):
            dest.unlink()
        elif sri and not verify_sri(dest, sri):
            dest.unlink()
        else:
            return dest
    if past_deadline(deadline):
        raise SetupError("operation deadline reached before download: %s" % url, code=75)
    fetch(url, dest, deadline=deadline)
    if sri and not verify_sri(dest, sri):
        raise SetupError("download failed SRI verification: %s" % url, code=1)
    if sha256 and not verify_sha256(dest, sha256):
        raise SetupError("download failed SHA256 verification: %s" % url, code=1)
    if shasums:
        shasums_file = dest.with_name(dest.name + ".SHASUMS256.txt")
        if past_deadline(deadline):
            raise SetupError("operation deadline reached before SHASUMS fetch: %s" % url, code=75)
        text = fetch(shasums, shasums_file, deadline=deadline).read_text()
        expected = parse_shasums(text, dest.name)
        if not expected or not verify_sha256(dest, expected):
            raise SetupError("download failed official SHASUMS check: %s" % url, code=1)
    return dest


# --- runtime provisioning ---------------------------------------------------
def require_platform():
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SetupError("unsupported platform; linux-x64 is required", code=1)


def _version(binary, timeout=30):
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip() or out.stderr.strip()
    return text.split()[0] if text else None


def validate_provided_runtime(path, kind, deadline=None):
    binary = Path(path)
    if not binary.is_file() or not os.access(str(binary), os.X_OK):
        raise SetupError("provided %s runtime is not an executable file" % kind, code=1)
    want = ("v%s" % NODE_VERSION) if kind == "node" else BUN_VERSION
    if _version(binary, timeout=time_budget(deadline, 30)) != want:
        raise SetupError("provided %s runtime failed version validation" % kind, code=1)
    return binary


def _link_runtime(root, dest, source):
    dest = assert_private(root, dest, "link runtime")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(Path(source))


def ensure_node(root, provided=None, deadline=None):
    assert_anchor(root, "install node")
    private_root(root).mkdir(parents=True, exist_ok=True)
    if provided is not None:
        _link_runtime(root, node_bin(root), validate_provided_runtime(provided, "node", deadline))
        record_runtime(root, "node", node_bin(root))
        return node_bin(root)
    if verified_runtime(root, "node") is not None:
        return node_bin(root)
    assert_private(root, node_dir(root), "install node")
    archive = _ensure_archive(NODE_URL, cache_dir(root) / NODE_ARCHIVE,
                              sha256=NODE_SHA256, shasums=NODE_SHASUMS_URL, deadline=deadline)
    tmp = Path(tempfile.mkdtemp(prefix="node-", dir=str(private_root(root))))
    try:
        safe_extract(archive, tmp)
        source = tmp / ("node-v%s-%s" % (NODE_VERSION, NODE_ARCH))
        if _version(source / "bin" / "node", timeout=time_budget(deadline, 30)) != "v%s" % NODE_VERSION:
            raise SetupError("extracted Node failed version check", code=1)
        assert_private(root, node_dir(root), "replace node")
        if node_dir(root).exists() or node_dir(root).is_symlink():
            shutil.rmtree(node_dir(root))
        node_dir(root).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(node_dir(root)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    record_runtime(root, "node", node_bin(root))
    return node_bin(root)


def ensure_bun(root, provided=None, deadline=None):
    assert_anchor(root, "install bun")
    private_root(root).mkdir(parents=True, exist_ok=True)
    if provided is not None:
        _link_runtime(root, bun_bin(root), validate_provided_runtime(provided, "bun", deadline))
        record_runtime(root, "bun", bun_bin(root))
        return bun_bin(root)
    if verified_runtime(root, "bun") is not None:
        return bun_bin(root)
    assert_private(root, bun_dir(root), "install bun")
    archive = _ensure_archive(BUN_URL, cache_dir(root) / BUN_ARCHIVE, sri=BUN_SRI,
                              sha256=BUN_SHA256, deadline=deadline)
    tmp = Path(tempfile.mkdtemp(prefix="bun-", dir=str(private_root(root))))
    try:
        safe_extract(archive, tmp)
        source = tmp / "package"
        if _version(source / "bin" / "bun", timeout=time_budget(deadline, 30)) != BUN_VERSION:
            raise SetupError("extracted Bun failed version check", code=1)
        destination = bun_bin(root).parent.parent  # runtimes/bun/package
        assert_private(root, destination, "replace bun")
        if bun_dir(root).exists() or bun_dir(root).is_symlink():
            shutil.rmtree(bun_dir(root))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    record_runtime(root, "bun", bun_bin(root))
    return bun_bin(root)


# --- source checkout --------------------------------------------------------
def _git(args, cwd=None, timeout=600):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SetupError("git %s timed out" % (args[0] if args else "command"), code=75)


def source_state(root, deadline=None):
    src = source_dir(root)
    if not src.exists():
        return {"present": False, "ok": False, "reason": "missing"}
    if src.is_symlink():
        return {"present": True, "ok": False, "reason": "unsafe_path"}
    if not (src / ".git").exists():
        return {"present": True, "ok": False, "reason": "not_a_checkout"}
    head = _git(["rev-parse", "HEAD"], cwd=str(src), timeout=time_budget(deadline, 30))
    if head.returncode != 0:
        return {"present": True, "ok": False, "reason": "git_error"}
    revision = head.stdout.strip()
    status = _git(["status", "--porcelain"], cwd=str(src), timeout=time_budget(deadline, 30))
    dirty = status.stdout.strip()
    if revision != UPSTREAM_PIN:
        return {"present": True, "ok": False, "reason": "wrong_pin", "revision": revision}
    if status.returncode != 0 or dirty:
        return {"present": True, "ok": False, "reason": "tracked_modifications", "revision": revision}
    return {"present": True, "ok": True, "reason": "pinned_clean", "revision": revision}


def ensure_source(root, deadline=None):
    src = source_dir(root)
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        # Fresh remaining budget per command; the original cutoff is never reset.
        clone = _git(["clone", UPSTREAM_REPO, str(src)], timeout=time_budget(deadline, 600))
        if clone.returncode != 0:
            shutil.rmtree(src, ignore_errors=True)
            raise SetupError("source clone failed: %s" % clone.stderr.strip(), code=1)
        checkout = _git(["checkout", "--detach", UPSTREAM_PIN], cwd=str(src),
                        timeout=time_budget(deadline, 600))
        if checkout.returncode != 0:
            raise SetupError("pin checkout failed: %s" % checkout.stderr.strip(), code=1)
    state = source_state(root, deadline=deadline)
    if not state["ok"]:
        raise SetupError("source not pinned/clean: %s" % state["reason"], code=1)
    return src


# --- workspace install / registrations --------------------------------------
def frozen_install_argv(bun):
    return [str(bun), "install", "--ignore-scripts", "--frozen-lockfile"]


def symlink_argv(bun):
    return [str(bun), "packages/scripts/ensure-workspace-symlinks.ts"]


def registration_argv(bun, package_dir):
    return [str(bun), "run", "--cwd", str(package_dir), "build:js"]


def build_env(root):
    return {
        "PATH": "%s:%s:/usr/bin:/bin" % (bun_bin(root).parent, node_bin(root).parent),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "BUN_INSTALL_CACHE_DIR": str(cache_dir(root)),
        "ELIZA_SKIP_FUSED_INFERENCE_SETUP": "1",
    }


def client_env(root):
    """Clean environment: only intended values, no inherited provider keys."""
    state = state_dir(root)
    env = build_env(root)
    env.update({
        "ELIZA_STATE_DIR": str(state),
        "ELIZA_CONFIG_PATH": str(state / "eliza.json"),
        "ELIZA_API_BIND": "127.0.0.1",
        "ELIZA_NODE_PATH": str(node_bin(root)),
        "ELIZA_DISABLE_LOCAL_EMBEDDINGS": "1",
        "ELIZA_SKIP_LOCAL_VOICE_WARMUP": "1",
        "ELIZA_DEFER_LOCAL_EMBEDDING_WARMUP": "1",
        "ELIZA_DISABLE_VAULT_PROFILE_RESOLVER": "1",
    })
    return env


def select_registration_packages(plugins_dir):
    """Metadata-selected workspace packages that declare appRegister + build:js."""
    selected = []
    for manifest in sorted(Path(plugins_dir).glob("*/package.json")):
        try:
            package = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        script = package.get("scripts", {}).get("build:js")
        if package.get("elizaos", {}).get("appRegister") and script:
            selected.append((package.get("name", manifest.parent.name), manifest.parent))
    return selected


def cli_argv(bun, args):
    return [str(bun), "--no-install", "--conditions=eliza-source",
            "packages/app/src/entry.ts", *args]


# --- provenance -------------------------------------------------------------
def write_provenance(root, phases):
    payload = {
        "pin": UPSTREAM_PIN,
        "repository": UPSTREAM_REPO,
        "node": {"version": NODE_VERSION, "sha256": NODE_SHA256, "archive": NODE_URL},
        "bun": {"version": BUN_VERSION, "sha256": BUN_SHA256, "sri": BUN_SRI, "archive": BUN_URL},
        "phases": phases,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    provenance_path(root).parent.mkdir(parents=True, exist_ok=True)
    provenance_path(root).write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def read_provenance(root):
    try:
        return json.loads(provenance_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- lock -------------------------------------------------------------------
@contextmanager
def setup_lock(root):
    path = private_root(root) / "setup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError("another setup holds the lock", code=1)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


# --- commands ---------------------------------------------------------------
def runtime_state(root):
    node = verified_runtime(root, "node")
    bun = verified_runtime(root, "bun")
    return {"ok": node is not None and bun is not None,
            "node": _version(node) if node else None,
            "bun": _version(bun) if bun else None,
            "expected": {"node": "v%s" % NODE_VERSION, "bun": BUN_VERSION}}


def dependencies_state(root):
    src = source_dir(root)
    modules = src / "node_modules"
    provenance = read_provenance(root)
    installed = modules.is_dir() and bool(provenance and provenance.get("phases", {}).get("install"))
    return {"ok": bool(installed), "node_modules": modules.is_dir(),
            "install_recorded": bool(provenance and provenance.get("phases", {}).get("install"))}


def check(root):
    source = source_state(root)
    runtime = runtime_state(root)
    deps = dependencies_state(root)
    ready = bool(source["ok"] and runtime["ok"] and deps["ok"])
    return {"pin": {"expected": UPSTREAM_PIN, **source},
            "runtime": runtime, "dependencies": deps, "readiness": ready}


def command_check(root):
    result = check(root)
    print(json.dumps(result, indent=2))
    return 0 if result["readiness"] else 1


SETUP_TIMEOUT_DEFAULT = 3600.0
SETUP_TIMEOUT_MAX = 7200.0


def valid_setup_timeout(timeout):
    return (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
            and math.isfinite(timeout) and 0 < timeout <= SETUP_TIMEOUT_MAX)


def setup_deadline(timeout, now=None):
    """Capture one fixed UTC cutoff from the caller's bounded setup timeout."""
    if not valid_setup_timeout(timeout):
        raise SetupError("setup timeout must be between 1 and 7200 seconds", code=2)
    captured = dt.datetime.now(dt.timezone.utc) if now is None else now
    return (captured + dt.timedelta(seconds=timeout)).isoformat()


def command_setup(root, node_path=None, bun_path=None, timeout=SETUP_TIMEOUT_DEFAULT):
    require_platform()
    assert_anchor(root, "setup")
    private_root(root).mkdir(parents=True, exist_ok=True)
    phases = {}
    with setup_lock(root):
        # Source/runtime preparation is provider-free. Capture one bounded caller
        # cutoff and pass it unchanged through every phase; no later phase extends it.
        deadline = setup_deadline(timeout)
        ensure_node(root, provided=node_path, deadline=deadline)
        ensure_bun(root, provided=bun_path, deadline=deadline)
        phases["runtimes"] = {"node": str(node_bin(root)), "bun": str(bun_bin(root))}
        src = ensure_source(root, deadline=deadline)
        phases["source"] = {"revision": UPSTREAM_PIN}
        assert_private(root, logs_dir(root), "write logs")
        steps = [("install", frozen_install_argv(bun_bin(root)), "install.log"),
                 ("symlinks", symlink_argv(bun_bin(root)), "symlinks.log")]
        for label, argv, log in steps:
            code = run_supervised(argv, cwd=src, env=build_env(root),
                                  log_path=logs_dir(root) / log, stop_at=deadline)
            if code != 0:
                raise SetupError("%s failed (exit %s)" % (label, code), code=1)
            phases[label] = {"argv": [str(part) for part in argv], "exit": code}
        packages = select_registration_packages(src / "plugins")
        for name, package_dir in packages:
            code = run_supervised(registration_argv(bun_bin(root), package_dir), cwd=src,
                                  env=build_env(root),
                                  log_path=logs_dir(root) / ("registration-%s.log" % name.replace("/", "_")),
                                  stop_at=deadline)
            if code != 0:
                raise SetupError("registration build failed for %s (exit %s)" % (name, code), code=1)
        phases["registrations"] = [name for name, _ in packages]
    write_provenance(root, phases)
    print(json.dumps({"ok": True, "phases": list(phases)}, indent=2))
    return 0


def command_cli(root, argv):
    require_platform()
    decision = enforce_gate()
    deadline = decision.get("stop_at")
    if not deadline:
        raise SetupError("off-peak gate has no verified cutoff; refusing CLI", code=75)
    source = source_state(root, deadline=deadline)
    if not source["ok"]:
        raise SetupError("source not pinned/clean: %s" % source["reason"], code=1)
    bun = verified_runtime(root, "bun")
    if bun is None:
        raise SetupError("pinned Bun runtime missing or unverified; run setup", code=1)
    state = assert_private(root, state_dir(root), "write state")
    state.mkdir(parents=True, exist_ok=True)
    return run_supervised(cli_argv(bun, argv), cwd=source_dir(root),
                          env=client_env(root), log_path=logs_dir(root) / "cli.log",
                          stop_at=deadline)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="inspect pinned source/runtime readiness")
    setup_parser = commands.add_parser("setup", help="prepare pinned source and private runtimes")
    setup_parser.add_argument("--node-path", help="operator-provided Node binary (validated)")
    setup_parser.add_argument("--bun-path", help="operator-provided Bun binary (validated)")
    setup_parser.add_argument("--timeout", type=float, default=SETUP_TIMEOUT_DEFAULT,
                              help="caller-bounded setup deadline in seconds (1-7200)")
    cli_parser = commands.add_parser("cli", help="run the pinned upstream CLI")
    cli_parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = ROOT if root is None else Path(root)
    try:
        if args.command == "check":
            return command_check(root)
        if args.command == "setup":
            if not valid_setup_timeout(args.timeout):
                raise SetupError("setup timeout must be between 1 and 7200 seconds", code=2)
            return command_setup(root, node_path=args.node_path, bun_path=args.bun_path,
                                 timeout=args.timeout)
        extra = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
        return command_cli(root, extra)
    except SetupError as error:
        print("eliza-upstream: %s" % error, file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
