#!/usr/bin/env python3
"""Persist the active builder's registered worktree names privately."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_MAX_WORKTREES = 4096
_MAX_REGISTRY_BYTES = 262144
_REGISTRY_ERROR = "Invalid worktree registry"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    root = Path(root).resolve()
    private = root / ".builder"
    directory = private / "doctor"
    registry = directory / "worktrees.json"
    lock = directory / "worktrees.lock"

    for path in (private, directory):
        try:
            info = path.lstat()
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Invalid worktree registry path")
        os.chmod(path, 0o700)
    return directory, registry, lock


def _read_registry(path: Path) -> list[str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(_REGISTRY_ERROR) from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_REGISTRY_BYTES:
            raise ValueError(_REGISTRY_ERROR)
        os.fchmod(fd, 0o600)
        chunks = []
        remaining = _MAX_REGISTRY_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_REGISTRY_BYTES:
            raise ValueError(_REGISTRY_ERROR)
    finally:
        os.close(fd)

    try:
        payload = json.loads(raw.decode("utf-8"))
        names = payload["worktrees"]
        if (not isinstance(payload, dict) or set(payload) != {"version", "worktrees"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or not isinstance(names, list) or len(names) > _MAX_WORKTREES
                or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names)
                or len(names) != len(set(names))):
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ValueError(_REGISTRY_ERROR) from None
    return names


@contextmanager
def _locked(lock_path: Path):
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("Invalid worktree registry lock") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid worktree registry lock")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _write_registry(directory: Path, path: Path, names: list[str]) -> None:
    payload = json.dumps({"version": 1, "worktrees": names}, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".worktrees-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def register_worktree(root: Path, name: str) -> None:
    """Record a validated worktree name without losing concurrent registrations."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Invalid worktree name")
    directory, registry, lock_path = _paths(Path(root))
    with _locked(lock_path):
        names = _read_registry(registry)
        if name in names:
            return
        if len(names) >= _MAX_WORKTREES:
            raise ValueError("Worktree registry limit reached")
        names = sorted([*names, name])
        _write_registry(directory, registry, names)


def registered_worktrees(root: Path) -> list[str]:
    """Return registered worktree names, or an empty list before first use."""
    directory, registry, lock_path = _paths(Path(root))
    with _locked(lock_path):
        return sorted(_read_registry(registry))
