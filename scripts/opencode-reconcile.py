#!/usr/bin/env python3
"""Read-only comparison of ledger claims with explicit OpenCode evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys

MAX_AGE_SECONDS = 300
OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class ReconcileError(ValueError):
    pass


def _timestamp(value: object, field: str) -> float:
    if not isinstance(value, str):
        raise ReconcileError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReconcileError(f"{field} must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None:
        raise ReconcileError(f"{field} must include a timezone")
    stamp = parsed.timestamp()
    if stamp < 0:
        raise ReconcileError(f"{field} is invalid")
    return stamp


def _load_json(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file():
            raise ReconcileError("input must be a regular non-symlink file")
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReconcileError("input is unreadable or malformed JSON") from None
    if not isinstance(value, dict):
        raise ReconcileError("input must be a JSON object")
    return value


def _ledger_rows(database: Path) -> list[dict]:
    if database.is_symlink() or not database.is_file():
        raise ReconcileError("ledger must be an existing regular file")
    before = database.lstat()
    if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
        raise ReconcileError("ledger must be owned and private")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT task_id, claim_id, state, worktree, owned_paths, expires_at FROM claims WHERE state IN ('active','quarantined') ORDER BY task_id").fetchall()
    except sqlite3.Error:
        raise ReconcileError("ledger read failed; no reconciliation produced") from None
    finally:
        if 'connection' in locals():
            connection.close()
    after = database.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ReconcileError("ledger changed during read")
    return [dict(task_id=r[0], claim_id=r[1], state=r[2], worktree=r[3],
                 owned_paths=json.loads(r[4]), expires_at=r[5]) for r in rows]


def _worktree_status(row: dict) -> str:
    root = Path(row["worktree"])
    if not root.is_absolute() or ".." in root.parts:
        return "unavailable"
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return "unavailable"
        except FileNotFoundError:
            return "missing"
    if not stat.S_ISDIR(root.lstat().st_mode):
        return "missing"
    paths = row["owned_paths"]
    if not isinstance(paths, list) or not paths:
        return "unavailable"
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            return "unavailable"
        parts = raw.split("/")
        if any(part in ("", ".", "..") for part in parts) or raw.startswith("/"):
            return "unavailable"
        current = root
        for part in parts:
            current = current / part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    return "unavailable"
            except FileNotFoundError:
                pass
    try:
        result = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", *row["owned_paths"]],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return "dirty" if result.stdout else "clean"


def reconcile(database: Path, evidence_path: Path, *, now: float | None = None) -> dict:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if type(now) not in (int, float):
        raise ReconcileError("now must be a timestamp")
    evidence = _load_json(evidence_path)
    if set(evidence) != {"version", "generated_at", "claims"} or type(evidence["version"]) is not int or evidence["version"] != 1 or not isinstance(evidence["claims"], list):
        raise ReconcileError("evidence schema is invalid")
    generated = _timestamp(evidence["generated_at"], "generated_at")
    if generated > now or now - generated > MAX_AGE_SECONDS:
        raise ReconcileError("evidence is future-dated or stale")
    rows = _ledger_rows(database)
    by_binding = {}
    for item in evidence["claims"]:
        if not isinstance(item, dict) or set(item) != {"task_id", "claim_id", "session", "observed_at"}:
            raise ReconcileError("claim evidence schema is invalid")
        task, claim = item["task_id"], item["claim_id"]
        if not all(isinstance(v, str) and OPAQUE.fullmatch(v) for v in (task, claim)):
            raise ReconcileError("claim evidence binding is invalid")
        key = (task, claim)
        if key in by_binding:
            raise ReconcileError("duplicate claim evidence binding")
        observed = _timestamp(item["observed_at"], "observed_at")
        if observed > now or now - observed > MAX_AGE_SECONDS:
            raise ReconcileError("claim evidence is future-dated or stale")
        session = item["session"]
        if not isinstance(session, dict) or set(session) != {"state", "session_id"} or session["state"] not in ("active", "idle", "missing", "reserved"):
            raise ReconcileError("session evidence schema is invalid")
        sid = session["session_id"]
        if session["state"] == "reserved":
            if sid is not None:
                raise ReconcileError("reserved evidence cannot bind a session")
        elif not isinstance(sid, str) or not OPAQUE.fullmatch(sid):
            raise ReconcileError("session ID is invalid")
        by_binding[key] = session["state"]
    if len(by_binding) != len(rows) or any((r["task_id"], r["claim_id"]) not in by_binding for r in rows):
        raise ReconcileError("ledger and evidence claim bindings do not match exactly")
    results = []
    for index, row in enumerate(rows, 1):
        session_state = by_binding[(row["task_id"], row["claim_id"])]
        worktree = _worktree_status(row)
        expired = row["expires_at"] <= now
        # These are advisory findings only; no ledger state is changed.
        reasons = []
        if session_state in ("idle", "missing", "reserved"):
            reasons.append("termination_unproven")
        if session_state == "active":
            reasons.append("session_active")
        if expired:
            reasons.append("lease_expired_not_termination")
        if worktree in ("dirty", "missing", "unavailable"):
            reasons.append("worktree_" + worktree)
        results.append({"claim": index, "session": session_state, "worktree": worktree,
                        "lease": "expired" if expired else "current",
                        "recommendation": "quarantine_review" if reasons else "unresolved_review",
                        "reasons": reasons or ["termination_unproven"]})
    return {"version": 1, "read_only": True, "claims": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path, help="existing private ledger database")
    parser.add_argument("--evidence", required=True, type=Path, help="explicit version-1 evidence JSON")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(reconcile(args.ledger, args.evidence), sort_keys=True, separators=(",", ":")))
        return 0
    except (ReconcileError, OSError, sqlite3.Error) as error:
        print(f"opencode-reconcile: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
