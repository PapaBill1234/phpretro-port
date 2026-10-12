#!/usr/bin/env python3
"""Index only the active PHPRetro launcher's saved sessions; never global history."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import importlib.util

_history_spec = importlib.util.spec_from_file_location("builder_history", Path(__file__).resolve().parent / "builder-history.py")
history_registry = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(history_registry)

ROOT = Path(__file__).resolve().parents[1]


def encoded(path):
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def scope_paths(root):
    names = json.loads((root / "config/doctor-scope.json").read_text())["worktrees"] + history_registry.registered_worktrees(root)
    if not isinstance(names, list) or any(not isinstance(n, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", n) for n in names):
        raise ValueError("Invalid named worktree scope")
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Active checkout must be a Git repository")
    base = Path(result.stdout.strip()).parent / ".claude/worktrees"
    return [root] + [base / name for name in sorted(set(names))]


def within(path, directory):
    return path.resolve().is_relative_to(directory.resolve())


def _entry_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _read_scoped(entry, root):
    """Read an indexed file through trusted directory descriptors only."""
    relative = Path(entry["path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Session path is not relative to the project root")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    root_fd = os.open(root, flags)
    fds = [root_fd]
    try:
        parent_fd = root_fd
        for component in relative.parts[:-1]:
            if component in ("", "."):
                continue
            parent_fd = os.open(component, flags, dir_fd=parent_fd)
            fds.append(parent_fd)
        leaf_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=parent_fd)
        fds.append(leaf_fd)
        info = os.fstat(leaf_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Indexed session is not a regular file")
        identity = (info.st_dev, info.st_ino)
        expected = (entry.get("device"), entry.get("inode"))
        if expected != identity:
            raise ValueError("Indexed session changed while it was being read")
        chunks = []
        while True:
            chunk = os.read(leaf_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def catalog(root=ROOT):
    root = root.resolve()
    history = root / ".builder/claude/projects"
    if not within(history, root):
        raise ValueError("Project history must stay inside the active checkout")
    entries = []
    for cwd in scope_paths(root):
        directory = history / encoded(cwd)
        if directory.is_symlink() or not directory.is_dir() or not within(directory, history):
            continue
        # This is a targeted project directory, not a recursive scan of global history.
        for path in sorted(directory.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file() or not within(path, directory):
                continue
            identity = _entry_identity(path)
            if identity is None:
                continue
            relative = str(path.relative_to(root))
            entries.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:20],
                            "kind": "claude-subagent" if "subagents" in path.parts else "claude",
                            "path": relative, "cwd": str(cwd),
                            "bytes": path.stat().st_size, "modified_epoch": path.stat().st_mtime,
                            "device": identity[0], "inode": identity[1]})
    imported = root / ".builder/doctor/imports/current-codex.jsonl"
    if imported.is_file() and not imported.is_symlink() and within(imported, root / ".builder/doctor/imports") and within(imported, root):
        relative = str(imported.relative_to(root))
        entries.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:20],
                        "kind": "codex-current-chat-snapshot", "path": relative,
                        "cwd": str(root), "bytes": imported.stat().st_size,
                        "modified_epoch": imported.stat().st_mtime,
                        "device": imported.stat().st_dev, "inode": imported.stat().st_ino})
    return entries


def read_session(session_id, offset=0, limit=16000, root=ROOT):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
        raise ValueError("Use offset >= 0 and limit 1..65536")
    match = next((e for e in catalog(root) if e["id"] == session_id), None)
    if match is None:
        raise ValueError("Session is not in this project's scoped index")
    content = _read_scoped(match, root)
    end = min(offset + limit, len(content))
    return {"session": match, "offset": offset, "next_offset": end if end < len(content) else None,
            "characters": len(content), "content": content[offset:end]}


def search(query, offset=0, limit=10, root=ROOT):
    if not query or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError("Use a nonempty literal query, offset >= 0, and limit 1..25")
    hits = []
    for entry in catalog(root):
        content = _read_scoped(entry, root)
        start = 0
        while True:
            position = content.find(query, start)
            if position < 0:
                break
            hits.append({"id": entry["id"], "kind": entry["kind"], "character_offset": position,
                         "excerpt": content[max(0, position - 100):position + len(query) + 150]})
            start = position + len(query)
            if len(hits) > offset + limit:
                return {"hits": hits[offset:offset+limit], "next_offset": offset + limit}
    return {"hits": hits[offset:offset+limit], "next_offset": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("read", "search"):
        p = sub.add_parser(command)
        p.add_argument("value")
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--limit", type=int, default=16000 if command == "read" else 10)
    args = parser.parse_args()
    if args.command == "list":
        result = {"root": str(ROOT), "scope_paths": [str(p) for p in scope_paths(ROOT)],
                  "sessions": catalog(), "content_is_private": True,
                  "coverage": "current shared Claude history and named worktrees, nested subagents, current Codex snapshot only"}
    elif args.command == "read":
        result = read_session(args.value, args.offset, args.limit)
    else:
        result = search(args.value, args.offset, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
