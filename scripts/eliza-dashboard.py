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
  * the registerd config source, git pin and runtime versions are validated
    without installing anything;
  * the fresh coding-window gate (project ``scripts/coding-window.py``) is
    required and fails closed when absent;
  * child groups receive bounded TERM then KILL, including descendants, and a
    partial startup is always cleaned;
  * ``stop`` verifies the stored PID start-ticks before signalling so a reused
    PID is never killed, and never uses a broad pkill.
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

# Explicitly registered config source. The runtime binaries and the pinned
# upstream checkout are provided by the operator; this CLI never installs them.
CONFIG_SOURCE = {
    "name": "eliza-upstream",
    "path": Path("/home/ubuntu/Documents/Codex/2026-10-11/al/work/research/eliza"),
    "pin": "28fef47c9d9d3acc7792be0fdaa3070020293cc5",
    "bun": Path("/home/ubuntu/Documents/Codex/2026-10-11/al/work/research/eliza"
                "/.phpretro-builder/runtimes/bun/package/bin/bun"),
    "bun_version": "1.4.2",
    "node": Path("/home/ubuntu/Documents/Codex/2026-10-11/al/work/research/eliza"
                 "/.phpretro-builder/runtimes/node/node-v24.15.0-linux-x64/bin/node"),
    "node_version": "v24.15.0",
    "api_entry": Path("packages/app/src/entry.ts"),
    "vite": Path("node_modules/vite/bin/vite.js"),
    "api_cwd": Path("."),
    "ui_cwd": Path("packages/app"),
}

SECRET_FILES = ("api.token", "vault.passphrase")


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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise DashboardError(f"{path.name} is not a safe regular file")
    os.fchmod(fd, 0o600)
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


def terminate_pid_group(pid, expected_start, term_grace: float) -> bool:
    """Signal a recorded process group only when its identity still matches."""
    pid = int(pid)
    current = pid_identity(pid)
    if expected_start is None or current is None or str(current) != str(expected_start):
        return False
    _killpg(pid, signal.SIGTERM)
    deadline = time.time() + term_grace
    while time.time() < deadline and pid_identity(pid) is not None:
        time.sleep(0.05)
    _killpg(pid, signal.SIGKILL)
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

def validate_source(source=CONFIG_SOURCE) -> dict:
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


def child_commands(source):
    src = Path(source["path"])
    api = [str(source["bun"]), "--no-install", "--conditions=eliza-source",
           "packages/app/src/entry.ts", "start"]
    ui = [str(source["node"]), "--conditions=eliza-source", "--import", "tsx",
          str(src / "node_modules/vite/bin/vite.js"), "--configLoader", "bundle",
          "--host", API_HOST, "--port", str(UI_PORT), "--strictPort"]
    return api, ui


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
        detail["source"] = validate_source(source)
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
        try:
            validate_source(source)
            for port in (API_PORT, UI_PORT):
                port_free(port)
            secrets = load_secrets(root, state)
        except DashboardError as exc:
            print(json.dumps({"command": "start", "status": "blocked", "reason": str(exc)}))
            return 2
        except OSError:
            print(json.dumps({"command": "start", "status": "blocked",
                              "reason": "a loopback port is busy"}))
            return 2

        cutoff = datetime.fromisoformat(window["stop_at"]).timestamp()
        api_env, ui_env = build_env(source, secrets)
        api_cmd, ui_cmd = child_commands(source)
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
        try:
            for name, argv, cwd, env in plan:
                log = open_private_log(state / f"{name}.log")
                child = subprocess.Popen(argv, cwd=str(cwd), env=env,
                                         stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                children.append((name, child, log))
            data = {"supervisorPid": os.getpid(), "supervisorStart": pid_identity(os.getpid()),
                    "stopAt": window.get("stop_at"), "sourcePin": source["pin"],
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
        except BaseException:
            cleanup_children(children)
            record = dict(read_runtime(state) or {})
            record["phase"] = "partial-startup-failed"
            atomic_json(state / "runtime.json", record)
            print(json.dumps({"command": "start", "status": "failed",
                              "reason": "child launch failed"}))
            return 2
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            cleanup_children(children)
        record = dict(read_runtime(state) or {})
        record["phase"] = "stopped"
        record["endedAt"] = utc_now()
        atomic_json(state / "runtime.json", record)
        code = 0 if early in (None, 0) else 1
        print(json.dumps({"command": "start", "status": "stopped",
                          "allowed_until": window.get("stop_at"), "exit_code": code}))
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
    terminated = []
    for name in ("api", "ui"):
        pid = runtime.get(f"{name}Pid")
        if pid is None:
            continue
        if terminate_pid_group(pid, runtime.get(f"{name}Start"), 3.0):
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
    sub.add_parser("check", help="validate source, gate, ports, lock and secrets without launching")
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
