#!/usr/bin/env python3
"""Local, atomic task coordination. Not a security or automatic approval boundary."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def scope_path(value):
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in {"..", ".git", ".builder", "secrets"} for x in p.parts):
        raise ValueError("scope must be a repository-relative source path")
    return str(p)


def covers(scope, path):
    return path == scope or path.startswith(scope.rstrip("/") + "/")


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY, status TEXT NOT NULL, scope TEXT NOT NULL,
      dependencies TEXT NOT NULL, base TEXT NOT NULL, owner TEXT,
      lease_until INTEGER, commit_sha TEXT, evidence TEXT, checks TEXT,
      reviewer TEXT, review_note TEXT, integrated_sha TEXT)""")
    return db


def run(args):
    db = connect(args.db)
    try:
        db.execute("BEGIN IMMEDIATE")
        if args.command == "add":
            base = git("rev-parse", args.base + "^{commit}")
            scope = [scope_path(x) for x in args.scope]
            db.execute("INSERT INTO tasks (id,status,scope,dependencies,base) VALUES (?,?,?,?,?)",
                       (args.id, "queued", json.dumps(scope), json.dumps(args.depends), base))
        elif args.command == "list":
            rows = [dict(x) for x in db.execute("SELECT * FROM tasks ORDER BY id")]
            for row in rows:
                row["scope"] = json.loads(row["scope"])
                row["dependencies"] = json.loads(row["dependencies"])
                row["lease_expired"] = bool(row["lease_until"] and row["lease_until"] < time.time())
            print(json.dumps(rows, indent=2))
        else:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (args.id,)).fetchone()
            if row is None:
                raise ValueError("task does not exist")
            if args.command == "claim":
                if row["status"] != "queued":
                    raise ValueError("task already claimed or complete; inspect before recovery")
                for dependency in json.loads(row["dependencies"]):
                    found = db.execute("SELECT status FROM tasks WHERE id=?", (dependency,)).fetchone()
                    if not found or found["status"] != "integrated":
                        raise ValueError("dependency is not integrated: " + dependency)
                scope = json.loads(row["scope"])
                for other in db.execute("SELECT id,scope FROM tasks WHERE status IN ('claimed','submitted','accepted')"):
                    if any(covers(a,b) or covers(b,a) for a in scope for b in json.loads(other["scope"])):
                        raise ValueError("write scope overlaps task " + other["id"])
                db.execute("UPDATE tasks SET status='claimed',owner=?,lease_until=? WHERE id=?",
                           (args.owner, int(time.time()) + args.lease_seconds, args.id))
            elif args.command == "renew":
                require_owner(row, args.owner, "claimed")
                db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (int(time.time()) + args.lease_seconds, args.id))
            elif args.command == "finish":
                require_owner(row, args.owner, "claimed")
                commit = git("rev-parse", args.commit + "^{commit}")
                subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", row["base"], commit], check=True)
                # Inspect both sides of moves; rename detection otherwise hides deletions.
                paths = git("diff", "--name-only", "--no-renames", row["base"], commit).splitlines()
                if not paths or any(not any(covers(s,p) for s in json.loads(row["scope"])) for p in paths):
                    raise ValueError("commit changes are empty or outside owned scope")
                evidence = scope_path(args.evidence) if not args.evidence.startswith(".builder/") else args.evidence
                evidence_file = (ROOT / evidence).resolve()
                if not evidence_file.is_relative_to(ROOT) or not evidence_file.is_file():
                    raise ValueError("evidence must be an existing file inside this repository")
                db.execute("UPDATE tasks SET status='submitted',commit_sha=?,evidence=?,checks=? WHERE id=?",
                           (commit, evidence, args.checks, args.id))
            elif args.command == "review":
                if row["status"] != "submitted" or args.reviewer == row["owner"]:
                    raise ValueError("a submitted task requires an independent reviewer")
                status = "accepted" if args.result == "accepted" else "claimed"
                db.execute("UPDATE tasks SET status=?,reviewer=?,review_note=? WHERE id=?",
                           (status, args.reviewer, args.note, args.id))
            elif args.command == "integrate":
                if row["status"] != "accepted":
                    raise ValueError("task must have recorded independent review")
                commit = git("rev-parse", args.commit + "^{commit}")
                integration_tip = git("rev-parse", "refs/heads/main^{commit}")
                if commit != integration_tip:
                    raise ValueError("integration commit must be the actual current main branch tip")
                subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", row["commit_sha"], commit], check=True)
                db.execute("UPDATE tasks SET status='integrated',integrated_sha=?,lease_until=NULL WHERE id=?", (commit, args.id))
            elif args.command == "release":
                require_owner(row, args.owner, "claimed")
                if not args.inspected:
                    raise ValueError("inspect the worker's worktree and checkpoint before releasing")
                db.execute("UPDATE tasks SET status='queued',owner=NULL,lease_until=NULL WHERE id=?", (args.id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def require_owner(row, owner, status):
    if row["owner"] != owner or row["status"] != status:
        raise ValueError("task owner or state does not match")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=ROOT / ".builder/tasks.sqlite")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add"); a.add_argument("id"); a.add_argument("--scope", nargs="+", required=True)
    a.add_argument("--depends", nargs="*", default=[]); a.add_argument("--base", default="HEAD")
    sub.add_parser("list")
    for command in ("claim", "renew", "finish", "release"):
        a = sub.add_parser(command); a.add_argument("id"); a.add_argument("--owner", required=True)
        if command in ("claim", "renew"):
            a.add_argument("--lease-seconds", type=int, default=1800)
        if command == "finish":
            a.add_argument("--commit", required=True); a.add_argument("--evidence", required=True); a.add_argument("--checks", required=True)
        if command == "release":
            a.add_argument("--inspected", action="store_true")
    a = sub.add_parser("review"); a.add_argument("id"); a.add_argument("--reviewer", required=True)
    a.add_argument("--result", choices=["accepted", "rejected"], required=True); a.add_argument("--note", required=True)
    a = sub.add_parser("integrate"); a.add_argument("id"); a.add_argument("--commit", required=True)
    return p


if __name__ == "__main__":
    try:
        run(parser().parse_args())
    except (ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
