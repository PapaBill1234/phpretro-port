#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Local controls for the standalone real Eliza builder; never starts inference on setup/start/status."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "builder/eliza"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=ROOT / ".builder/eliza")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("setup")
    sub.add_parser("start")
    sub.add_parser("status")
    task = sub.add_parser("task").add_subparsers(dest="action", required=True)
    task.add_parser("list")
    for verb in ("create", "import"):
        a = task.add_parser(verb); a.add_argument("--file", type=Path, required=True)
    for verb in ("status", "claim", "check", "cancel", "dispatch"):
        a = task.add_parser(verb); a.add_argument("id")
        if verb == "dispatch": a.add_argument("--revision")
    for command in ("fail", "report", "help"):
        a = sub.add_parser(command); a.add_argument("id")
        if command == "help":
            a.add_argument("--reason", choices=["architecture", "final-review", "retry"], default="architecture")
            a.add_argument("--prompt-file", type=Path)
    a = sub.add_parser("chat"); a.add_argument("message", choices=["/status", "/tasks", "/help"]); a.add_argument("--id"); a.add_argument("--prompt-file", type=Path)
    args = p.parse_args()
    state = args.state_dir.resolve()
    if not state.is_relative_to(ROOT / ".builder"):
        p.error("state directory must be private project .builder state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "setup":
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", str(state / "npm-cache")], cwd=PACKAGE, check=True)
        config = state / "config.json"
        if not config.exists():
            config.write_text(json.dumps({"workersEnabled": False, "port": 18791, "boardDb": ".builder/tasks.sqlite", "credentialFile": ".builder/direct-deepseek/settings.json"}, indent=2) + "\n")
            config.chmod(0o600)
        retained = json.loads(config.read_text())
        print(json.dumps({"installed": True, "workersEnabled": retained.get("workersEnabled") is True, "config": str(config)}))
        return
    if args.command == "start":
        env = os.environ.copy(); env["ELIZA_BUILDER_STATE_DIR"] = str(state)
        os.execvpe("node", ["node", str(PACKAGE / "src/main.mjs"), "serve"], env)
    token = (state / "control.token").read_text().strip()
    server = json.loads((state / "server.json").read_text())
    port = server["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535: raise ValueError("invalid local server port")
    route, payload = "/control", {"command": args.command}
    if args.command == "status": route, payload = "/status", None
    elif args.command == "task":
        if args.action == "list": route, payload = "/tasks", None
        elif args.action in ("create", "import"):
            payload = {"command": "task-" + args.action, "task": json.loads(args.file.read_text())}
        else:
            payload = {"command": "task-" + args.action, "id": args.id}
            if args.action == "dispatch": payload["revision"] = args.revision
    else:
        for key in ("id", "reason", "message"):
            value = getattr(args, key, None)
            if value is not None: payload[key] = value
        if getattr(args, "prompt_file", None): payload["promptFile"] = str(args.prompt_file.resolve())
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=json.dumps(payload).encode() if payload else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    # Ignore ambient proxy configuration for local control and avoid remote redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=310) as response: print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as error:
        print(error.read().decode()); raise SystemExit(1)


if __name__ == "__main__":
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError, urllib.error.URLError):
        raise SystemExit("Eliza control unavailable; run setup/start and verify private configuration")
