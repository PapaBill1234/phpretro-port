#!/usr/bin/env python3
"""Run one explicitly authorized, bounded PHPRetro continuation."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".builder" / "continuation"
READINESS = ROOT / ".builder" / "doctor" / "readiness.json"


def utc():
    return datetime.now(timezone.utc).isoformat()


def ensure_private_dir(path: Path):
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("continuation state directory is unsafe")
        os.chmod(current, 0o700)


def readiness():
    ensure_private_dir(READINESS.parent)
    try:
        fd = os.open(READINESS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        raise RuntimeError("readiness file is not a safe regular file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("readiness file is not a safe regular file")
        with os.fdopen(fd, "r") as handle:
            fd = -1
            try:
                data = json.load(handle)
            except (ValueError, TypeError):
                raise RuntimeError("readiness file contains invalid JSON") from None
        return isinstance(data, dict) and data.get("ready") is True
    finally:
        if fd >= 0:
            os.close(fd)


def lock_status():
    path = STATE / "run.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return "error"
    keep_fd = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return "error"
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        keep_fd = True
        return ("free", fd)
    except OSError:
        return "error"
    finally:
        if not keep_fd:
            os.close(fd)


def atomic_json(path: Path, data):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def terminate_group(process, term_grace):
    """TERM, bounded leader wait, then KILL the group even if leader exited."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
    try:
        process.wait(timeout=term_grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
    try:
        process.wait()
    except ChildProcessError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    fd = None
    try:
        ensure_private_dir(STATE)
        if not readiness():
            print(json.dumps({"status": "skipped", "reason": "not-ready"}))
            return 0
        lock = lock_status()
        if lock == "error":
            print("Continuation lock is not a safe regular file.", file=sys.stderr)
            return 2
        if lock == "busy":
            print(json.dumps({"status": "skipped", "reason": "busy"}))
            return 0
        _, fd = lock
        if args.check:
            print(json.dumps({"status": "ready", "lock": "free", "launched": False}))
            return 0
        os.write(fd, f"pid={os.getpid()}\n".encode())
        started = utc()
        token = uuid.uuid4().hex
        out_path, err_path = STATE / f"{token}.stdout.log", STATE / f"{token}.stderr.log"
        handoff = ROOT / ".builder" / "handoff"
        prompt = (f"Read {handoff / 'CURRENT-HANDOFF.md'} and {handoff / 'CONTINUATION.md'}; "
                  "continue one bounded authorized PHPRetro task. Use Luna for implementation, Sol for review, "
                  "claimed scopes and isolated worktrees. Never duplicate active workers or inspect other projects. "
                  "Save all sessions and checkpoint before exiting. Do not ask questions.")
        command = ["python3", str(ROOT / "scripts" / "claude-builder.py"), "--role", "lead", "--",
                   "-p", prompt, "--max-turns", "30"]
        timed_out = False
        code = None
        try:
            with out_path.open("xb") as out, err_path.open("xb") as err:
                os.chmod(out_path, 0o600)
                os.chmod(err_path, 0o600)
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                            stdout=out, stderr=err, start_new_session=True)
                try:
                    code = process.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_group(process, 10)
                    code = 124
        except BaseException:
            process = locals().get("process")
            if process is not None:
                terminate_group(process, 10)
            raise
        record = {"started": started, "ended": utc(), "exit_code": code,
                  "stdout_log": str(out_path), "stderr_log": str(err_path), "timed_out": timed_out}
        atomic_json(STATE / "last-run.json", record)
        print(json.dumps({"status": "finished", "exit_code": code, "timed_out": timed_out,
                          "stdout_log": str(out_path), "stderr_log": str(err_path)}))
        return 0 if code == 0 else code if code in (124, 137) else 1
    except (OSError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else f"Continuation failed: {type(exc).__name__}"
        print(message, file=sys.stderr)
        return 2
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
