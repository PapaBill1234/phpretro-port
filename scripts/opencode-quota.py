"""Offline validation and private checkpointing for manual OpenCode quota evidence.

This module does not query OpenCode or enforce dispatch admission.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FRESHNESS_SECONDS = 300
PROFILE_NAMES = {"primary", "secondary"}
SOURCE_MANUAL = "manual_native_usage_page"
_BINDING = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVIDENCE_FIELDS = {
    "schema_version", "profile", "connection_binding", "source",
    "operator_attested", "observed_at", "verified_at", "windows",
}
_WINDOW_FIELDS = {"id", "percentage", "basis", "duration_minutes", "reset_at"}


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive_timestamp")
    return parsed.astimezone(timezone.utc)


def validate_evidence(
    evidence: Any,
    *,
    expected_profile: str,
    expected_binding: str,
    now: datetime,
) -> dict[str, Any]:
    """Validate a strict evidence object and return a safe typed decision."""
    try:
        if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
            raise ValueError("invalid_evidence_fields")
        if (isinstance(evidence["schema_version"], bool)
                or not isinstance(evidence["schema_version"], int)
                or evidence["schema_version"] != 1):
            raise ValueError("unsupported_schema")
        if expected_profile not in PROFILE_NAMES or evidence["profile"] != expected_profile:
            raise ValueError("profile_mismatch")
        binding = evidence["connection_binding"]
        if (not isinstance(binding, str) or not _BINDING.fullmatch(binding)
                or not isinstance(expected_binding, str) or binding != expected_binding):
            raise ValueError("binding_mismatch")
        if evidence["source"] != SOURCE_MANUAL:
            raise ValueError("unsupported_source")
        if evidence["operator_attested"] is not True:
            raise ValueError("operator_attestation_required")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("invalid_clock")
        now_utc = now.astimezone(timezone.utc)
        observed = _timestamp(evidence["observed_at"])
        verified = _timestamp(evidence["verified_at"])
        if observed > now_utc or verified > now_utc:
            raise ValueError("future_timestamp")
        if observed > verified:
            raise ValueError("observation_after_verification")
        if ((now_utc - observed).total_seconds() > FRESHNESS_SECONDS
                or (now_utc - verified).total_seconds() > FRESHNESS_SECONDS):
            raise ValueError("stale_evidence")

        windows = evidence["windows"]
        if not isinstance(windows, list) or not windows:
            raise ValueError("missing_windows")
        seen: set[str] = set()
        assessed = []
        checkpoint = False
        for window in windows:
            if not isinstance(window, dict) or not set(window).issubset(_WINDOW_FIELDS):
                raise ValueError("invalid_window_fields")
            if not {"id", "percentage", "basis"}.issubset(window):
                raise ValueError("incomplete_window")
            identifier = window["id"]
            if (not isinstance(identifier, str) or not identifier.strip()
                    or len(identifier) > 128 or identifier in seen):
                raise ValueError("invalid_window_id")
            seen.add(identifier)
            percentage = window["percentage"]
            if (isinstance(percentage, bool) or not isinstance(percentage, (int, float))
                    or not math.isfinite(percentage) or not 0 <= percentage <= 100):
                raise ValueError("invalid_percentage")
            basis = window["basis"]
            if basis not in {"used", "remaining", "unspecified"}:
                raise ValueError("invalid_percentage_basis")
            if basis == "remaining":
                lower_bound = float(percentage)
            elif basis == "used":
                lower_bound = 100.0 - float(percentage)
            else:
                lower_bound = min(float(percentage), 100.0 - float(percentage))
            duration = window.get("duration_minutes")
            if duration is not None and (
                    isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
                raise ValueError("invalid_window_duration")
            reset_at = window.get("reset_at")
            if reset_at is not None:
                _timestamp(reset_at)
            if lower_bound <= 10:
                checkpoint = True
            assessed.append({
                "id": identifier,
                "reported_percentage": percentage,
                "reported_basis": basis,
                "conservative_remaining_percent": lower_bound,
                "duration_minutes": duration,
                "reset_at": reset_at,
            })
        return {
            "decision": "checkpoint" if checkpoint else "allow",
            "dispatch_allowed": not checkpoint,
            "reason": "reserve_threshold" if checkpoint else "fresh_evidence_above_reserve",
            "profile": expected_profile,
            "connection_binding": binding,
            "observed_at": evidence["observed_at"],
            "verified_at": evidence["verified_at"],
            "windows": assessed,
            "freshness_seconds": FRESHNESS_SECONDS,
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        reason = str(exc) if str(exc) in {
            "invalid_evidence_fields", "unsupported_schema", "profile_mismatch",
            "binding_mismatch", "unsupported_source", "operator_attestation_required",
            "invalid_clock", "invalid_timestamp", "naive_timestamp", "future_timestamp",
            "observation_after_verification", "stale_evidence", "missing_windows",
            "invalid_window_fields", "incomplete_window", "invalid_window_id",
            "invalid_percentage", "invalid_percentage_basis", "invalid_window_duration",
        } else "invalid_evidence"
        return {"decision": "blocked", "dispatch_allowed": False, "reason": reason}


def _check_private_root(root: Path, raw_path: str | None = None) -> None:
    if not root.is_absolute():
        raise ValueError("checkpoint_root_must_be_absolute")
    if ".." in root.parts or (raw_path is not None and raw_path != str(root)):
        raise ValueError("checkpoint_root_must_be_canonical")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink_path_component")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("checkpoint_root_must_be_private_directory")
    if info.st_uid != os.getuid():
        raise ValueError("checkpoint_root_not_owned")


def write_checkpoint(root: str | os.PathLike[str], evidence: Any, decision: dict[str, Any],
                     *, expected_profile: str, expected_binding: str,
                     now: datetime, filename: str = "opencode-quota-handoff.json") -> Path:
    """Atomically write whitelisted operator-supplied metadata beneath a private root."""
    root_path = Path(root)
    raw_root = os.fspath(root)
    if not isinstance(raw_root, str):
        raise ValueError("checkpoint_root_must_be_canonical")
    _check_private_root(root_path, raw_root)
    if (Path(filename).name != filename or filename in {".", ".."}
            or not filename.endswith(".json")):
        raise ValueError("invalid_checkpoint_filename")
    result = validate_evidence(evidence, expected_profile=expected_profile,
                               expected_binding=expected_binding, now=now)
    if result["decision"] == "blocked" or decision != result:
        raise ValueError("checkpoint_requires_matching_validated_decision")
    target = root_path / filename
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or stat.S_IMODE(existing.st_mode) & 0o077):
        raise ValueError("unsafe_checkpoint_target")
    if existing is not None and existing.st_uid != os.getuid():
        raise ValueError("checkpoint_target_not_owned")
    payload = {
        "schema_version": 1,
        "decision": result["decision"],
        "reason": result["reason"],
        "profile": result["profile"],
        "connection_binding": result["connection_binding"],
        "observed_at": result["observed_at"],
        "verified_at": result["verified_at"],
        "windows": result["windows"],
        "next_steps": [
            "Stop new dispatch and preserve task receipts privately.",
            "Manually re-open the native usage page and verify the selected connection.",
            "Re-enter all applicable windows and verify them freshly before any resume decision.",
        ] if result["decision"] == "checkpoint" else [
            "This record is not an admission token; manually re-verify before dispatch."
        ],
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = root_path / ("." + filename + "." + secrets.token_hex(12) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        dirfd = os.open(root_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target
