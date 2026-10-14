#!/usr/bin/env python3
"""Operate the two-process Eliza dashboard (API + UI) as a bounded local CLI.

This reproduces the reviewed two-process launch design
(``check``/``start``/``status``/``stop``) without changing the live launcher.
It never installs runtimes, never performs model inference on ``check`` or
``status``, never prints secret values, and keeps all state under the project
``.builder/eliza-dashboard`` directory as private regular files.

The integration ``ROOT`` is resolved from this script's location. Root is a
parameter of the pure helper functions so tests can inject an isolated tree
without adding an arbitrary ``--project-root`` option.

Security notes:
  * state directory is created/validated 0700 and owned by the current user;
  * lock/log/runtime files are validated regular, non-symlink, 0600, owned;
  * the registered config source, git pin and runtime versions are validated
    without installing anything; the default integration source is checked with
    the reviewed ``scripts/eliza-upstream.py`` byte-verification helpers, so a
    runtime version is never read from an unverified or cached binary;
  * the fresh coding-window gate (project ``scripts/coding-window.py``) is
    required and fails closed when absent;
  * the reviewed DeepSeek chat-compat module is embedded verbatim and written
    0600 under the private state dir; an existing copy is preserved only when
    its bytes match, and symlinks/foreign files are refused. It is added to the
    Bun API argv via ``--preload`` only, never to the UI;
  * ``check`` detects a saved config that selects a non-direct DeepSeek route
    without printing or mutating that config, failing closed on an existing
    config that is unsafe/unreadable/malformed/non-object, carries no explicit
    supported direct selection, selects a native DeepSeek route or an unknown
    provider; only a genuinely absent first-run config is compatible;
  * child groups receive TERM then KILL bounded by the coding cutoff, including
    descendants, and a partial startup is always cleaned exactly once;
  * ``start`` re-checks the coding gate after slow validation and immediately
    before each launch, never launches within five seconds of the original
    validated cutoff, and keeps that original cutoff for every cleanup deadline;
  * ``stop`` verifies the stored PID start-ticks immediately before every
    signal so a reused leader PID is never killed, reclaims verified original
    descendants after the leader is reaped, bounds its deadlines by the recorded
    cutoff, and never uses a broad pkill.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import shlex
import stat
import subprocess
import sys
import time
import uuid
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]

STATE_RELATIVE = Path(".builder") / "eliza-dashboard"
API_HOST = "127.0.0.1"
API_PORT = 18792
UI_PORT = 18793
PUBLIC_HOST = "phpretro.work.gd"
PUBLIC_ORIGIN = "https://phpretro.work.gd"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-flash"
TEXT_TIERS = ("NANO", "SMALL", "MEDIUM", "LARGE", "MEGA",
              "RESPONSE_HANDLER", "SHOULD_RESPOND", "ACTION_PLANNER", "PLANNER")
DOTENV_RELATIVE = Path("secrets") / "deepseek.env"

# Explicitly registered config source. The integration ROOT is resolved from this
# script's location; the pinned upstream checkout and the private pinned runtimes
# are installed by the reviewed setup installer ``scripts/eliza-upstream.py``
# (run ``setup`` first). This CLI never installs them. The default (``managed``)
# source is validated through the installer's own byte-verified
# ``check``/``verified_runtime`` helpers before any runtime version is executed,
# so an unverified or cached binary is never run. A test-injected source keeps
# its explicit paths and is validated directly instead.
UPSTREAM_SOURCE_REL = Path("builder") / "eliza" / "upstream"
UPSTREAM_PRIVATE_REL = Path(".builder") / "upstream"
UPSTREAM_INSTALLER_REL = Path("scripts") / "eliza-upstream.py"
CONFIG_SOURCE = {
    "name": "eliza-upstream",
    "managed": True,
    "path": ROOT / UPSTREAM_SOURCE_REL,
    "pin": "28fef47c9d9d3acc7792be0fdaa3070020293cc5",
    "bun": ROOT / UPSTREAM_PRIVATE_REL / "runtimes" / "bun" / "package" / "bin" / "bun",
    "bun_version": "1.4.2",
    "node": ROOT / UPSTREAM_PRIVATE_REL / "runtimes" / "node-v24.15.0-linux-x64" / "bin" / "node",
    "node_version": "v24.15.0",
    "api_entry": Path("packages/app/src/entry.ts"),
    "vite": Path("node_modules/vite/bin/vite.js"),
    "api_cwd": Path("."),
    "ui_cwd": Path("packages/app"),
}

SECRET_FILES = ("api.token", "vault.passphrase")

# Reviewed temporary deployment transport (independently approved by native Sol
# after exact-contract tests). Embedded verbatim from the private reference so
# this launcher is repeatable. It forces DeepSeek chat completions onto the
# OpenAI-compatible transport with thinking disabled; it is preloaded ONLY into
# the Bun API child process, never the UI.
COMPAT_MODULE_NAME = "deepseek-chat-compat.mjs"
COMPAT_MODULE_SOURCE = '''// Private deployment compatibility for pinned upstream; no request logging.
export function compatibleFetch(original) {
  const wrapped = function(input, init) {
    let url;
    try { url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url); }
    catch { return original(input, init); }
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.protocol !== 'https:' || url.hostname !== 'api.deepseek.com' || url.port || url.username || url.password || url.search ||
        !['/v1/chat/completions', '/chat/completions'].includes(url.pathname) || method !== 'POST' || typeof init?.body !== 'string') return original(input, init);
    let body;
    try { body = JSON.parse(init.body); } catch { return original(input, init); }
    if (!body || typeof body !== 'object' || Array.isArray(body) || body.model !== 'deepseek-flash') return original(input, init);
    body = {...body, thinking: {type: 'disabled'}, reasoning_effort: 'none'};
    const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.delete('content-length');
    return original(input, {...init, headers, body: JSON.stringify(body)});
  };
  Object.assign(wrapped, original);
  if (typeof original.preconnect === 'function') wrapped.preconnect = original.preconnect.bind(original);
  return wrapped;
}
globalThis.fetch = compatibleFetch(globalThis.fetch);
'''

# The correct route is the OpenAI-compatible direct transport at
# api.deepseek.com, driven by the preserved OPENAI_*_MODEL environment. A saved
# config that selects a native DeepSeek provider or a provider-qualified
# "deepseek/..." model bypasses the preloaded fetch shim. Only an explicit
# supported direct selection is compatible; an absent selection (an empty or
# non-selection object), an unknown provider and native providers all fail
# closed. The only legitimate compatible case without this proof is a genuinely
# absent first-run config.
NATIVE_PROVIDER_IDENTIFIERS = {"deepseek", "deepseek-native", "deepseek_native"}
DIRECT_PROVIDER_IDENTIFIERS = {"openai", "openai-compatible", "openai_compatible",
                               "openai-compat", "openai_compat"}
PROVIDER_ROUTING_KEYS = {"provider", "providerid", "provider_id", "providername",
                         "provider_name", "catalog", "adapter", "sdk", "route"}
MODEL_KEYS = {"model", "modelid", "model_id", "modelname", "model_name"}



class DashboardError(RuntimeError):
    """Sanitized launch/validation failure safe to print."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(root) -> Path:
    return Path(root) / STATE_RELATIVE


# --------------------------------------------------------------------------
# Private filesystem helpers
# --------------------------------------------------------------------------

def ensure_state_dir(root) -> Path:
    """Create/validate the private state directory (0700, owned, no symlinks)."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise DashboardError("project root is not a safe directory")
    current = root
    for part in STATE_RELATIVE.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DashboardError("dashboard state path is unsafe")
        if info.st_uid != os.getuid():
            raise DashboardError("dashboard state is not owned by the current user")
    os.chmod(current, 0o700)
    return current


def read_private_text(path, require_mode: int | None = 0o600) -> str:
    """Read a private regular file; reject symlinks, foreign owners, bad modes."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError:
        raise DashboardError(f"{path.name} is not a safe regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DashboardError(f"{path.name} is not a safe regular file")
        if info.st_uid != os.getuid():
            raise DashboardError(f"{path.name} is not owned by the current user")
        if require_mode is not None and stat.S_IMODE(info.st_mode) != require_mode:
            raise DashboardError(f"{path.name} must be mode {require_mode:04o}")
        with os.fdopen(fd, "r") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def parse_dotenv(text: str) -> dict:
    """Parse dotenv content as inert data; never sourced or executed."""
    data = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        if key in data:
            raise DashboardError("duplicate DeepSeek credential assignment")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise DashboardError("invalid DeepSeek credential assignment") from None
        if len(parts) != 1 or not parts[0]:
            raise DashboardError("invalid DeepSeek credential assignment")
        data[key] = parts[0]
    return data


def atomic_json(path, data) -> None:
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp.exists():
            temp.unlink()


def open_private_log(path):
    path = Path(path)
    # O_NONBLOCK is essential: opening a FIFO O_WRONLY with no reader blocks
    # before the post-open type check. Non-blocking open returns promptly so a
    # FIFO/unsafe target is rejected instead of hanging. The mode is never
    # changed for a non-regular file.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600)
    except OSError:
        raise DashboardError(f"{path.name} is not a safe regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise DashboardError(f"{path.name} is not a safe regular file")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "ab", buffering=0)


def acquire_lock(state):
    """Non-blocking exclusive lock. Returns fd, or None when already held."""
    path = Path(state) / "dashboard.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return "error", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            return "error", None
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return "busy", None
        return "free", fd
    except OSError:
        os.close(fd)
        return "error", None


def release_lock(fd) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Process identity and lifecycle
# --------------------------------------------------------------------------

def read_starttime(pid):
    """Return field 22 (starttime) of /proc/<pid>/stat for PID-reuse checks."""
    text = Path(f"/proc/{int(pid)}/stat").read_text()
    close = text.rfind(")")
    fields = text[close + 2:].split()
    return fields[19]


def pid_identity(pid):
    try:
        return read_starttime(pid)
    except (FileNotFoundError, ProcessLookupError):
        return None


def _identity_matches(pid, expected_start) -> bool:
    """True only when the live start-tick still equals the recorded identity."""
    if expected_start is None:
        return False
    current = pid_identity(pid)
    return current is not None and str(current) == str(expected_start)


def bounded_grace(term_grace: float, cutoff) -> float:
    """Clamp a cleanup grace so no wait extends past the known coding cutoff."""
    if cutoff is None:
        return term_grace
    return max(0.0, min(term_grace, cutoff - time.time()))


def _killpg(pid, sig):
    try:
        os.killpg(pid, sig)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def terminate_group(process, term_grace: float) -> None:
    """TERM, bounded leader wait, then KILL the group even if the leader exited."""
    _killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=term_grace)
    except subprocess.TimeoutExpired:
        pass
    _killpg(process.pid, signal.SIGKILL)
    try:
        process.wait()
    except ChildProcessError:
        pass


def _group_members(pgid) -> dict:
    """Map pid -> starttime for live members of process group ``pgid``."""
    members = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for name in entries:
        if not name.isdigit():
            continue
        try:
            text = Path(f"/proc/{name}/stat").read_text()
        except OSError:
            continue
        close = text.rfind(")")
        if close < 0:
            continue
        fields = text[close + 2:].split()
        if len(fields) < 20:
            continue
        try:
            pgrp = int(fields[2])
        except ValueError:
            continue
        if pgrp == int(pgid):
            members[int(name)] = fields[19]
    return members


def _kill_verified(member_pid, member_start, sig) -> None:
    """Signal one PID only when its start-tick identity is unchanged."""
    if not _identity_matches(member_pid, member_start):
        return
    try:
        os.kill(int(member_pid), sig)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def terminate_pid_group(pid, expected_start, term_grace: float) -> bool:
    """Signal a recorded process group only while its leader identity matches.

    The start-tick identity is re-verified immediately before *every* signal,
    including the final KILL, so a leader PID that exited and was reused for a
    different process is never signalled. The identities of the original group
    members are captured before signalling so known original descendants remain
    reclaimable (by identity-checked single signals) even if the leader is
    reaped; a group-wide KILL is only ever sent while the original leader
    identity still holds.
    """
    pid = int(pid)
    if not _identity_matches(pid, expected_start):
        return False
    known = _group_members(pid)
    # The /proc scan may outlive the original leader. Discard the snapshot if
    # that PID was reaped/reused; none of those members can be trusted as ours.
    if not _identity_matches(pid, expected_start):
        return False
    known[pid] = str(expected_start)
    _killpg(pid, signal.SIGTERM)
    deadline = time.time() + term_grace
    while time.time() < deadline and _identity_matches(pid, expected_start):
        time.sleep(0.05)
    if _identity_matches(pid, expected_start):
        _killpg(pid, signal.SIGKILL)
    for member_pid, member_start in known.items():
        if member_pid == pid:
            continue
        _kill_verified(member_pid, member_start, signal.SIGKILL)
    return True


def signal_pid(pid, expected_start, sig) -> bool:
    """Send a single signal to an identity-verified PID (no group/broad kill)."""
    pid = int(pid)
    current = pid_identity(pid)
    if expected_start is None or current is None or str(current) != str(expected_start):
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def cleanup_children(children, term_grace: float = 3.0) -> None:
    for _, proc, _ in children:
        _killpg(proc.pid, signal.SIGTERM)
    deadline = time.time() + term_grace
    while time.time() < deadline and any(p.poll() is None for _, p, _ in children):
        time.sleep(0.05)
    for _, proc, log in children:
        _killpg(proc.pid, signal.SIGKILL)
        try:
            proc.wait()
        except ChildProcessError:
            pass
        try:
            log.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Validation of the registered config source
# --------------------------------------------------------------------------

def load_eliza_upstream(root):
    """Import the reviewed pinned-upstream installer, failing closed when absent.

    The default registered source is validated with the installer's own byte
    verification, so this never reimplements (or weakens) the trust chain.
    """
    path = Path(root) / UPSTREAM_INSTALLER_REL
    if path.is_symlink() or not path.is_file():
        raise DashboardError("upstream installer is missing")
    spec = importlib.util.spec_from_file_location("eliza_upstream", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except DashboardError:
        raise
    except Exception:
        raise DashboardError("upstream installer is invalid") from None
    for attr in ("check", "verified_runtime"):
        if not callable(getattr(module, attr, None)):
            raise DashboardError("upstream installer is invalid")
    return module


def _validate_managed_source(root, source=CONFIG_SOURCE):
    """Validate the integration source through the reviewed setup installer.

    The pinned checkout, the setup dependencies and each runtime are checked with
    the installer's ``check`` helper, which only reports a runtime when its bytes
    match the recorded digest; ``verified_runtime`` then confirms the exact
    canonical binary that this CLI will execute. A runtime version is therefore
    never read from an unverified or cached binary.
    """
    if Path(source["path"]) != Path(root) / UPSTREAM_SOURCE_REL:
        raise DashboardError("registered config source path is not the integration checkout")
    installer = load_eliza_upstream(root)
    try:
        state = installer.check(root)
    except DashboardError:
        raise
    except Exception:
        raise DashboardError("registered config source could not be verified") from None
    if not isinstance(state, dict):
        raise DashboardError("registered config source could not be verified")
    pin = state.get("pin") or {}
    if not pin.get("ok"):
        raise DashboardError("registered config source is not the pinned clean checkout")
    runtime = state.get("runtime") or {}
    if not runtime.get("ok"):
        raise DashboardError("a registered runtime binary is missing or unverified")
    dependencies = state.get("dependencies") or {}
    if not dependencies.get("ok"):
        raise DashboardError("upstream setup dependencies are incomplete")
    for kind in ("node", "bun"):
        verified = installer.verified_runtime(root, kind)
        if verified is None or Path(verified) != Path(source[kind]):
            raise DashboardError("registered runtime path is not the verified canonical binary")
    if runtime.get("node") != source["node_version"]:
        raise DashboardError("node runtime version mismatch")
    if runtime.get("bun") != source["bun_version"]:
        raise DashboardError("bun runtime version mismatch")
    src = Path(source["path"])
    for label, rel in (("api entry", source["api_entry"]), ("vite", source["vite"])):
        if not (src / rel).is_file():
            raise DashboardError(f"registered config source is missing {label}")
    return {"name": source["name"], "pin": pin.get("revision") or source["pin"],
            "bun": runtime.get("bun"), "node": runtime.get("node"), "readiness": True}


def validate_source(source=CONFIG_SOURCE, root=ROOT) -> dict:
    """Validate a registered config source without installing anything.

    The default (``managed``) source is validated through ``scripts/eliza-upstream.py``
    so the pinned checkout is checked with the installer's own ``check`` and every
    runtime binary's bytes are verified by ``verified_runtime`` before its version
    is executed. An injected/isolated source keeps its explicit paths and is
    checked directly, so a fixture never touches the private ROOT runtime tree.
    """
    if source.get("managed"):
        return _validate_managed_source(root, source)
    src = Path(source["path"])
    if src.is_symlink() or not src.is_dir():
        raise DashboardError("registered config source is unavailable")
    try:
        pin = subprocess.check_output(
            ["git", "-C", str(src), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise DashboardError("registered config source is not a git checkout") from None
    if pin != source["pin"]:
        raise DashboardError("registered config source git pin mismatch")
    for label, rel in (("api entry", source["api_entry"]), ("vite", source["vite"])):
        if not (src / rel).is_file():
            raise DashboardError(f"registered config source is missing {label}")
    try:
        bun = subprocess.check_output([str(source["bun"]), "--version"], text=True, timeout=10).strip()
        node = subprocess.check_output([str(source["node"]), "--version"], text=True, timeout=10).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise DashboardError("a registered runtime binary is unavailable") from None
    if bun != source["bun_version"]:
        raise DashboardError("bun runtime version mismatch")
    if node != source["node_version"]:
        raise DashboardError("node runtime version mismatch")
    return {"name": source["name"], "pin": pin, "bun": bun, "node": node}


def load_coding_window(root):
    """Import the project coding-window gate, failing closed when absent."""
    path = Path(root) / "scripts" / "coding-window.py"
    if path.is_symlink() or not path.is_file():
        raise DashboardError("coding window gate is missing")
    spec = importlib.util.spec_from_file_location("eliza_coding_window", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "require_allowed"):
        raise DashboardError("coding window gate is invalid")
    return module


def require_window(root) -> dict:
    module = load_coding_window(root)
    try:
        return module.require_allowed()
    except DashboardError:
        raise
    except ValueError:
        raise DashboardError("coding window is closed") from None


def port_free(port, host=API_HOST) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    return True


# --------------------------------------------------------------------------
# Secrets and child environment
# --------------------------------------------------------------------------

def secret_state(path) -> str:
    try:
        read_private_text(path)
        return "safe"
    except FileNotFoundError:
        return "missing"
    except DashboardError:
        return "unsafe"


def load_secrets(root, state) -> dict:
    values = {}
    for name in SECRET_FILES:
        values[name.replace(".", "_")] = read_private_text(Path(state) / name).strip()
    if not values["api_token"]:
        raise DashboardError("API token is empty")
    if len(values["vault_passphrase"]) < 12:
        raise DashboardError("vault passphrase must contain at least 12 characters")
    secret_dir = Path(root) / DOTENV_RELATIVE.parent
    info = secret_dir.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise DashboardError("credential directory is unsafe")
    dotenv = parse_dotenv(read_private_text(Path(root) / DOTENV_RELATIVE))
    key = dotenv.get("DEEPSEEK_API_KEY")
    if not key:
        raise DashboardError("DeepSeek credential is unavailable")
    values["deepseek_key"] = key
    profile = Path(state) / "upstream-state"
    if profile.is_symlink():
        raise DashboardError("runtime profile is unsafe")
    profile.mkdir(mode=0o700, exist_ok=True)
    info = profile.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DashboardError("runtime profile is unsafe")
    os.chmod(profile, 0o700)
    values["profile"] = profile
    return values


def build_env(source, secrets):
    """Return (api_env, ui_env). The UI receives no key, passphrase or API token."""
    node_bin = Path(source["node"])
    bun_bin = Path(source["bun"])
    base = {
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", "ubuntu"),
        "LANG": "C.UTF-8",
        "PATH": f"{node_bin.parent}:{bun_bin.parent}:/usr/bin:/bin",
        "NO_COLOR": "1",
        "ELIZA_STATE_DIR": str(secrets["profile"]),
        "ELIZA_CONFIG_PATH": str(secrets["profile"] / "eliza.json"),
        "ELIZA_API_BIND": API_HOST,
        "ELIZA_API_PORT": str(API_PORT),
        "ELIZA_UI_PORT": str(UI_PORT),
        "ELIZA_NODE_PATH": str(node_bin),
        "ELIZA_REQUIRE_LOCAL_AUTH": "1",
        "ELIZA_ALLOWED_ORIGINS": PUBLIC_ORIGIN,
        "ELIZA_ALLOWED_HOSTS": PUBLIC_HOST,
        "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": PUBLIC_HOST,
        # Environment skips preserved from the reviewed launch design.
        "ELIZA_DISABLE_LOCAL_EMBEDDINGS": "1",
        "ELIZA_SKIP_LOCAL_VOICE_WARMUP": "1",
        "ELIZA_SKIP_FUSED_INFERENCE_SETUP": "1",
        "ELIZA_DEFER_LOCAL_EMBEDDING_WARMUP": "1",
        "ELIZA_DISABLE_VAULT_PROFILE_RESOLVER": "1",
    }
    api_env = dict(base)
    api_env["ELIZA_API_TOKEN"] = secrets["api_token"]
    api_env["ELIZA_VAULT_PASSPHRASE"] = secrets["vault_passphrase"]
    api_env["ELIZA_VAULT_DISABLE_KEYCHAIN"] = "1"
    api_env["ENABLE_AUTONOMY"] = "false"
    api_env["ELIZA_AGENT_ORCHESTRATOR"] = "0"
    api_env["OPENAI_API_KEY"] = secrets["deepseek_key"]
    api_env["OPENAI_BASE_URL"] = DEEPSEEK_BASE_URL
    for tier in TEXT_TIERS:
        api_env[f"OPENAI_{tier}_MODEL"] = DEEPSEEK_MODEL
    return api_env, dict(base)


def child_commands(source, compat_path=None):
    src = Path(source["path"])
    api = [str(source["bun"]), "--no-install", "--conditions=eliza-source"]
    if compat_path is not None:
        api += ["--preload", str(Path(compat_path).resolve())]
    api += ["packages/app/src/entry.ts", "start"]
    ui = [str(source["node"]), "--conditions=eliza-source", "--import", "tsx",
          str(src / "node_modules/vite/bin/vite.js"), "--configLoader", "bundle",
          "--host", API_HOST, "--port", str(UI_PORT), "--strictPort"]
    return api, ui


def write_compat_module(state) -> Path:
    """Create the reviewed compat module under the private state dir at 0600.

    An existing module is preserved byte-for-byte only when it already matches
    the embedded reviewed source; a symlink, a foreign/unreadable file or a file
    whose bytes differ is refused.
    """
    path = Path(state) / COMPAT_MODULE_NAME
    expected = COMPAT_MODULE_SOURCE.encode()
    try:
        existing = read_private_text(path, require_mode=None)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing.encode() != expected:
            raise DashboardError("deepseek compat module differs from the reviewed source")
        os.chmod(path, 0o600)
        return path
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise DashboardError("deepseek compat module could not be created safely") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def compat_module_state(state) -> str:
    path = Path(state) / COMPAT_MODULE_NAME
    if path.is_symlink():
        return "unsafe"
    try:
        existing = read_private_text(path, require_mode=None)
    except FileNotFoundError:
        return "absent"
    except DashboardError:
        return "unsafe"
    return "match" if existing.encode() == COMPAT_MODULE_SOURCE.encode() else "conflict"


def _routing_scan(node, flags) -> None:
    """Walk a config tree recording direct/native/unknown selection signals."""
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).strip().lower()
            if isinstance(value, str):
                token = value.strip().lower()
                base = token.split("/")[0].split(":")[0]
                if name in PROVIDER_ROUTING_KEYS:
                    if base in NATIVE_PROVIDER_IDENTIFIERS:
                        flags["native"] = True
                    elif base in DIRECT_PROVIDER_IDENTIFIERS:
                        flags["direct"] = True
                    elif token:
                        flags["unknown"] = True
                if name in MODEL_KEYS:
                    if token.startswith("deepseek/") or token.startswith("deepseek:"):
                        flags["native"] = True
                    elif token == DEEPSEEK_MODEL:
                        flags["direct"] = True
            _routing_scan(value, flags)
    elif isinstance(node, list):
        for item in node:
            _routing_scan(item, flags)


def _explicit_direct_selection(config) -> bool:
    """Pinned model-resolution.ts: primary hint or direct llmText route.

    Merely finding an OpenAI label elsewhere in a config is not selection.
    This accepts the reviewed Flash route only; other profiles require review.
    """
    def record(value):
        return value if isinstance(value, dict) else {}
    route = record(record(config.get("serviceRouting")).get("llmText"))
    allowed = {DEEPSEEK_MODEL, "openai/" + DEEPSEEK_MODEL}
    if route:
        return (route.get("backend") == "openai" and route.get("transport") == "direct"
                and isinstance(route.get("primaryModel"), str) and route["primaryModel"] in allowed
                and all(not key.endswith("Model") or (isinstance(value, str) and value in allowed)
                        for key, value in route.items()))
    model = record(record(record(config.get("agents")).get("defaults")).get("model"))
    return (model.get("primary") == "openai/" + DEEPSEEK_MODEL
            and isinstance(model.get("fallbacks", []), list)
            and all(value == "openai/" + DEEPSEEK_MODEL for value in model.get("fallbacks", [])))


def detect_incompatible_routing(state) -> bool:
    """Report (never print or mutate) whether saved config selects a non-direct route.

    Fail closed: only a genuinely absent config (legitimate first-run) is
    compatible. An existing config that is unsafe/unreadable/a symlink, that is
    malformed JSON, whose root is not a JSON object, that carries no explicit
    supported direct selection (an empty/non-selection object), that selects a
    native DeepSeek route or an unknown provider is treated as incompatible so
    an unverifiable route can never silently pass.
    """
    path = Path(state) / "upstream-state" / "eliza.json"
    try:
        text = read_private_text(path, require_mode=0o600)
    except FileNotFoundError:
        return False  # legitimate first-run: no saved config yet
    except (DashboardError, OSError):
        return True  # existing but unsafe/unreadable -> fail closed
    try:
        config = json.loads(text)
    except (ValueError, TypeError):
        return True  # malformed JSON -> fail closed
    if not isinstance(config, dict) or not config:
        return True  # non-object or empty object root -> no verifiable selection
    flags = {"native": False, "direct": False, "unknown": False}
    _routing_scan(config, flags)
    if flags["native"] or flags["unknown"]:
        return True  # native/unknown selection -> fail closed
    return not _explicit_direct_selection(config)  # require actual upstream selection fields


def read_runtime(state):
    path = Path(state) / "runtime.json"
    try:
        text = read_private_text(path)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise DashboardError("runtime state is invalid") from None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_check(root, source=CONFIG_SOURCE) -> int:
    problems, detail = [], {}
    try:
        state = ensure_state_dir(root)
    except DashboardError as exc:
        print(json.dumps({"command": "check", "ok": False, "problems": [str(exc)]}))
        return 2
    try:
        detail["source"] = validate_source(source, root)
    except DashboardError as exc:
        problems.append(str(exc))
    try:
        window = require_window(root)
        detail["window"] = {"allowed": True, "stop_at": window.get("stop_at")}
    except DashboardError as exc:
        problems.append(str(exc))
    status, fd = acquire_lock(state)
    detail["lock"] = status
    if status == "error":
        problems.append("dashboard lock is unsafe")
    release_lock(fd)
    for port in (API_PORT, UI_PORT):
        try:
            port_free(port)
        except OSError:
            problems.append(f"loopback port {port} is busy")
    missing = [name for name in SECRET_FILES if secret_state(Path(state) / name) == "missing"]
    unsafe = [name for name in SECRET_FILES if secret_state(Path(state) / name) == "unsafe"]
    if secret_state(Path(root) / DOTENV_RELATIVE) == "missing":
        missing.append(str(DOTENV_RELATIVE))
    if secret_state(Path(root) / DOTENV_RELATIVE) == "unsafe":
        unsafe.append(str(DOTENV_RELATIVE))
    if missing:
        problems.append("private setup files are missing")
    if unsafe:
        problems.append("a private secret file is unsafe: " + ", ".join(unsafe))
    compat_state = compat_module_state(state)
    detail["compat_module"] = compat_state
    if compat_state == "conflict":
        problems.append("local deepseek compat module differs from the reviewed source")
    elif compat_state == "unsafe":
        problems.append("local deepseek compat module path is unsafe")
    if detect_incompatible_routing(state):
        detail["routing"] = "incompatible"
        problems.append("saved config selects a non-direct DeepSeek route")
    else:
        detail["routing"] = "compatible"
    detail["onboarding"] = "pending" if missing else "ready"
    print(json.dumps({"command": "check", "ok": not problems, "problems": problems,
                      "detail": detail}))
    return 0 if not problems else 2


def probe_agent(state):
    """Bounded authenticated loopback probe; expose only readiness fields."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        token = read_private_text(Path(state) / "api.token").strip()
        request = urllib.request.Request(f"http://127.0.0.1:{API_PORT}/api/status",
                                        headers={"Authorization": "Bearer " + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=2) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            return None
        data = json.loads(payload)
        if not isinstance(data, dict):
            return None
        startup = data.get("startup")
        startup = startup if isinstance(startup, dict) else {}
        ready = data.get("state") == "running" and data.get("canRespond") is True and startup.get("phase") == "ready"
        return {"status": "runtime-ready" if ready else "awaiting-agent",
                "ready": ready, "canRespond": data.get("canRespond") is True}
    except (OSError, ValueError, DashboardError):
        return None


def cmd_status(root) -> int:
    state = state_path(root)
    report = {"command": "status", "status": "awaiting-onboarding", "ready": False}
    missing = [name for name in SECRET_FILES if secret_state(state / name) != "safe"]
    if missing or secret_state(Path(root) / DOTENV_RELATIVE) != "safe":
        report["missing"] = missing + ([str(DOTENV_RELATIVE)]
                                       if secret_state(Path(root) / DOTENV_RELATIVE) != "safe" else [])
        print(json.dumps(report))
        return 0
    try:
        runtime = read_runtime(state)
    except DashboardError as exc:
        print(json.dumps({"command": "status", "status": "error", "ready": False,
                          "problem": str(exc)}))
        return 2
    if not runtime:
        report["status"] = "stopped"
    else:
        running = []
        for name in ("api", "ui"):
            pid = runtime.get(f"{name}Pid")
            if pid is not None and pid_identity(pid) is not None and \
                    str(pid_identity(pid)) == str(runtime.get(f"{name}Start")):
                running.append(name)
        if running:
            report.update({"status": "starting", "ready": False,
                           "phase": runtime.get("phase"), "running": running,
                           "stopAt": runtime.get("stopAt")})
            if len(running) == 2:
                health = probe_agent(state)
                if health:
                    report.update(health)
        else:
            report.update({"status": "stopped", "phase": runtime.get("phase")})
    print(json.dumps(report))
    return 0


def cmd_start(root, source=CONFIG_SOURCE) -> int:
    state = ensure_state_dir(root)
    status, fd = acquire_lock(state)
    if status == "error":
        print(json.dumps({"command": "start", "status": "error",
                          "reason": "lock-unsafe"}))
        return 2
    if status == "busy":
        print(json.dumps({"command": "start", "status": "busy"}))
        return 1
    try:
        if any(secret_state(state / name) != "safe" for name in SECRET_FILES):
            print(json.dumps({"command": "start", "status": "awaiting-onboarding"}))
            return 2
        try:
            window = require_window(root)
        except DashboardError as exc:
            print(json.dumps({"command": "start", "status": "blocked", "reason": str(exc)}))
            return 2
        # Preserve the cutoff validated by the first gate check: a later re-check
        # must never extend the deadline.
        original_stop_at = window.get("stop_at")
        try:
            validate_source(source, root)
            for port in (API_PORT, UI_PORT):
                port_free(port)
            secrets = load_secrets(root, state)
            compat_path = write_compat_module(state)
            if detect_incompatible_routing(state):
                raise DashboardError("saved config selects a non-direct DeepSeek route")
        except DashboardError as exc:
            print(json.dumps({"command": "start", "status": "blocked", "reason": str(exc)}))
            return 2
        except OSError:
            print(json.dumps({"command": "start", "status": "blocked",
                              "reason": "a loopback port is busy"}))
            return 2

        # Fresh gate check after the slow prerequisite work, before any launch.
        try:
            require_window(root)
        except DashboardError as exc:
            print(json.dumps({"command": "start", "status": "blocked", "reason": str(exc)}))
            return 2
        try:
            cutoff = datetime.fromisoformat(str(original_stop_at)).timestamp()
        except (TypeError, ValueError):
            print(json.dumps({"command": "start", "status": "blocked",
                              "reason": "coding window cutoff is invalid"}))
            return 2

        api_env, ui_env = build_env(source, secrets)
        api_cmd, ui_cmd = child_commands(source, compat_path)
        src = Path(source["path"])
        plan = [("api", api_cmd, src / source["api_cwd"], api_env),
                ("ui", ui_cmd, src / source["ui_cwd"], ui_env)]

        stopping = False

        def stop(sig, frame):
            nonlocal stopping
            stopping = True

        old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
        children = []
        early = None
        abort_reason = None
        failure = None
        try:
            for name, argv, cwd, env in plan:
                # A stopping signal must not allow another spawn.
                if stopping:
                    abort_reason = "stop requested before launch"
                    break
                # Re-check the gate and the cutoff immediately before each spawn.
                try:
                    require_window(root)
                except DashboardError:
                    abort_reason = "coding window closed before launch"
                    break
                if time.time() >= cutoff - 5:
                    abort_reason = "coding window cutoff reached before launch"
                    break
                log = open_private_log(state / f"{name}.log")
                try:
                    child = subprocess.Popen(argv, cwd=str(cwd), env=env,
                                             stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                except BaseException:
                    log.close()
                    raise
                children.append((name, child, log))
            if abort_reason is None:
                data = {"supervisorPid": os.getpid(), "supervisorStart": pid_identity(os.getpid()),
                        "stopAt": original_stop_at, "sourcePin": source["pin"],
                        "phase": "running", "inferenceRequested": False,
                        "apiPort": API_PORT, "uiPort": UI_PORT}
                for name, proc, _ in children:
                    data[f"{name}Pid"] = proc.pid
                    data[f"{name}Start"] = pid_identity(proc.pid)
                atomic_json(state / "runtime.json", data)
                while not stopping and time.time() < cutoff - 5 and \
                        all(p.poll() is None for _, p, _ in children):
                    time.sleep(0.2)
                if not stopping and time.time() < cutoff - 5:
                    early = next((p.returncode for _, p, _ in children if p.poll() is not None), None)
        except BaseException as exc:
            failure = exc
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            # Cleanup runs exactly once, always bounded by the original cutoff.
            cleanup_children(children, bounded_grace(3.0, cutoff))
        if failure is not None:
            record = dict(read_runtime(state) or {})
            record["phase"] = "partial-startup-failed"
            atomic_json(state / "runtime.json", record)
            print(json.dumps({"command": "start", "status": "failed",
                              "reason": "child launch failed"}))
            return 2
        if abort_reason is not None:
            record = dict(read_runtime(state) or {})
            record["phase"] = "stopped"
            record["endedAt"] = utc_now()
            atomic_json(state / "runtime.json", record)
            print(json.dumps({"command": "start", "status": "blocked",
                              "reason": abort_reason, "allowed_until": original_stop_at}))
            return 2
        record = dict(read_runtime(state) or {})
        record["phase"] = "stopped"
        record["endedAt"] = utc_now()
        atomic_json(state / "runtime.json", record)
        code = 0 if early in (None, 0) else 1
        print(json.dumps({"command": "start", "status": "stopped",
                          "allowed_until": original_stop_at, "exit_code": code}))
        return code
    finally:
        release_lock(fd)


def cmd_stop(root) -> int:
    state = state_path(root)
    try:
        runtime = read_runtime(state)
    except DashboardError as exc:
        print(json.dumps({"command": "stop", "status": "error", "problem": str(exc)}))
        return 2
    if not runtime:
        print(json.dumps({"command": "stop", "status": "stopped", "detail": "no-runtime"}))
        return 0
    # Bound every stop deadline to the recorded coding cutoff when present.
    stop_at = runtime.get("stopAt")
    try:
        stop_cutoff = datetime.fromisoformat(str(stop_at)).timestamp() if stop_at else None
    except (TypeError, ValueError):
        stop_cutoff = None
    terminated = []
    for name in ("api", "ui"):
        pid = runtime.get(f"{name}Pid")
        if pid is None:
            continue
        if terminate_pid_group(pid, runtime.get(f"{name}Start"),
                               bounded_grace(3.0, stop_cutoff)):
            terminated.append(name)
    supervisor_pid = runtime.get("supervisorPid")
    if supervisor_pid is not None and int(supervisor_pid) != os.getpid():
        if signal_pid(supervisor_pid, runtime.get("supervisorStart"), signal.SIGTERM):
            terminated.append("supervisor")
    runtime["phase"] = "stopped"
    runtime["endedAt"] = utc_now()
    atomic_json(state / "runtime.json", runtime)
    print(json.dumps({"command": "stop", "status": "stopped", "terminated": terminated}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate source, gate, ports, lock, secrets, compat module and routing without launching")
    sub.add_parser("start", help="supervise the api and ui child groups in the foreground")
    sub.add_parser("status", help="report onboarding/runtime truth without model inference")
    sub.add_parser("stop", help="stop identity-verified child groups and the supervisor")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(ROOT)
        if args.command == "start":
            return cmd_start(ROOT)
        if args.command == "status":
            return cmd_status(ROOT)
        return cmd_stop(ROOT)
    except DashboardError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
