#!/usr/bin/env python3
"""Start a project-scoped Claude CLI through the verified A6API Messages route.

No shell evaluation, provider-key logging or unattended swarm. Permission bypass
is enabled explicitly at the operator's request. This launcher does not enforce the supervisor's spending ledger.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
RESERVED = {
    "--model", "--agent", "--effort", "--settings", "--setting-sources",
    "--permission-mode", "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions", "--tools", "--allowedTools",
    "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--mcp-config", "--strict-mcp-config", "--bare", "--safe-mode",
    "--restricted", "--system-prompt", "--system-prompt-file",
    "--append-system-prompt", "--append-system-prompt-file",
    "--worktree", "-w", "--plugin-dir", "--plugin-url",
}


class SetupError(Exception):
    pass


def private_key(path: Path, name: str) -> str:
    """Parse one dotenv key as data, never source/evaluate the file."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SetupError(f"Credential source must be an owned regular file: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
        raise SetupError(f"Credential source must be mode 0600 and <=64 KiB: {path}")
    found = None
    for line in path.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] != name:
            continue
        try:
            parts = shlex.split(match[2], comments=True, posix=True)
        except ValueError:
            raise SetupError(f"Credential source has an invalid assignment: {path}") from None
        if len(parts) != 1 or not parts[0].strip():
            raise SetupError(f"Credential source has an empty/invalid {name}: {path}")
        if found is not None:
            raise SetupError(f"Credential source defines {name} more than once: {path}")
        found = parts[0]
    if found is None:
        raise SetupError(f"Credential source does not define {name}: {path}")
    return found


def credential() -> tuple[str, str]:
    supplied = os.environ.get("A6API_API_KEY", "").strip()
    if supplied:
        return supplied, "A6API_API_KEY environment"
    explicit = os.environ.get("PHPRETRO_A6API_ENV")
    candidates = [Path(explicit).expanduser()] if explicit else [
        ROOT / "secrets" / "a6api.env",
        Path.home() / "phpretro-dashboard" / "secrets" / "a6api.env",
    ]
    for path in candidates:
        if path.exists() or path.is_symlink():
            return private_key(path, "A6API_API_KEY"), str(path)
    raise SetupError("Set A6API_API_KEY or PHPRETRO_A6API_ENV to an existing private credential file.")


def native_binary(version: str) -> Path:
    override = os.environ.get("PHPRETRO_CLAUDE_BIN")
    path = Path(override).expanduser() if override else Path.home() / ".local/share/claude/versions" / version
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SetupError(f"Raw Claude Code executable is unavailable: {path}")
    with path.open("rb") as handle:
        if handle.read(2) == b"#!":
            raise SetupError("PHPRETRO_CLAUDE_BIN must point to the raw executable, not a shell wrapper.")
    return path.resolve()


def configuration() -> dict:
    data = json.loads((ROOT / "config/models.json").read_text())
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    if data.get("anthropic_base_url") != "https://api.a6api.com":
        raise SetupError("Unexpected provider endpoint; verify a new route before changing this launcher.")
    env = settings.get("env", {})
    if env.get("ANTHROPIC_BASE_URL") != data["anthropic_base_url"]:
        raise SetupError("Project settings and the verified endpoint disagree.")
    if str(env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "0")) not in ("0", ""):
        raise SetupError("Remove CLAUDE_CODE_SUBAGENT_MODEL_FORCE; it overrides the reviewer model.")
    if any(key in env for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "A6API_API_KEY", "OPENROUTER_API_KEY")):
        raise SetupError("Remove provider credentials from checked-in project settings.")
    if settings.get("worktree", {}).get("baseRef") != "head":
        raise SetupError("Use worktree.baseRef=head so workers branch from the recorded current commit.")
    for alias, model in data["aliases"].items():
        if env.get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL") != model:
            raise SetupError(f"The {alias} alias differs between settings and config/models.json.")
    return data


def git_head(path: Path = ROOT) -> str:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
    if result.returncode:
        raise SetupError("Create the initial repository commit before starting an isolated worker.")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("lead", "backup", "worker", "reviewer"), default="lead")
    parser.add_argument("--worktree-name", help="Worker worktree name; a unique name is generated by default.")
    parser.add_argument("--check", action="store_true", help="Print redacted local setup metadata without inference.")
    options, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra = extra[1:]
    # Only actual option tokens are inspected; a prompt containing a flag is data.
    for token in extra:
        flag = token.split("=", 1)[0]
        if flag in RESERVED or token.startswith("--dangerously-"):
            raise SetupError(f"The launcher owns {flag}; configure the reviewed role instead.")
    if options.worktree_name and options.role != "worker":
        raise SetupError("--worktree-name is available only for a worker role.")
    if options.worktree_name and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", options.worktree_name):
        raise SetupError("Use a short alphanumeric worktree name with hyphens or underscores.")
    data = configuration()
    role = data["roles"][options.role]
    binary = native_binary(data["native_claude_version"])
    key, key_source = credential()
    config_home = ROOT / ".builder" / "claude"
    worktree_name = options.worktree_name or f"worker-{uuid.uuid4().hex[:8]}"
    summary = {
        "role": options.role, "model": role["model"], "effort": role["effort"],
        "endpoint": data["anthropic_base_url"], "binary": str(binary),
        "credential_source": key_source, "credential_available": True,
        "config_home": str(config_home), "permission_mode": "bypassPermissions",
        "worker_worktree": worktree_name if options.role == "worker" else None,
        "secrets_in_logs": False, "spending_guard_enforced_by_launcher": False,
    }
    if options.check:
        print(json.dumps(summary, indent=2))
        return 0
    if options.role == "worker":
        # Reviewed settings pin worktree.baseRef=head; record the task base.
        worktree_path = ROOT / ".claude" / "worktrees" / worktree_name
        summary["reused_worktree"] = worktree_path.exists()
        summary["starting_head"] = git_head(worktree_path if worktree_path.exists() else ROOT)
    config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
           and key not in ("A6API_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")}
    env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env.update({
        "ANTHROPIC_BASE_URL": data["anthropic_base_url"],
        "ANTHROPIC_AUTH_TOKEN": key,
        "CLAUDE_CONFIG_DIR": str(config_home),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    for alias, model in data["aliases"].items():
        env[f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"] = model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = "haiku"
    if os.environ.get("PHPRETRO_OPENROUTER_ENV"):
        env["PHPRETRO_OPENROUTER_ENV"] = os.environ["PHPRETRO_OPENROUTER_ENV"]
    mcp = ROOT / ".mcp.json"
    command = [str(binary), "--model", role["model"], "--agent", role["agent"],
               "--effort", role["effort"], "--permission-mode", "bypassPermissions",
               "--tools", role["tools"], "--setting-sources", "project",
               "--settings", str(ROOT / ".claude/settings.json"),
               "--strict-mcp-config", "--mcp-config",
               str(mcp) if mcp.exists() else '{"mcpServers":{}}', "--no-chrome"]
    if options.role == "worker":
        command += ["--worktree", worktree_name]
    if not any(token in ("--name", "-n") or token.startswith("--name=") for token in extra):
        command += ["--name", f"phpretro-{options.role}"]
    command += extra
    print("[phpretro-builder] " + json.dumps(summary), file=sys.stderr, flush=True)
    os.chdir(ROOT)
    os.execve(str(binary), command, env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, ValueError, KeyError) as error:
        # Exception strings never include dotenv contents or network response bodies.
        print(f"Builder setup error: {error}", file=sys.stderr)
        raise SystemExit(2)
