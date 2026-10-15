#!/usr/bin/env python3
"""Offline, shared OpenCode worker admission ledger (not a dispatch adapter)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import time
import uuid

MAX_ACTIVE = 4
DEFAULT_LEASE_SECONDS = 300
BUSY_TIMEOUT_SECONDS = 10
DATABASE_NAME = "opencode-ledger.sqlite3"


class LedgerError(ValueError):
    """A claim or private-ledger operation was rejected safely."""


def _canonical_private_root(root: Path) -> Path:
    supplied = Path(root)
    if not supplied.is_absolute():
        raise LedgerError("state root must be an explicit absolute path")
    if ".." in supplied.parts:
        raise LedgerError("state root path traversal is not allowed")
    current = Path(supplied.anchor)
    for part in supplied.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise LedgerError("state root must already exist") from None
        if stat.S_ISLNK(mode):
            raise LedgerError("state root cannot contain symlink aliases")
    info = supplied.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise LedgerError("state root must be a directory")
    if info.st_uid != os.getuid():
        raise LedgerError("state root must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LedgerError("state root must be private (mode 0700 or stricter)")
    return supplied


def _database(root: Path) -> sqlite3.Connection:
    path = root / DATABASE_NAME
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = True
        before_open = None
    except FileExistsError:
        created = False
        try:
            before_open = path.lstat()
        except FileNotFoundError:
            raise LedgerError("ledger database disappeared during validation") from None
        if not stat.S_ISREG(before_open.st_mode) or before_open.st_uid != os.getuid():
            raise LedgerError("ledger database must be an owned regular file")
        if stat.S_IMODE(before_open.st_mode) & 0o077:
            raise LedgerError("ledger database must be private (mode 0600)")
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise LedgerError("ledger database must be an owned regular file")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise LedgerError("ledger database must be private (mode 0600)")
        if before_open is not None and (opened.st_dev, opened.st_ino) != (before_open.st_dev, before_open.st_ino):
            raise LedgerError("ledger database changed during validation")
        if created:
            # The descriptor is the O_EXCL-created file, so tighten only this
            # new inode; never repair permissions on a pre-existing database.
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("""CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE,
        profile TEXT NOT NULL,
        branch TEXT NOT NULL,
        worktree TEXT NOT NULL,
        owned_paths TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','quarantined','released')),
        heartbeat_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        termination_evidence TEXT
    )""")
    return connection


def _canonical_worktree(worktree: Path) -> Path:
    supplied = Path(worktree)
    if not supplied.is_absolute():
        raise LedgerError("worktree must be an explicit absolute path")
    if ".." in supplied.parts:
        raise LedgerError("worktree path traversal is not allowed")
    current = Path(supplied.anchor)
    for part in supplied.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise LedgerError("worktree cannot contain symlink aliases")
        except FileNotFoundError:
            raise LedgerError("worktree must already exist") from None
    if not stat.S_ISDIR(supplied.lstat().st_mode):
        raise LedgerError("worktree must be a directory")
    return supplied


def _owned_paths(worktree: Path, paths: list[str]) -> list[str]:
    if not paths:
        raise LedgerError("at least one owned path is required")
    result = []
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise LedgerError("owned paths must be non-empty relative POSIX paths")
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in raw.split("/")):
            raise LedgerError("owned path traversal or non-canonical path rejected")
        current = worktree
        for part in relative.parts:
            current = current / part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise LedgerError("owned path symlink aliases are not allowed")
            except FileNotFoundError:
                pass
        result.append(relative.as_posix())
    if len(set(result)) != len(result):
        raise LedgerError("duplicate canonical owned path")
    return sorted(result)


def _overlap(first: list[str], second: list[str]) -> bool:
    return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for a in first for b in second)


def _insert_claim(connection: sqlite3.Connection, values: tuple) -> None:
    connection.execute("""INSERT INTO claims
        (claim_id, task_id, profile, branch, worktree, owned_paths, state,
         heartbeat_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""", values)


def claim(state_root: Path, *, task_id: str, profile: str, branch: str,
          worktree: Path, owned_paths: list[str], lease_seconds: int = DEFAULT_LEASE_SECONDS,
          now: float | None = None) -> str:
    """Atomically claim one of four shared slots; returns its durable claim ID."""
    root = _canonical_private_root(Path(state_root))
    canonical_worktree = _canonical_worktree(Path(worktree))
    paths = _owned_paths(canonical_worktree, owned_paths)
    if not all(isinstance(v, str) and v.strip() for v in (task_id, profile, branch)):
        raise LedgerError("task ID, profile and branch are required")
    if not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise LedgerError("lease duration must be a positive integer")
    stamp = time.time() if now is None else float(now)
    claim_id = str(uuid.uuid4())
    connection = _database(root)
    try:
        # Make quarantine durable even when this admission is subsequently denied.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE claims SET state='quarantined' WHERE state='active' AND expires_at <= ?", (stamp,))
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM claims WHERE task_id=?", (task_id,)).fetchone():
            raise LedgerError("duplicate task claim")
        live = connection.execute("SELECT task_id, branch, worktree, owned_paths FROM claims "
                                  "WHERE state IN ('active','quarantined')").fetchall()
        proposed = paths
        for other_task, other_branch, other_worktree, encoded in live:
            if branch == other_branch:
                raise LedgerError(f"branch already claimed by task {other_task}")
            if str(canonical_worktree) == other_worktree:
                raise LedgerError(f"worktree already claimed by task {other_task}")
            if _overlap(proposed, json.loads(encoded)):
                raise LedgerError(f"owned paths overlap existing task {other_task}")
        occupied = connection.execute("SELECT COUNT(*) FROM claims WHERE state IN ('active','quarantined')").fetchone()[0]
        if occupied >= MAX_ACTIVE:
            raise LedgerError("four-slot admission limit reached")
        _insert_claim(connection, (claim_id, task_id, profile, branch,
                                   str(canonical_worktree), json.dumps(paths), stamp,
                                   stamp + lease_seconds))
        connection.commit()
        return claim_id
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def heartbeat(state_root: Path, claim_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
              now: float | None = None) -> None:
    """Extend an unexpired claim; an expired claim is quarantined, never revived."""
    if lease_seconds <= 0:
        raise LedgerError("lease duration must be positive")
    root = _canonical_private_root(Path(state_root))
    stamp = time.time() if now is None else float(now)
    connection = _database(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT state, expires_at FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown claim")
        if row[0] != "active" or row[1] <= stamp:
            connection.execute("UPDATE claims SET state='quarantined' WHERE claim_id=? AND state='active'", (claim_id,))
            connection.commit()
            raise LedgerError("expired or quarantined claim cannot be revived")
        connection.execute("UPDATE claims SET heartbeat_at=?, expires_at=? WHERE claim_id=?",
                           (stamp, stamp + lease_seconds, claim_id))
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def release(state_root: Path, claim_id: str, *, termination_evidence: dict) -> None:
    """Release only with explicit caller-attested termination evidence.

    This offline API cannot verify that a native OpenCode session actually ended.
    """
    if not isinstance(termination_evidence, dict) or termination_evidence.get("verified_terminated") is not True:
        raise LedgerError("explicit verified-termination evidence is required")
    if not all(isinstance(termination_evidence.get(k), str) and termination_evidence[k].strip()
               for k in ("verifier", "evidence_ref", "terminated_at")):
        raise LedgerError("termination evidence requires verifier, evidence_ref and terminated_at")
    root = _canonical_private_root(Path(state_root))
    connection = _database(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("UPDATE claims SET state='released', termination_evidence=? "
                                    "WHERE claim_id=? AND state IN ('active','quarantined')",
                                    (json.dumps(termination_evidence, sort_keys=True), claim_id))
        if cursor.rowcount != 1:
            raise LedgerError("claim is unknown or already released")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path,
                        help="existing absolute private shared ledger directory (mode 0700)")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("claim")
    create.add_argument("--task", required=True)
    create.add_argument("--profile", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True, type=Path)
    create.add_argument("--owned", action="append", required=True)
    create.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    args = parser.parse_args(argv)
    try:
        claim_id = claim(args.state_root, task_id=args.task, profile=args.profile,
                         branch=args.branch, worktree=args.worktree, owned_paths=args.owned,
                         lease_seconds=args.lease_seconds)
        print(json.dumps({"claim_id": claim_id, "state": "active"}))
        return 0
    except (LedgerError, OSError, sqlite3.Error) as error:
        print(f"opencode-ledger: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
