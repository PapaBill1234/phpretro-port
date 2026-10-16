#!/usr/bin/env python3
"""Record an explicit local reviewer decision for a verified acceptance run.

This is a caller-attested local adapter, not reviewer authentication or native
OpenCode qualification. It never runs a command or changes/releases a claim.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("local adapter dependency unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = _load("opencode_outcome_acceptance", "opencode-acceptance.py")
ledger = _load("opencode_outcome_ledger", "opencode-ledger.py")


class OutcomeError(ValueError):
    """A verified run or explicit reviewer decision was rejected."""


def record(state_root: Path, *, run_id: str, task_id: str, claim_id: str,
           attempt_id: str, verdict: str, review_confirmed: bool,
           implementation_attributed: bool = False,
           failure_signature: str | None = None) -> dict:
    """Validate all local evidence, then persist one caller-attested outcome."""
    run = ledger._opaque_id(run_id, "run ID", 96)
    task = ledger._opaque_id(task_id, "task ID")
    claim = ledger._opaque_id(claim_id, "claim ID", 64)
    attempt = ledger._opaque_id(attempt_id, "attempt ID")
    if verdict not in ("pass", "failure"):
        raise OutcomeError("verdict must be pass or failure")
    if type(review_confirmed) is not bool or review_confirmed is not True:
        raise OutcomeError("explicit reviewer confirmation is required")
    if type(implementation_attributed) is not bool:
        raise OutcomeError("implementation attribution must be a boolean")
    if verdict == "pass":
        if implementation_attributed or failure_signature is not None:
            raise OutcomeError("pass cannot carry failure attribution")
    else:
        if implementation_attributed is not True:
            raise OutcomeError("review-confirmed implementation attribution is required")
        signature = ledger._bounded_text(failure_signature, "failure signature", 256)
        ledger._safe_label(signature, "failure signature")

    # Recheck on every call, even an exact ledger attempt replay: a cached
    # outcome must not bypass receipt/log integrity verification.
    receipt = acceptance.verify(Path(state_root), run)
    expected_status = "command_completed_zero" if verdict == "pass" else "unclassified_nonzero"
    if receipt.get("status") != expected_status:
        raise OutcomeError("verified command status is not eligible for this verdict")
    if (receipt.get("run_id") != run or receipt.get("task_id") != task or
            receipt.get("claim_id") != claim):
        raise OutcomeError("verified receipt binding mismatch")

    # The original claim row is historical evidence. It need not remain active
    # or unexpired after a reviewed run, but its task/worktree binding must match.
    root = ledger._canonical_private_root(Path(state_root))
    connection = ledger._read_database(root)
    if connection is None:
        raise OutcomeError("original claim is unavailable")
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT task_id, worktree FROM claims WHERE claim_id=?", (claim,)
        ).fetchone()
        if row is None or row[0] != task or row[1] != receipt.get("worktree"):
            raise OutcomeError("original claim binding mismatch")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    # Stable run identity, deliberately not a mutable-content hash. The ledger
    # uniqueness constraint blocks another attempt/verdict for this execution.
    evidence_ref = f"acceptance/{run}"
    if verdict == "pass":
        return ledger.record_outcome(
            root, task_id=task, claim_id=claim, attempt_id=attempt,
            kind="acceptance_pass", acceptance_ran=True,
            evidence_ref=evidence_ref)
    return ledger.record_outcome(
        root, task_id=task, claim_id=claim, attempt_id=attempt,
        kind="acceptance_failure", acceptance_ran=True,
        implementation_attributed=True, evidence_ref=evidence_ref,
        failure_signature=signature)


def _main(argv: list[str] | None = None) -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, _message):
            raise OutcomeError("invalid command arguments")

    parser = SafeParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--claim", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--verdict", required=True, choices=("pass", "failure"))
    parser.add_argument("--review-confirmed", required=True, action="store_true")
    parser.add_argument("--implementation-attributed", action="store_true")
    parser.add_argument("--failure-signature")
    try:
        args = parser.parse_args(argv)
        result = record(args.state_root, run_id=args.run_id, task_id=args.task,
                        claim_id=args.claim, attempt_id=args.attempt_id,
                        verdict=args.verdict, review_confirmed=args.review_confirmed,
                        implementation_attributed=args.implementation_attributed,
                        failure_signature=args.failure_signature)
        print(json.dumps({"kind": result["kind"], "recorded": result["recorded"],
                          "acceptance_failures": result["acceptance_failures"],
                          "routine_retry_blocked": result["routine_retry_blocked"],
                          "sol_help_intent": result["sol_help_intent"]}, sort_keys=True))
        return 0
    except (OutcomeError, acceptance.AcceptanceError, ledger.LedgerError,
            OSError, sqlite3.Error, ValueError, TypeError):
        print("opencode-acceptance-outcome: decision rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
