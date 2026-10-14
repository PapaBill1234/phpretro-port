#!/usr/bin/env python3
"""Private, offline, metadata-only Eliza builder token/attempt usage reporting.

Reads trusted ``.builder/current-workers/<attempt>/`` bundles (task metadata plus a
raw native result stream) and writes an allowlisted summary to a private 0700 tree.
Never emits worker result text, errors, private paths, raw model-usage maps, session
or account identities, provider headers, keys or transcripts. Missing accounting is
recorded as unknown, never as zero. Costs stay unknown unless the operator supplies a
verified price table. No network, model or worker dispatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "eliza.builder.usage.v1"

MAX_FILES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
HIGH_CONTEXT_CACHE_TOKENS = 200_000

# Only these metadata keys are ever read; anything else is dropped. The task id
# field has no single name, so the trusted forms are aliased here.
META_KEYS = {
    "attempt_id": ("attempt_id", "attemptId", "id"),
    "task_id": ("task_id", "taskId", "task"),
    "provider": ("provider",),
    "model": ("model",),
}
# Defensive: a metadata key matching this is never copied even if allowlisted.
SECRET_NAME = re.compile(r"(?i)(key|token|secret|auth|passwd|password|bearer|credential|cookie|session|account)")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# Model ids may carry a routing suffix such as deepseek-flash[1m]; brackets and a
# few separators are allowed but the label stays bounded with no spaces/quotes.
MODEL_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@\[\]-]{0,63}$")
UNIT_FACTORS = {"per_million_tokens": 1_000_000, "per_thousand_tokens": 1_000}
BUDGET_SUBTYPES = {"error_max_turns", "max_turns", "error_budget", "budget_stop", "error_limit"}
COUNTER_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


class UsageError(Exception):
    """A bounded input or private-state contract violation."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def sanitize_label(value, pattern=LABEL):
    if isinstance(value, str) and pattern.match(value):
        return value
    return None


def as_count(value):
    """Non-negative integer token/turn count, else None (unknown; bools are unknown)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value == int(value):
        return int(value)
    return None


def as_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 3)


def safe_read_bytes(path: Path, limit: int) -> bytes:
    """Read an owned, regular, non-symlink file within ``limit`` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise UsageError("input_unreadable") from error
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise UsageError("input_not_regular")
        if info.st_uid != os.getuid():
            raise UsageError("input_not_owned")
        if info.st_size > limit:
            raise UsageError("input_too_large")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise UsageError("input_too_large")
    return data


def decode_values(data: bytes):
    """Extract complete top-level JSON values, skipping prose diagnostics safely.

    Native ``--output-format json`` can interleave CLI startup text, one complete
    JSON config frame (which may hold secrets), intervening diagnostic text and the
    final result value. Only values that begin at the start of a line and end before
    any trailing prose are accepted, so JSON embedded inside a diagnostic or inside a
    quoted string of another value is never mistaken for a top-level result.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    values = []
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        if text[index] not in '{["-0123456789tfn':
            index += 1
            continue
        line_start = text.rfind("\n", 0, index) + 1
        if text[line_start:index].strip():  # value must begin a line, not sit in prose
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        tail = end
        while tail < length and text[tail] in " \t\r":
            tail += 1
        if tail < length and text[tail] != "\n":  # trailing prose => not a top-level value
            index += 1
            continue
        values.append(value)
        index = end
    return values


def looks_bridge(obj) -> bool:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return False
    if isinstance(usage.get("cumulative"), dict) or isinstance(usage.get("last"), dict):
        return True
    return any(key in usage for key in COUNTER_KEYS)


def select_result(values):
    """Return (format, object, reason); the last top-level ``type=result`` wins."""
    result = None
    for value in values:  # top-level stream values only; nested objects never inspected
        if isinstance(value, dict) and value.get("type") == "result":
            result = value
    if result is not None:
        return "claude", result, None
    for value in reversed(values):
        if isinstance(value, dict) and looks_bridge(value):
            return "codex", value, None
    return "unsupported", None, "unsupported_format"


def extract_claude(obj) -> dict:
    record = {"result_subtype": sanitize_label(obj.get("subtype"))}
    milliseconds = obj.get("duration_ms")
    if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool) and as_seconds(milliseconds) is not None:
        record["seconds"] = as_seconds(milliseconds / 1000.0)
    else:
        record["seconds"] = as_seconds(obj.get("duration_seconds"))
    record["reported_turns"] = as_count(obj.get("num_turns"))
    usage = obj.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    record["input_tokens"] = as_count(usage.get("input_tokens"))
    record["cache_read_tokens"] = as_count(usage.get("cache_read_input_tokens"))
    record["cache_write_tokens"] = as_count(usage.get("cache_creation_input_tokens"))
    record["output_tokens"] = as_count(usage.get("output_tokens"))
    return record


def extract_codex(obj) -> dict:
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    # Use one cumulative/last snapshot; never sum both to avoid double counting.
    source = None
    for key in ("cumulative", "last"):
        if isinstance(usage.get(key), dict):
            source = usage[key]
            break
    source = source if isinstance(source, dict) else usage
    record = {"result_subtype": sanitize_label(obj.get("status"))}
    record["seconds"] = as_seconds(obj.get("elapsed_seconds"))
    record["reported_turns"] = as_count(obj.get("turns"))
    record["input_tokens"] = as_count(source.get("inputTokens"))
    record["cache_read_tokens"] = as_count(source.get("cachedInputTokens"))
    record["cache_write_tokens"] = None  # bridge result carries no cache-write counter
    record["output_tokens"] = as_count(source.get("outputTokens"))
    return record


def read_metadata(path: Path) -> dict:
    meta = {"attempt_id": None, "task_id": None, "provider": None, "model": None}
    try:
        raw = safe_read_bytes(path, 256 * 1024)
    except UsageError:
        return meta
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return meta
    if not isinstance(data, dict):
        return meta
    for field, names in META_KEYS.items():
        pattern = MODEL_LABEL if field == "model" else LABEL
        for name in names:
            if SECRET_NAME.search(name):  # never copy a secret-looking field
                continue
            if name in data:
                meta[field] = sanitize_label(data[name], pattern)
                break
    return meta


def load_price_table(path):
    try:
        raw = safe_read_bytes(path, 512 * 1024)
        data = json.loads(raw.decode("utf-8"))
    except (UsageError, ValueError, UnicodeDecodeError):
        raise UsageError("price_table_unreadable")
    entries = data.get("entries") if isinstance(data, dict) and isinstance(data.get("entries"), list) else (
        data if isinstance(data, list) else None)
    if not isinstance(entries, list):
        raise UsageError("price_table_malformed")
    table = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("provider"), entry.get("model"))
        if all(isinstance(part, str) and part for part in key):
            table[key] = entry
    return table


def attempt_cost(record, table):
    """Return (cost, currency) from a verified table, else (None, None)."""
    if not table:
        return None, None
    entry = table.get((record.get("provider"), record.get("model")))
    if not isinstance(entry, dict):
        return None, None
    currency, unit, prices = entry.get("currency"), entry.get("unit"), entry.get("prices")
    validity = entry.get("context_validity") or entry.get("validity")
    if not (isinstance(currency, str) and currency):
        return None, None
    if unit not in UNIT_FACTORS or not isinstance(prices, dict):
        return None, None
    if not (isinstance(validity, str) and validity):
        return None, None
    counters = ("input", "cache_read", "cache_write", "output")
    values = [record.get(field + "_tokens") for field in counters]
    if any(value is None for value in values):
        return None, currency
    rate = []
    for counter in counters:
        price = prices.get(counter)
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
            return None, currency
        rate.append(price)
    factor = UNIT_FACTORS[unit]
    cost = sum(value / factor * price for value, price in zip(values, rate))
    return round(cost, 6), currency


def extract_attempt(directory: Path, total_state, max_file_bytes, max_total_bytes):
    """Return (record, skip_reason). Reads only the two bounded trusted files."""
    remaining = max_total_bytes - total_state["bytes"]
    if remaining <= 0:
        return None, "max_total_bytes"
    meta = read_metadata(directory / "task.json")
    try:
        data = safe_read_bytes(directory / "result.private.json", min(max_file_bytes, remaining))
    except UsageError as error:
        return None, str(error)
    total_state["bytes"] += len(data)
    attempt_id = meta["attempt_id"] or directory.name
    try:
        values = decode_values(data)
        fmt, obj, reason = select_result(values)
    except UsageError as error:
        fmt, obj, reason = "unsupported", None, str(error)
    if fmt == "claude":
        record = extract_claude(obj)
    elif fmt == "codex":
        record = extract_codex(obj)
    else:
        record = {"result_subtype": None, "seconds": None, "reported_turns": None,
                  "input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
                  "output_tokens": None}
    provider = meta["provider"] or {"claude": "deepseek", "codex": "codex"}.get(fmt)
    record.update({
        "attempt": _sha(attempt_id),
        "task_id": meta["task_id"],
        "provider": provider,
        "model": meta["model"],
        "format": fmt,
        "source_evidence": hashlib.sha256(data).hexdigest()[:32],
    })
    if reason:
        record["format_reason"] = reason
    return record, None


def _blank_totals():
    return {field: 0 for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "seconds", "reported_turns")}


def build_report(workers_dir: Path, *, max_files=MAX_FILES, max_file_bytes=MAX_FILE_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES, price_table=None):
    total_state = {"bytes": 0}
    attempts, skipped, seen = [], [], set()
    duplicates = 0
    worker_root = Path(workers_dir)
    if not worker_root.is_dir():
        return {"schema": SCHEMA, "counts": {"attempts": 0, "deduplicated": 0, "skipped": 0},
                "providers": [], "attempts": [], "skipped": [{"reason": "workers_dir_missing"}],
                "recommendations": [], "costs": {"estimated": False, "currency": None},
                "savings_measured": False, "notes": ["Input tree absent; nothing read."]}
    directories = sorted((p for p in worker_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for index, directory in enumerate(directories):
        if index >= max_files:
            skipped.append({"reason": "max_files"})
            break
        if total_state["bytes"] >= max_total_bytes:
            skipped.append({"reason": "max_total_bytes"})
            break
        record, reason = extract_attempt(directory, total_state, max_file_bytes, max_total_bytes)
        if reason:
            skipped.append({"reason": reason})
            continue
        key = (record["attempt"], record["source_evidence"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        record["cost"], record["currency"] = attempt_cost(record, price_table)
        attempts.append(record)

    groups = {}
    for record in attempts:
        key = (record["provider"], record["model"])
        group = groups.setdefault(key, {"provider": key[0], "model": key[1], "attempts": 0,
                                        "totals": _blank_totals(), "unknown": _blank_totals(),
                                        "cost": 0.0, "cost_currency": None, "cost_known": True})
        group["attempts"] += 1
        for field in group["totals"]:
            value = record.get(field)
            if value is None:
                group["unknown"][field] += 1
            else:
                group["totals"][field] += value
        if record["cost"] is None:
            group["cost_known"] = False
        else:
            group["cost"] += record["cost"]
            group["cost_currency"] = group["cost_currency"] or record["currency"]
    providers = []
    for group in groups.values():
        group["totals"] = {k: (None if group["unknown"][k] else v) for k, v in group["totals"].items()}
        group["cost"] = round(group["cost"], 6) if group["cost_known"] else None
        group.pop("cost_known")
        providers.append(group)
    providers.sort(key=lambda g: (g["provider"] or "", g["model"] or ""))

    return {
        "schema": SCHEMA,
        "counts": {"attempts": len(attempts), "deduplicated": duplicates, "skipped": len(skipped)},
        "providers": providers,
        "attempts": attempts,
        "skipped": skipped,
        "recommendations": recommend(attempts),
        "costs": {"estimated": any(a["cost"] is not None for a in attempts),
                  "currency": next((a["currency"] for a in attempts if a["currency"]), None),
                  "note": "Unknown unless the operator supplied a verified price table with units, currency, provider, model and context validity."},
        "savings_measured": False,
        "notes": ["Deterministic rule-based Doctor evidence only; not a matched same-task/model Jev comparison.",
                  "Cached tokens are aggregated separately; no free-cache assumption and no token-derived session limit."],
    }


def recommend(attempts):
    findings = []
    stops = [a for a in attempts if a.get("result_subtype") in BUDGET_SUBTYPES]
    if len(stops) >= 2:
        findings.append({"code": "repeat_budget_or_turn_stops", "evidence": {"count": len(stops)}})
    high_context = [a for a in attempts if isinstance(a.get("cache_read_tokens"), int)
                    and a["cache_read_tokens"] >= HIGH_CONTEXT_CACHE_TOKENS]
    if high_context:
        findings.append({"code": "high_context_reuse", "evidence": {"count": len(high_context),
                                                                     "threshold_tokens": HIGH_CONTEXT_CACHE_TOKENS}})
    no_progress = [a for a in attempts if a.get("result_subtype") in BUDGET_SUBTYPES
                   and a.get("output_tokens") == 0]
    if no_progress:
        findings.append({"code": "no_progress_attempts", "evidence": {"count": len(no_progress)}})
    return findings


def ensure_private_dir(path: Path) -> Path:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UsageError("output_path_unsafe")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise UsageError("output_dir_not_private")
    return path


def write_private_file(path: Path, text: str):
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def render_markdown(report) -> str:
    lines = ["# Eliza builder usage (private)", "",
             "Metadata-only allowlisted aggregate. Missing accounting is unknown, not zero.", "",
             "## Counts", "",
             "- Attempts: {attempts}".format(**report["counts"]),
             "- Deduplicated copies excluded: {deduplicated}".format(**report["counts"]),
             "- Skipped inputs: {skipped}".format(**report["counts"]),
             "- Savings measured: " + ("yes" if report["savings_measured"] else "no (no matched comparison)"), "",
             "## Providers", "",
             "| provider | model | attempts | input | cache_read | cache_write | output | seconds | turns | cost |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for group in report["providers"]:
        t = group["totals"]
        lines.append("| {p} | {m} | {n} | {i} | {cr} | {cw} | {o} | {s} | {tu} | {c} |".format(
            p=group["provider"] or "unknown", m=group["model"] or "unknown", n=group["attempts"],
            i=t["input_tokens"], cr=t["cache_read_tokens"], cw=t["cache_write_tokens"],
            o=t["output_tokens"], s=t["seconds"], tu=t["reported_turns"],
            c=("unknown" if group["cost"] is None else "{0} {1}".format(group["cost"], group["cost_currency"] or ""))))
    lines += ["", "## Doctor recommendations (deterministic)", ""]
    if report["recommendations"]:
        for finding in report["recommendations"]:
            lines.append("- {code}: {evidence}".format(code=finding["code"], evidence=json.dumps(finding["evidence"], sort_keys=True)))
    else:
        lines.append("- None from observed evidence.")
    lines += ["", "## Notes", ""]
    for note in report["notes"]:
        lines.append("- " + note)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers-dir", type=Path, default=ROOT / ".builder/current-workers")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".builder/eliza-usage")
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    parser.add_argument("--print-summary", action="store_true", help="Print the sanitized allowlisted summary to stdout.")
    args = parser.parse_args(argv)
    try:
        price_table = load_price_table(args.price_table) if args.price_table else None
        report = build_report(args.workers_dir, max_files=args.max_files, max_file_bytes=args.max_file_bytes,
                              max_total_bytes=args.max_total_bytes, price_table=price_table)
        output_dir = ensure_private_dir(args.output_dir)
        write_private_file(output_dir / "summary.json", json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        write_private_file(output_dir / "summary.md", render_markdown(report))
    except UsageError as error:
        print(json.dumps({"status": "error", "reason": str(error)}), file=sys.stderr)
        return 2
    if args.print_summary:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(json.dumps({"status": "ok", "attempts": report["counts"]["attempts"],
                          "deduplicated": report["counts"]["deduplicated"], "skipped": report["counts"]["skipped"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
