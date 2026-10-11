#!/usr/bin/env python3
"""Check candidate public Git files, local document links, and planning/config integrity."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
errors = []
files = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode().split("\0")
for name in sorted(set(x for x in files if x)):
    path = ROOT / name
    if not path.is_file():
        continue
    if path.name.startswith(".env") and path.name != ".env.example":
        errors.append(name + ": environment file cannot be published")
    if path.suffix.lower() in {".swf", ".dcr", ".pem", ".p12", ".pfx", ".log", ".jsonl", ".sqlite", ".db"}:
        errors.append(name + ": private/upstream binary file cannot be published")
    try:
        value = path.read_text()
    except UnicodeDecodeError:
        errors.append(name + ": binary requires an explicit provenance review")
        continue
    if SECRET.search(value):
        errors.append(name + ": possible credential detected (value redacted)")
    if path.suffix == ".json":
        try:
            json.loads(value)
        except json.JSONDecodeError:
            errors.append(name + ": invalid JSON")
    if path.suffix == ".md":
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', value):
            if re.match(r'^(?:https?://|mailto:|#)', target):
                continue
            target = target.split("#", 1)[0].strip("<>")
            if target and not (path.parent / target).exists():
                errors.append(name + ": missing local link " + target)

settings = ROOT / ".claude/settings.json"
if settings.exists():
    config = json.loads(settings.read_text())
    if config.get("env", {}).get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        errors.append("forced subagent model would override reviewer")
    if config.get("hooks"):
        errors.append("initial builder configuration must have no hooks")

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print("Public-file, document-link and configuration checks passed.")
