#!/usr/bin/env python3
"""Offline, shared OpenCode worker admission ledger (not a dispatch adapter)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import time
import uuid

MAX_ACTIVE = 4
DEFAULT_LEASE_SECONDS = 300
BUSY_TIMEOUT_SECONDS = 10
DATABASE_NAME = "opencode-ledger.sqlite3"
OUTCOME_KINDS = frozenset({
    "acceptance_pass", "acceptance_failure", "funding", "quota", "schedule",
    "infrastructure", "model_unavailable", "elapsed_time", "turn_budget",
    "interrupted", "unresolved",
})


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
    connection.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN (
            'acceptance_pass','acceptance_failure','funding','quota','schedule',
            'infrastructure','model_unavailable','elapsed_time','turn_budget',
            'interrupted','unresolved')),
        acceptance_ran INTEGER NOT NULL CHECK(acceptance_ran IN (0,1)),
        implementation_attributed INTEGER NOT NULL CHECK(implementation_attributed IN (0,1)),
        evidence_ref TEXT NOT NULL,
        failure_signature TEXT NOT NULL,
        payload TEXT NOT NULL,
        receipt TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS outcomes_task ON outcomes(task_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS escalation_intents (
        task_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        created_by_attempt TEXT NOT NULL,
        reason TEXT NOT NULL
    )""")
    return connection


def _bounded_text(value: object, field: str, maximum: int, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LedgerError(f"{field} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _safe_label(value: str, field: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value) is None:
        raise LedgerError(f"{field} must be a bounded opaque reference, not free-form text")
    return value


def _task_outcome_status(connection: sqlite3.Connection, task_id: str) -> dict:
    failures, same_signature = connection.execute(
        """SELECT COALESCE(SUM(signature_count), 0), MAX(signature_count) FROM (
             SELECT failure_signature, COUNT(*) AS signature_count FROM outcomes
             WHERE task_id=? AND kind='acceptance_failure' GROUP BY failure_signature
           )""", (task_id,)).fetchone()
    if failures is None:
        failures, same_signature = 0, 0
    pause = connection.execute(
        "SELECT 1 FROM outcomes WHERE task_id=? AND kind='funding' LIMIT 1", (task_id,)
    ).fetchone() is not None
    intent = connection.execute(
        "SELECT created_by_attempt, reason FROM escalation_intents WHERE task_id=?", (task_id,)
    ).fetchone()
    return {
        "task_id": task_id,
        "acceptance_failures": failures,
        "identical_failure_count": same_signature or 0,
        "routine_retry_blocked": failures >= 3 or (same_signature or 0) >= 2,
        "sol_help_intent": None if intent is None else {
            "created_by_attempt": intent[0], "reason": intent[1]},
        "funding_paused": pause,
    }


def record_outcome(state_root: Path, *, task_id: str, claim_id: str, attempt_id: str,
                   kind: str, acceptance_ran: bool = False,
                   implementation_attributed: bool = False,
                   evidence_ref: str | None = None,
                   failure_signature: str | None = None) -> dict:
    """Persist a caller-attested task result; this never dispatches or releases a claim."""
    task = _bounded_text(task_id, "task ID", 128)
    claim = _bounded_text(claim_id, "claim ID", 64)
    attempt = _bounded_text(attempt_id, "attempt ID", 128)
    if not isinstance(kind, str) or kind not in OUTCOME_KINDS:
        raise LedgerError("outcome kind is not an allowed typed value")
    if type(acceptance_ran) is not bool or type(implementation_attributed) is not bool:
        raise LedgerError("acceptance evidence flags must be booleans")
    evidence = _bounded_text(evidence_ref, "evidence reference", 256, optional=True)
    signature = _bounded_text(failure_signature, "failure signature", 256, optional=True)
    _safe_label(evidence, "evidence reference")
    _safe_label(signature, "failure signature")
    if kind == "acceptance_failure":
        if acceptance_ran is not True or implementation_attributed is not True or not evidence or not signature:
            raise LedgerError("counted acceptance failure requires caller-attested run, implementation attribution, evidence and signature")
    elif kind == "acceptance_pass":
        if acceptance_ran is not True or not evidence:
            raise LedgerError("acceptance pass requires caller-attested acceptance run and evidence")
        if implementation_attributed or signature:
            raise LedgerError("acceptance pass cannot carry failure attribution")
    elif acceptance_ran or implementation_attributed or evidence or signature:
        raise LedgerError("non-acceptance outcomes cannot carry acceptance-failure evidence")

    root = _canonical_private_root(Path(state_root))
    connection = _database(root)
    payload = json.dumps({
        "task_id": task, "claim_id": claim, "attempt_id": attempt, "kind": kind,
        "acceptance_ran": acceptance_ran, "implementation_attributed": implementation_attributed,
        "evidence_ref": evidence, "failure_signature": signature,
    }, sort_keys=True, separators=(",", ":"))
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT payload, receipt FROM outcomes WHERE attempt_id=?", (attempt,)).fetchone()
        if existing is not None:
            if existing[0] != payload:
                raise LedgerError("attempt ID already exists with conflicting outcome")
            receipt = json.loads(existing[1])
            connection.commit()
            return receipt
        owner = connection.execute("SELECT task_id FROM claims WHERE claim_id=?", (claim,)).fetchone()
        if owner is None or owner[0] != task:
            raise LedgerError("outcome claim does not belong to task")
        connection.execute("""INSERT INTO outcomes
            (attempt_id, task_id, claim_id, kind, acceptance_ran,
             implementation_attributed, evidence_ref, failure_signature, payload, receipt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')""",
            (attempt, task, claim, kind, int(acceptance_ran), int(implementation_attributed),
             evidence, signature, payload))
        failure_count = connection.execute(
            "SELECT COUNT(*) FROM outcomes WHERE task_id=? AND kind='acceptance_failure'", (task,)
        ).fetchone()[0]
        if failure_count >= 3:
            connection.execute("""INSERT OR IGNORE INTO escalation_intents
                (task_id, claim_id, created_by_attempt, reason) VALUES (?, ?, ?, ?)""",
                (task, claim, attempt, "three caller-attested implementation acceptance failures"))
        receipt = _task_outcome_status(connection, task)
        receipt.update({"attempt_id": attempt, "kind": kind, "recorded": True})
        connection.execute("UPDATE outcomes SET receipt=? WHERE attempt_id=?",
                           (json.dumps(receipt, sort_keys=True, separators=(",", ":")), attempt))
        connection.commit()
        return receipt
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def get_task_status(state_root: Path, task_id: str) -> dict:
    """Return durable local outcome counters and escalation/pause indicators."""
    task = _bounded_text(task_id, "task ID", 128)
    root = _canonical_private_root(Path(state_root))
    connection = _database(root)
    try:
        if connection.execute("SELECT 1 FROM claims WHERE task_id=?", (task,)).fetchone() is None:
            raise LedgerError("unknown task")
        return _task_outcome_status(connection, task)
    finally:
        connection.close()


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
