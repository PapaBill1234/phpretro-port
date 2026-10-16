#!/usr/bin/env python3
"""Run explicitly supplied local acceptance argv and keep private receipts.

This is not a sandbox or dispatch adapter. The coordinator trusts the argv and
must ensure it is a test command. It never classifies implementation failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "scripts" / "opencode-ledger.py"
_spec = importlib.util.spec_from_file_location("opencode_acceptance_ledger", LEDGER_PATH)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

MAX_TIMEOUT = 3600
MAX_OUTPUT = 16 * 1024 * 1024
MAX_ARGV_ITEMS = 128
MAX_ARG_LENGTH = 4096
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RECEIPT_SCHEMA = 2


class AcceptanceError(ValueError):
    pass


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise AcceptanceError(f"invalid {field}")
    return value


def _private_root(path: Path) -> Path:
    root = ledger._canonical_private_root(path)
    return root


def _preflight(state_root: Path, task: str, claim_id: str, worktree: Path) -> Path:
    root = _private_root(state_root)
    canonical = ledger._canonical_worktree(worktree)
    db = root / ledger.DATABASE_NAME
    before = db.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
        raise AcceptanceError("invalid ledger database")
    uri = db.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT task_id, worktree, state, expires_at FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None or row[0] != task or row[1] != str(canonical):
            raise AcceptanceError("claim binding mismatch")
        if row[2] != "active" or row[3] <= time.time():
            raise AcceptanceError("claim is not active")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    after = db.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise AcceptanceError("ledger database changed during preflight")
    return canonical


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> tuple[str, int]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
        raise AcceptanceError("unsafe log file")
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AcceptanceError("log changed during verification")
        with os.fdopen(fd, "rb", closefd=False) as source:
            for block in iter(lambda: source.read(65536), b""):
                digest.update(block)
                size += len(block)
    finally:
        os.close(fd)
    return digest.hexdigest(), size


def _read_json_regular(path: Path) -> dict:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
            stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > 1024 * 1024):
        raise AcceptanceError("unsafe receipt file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AcceptanceError("receipt changed during verification")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as source:
            value = json.load(source)
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise AcceptanceError("invalid receipt data")
    return value


def _atomic_receipt(run_dir: Path, receipt: dict) -> None:
    temp = run_dir / ".receipt.tmp"
    _write_new(temp, (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.replace(temp, run_dir / "receipt.json")
    _fsync_dir(run_dir)


def _terminate(process: subprocess.Popen, grace: float = 0.25) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit while an in-group descendant remains. Waiting for the
    # leader is bounded too; neither this nor the receipt proves group death.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    return process.returncode is not None


def _new_log(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _close_pipes(process: subprocess.Popen | None, selector: selectors.BaseSelector | None) -> None:
    if selector is not None:
        try:
            selector.close()
        except OSError:
            pass
    if process is not None:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def run(state_root: Path, task_id: str, claim_id: str, worktree: Path, run_id: str,
        argv: list[str], timeout: float) -> dict:
    task = _safe_id(task_id, "task ID")
    claim = _safe_id(claim_id, "claim ID")
    run_name = _safe_id(run_id, "run ID")
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_ITEMS or any(
        not isinstance(item, str) or not item or len(item) > MAX_ARG_LENGTH or "\x00" in item for item in argv
    ):
        raise AcceptanceError("invalid argv")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise AcceptanceError("invalid timeout")
    canonical = _preflight(state_root, task, claim, worktree)
    root = _private_root(state_root)
    runs = root / "acceptance-runs"
    try:
        runs.mkdir(mode=0o700)
        _fsync_dir(root)
    except FileExistsError:
        pass
    info = runs.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise AcceptanceError("unsafe receipt directory")
    run_dir = runs / run_name
    run_dir.mkdir(mode=0o700)  # O_EXCL semantics: any prior run, complete or partial, is non-replayable.
    _fsync_dir(runs)
    started_wall = time.time()
    started_mono = time.monotonic()
    intent = {
        "schema": RECEIPT_SCHEMA, "run_id": run_name, "task_id": task, "claim_id": claim,
        "worktree": str(canonical), "argv": argv,
        "argv_sha256": hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest(),
        "started_at": started_wall, "timeout_seconds": float(timeout),
    }
    _write_new(run_dir / "intent.json", (json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n").encode())
    _fsync_dir(run_dir)
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    status = "unresolved"
    returncode = None
    process = None
    selector = None
    output_fds = {}
    output_size = 0
    overflow = False
    try:
        output_fds["stdout"] = _new_log(run_dir / "stdout.log")
        output_fds["stderr"] = _new_log(run_dir / "stderr.log")
        process = subprocess.Popen(argv, cwd=canonical, env=env, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
        selector = selectors.DefaultSelector()
        for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        deadline = started_mono + float(timeout)
        while True:
            if overflow:
                _terminate(process)
                status = "unresolved_output_limit"
                break
            if time.monotonic() >= deadline:
                _terminate(process)
                status = "unresolved_timeout"
                break
            returncode = process.poll()
            if returncode is not None and not selector.get_map() and not _group_exists(process.pid):
                status = "command_completed_zero" if returncode == 0 else "unclassified_nonzero"
                break
            remaining = max(0.0, deadline - time.monotonic())
            for key, _ in selector.select(min(0.05, remaining)):
                label = key.data
                try:
                    block = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                available = MAX_OUTPUT - output_size
                accepted = block[:max(0, available)]
                if accepted:
                    view = memoryview(accepted)
                    while view:
                        written = os.write(output_fds[label], view)
                        view = view[written:]
                    output_size += len(accepted)
                if len(accepted) != len(block):
                    overflow = True
    except (KeyboardInterrupt, SystemExit):
        if process is not None:
            _terminate(process)
        status = "unresolved_interrupted"
    except BaseException:
        if process is not None:
            _terminate(process)
        status = "unresolved_infrastructure"
    finally:
        _close_pipes(process, selector)
        for fd in output_fds.values():
            try:
                os.fsync(fd)
            except OSError:
                status = "unresolved_infrastructure"
            os.close(fd)
    for log_name in ("stdout.log", "stderr.log"):
        log_path = run_dir / log_name
        try:
            log_path.lstat()
        except FileNotFoundError:
            _write_new(log_path, b"")
    stdout_hash, stdout_size = _hash_file(run_dir / "stdout.log") if (run_dir / "stdout.log").exists() else (hashlib.sha256(b"").hexdigest(), 0)
    stderr_hash, stderr_size = _hash_file(run_dir / "stderr.log") if (run_dir / "stderr.log").exists() else (hashlib.sha256(b"").hexdigest(), 0)
    receipt = {"schema": RECEIPT_SCHEMA, "run_id": run_name, "task_id": task, "claim_id": claim,
               "worktree": str(canonical), "argv_sha256": intent["argv_sha256"],
               "started_at": started_wall, "timeout_seconds": float(timeout),
               "finished_at": time.time(), "status": status,
               "returncode": returncode, "stdout_sha256": stdout_hash, "stdout_size": stdout_size,
               "stderr_sha256": stderr_hash, "stderr_size": stderr_size}
    _atomic_receipt(run_dir, receipt)
    return receipt


def verify(state_root: Path, run_id: str) -> dict:
    name = _safe_id(run_id, "run ID")
    root = _private_root(state_root)
    runs = root / "acceptance-runs"
    run_dir = runs / name
    for path in (runs, run_dir):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AcceptanceError("unsafe receipt path")
    intent_path, receipt_path = run_dir / "intent.json", run_dir / "receipt.json"
    intent = _read_json_regular(intent_path)
    try:
        receipt_path.lstat()
    except FileNotFoundError:
        if (type(intent.get("schema")) is not int or intent.get("schema") != RECEIPT_SCHEMA or
                intent.get("run_id") != name):
            raise AcceptanceError("invalid incomplete intent")
        return {"run_id": name, "status": "unresolved_incomplete_intent"}
    receipt = _read_json_regular(receipt_path)
    if (type(intent.get("schema")) is not int or intent.get("schema") != RECEIPT_SCHEMA or
            intent.get("run_id") != name):
        raise AcceptanceError("invalid intent schema")
    if set(intent) != {"schema", "run_id", "task_id", "claim_id", "worktree", "argv",
                       "argv_sha256", "started_at", "timeout_seconds"}:
        raise AcceptanceError("invalid intent fields")
    _safe_id(intent.get("task_id"), "task ID")
    _safe_id(intent.get("claim_id"), "claim ID")
    worktree = ledger._canonical_worktree(Path(intent.get("worktree", "")))
    if str(worktree) != intent.get("worktree"):
        raise AcceptanceError("non-canonical worktree binding")
    argv = intent.get("argv")
    if (not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_ITEMS or
            any(not isinstance(item, str) or not item or len(item) > MAX_ARG_LENGTH or "\x00" in item for item in argv)):
        raise AcceptanceError("invalid intent argv")
    argv_hash = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
    if argv_hash != intent.get("argv_sha256"):
        raise AcceptanceError("intent argv hash mismatch")
    if (type(intent.get("started_at")) is not float or
            not math.isfinite(intent["started_at"]) or intent["started_at"] < 0 or
            type(intent.get("timeout_seconds")) is not float or
            not math.isfinite(intent["timeout_seconds"]) or not 0 < intent["timeout_seconds"] <= MAX_TIMEOUT):
        raise AcceptanceError("invalid intent times")
    expected_fields = {"schema", "run_id", "task_id", "claim_id", "worktree", "argv_sha256",
                       "started_at", "timeout_seconds", "finished_at", "status", "returncode",
                       "stdout_sha256", "stdout_size", "stderr_sha256", "stderr_size"}
    if (set(receipt) != expected_fields or type(receipt.get("schema")) is not int or
            receipt.get("schema") != RECEIPT_SCHEMA):
        raise AcceptanceError("invalid receipt fields")
    if (type(receipt.get("started_at")) is not float or not math.isfinite(receipt["started_at"]) or
            type(receipt.get("timeout_seconds")) is not float or
            not math.isfinite(receipt["timeout_seconds"])):
        raise AcceptanceError("invalid receipt binding types")
    for key in ("schema", "run_id", "task_id", "claim_id", "worktree", "argv_sha256", "started_at", "timeout_seconds"):
        if receipt.get(key) != intent.get(key):
            raise AcceptanceError("receipt binding mismatch")
    if (not isinstance(receipt.get("status"), str) or
            receipt["status"] not in {"command_completed_zero", "unclassified_nonzero", "unresolved_timeout",
                                      "unresolved_output_limit", "unresolved_capture", "unresolved_interrupted",
                                      "unresolved_infrastructure"}):
        raise AcceptanceError("invalid receipt status")
    if (type(receipt.get("finished_at")) is not float or
            not math.isfinite(receipt["finished_at"]) or receipt["finished_at"] < intent["started_at"]):
        raise AcceptanceError("invalid receipt time")
    code = receipt.get("returncode")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
        raise AcceptanceError("invalid receipt return code")
    if receipt["status"] == "command_completed_zero" and code != 0:
        raise AcceptanceError("invalid zero-exit receipt")
    if receipt["status"] == "unclassified_nonzero" and (code is None or code == 0):
        raise AcceptanceError("invalid nonzero receipt")
    total_size = 0
    for stream in ("stdout", "stderr"):
        digest, size = _hash_file(run_dir / f"{stream}.log")
        stored_size = receipt.get(f"{stream}_size")
        stored_hash = receipt.get(f"{stream}_sha256")
        if (digest != receipt.get(f"{stream}_sha256") or size != stored_size or
                isinstance(stored_size, bool) or not isinstance(stored_size, int) or
                not isinstance(stored_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", stored_hash)):
            raise AcceptanceError("receipt log verification failed")
        if size > MAX_OUTPUT or stored_size < 0:
            raise AcceptanceError("receipt output exceeds bound")
        total_size += size
    if total_size > MAX_OUTPUT:
        raise AcceptanceError("receipt output exceeds bound")
    return receipt


def _main(argv: list[str] | None = None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, _message):
            raise AcceptanceError("invalid command arguments")

    def terminate_handler(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_handler)
    parser = SafeParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    execute = sub.add_parser("run")
    execute.add_argument("--task", required=True)
    execute.add_argument("--claim", required=True)
    execute.add_argument("--worktree", required=True, type=Path)
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--timeout", required=True, type=float)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    check = sub.add_parser("verify")
    check.add_argument("--run-id", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "run":
            command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            result = run(args.state_root, args.task, args.claim, args.worktree,
                         args.run_id, command, args.timeout)
            public_result = {"status": result["status"], "receipt": "written"}
        else:
            result = verify(args.state_root, args.run_id)
            public_result = {"status": result["status"],
                             "verified": result["status"] != "unresolved_incomplete_intent"}
        print(json.dumps(public_result, sort_keys=True))
        return 0
    except (AcceptanceError, ledger.LedgerError, OSError, sqlite3.Error, ValueError, TypeError):
        print("opencode-acceptance: request rejected or receipt unresolved", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
