#!/usr/bin/env python3
"""Open interactive Claude handoff windows without dispatching a prompt."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SESSION = "phpretro-builder"
TARGET = SESSION
WINDOWS = (
    ("coordinator", "lead", None),
    ("luna-discovery", "worker", "luna-discovery"),
    ("luna-backend", "worker", "luna-backend"),
    ("luna-frontend", "worker", "luna-frontend"),
)


class SetupError(Exception):
    pass


def tmux(*args, check=True):
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise SetupError("tmux operation failed; inspect the named session in your terminal.")
    return result


def exists():
    return tmux("has-session", "-t", "=" + SESSION, check=False).returncode == 0


def owned():
    origin = tmux("show-options", "-qv", "-t", TARGET, "@phpretro_root").stdout.strip()
    version = tmux("show-options", "-qv", "-t", TARGET, "@phpretro_handoff").stdout.strip()
    if origin != str(ROOT) or version != "1":
        raise SetupError("Existing phpretro-builder session is unrelated; it was preserved.")


def names():
    return tmux("list-windows", "-t", TARGET, "-F", "#{window_name}").stdout.splitlines()


def clean_checkpoint():
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
    state = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True)
    if head.returncode or state.returncode or state.stdout.strip():
        raise SetupError("Commit the complete auth/handoff checkpoint before starting new worktree windows.")
    handoff = ROOT / ".builder/handoff"
    if not handoff.is_dir():
        raise SetupError("Prepare the private .builder/handoff directory before starting sessions.")


def command(role, worktree, name):
    argv = [sys.executable, str(ROOT / "scripts/claude-builder.py"), "--role", role]
    if worktree:
        argv += ["--worktree-name", worktree]
    argv += ["--", "--name", name, "--add-dir", str(ROOT / ".builder/handoff")]
    return "exec " + shlex.join(argv)


def configure_window(window, name):
    for key, value in (("automatic-rename", "off"), ("allow-rename", "off"), ("remain-on-exit", "on")):
        tmux("set-window-option", "-t", window, key, value)
    tmux("rename-window", "-t", window, name)


def start():
    present = exists()
    current = []
    if present:
        owned()
        current = names()
        if len(current) != len(set(current)):
            raise SetupError("Duplicate window names found; inspect them before continuing.")
        snapshot = status()
        if all(name in current for name, _, _ in WINDOWS) and all(item["state"] == "open" for item in snapshot["windows"]):
            return snapshot
    clean_checkpoint()
    for name, role, worktree in WINDOWS:
        shell = command(role, worktree, name)
        if name in current:
            target = SESSION + ":" + name
            dead = tmux("list-panes", "-t", target, "-F", "#{pane_dead}").stdout.splitlines()
            if dead == ["1"]:
                tmux("respawn-pane", "-t", target, "-c", str(ROOT), shell)
            continue
        if not present:
            window = tmux("new-session", "-d", "-s", SESSION, "-n", name, "-c", str(ROOT), "-P", "-F", "#{window_id}", shell).stdout.strip()
            tmux("set-option", "-t", TARGET, "@phpretro_root", str(ROOT))
            tmux("set-option", "-t", TARGET, "@phpretro_handoff", "1")
            present = True
        else:
            window = tmux("new-window", "-t", TARGET, "-n", name, "-c", str(ROOT), "-P", "-F", "#{window_id}", shell).stdout.strip()
        configure_window(window, name)
    return status()


def status():
    if not exists():
        return {"session": SESSION, "running": False}
    owned()
    panes = tmux("list-panes", "-a", "-F", "#{session_name}\t#{window_name}\t#{pane_dead}").stdout.splitlines()
    windows = []
    for pane in panes:
        session, name, dead = pane.split("\t", 2)
        if session == SESSION:
            windows.append({"window": name, "state": "exited" if dead == "1" else "open"})
    return {"session": SESSION, "running": True, "windows": windows,
            "helper_dispatches_initial_prompt": False, "private_history": str(ROOT / ".builder/claude")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "attach"))
    args = parser.parse_args()
    if not shutil.which("tmux"):
        raise SetupError("tmux is unavailable; install it before using this handoff helper.")
    if args.action == "attach":
        if not exists():
            raise SetupError("The session is absent; run start after the checkpoint is committed.")
        owned()
        import os
        operation = "switch-client" if os.environ.get("TMUX") else "attach-session"
        os.execvp("tmux", ["tmux", operation, "-t", TARGET])
    print(json.dumps(start() if args.action == "start" else status(), indent=2))


if __name__ == "__main__":
    try:
        main()
    except SetupError as error:
        raise SystemExit(str(error))
