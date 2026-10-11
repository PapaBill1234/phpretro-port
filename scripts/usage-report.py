#!/usr/bin/env python3
"""Aggregate normalized metadata-only JSONL; preserve missing accounting as unknown."""
import argparse
import json
from pathlib import Path

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "elapsed_seconds", "estimated_usd", "jev_calls", "jev_estimated_usd")


def summarize(rows):
    groups = {}
    for row in rows:
        key = (row.get("role", "unknown"), row.get("model", "unknown"))
        group = groups.setdefault(key, {"role": key[0], "model": key[1], "runs": 0, "accepted": 0, "totals": {k: 0 for k in FIELDS}, "unknown_rows": {k: 0 for k in FIELDS}})
        group["runs"] += 1
        group["accepted"] += int(row.get("accepted") is True)
        for field in FIELDS:
            value = row.get(field)
            if value is None:
                group["unknown_rows"][field] += 1
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                group["totals"][field] += value
            else:
                raise ValueError("usage must be a nonnegative number or null: " + field)
    for group in groups.values():
        group["totals"] = {k: v if not group["unknown_rows"][k] else None for k, v in group["totals"].items()}
    return list(groups.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    a = p.parse_args()
    try:
        rows = [json.loads(line) for line in a.input.read_text().splitlines() if line.strip()]
        print(json.dumps({"groups": summarize(rows), "savings_measured": False,
                          "note": "This aggregate is not a matched same-task/model Jev comparison. Provider-normalized token definitions and pricing provenance are required."}, indent=2))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
