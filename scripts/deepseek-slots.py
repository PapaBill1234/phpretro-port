#!/usr/bin/env python3
"""Shared, local four-process admission for direct DeepSeek workers. No API calls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import importlib.util
import tempfile

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('coding_window', ROOT / 'scripts/coding-window.py')
window_policy = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(window_policy)


def shared_directory():
    common = Path(subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip())
    return common / "phpretro-builder/deepseek-slots"


def group_alive(group):
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path / 'stat').read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        fields = raw[raw.rfind(')') + 2:].split()
        if len(fields) < 20:
            raise ValueError('Unknown process-group state')
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            return True
    return False


def lease_path(fd):
    return Path(os.readlink('/proc/self/fd/%s' % fd)).with_suffix('.lock.json')


def lease_read(fd, created):
    path = lease_path(fd)
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # Legacy nonempty .lock records can be inspected conservatively. An
        # existing empty lock is ambiguous after a prior crash, never free.
        raw = os.read(fd, 65537)
        if not raw and created: return None
        if not raw: raise ValueError('Existing empty lease needs inspection')
    else:
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Unsafe lease metadata')
            raw = os.read(handle, 65537)
        finally: os.close(handle)
    if not raw or len(raw) > 65536: raise ValueError('Uncertain lease metadata')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('Uncertain lease metadata')
    return value


def lease_write(fd, data):
    path = lease_path(fd)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe lease metadata')
    handle, temp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w') as stream:
            json.dump(data, stream); stream.flush(); os.fsync(stream.fileno())
        # The stable lock inode stays locked. A killed update leaves the prior
        # parseable reserved/running lease in place; it never publishes emptiness.
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def reserve(directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Worker slot directory must be private and owned")
    for number in range(4):
        try:
            fd = os.open(directory / f"{number}.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(directory / f"{number}.lock", os.O_RDWR | os.O_NOFOLLOW, 0o600)
            created = False
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ValueError("Unsafe worker slot file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            previous = lease_read(fd, created)
            if previous is not None:
                if previous.get('phase') == 'reserved' or previous.get('phase') == 'running' and group_alive(previous['group']):
                    os.close(fd)
                    continue
                if previous.get('phase') not in ('running', 'finished'):
                    raise ValueError('Unknown lease needs manual inspection')
        except Exception:
            os.close(fd)
            raise
        # Persist BEFORE spawn. A crash in this phase requires manual inspection;
        # it cannot falsely establish that no unregistered child was launched.
        lease_write(fd, {"phase": "reserved", "pid": os.getpid(), "provider": "direct-deepseek", "reservedAt": time.time()})
        return fd
    raise ValueError("Four direct DeepSeek workers are already admitted")


def run(directory, argv, window=None):
    if not argv:
        raise ValueError("Worker command is required")
    window = window or window_policy.require_allowed()
    cutoff = window_policy.dt.datetime.fromisoformat(window['stop_at']).timestamp()
    fd = reserve(directory)
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if child is None:
            return
        if stopping:
            return
        stopping = True
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        # The child inherits the locked open-file description, retaining admission
        # if this supervisor is killed. The group is separate for safe cleanup.
        child = subprocess.Popen(argv, pass_fds=(fd,), start_new_session=True)
        lease_write(fd, {"phase": "running", "pid": os.getpid(), "group": child.pid, "provider": "direct-deepseek"})
        deadline = None
        while child.poll() is None:
            if time.time() >= cutoff - 5 and not stopping:
                stop(signal.SIGTERM, None)
            if stopping:
                deadline = deadline or min(time.monotonic() + 2, time.monotonic() + max(0, cutoff - time.time()))
                if time.monotonic() >= deadline:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.05)
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            end = time.monotonic() + 2
            while group_alive(child.pid) and time.monotonic() < end:
                time.sleep(0.05)
            if not group_alive(child.pid):
                lease_write(fd, {"phase": "finished", "provider": "direct-deepseek"})
        else:
            lease_write(fd, {"phase": "finished", "provider": "direct-deepseek"})
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "where"])
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        directory = shared_directory()
        if directory.resolve() != directory:
            raise ValueError("Shared worker state must not use symlinks")
        if args.command == "where":
            print(json.dumps({"directory": str(directory), "maxWorkers": 4, "provider": "direct-deepseek"}))
            return 0
        return run(directory, argv)
    except (OSError, ValueError):
        print("Direct DeepSeek admission unavailable; inspect private slots and running workers.", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
