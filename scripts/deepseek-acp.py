#!/usr/bin/env python3
"""Private direct DeepSeek ACP environment; only explicit dispatch checks funding."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location("claude_builder", ROOT / "scripts/claude-builder.py")
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    models = json.loads((ROOT / "config/models.json").read_text())
    settings = ROOT / ".builder/provider-settings/settings.json"
    if not settings.is_file() or settings.is_symlink() or settings.stat().st_uid != os.getuid() or settings.stat().st_mode & 0o077:
        raise ValueError("Offline private provider settings must be prepared first")
    binary = provider.native_binary(models["native_claude_version"])
    key, _ = provider.credential()
    provider.funding_ready(key)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL")}
    env.update({
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": "deepseek-flash", "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash", "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash",
        "CLAUDE_CONFIG_DIR": str(ROOT / ".builder/claude"), "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_UPDATE": "1",
        "CLAUDE_MODEL_CONFIG": json.dumps({"availableModels": ["deepseek-flash"], "modelOverrides": {"default": "deepseek-flash", "opus": "deepseek-flash", "sonnet": "deepseek-flash", "haiku": "deepseek-flash"}}),
        "CLAUDE_CODE_EXECUTABLE": str(ROOT / "builder/eliza/src/native-claude.mjs"),
        "ELIZA_NATIVE_CLAUDE": str(binary), "ELIZA_DEEPSEEK_SETTINGS": str(settings),
    })
    adapter = subprocess.check_output(["node", "--input-type=module", "-e",
        "import{createRequire}from'node:module';process.stdout.write(createRequire(import.meta.url).resolve('@agentclientprotocol/claude-agent-acp/dist/index.js'))"],
        cwd=ROOT / "builder/eliza", env=env, text=True, timeout=15).strip()
    os.execve(sys.executable, [sys.executable, str(ROOT / "scripts/deepseek-slots.py"), "run", "--", "node", adapter], env)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Direct DeepSeek ACP unavailable; prepare private settings, funding and runtime first.", file=sys.stderr)
        raise SystemExit(78)
