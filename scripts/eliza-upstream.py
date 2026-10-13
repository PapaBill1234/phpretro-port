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
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
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

# Off-peak coding gate mirrored from scripts/coding-window.py: DeepSeek peak
# windows are weekdays (Mon-Fri, UTC) 01:00-04:00 and 06:00-10:00, buffered by
# +5 min at the start and -30 min before the end.  Setup and CLI refuse to run
# while a window is active or missing.
PEAK_UTC = ((1, 4), (6, 10))
GATE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
GATE_LOOKAHEAD_DAYS = 14


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


def bun_bin(root):
    return runtimes_dir(root) / "bun" / "package" / "bin" / "bun"


def cache_dir(root):
    return private_root(root) / "cache"


def logs_dir(root):
    return private_root(root) / "logs"


def state_dir(root):
    return private_root(root) / "state"


def source_dir(root):
    return Path(root) / SOURCE_REL


def provenance_path(root):
    return private_root(root) / "provenance.json"


# --- off-peak gate ----------------------------------------------------------
def gate_decision(now=None):
    """Return the off-peak decision for ``now`` (UTC)."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    active = []
    upcoming = []
    for offset in range(-1, GATE_LOOKAHEAD_DAYS):
        date = day + dt.timedelta(days=offset)
        if date.weekday() >= 5:
            continue
        for start, end in PEAK_UTC:
            begin = date + dt.timedelta(hours=start, minutes=-30)
            finish = date + dt.timedelta(hours=end, minutes=5)
            if begin <= now < finish:
                active.append((begin, finish))
            elif begin > now:
                upcoming.append(begin)
    blocked = bool(active)
    cutoff = min(upcoming) if upcoming else None
    return {
        "allowed": not blocked and cutoff is not None,
        "timezone": "UTC",
        "observed_at": now.isoformat(),
        "reason": "peak_or_buffer" if blocked else ("off_peak" if cutoff else "no_cutoff"),
        "next_allowed_at": (active[0][1] if blocked else now).isoformat(),
        "stop_at": cutoff.isoformat() if (not blocked and cutoff) else None,
        "source": GATE_SOURCE,
    }


def enforce_gate(now=None):
    decision = gate_decision(now)
    if not decision["allowed"]:
        raise SetupError("off-peak gate closed (%s); refusing to run" % decision["reason"], code=75)
    return decision


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
    """Extract one validated member; use the data filter where supported."""
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
        return child.returncode if child.returncode and child.returncode > 0 else 0
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
def fetch(url, dest):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise SetupError("refusing off-host URL: %s" % url, code=1)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "phpretro-eliza-upstream"})
    with urllib.request.urlopen(request, timeout=120) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    os.replace(tmp, dest)
    return dest


def _ensure_archive(url, dest, sha256=None, sri=None, shasums=None):
    dest = Path(dest)
    if dest.exists():
        if sha256 and not verify_sha256(dest, sha256):
            dest.unlink()
        elif sri and not verify_sri(dest, sri):
            dest.unlink()
        else:
            return dest
    fetch(url, dest)
    if sri and not verify_sri(dest, sri):
        raise SetupError("download failed SRI verification: %s" % url, code=1)
    if sha256 and not verify_sha256(dest, sha256):
        raise SetupError("download failed SHA256 verification: %s" % url, code=1)
    if shasums:
        shasums_file = dest.with_name(dest.name + ".SHASUMS256.txt")
        text = fetch(shasums, shasums_file).read_text()
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


def validate_provided_runtime(path, kind):
    binary = Path(path)
    if not binary.is_file() or not os.access(str(binary), os.X_OK):
        raise SetupError("provided %s runtime is not an executable file" % kind, code=1)
    want = ("v%s" % NODE_VERSION) if kind == "node" else BUN_VERSION
    if _version(binary) != want:
        raise SetupError("provided %s runtime failed version validation" % kind, code=1)
    return binary


def _link_runtime(dest, source):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(Path(source))


def ensure_node(root, provided=None):
    if provided is not None:
        _link_runtime(node_bin(root), validate_provided_runtime(provided, "node"))
        return node_bin(root)
    if _version(node_bin(root)) == "v%s" % NODE_VERSION:
        return node_bin(root)
    archive = _ensure_archive(NODE_URL, cache_dir(root) / NODE_ARCHIVE,
                              sha256=NODE_SHA256, shasums=NODE_SHASUMS_URL)
    tmp = Path(tempfile.mkdtemp(prefix="node-", dir=str(private_root(root))))
    try:
        safe_extract(archive, tmp)
        source = tmp / ("node-v%s-%s" % (NODE_VERSION, NODE_ARCH))
        if _version(source / "bin" / "node") != "v%s" % NODE_VERSION:
            raise SetupError("extracted Node failed version check", code=1)
        if node_dir(root).exists():
            shutil.rmtree(node_dir(root))
        node_dir(root).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(node_dir(root)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return node_bin(root)


def ensure_bun(root, provided=None):
    if provided is not None:
        _link_runtime(bun_bin(root), validate_provided_runtime(provided, "bun"))
        return bun_bin(root)
    if _version(bun_bin(root)) == BUN_VERSION:
        return bun_bin(root)
    archive = _ensure_archive(BUN_URL, cache_dir(root) / BUN_ARCHIVE, sri=BUN_SRI, sha256=BUN_SHA256)
    tmp = Path(tempfile.mkdtemp(prefix="bun-", dir=str(private_root(root))))
    try:
        safe_extract(archive, tmp)
        source = tmp / "package"
        if _version(source / "bin" / "bun") != BUN_VERSION:
            raise SetupError("extracted Bun failed version check", code=1)
        destination = runtimes_dir(root) / "bun"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return bun_bin(root)


# --- source checkout --------------------------------------------------------
def _git(args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def source_state(root):
    """Truthful pin/modification status without installing anything."""
    src = source_dir(root)
    if not src.exists():
        return {"present": False, "ok": False, "reason": "missing"}
    if src.is_symlink():
        return {"present": True, "ok": False, "reason": "unsafe_path"}
    if not (src / ".git").exists():
        return {"present": True, "ok": False, "reason": "not_a_checkout"}
    head = _git(["rev-parse", "HEAD"], cwd=str(src))
    if head.returncode != 0:
        return {"present": True, "ok": False, "reason": "git_error"}
    revision = head.stdout.strip()
    status = _git(["status", "--porcelain"], cwd=str(src))
    dirty = status.stdout.strip()
    if revision != UPSTREAM_PIN:
        return {"present": True, "ok": False, "reason": "wrong_pin", "revision": revision}
    if status.returncode != 0 or dirty:
        return {"present": True, "ok": False, "reason": "tracked_modifications", "revision": revision}
    return {"present": True, "ok": True, "reason": "pinned_clean", "revision": revision}


def ensure_source(root):
    src = source_dir(root)
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        clone = _git(["clone", UPSTREAM_REPO, str(src)])
        if clone.returncode != 0:
            raise SetupError("source clone failed: %s" % clone.stderr.strip(), code=1)
        checkout = _git(["checkout", "--detach", UPSTREAM_PIN], cwd=str(src))
        if checkout.returncode != 0:
            raise SetupError("pin checkout failed: %s" % checkout.stderr.strip(), code=1)
    state = source_state(root)
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
    node = node_bin(root)
    bun = bun_bin(root)
    node_version = _version(node) if node.exists() or node.is_symlink() else None
    bun_version = _version(bun) if bun.exists() or bun.is_symlink() else None
    ok = node_version == "v%s" % NODE_VERSION and bun_version == BUN_VERSION
    return {"ok": ok, "node": node_version, "bun": bun_version,
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


def command_setup(root, node_path=None, bun_path=None):
    require_platform()
    private_root(root).mkdir(parents=True, exist_ok=True)
    phases = {}
    with setup_lock(root):
        enforce_gate()
        ensure_node(root, provided=node_path)
        ensure_bun(root, provided=bun_path)
        phases["runtimes"] = {"node": str(node_bin(root)), "bun": str(bun_bin(root))}
        enforce_gate()
        src = ensure_source(root)
        phases["source"] = {"revision": UPSTREAM_PIN}
        enforce_gate()
        code = run_supervised(frozen_install_argv(bun_bin(root)), cwd=src,
                              env=build_env(root), log_path=logs_dir(root) / "install.log")
        if code != 0:
            raise SetupError("frozen install failed (exit %s)" % code, code=1)
        phases["install"] = {"argv": frozen_install_argv(str(bun_bin(root))), "exit": code}
        enforce_gate()
        code = run_supervised(symlink_argv(bun_bin(root)), cwd=src,
                              env=build_env(root), log_path=logs_dir(root) / "symlinks.log")
        if code != 0:
            raise SetupError("workspace symlink step failed (exit %s)" % code, code=1)
        enforce_gate()
        packages = select_registration_packages(src / "plugins")
        for name, package_dir in packages:
            enforce_gate()
            code = run_supervised(registration_argv(bun_bin(root), package_dir), cwd=src,
                                  env=build_env(root),
                                  log_path=logs_dir(root) / ("registration-%s.log" % name.replace("/", "_")))
            if code != 0:
                raise SetupError("registration build failed for %s (exit %s)" % (name, code), code=1)
        phases["registrations"] = [name for name, _ in packages]
    write_provenance(root, phases)
    print(json.dumps({"ok": True, "phases": list(phases)}, indent=2))
    return 0


def command_cli(root, argv):
    require_platform()
    decision = enforce_gate()
    source = source_state(root)
    if not source["ok"]:
        raise SetupError("source not pinned/clean: %s" % source["reason"], code=1)
    if not (bun_bin(root).exists() or bun_bin(root).is_symlink()):
        raise SetupError("pinned Bun runtime missing; run setup", code=1)
    state_dir(root).mkdir(parents=True, exist_ok=True)
    return run_supervised(cli_argv(bun_bin(root), argv), cwd=source_dir(root),
                          env=client_env(root), log_path=logs_dir(root) / "cli.log",
                          stop_at=decision.get("stop_at"))


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "setup", "cli"])
    parser.add_argument("--node-path", help="operator-provided Node binary (validated)")
    parser.add_argument("--bun-path", help="operator-provided Bun binary (validated)")
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = ROOT if root is None else Path(root)
    try:
        if args.command == "check":
            return command_check(root)
        if args.command == "setup":
            return command_setup(root, node_path=args.node_path, bun_path=args.bun_path)
        extra = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
        return command_cli(root, extra)
    except SetupError as error:
        print("eliza-upstream: %s" % error, file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
