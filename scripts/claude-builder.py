#!/usr/bin/env python3
"""Start a project-scoped Claude CLI through direct DeepSeek or native subscription Codex.

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
import urllib.request
import urllib.error
from decimal import Decimal, InvalidOperation

import importlib.util

_history_spec = importlib.util.spec_from_file_location("builder_history", Path(__file__).resolve().parent / "builder-history.py")
history_registry = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(history_registry)

ROOT = Path(__file__).resolve().parents[1]
RESERVED = {
    "--model", "--agent", "--effort", "--settings", "--setting-sources",
    "--permission-mode", "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions", "--tools", "--allowedTools",
    "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--mcp-config", "--strict-mcp-config", "--bare", "--safe-mode",
    "--restricted", "--system-prompt", "--system-prompt-file",
    "--append-system-prompt", "--append-system-prompt-file",
    "--worktree", "-w", "--plugin-dir", "--plugin-url", "--no-session-persistence",
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
    supplied = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if supplied:
        return supplied, "DEEPSEEK_API_KEY environment"
    explicit = os.environ.get("PHPRETRO_DEEPSEEK_ENV")
    candidates = [Path(explicit).expanduser()] if explicit else [
        ROOT / "secrets" / "deepseek.env",
        Path.home() / ".hermes" / ".env",
    ]
    for path in candidates:
        if path.exists() or path.is_symlink():
            return private_key(path, "DEEPSEEK_API_KEY"), str(path)
    raise SetupError("Set DEEPSEEK_API_KEY or PHPRETRO_DEEPSEEK_ENV to an existing private credential file.")


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
    if data.get("anthropic_base_url") != "https://api.deepseek.com/anthropic":
        raise SetupError("Unexpected provider endpoint; verify a new route before changing this launcher.")
    env = settings.get("env", {})
    if env.get("ANTHROPIC_BASE_URL") != data["anthropic_base_url"]:
        raise SetupError("Project settings and the verified endpoint disagree.")
    if str(env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "0")) not in ("0", ""):
        raise SetupError("Remove CLAUDE_CODE_SUBAGENT_MODEL_FORCE; it overrides the reviewer model.")
    if any(key in env for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "A6API_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")):
        raise SetupError("Remove provider credentials from checked-in project settings.")
    if settings.get("worktree", {}).get("baseRef") != "head":
        raise SetupError("Use worktree.baseRef=head so workers branch from the recorded current commit.")
    for alias, model in data["aliases"].items():
        if env.get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL") != model:
            raise SetupError(f"The {alias} alias differs between settings and config/models.json.")
    if any(model not in ("deepseek-flash", "deepseek-flash[1m]") for model in data["aliases"].values()):
        raise SetupError("Claude aliases must be direct DeepSeek Flash; Sol uses Codex.")
    return data


def git_head(path: Path = ROOT) -> str:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
    if result.returncode:
        raise SetupError("Create the initial repository commit before starting an isolated worker.")
    return result.stdout.strip()


def worker_path(name: str) -> Path:
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if result.returncode:
        raise SetupError("Active checkout must be a Git repository.")
    return Path(result.stdout.strip()).parent / ".claude" / "worktrees" / name


def shared_slots_directory():
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    return str(Path(result.stdout.strip()) / "phpretro-builder/deepseek-slots") if result.returncode == 0 else None


def funding_ready(key):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request("https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SetupError("DeepSeek balance response exceeded its bound.")
            data = json.loads(raw)
        balances = data.get("balance_infos")
        amounts = [Decimal(item["total_balance"]) for item in balances if isinstance(item, dict) and item.get("currency") in ("USD", "CNY")]
        if data.get("is_available") is not True or not amounts or any(not amount.is_finite() for amount in amounts) or not any(amount > 0 for amount in amounts):
            raise SetupError("DeepSeek dispatch paused: available funding is not positive; checkpoint until the owner funds this provider.")
    except SetupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        raise SetupError("DeepSeek dispatch paused: provider balance is unavailable.") from None


def codex_readiness():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/codex-bridge.py"), "limits", "--timeout", "30"], capture_output=True, text=True, timeout=35)
    try:
        report = json.loads(result.stdout)
        ready = result.returncode == 0 and report.get("ok") is True and report["limits"]["dispatch_allowed"] is True
    except (ValueError, KeyError, TypeError):
        ready = False
    if not ready:
        raise SetupError("Codex dispatch paused: subscription/model/quota readiness must be known with more than10%remaining; inspect codex-bridge limits and checkpoint.")


def codex_role(role, options, extra):
    if extra:
        raise SetupError("Interactive Codex roles accept no forwarded Claude arguments; use codex-bridge help --prompt-file for bounded requests.")
    summary = {"role": options.role, "model": "gpt-6.1-sol", "provider": "codex-chatgpt-subscription", "history": "native selected CODEX_HOME, separate from Claude", "offline": True, "readiness_checked": False, "secrets_in_logs": False}
    if options.check:
        print(json.dumps(summary, indent=2)); return 0
    codex_readiness()
    env = {k:v for k,v in os.environ.items() if k in ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL", "CODEX_HOME")}
    command = ["codex", "--model", "gpt-6.1-sol", "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"', "--sandbox", "read-only" if options.role == "reviewer" else "workspace-write", "--ask-for-approval", "on-request", "--cd", str(ROOT)]
    os.execvpe("codex", command, env); return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("lead", "backup", "worker", "reviewer"), default="lead")
    parser.add_argument("--worktree-name", help="Worker worktree name; a unique name is generated by default.")
    parser.add_argument("--prepare", action="store_true", help="Create private provider settings offline, without paid requests.")
    parser.add_argument("--check", "--status", action="store_true", help="Print redacted local setup metadata without inference.")
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
    if options.prepare and options.role != "worker":
        raise SetupError("Offline --prepare is available for direct DeepSeek worker settings only.")
    if role.get("provider") == "codex-chatgpt-subscription":
        return codex_role(role, options, extra)
    if role.get("model") not in ("deepseek-flash", "deepseek-flash[1m]"):
        raise SetupError("Sol models cannot use the DeepSeek endpoint.")
    binary = native_binary(data["native_claude_version"])
    key, key_source = credential()
    config_home = ROOT / ".builder" / "claude"
    worktree_name = options.worktree_name or f"worker-{uuid.uuid4().hex[:8]}"
    summary = {
        "role": options.role, "model": role["model"], "effort": role["effort"],
        "endpoint": data["anthropic_base_url"], "binary": str(binary),
        "credential_source": key_source, "credential_available": True,
        "config_home": str(config_home), "permission_mode": "bypassPermissions",
        "session_persistence": True, "context_scope": "current PHPRetro project and its named worker sessions",
        "worker_worktree": worktree_name if options.role == "worker" else None,
        "secrets_in_logs": False, "spending_guard_enforced_by_launcher": False,
    }
    summary["worker_slots_directory"] = shared_slots_directory()
    summary["slots_wrapper_available"] = (ROOT / "scripts/deepseek-slots.py").is_file()
    if options.prepare:
        directory = ROOT / ".builder/provider-settings"
        # Reuse audited no-symlink private path construction; settings contain no key.
        spec = importlib.util.spec_from_file_location("codex_bridge", ROOT / "scripts/codex-bridge.py")
        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
        helper.private_directory(directory)
        prepared = json.loads((ROOT / ".claude/settings.json").read_text())
        prepared["disableAllHooks"] = True
        helper.save_json(directory / "settings.json", prepared)
        summary.update(offline=True, prepared=True, settings_path=str(directory / "settings.json"),
                       main_model=role["model"], subagent_model="deepseek-flash", funding_preflight_required=True)
        print(json.dumps(summary, indent=2)); return 0
    if options.check:
        print(json.dumps(summary, indent=2))
        return 0
    funding_ready(key)
    if options.role == "worker":
        # Reviewed settings pin worktree.baseRef=head; record the task base.
        worktree_path = worker_path(worktree_name)
        summary["reused_worktree"] = worktree_path.exists()
        summary["starting_head"] = git_head(worktree_path if worktree_path.exists() else ROOT)
    if options.role == "worker":
        history_registry.register_worktree(ROOT, worktree_name)
    config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {k:v for k,v in os.environ.items() if k in ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL")}
    env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env.update({
        "ANTHROPIC_BASE_URL": data["anthropic_base_url"],
        "ANTHROPIC_AUTH_TOKEN": key,
        "CLAUDE_CONFIG_DIR": str(config_home),
    })
    for alias, model in data["aliases"].items():
        env[f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"] = model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = "deepseek-flash"
    if os.environ.get("PHPRETRO_OPENROUTER_ENV"):
        env["PHPRETRO_OPENROUTER_ENV"] = os.environ["PHPRETRO_OPENROUTER_ENV"]
    mcp = ROOT / ".mcp.json"
    command = [str(binary), "--model", role["model"], "--agent", role["agent"],
               "--effort", role["effort"], "--permission-mode", "bypassPermissions",
               "--tools", role["tools"], "--setting-sources", "project",
               "--settings", str(ROOT / ".claude/settings.json"),
               "--strict-mcp-config", "--mcp-config",
               str(mcp) if mcp.exists() else '{"mcpServers":{}}',
               "--append-system-prompt-file", str(ROOT / ".claude/project-scope.md")]
    if options.role == "worker":
        command += ["--worktree", worktree_name]
    if not any(token in ("--name", "-n") or token.startswith("--name=") for token in extra):
        command += ["--name", worktree_name if options.role == "worker" else f"phpretro-{options.role}"]
    command += extra
    print("[phpretro-builder] " + json.dumps(summary), file=sys.stderr, flush=True)
    os.chdir(ROOT)
    slots = ROOT / "scripts/deepseek-slots.py"
    if not slots.is_file():
        raise SetupError("Worker dispatch paused: the shared four-worker slots wrapper is not integrated.")
    os.execve(sys.executable, [sys.executable, str(slots), "run", "--", *command], env)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, ValueError, KeyError) as error:
        # Exception strings never include dotenv contents or network response bodies.
        print(f"Builder setup error: {error}", file=sys.stderr)
        raise SystemExit(2)
