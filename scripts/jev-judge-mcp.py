#!/usr/bin/env python3
"""Newline JSON-RPC proxy exposing only jev_judge; never forwards jev_gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_MESSAGE = 1024 * 1024
MAX_PENDING = 128
REQUESTS = {"initialize", "ping", "tools/list", "tools/call"}
NOTIFICATIONS = {"notifications/initialized", "notifications/cancelled"}
OR_FIELDS = {"OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "JEV_MODEL"}


class SetupError(Exception):
    """Contains only fixed, public diagnostic text."""


def private_text(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SetupError("Jev configuration must be an owned regular file.")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
        raise SetupError("Jev private configuration must be mode 0600 and <=64 KiB.")
    return path.read_text()


def dotenv(path: Path) -> dict[str, str]:
    values = {}
    for line in private_text(path).splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] not in OR_FIELDS:
            continue
        try:
            parts = shlex.split(match[2], comments=True, posix=True)
        except ValueError:
            raise SetupError("Invalid Jev private dotenv assignment.") from None
        if len(parts) != 1 or not parts[0].strip() or match[1] in values:
            raise SetupError("Empty or duplicate Jev private dotenv assignment.")
        values[match[1]] = parts[0]
    return values


def upstream_config() -> tuple[list[str], dict[str, str], tuple[str, ...]]:
    config_path = Path(os.environ.get("PHPRETRO_JEV_CONFIG", ROOT / ".builder/jev.json")).expanduser()
    config = {}
    if config_path.exists() or config_path.is_symlink():
        config = json.loads(private_text(config_path))
        if not isinstance(config, dict) or set(config) - {"node", "cli", "env_file"}:
            raise SetupError("Invalid Jev private configuration fields.")
        if any(not isinstance(value, str) or not value for value in config.values()):
            raise SetupError("Invalid Jev private configuration values.")
    values = {name: os.environ[name] for name in OR_FIELDS if os.environ.get(name)}
    if not values.get("OPENROUTER_API_KEY"):
        env_path = Path(os.environ.get("PHPRETRO_OPENROUTER_ENV", config.get("env_file", str(Path.home() / "phpretro-dashboard/secrets/openrouter.env")))).expanduser()
        values = dotenv(env_path) | values
    if not values.get("OPENROUTER_API_KEY"):
        raise SetupError("Set OPENROUTER_API_KEY or a private PHPRETRO_OPENROUTER_ENV file.")
    if values.get("OPENROUTER_BASE_URL"):
        url = urlsplit(values["OPENROUTER_BASE_URL"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise SetupError("OpenRouter base URL must be HTTPS without embedded credentials/query.")
    node = os.environ.get("PHPRETRO_JEV_NODE", config.get("node", "")) or shutil.which("node")
    cli = Path(os.environ.get("PHPRETRO_JEV_CLI", config.get("cli", str(Path.home() / ".codex/jev/jev-use-0.8.0/dist/cli.js")))).expanduser()
    if not node or not Path(node).is_file() or not os.access(node, os.X_OK) or not cli.is_file():
        raise SetupError("Jev requires an executable Node runtime and installed jev-use CLI.")
    env = {name: value for name, value in os.environ.items() if name in {
        "PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    }}
    env.update(values)
    secrets = tuple({value for name, value in os.environ.items()
                     if len(value) >= 8 and (name.endswith("API_KEY") or name.endswith("AUTH_TOKEN"))}
                    | {values["OPENROUTER_API_KEY"]})
    return [str(Path(node).expanduser()), str(cli), "serve", "--backend", "openrouter"], env, secrets


def id_key(value):
    return (type(value), value)


class JudgeProxy:
    def __init__(self, command, env, source, sink, secrets=()):
        self.command, self.env, self.source, self.sink = command, env, source, sink
        self.secrets = secrets
        self.pending = {}
        self.client_ids = {}
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.sequence = 0
        self.process = None
        self.upstream_closed = threading.Event()

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        return value

    def send(self, message):
        with self.output_lock:
            self.sink.write(json.dumps(self.clean(message), separators=(",", ":")) + "\n")
            self.sink.flush()

    def error(self, request_id, code, message):
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def finish_pending(self):
        with self.lock:
            records = list(self.pending.values())
            self.pending.clear()
            self.client_ids.clear()
        for request_id, _ in records:
            self.error(request_id, -32603, "Jev upstream unavailable; handle locally.")

    def read_upstream(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    break
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    continue
                # Jev has no server-initiated requests; forward no untracked data.
                if "id" not in message or type(message["id"]) is not int:
                    continue
                with self.lock:
                    record = self.pending.pop(message["id"], None)
                    if record:
                        self.client_ids.pop(id_key(record[0]), None)
                if not record:
                    continue
                request_id, method = record
                if "error" in message:
                    self.error(request_id, -32603, "Jev upstream request failed; handle locally.")
                    continue
                result = message.get("result")
                if method == "tools/list":
                    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                        self.error(request_id, -32603, "Invalid Jev tool inventory.")
                        continue
                    result = dict(result, tools=[tool for tool in result["tools"]
                                               if isinstance(tool, dict) and tool.get("name") == "jev_judge"])
                elif method == "initialize":
                    if not isinstance(result, dict) or not isinstance(result.get("capabilities", {}), dict):
                        self.error(request_id, -32603, "Invalid Jev initialization.")
                        continue
                    result = {"protocolVersion": result.get("protocolVersion"),
                              "capabilities": {"tools": result.get("capabilities", {}).get("tools", {})},
                              "serverInfo": {"name": "phpretro-jev-judge", "version": "1.0.0"}}
                elif isinstance(result, dict) and result.get("isError"):
                    result = {"isError": True, "content": [{"type": "text", "text": "Jev judge unavailable; handle locally."}]}
                self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except (OSError, ValueError, TypeError, BrokenPipeError):
            pass
        finally:
            self.upstream_closed.set()
            self.finish_pending()

    def forward(self, message):
        method = message["method"]
        request = "id" in message
        if method == "tools/call" and message.get("params", {}).get("name") != "jev_judge":
            if request:
                self.error(message["id"], -32601, "Only jev_judge is exposed; gate calls are disabled.")
            return
        if (request and method not in REQUESTS) or (not request and method not in NOTIFICATIONS):
            if request:
                self.error(message["id"], -32601, "Method is not exposed by the judge-only proxy.")
            return
        encoded = json.dumps(message, separators=(",", ":"))
        if any(secret and secret in encoded for secret in self.secrets):
            if request:
                self.error(message["id"], -32602, "Known credentials cannot be submitted to Jev.")
            return
        failure = None
        with self.lock:
            forwarded = dict(message)
            if request:
                request_id = message["id"]
                if id_key(request_id) in self.client_ids or len(self.pending) >= MAX_PENDING:
                    failure = "Duplicate request ID or too many pending requests."
                else:
                    self.sequence += 1
                    forwarded["id"] = self.sequence
                    self.pending[self.sequence] = (request_id, method)
                    self.client_ids[id_key(request_id)] = self.sequence
            elif method == "notifications/cancelled":
                upstream_id = self.client_ids.get(id_key(message.get("params", {}).get("requestId")))
                if upstream_id is None:
                    return
                forwarded["params"] = dict(message["params"], requestId=upstream_id)
            if not failure:
                try:
                    if self.upstream_closed.is_set() or self.process.poll() is not None:
                        raise BrokenPipeError
                    self.process.stdin.write(json.dumps(forwarded, separators=(",", ":")) + "\n")
                    self.process.stdin.flush()
                except (OSError, BrokenPipeError):
                    if request:
                        self.pending.pop(forwarded["id"], None)
                        self.client_ids.pop(id_key(message["id"]), None)
                    failure = "Jev upstream unavailable; handle locally."
        if failure and request:
            self.error(message["id"], -32603, failure)

    def run(self):
        self.process = subprocess.Popen(self.command, env=self.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", bufsize=1)
        reader = threading.Thread(target=self.read_upstream, daemon=True)
        reader.start()
        try:
            while True:
                line = self.source.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    self.error(None, -32600, "MCP message exceeds the bounded transport limit.")
                    break
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                        raise ValueError
                    if "id" in message and type(message["id"]) not in (str, int):
                        raise ValueError
                    if "params" in message and not isinstance(message["params"], dict):
                        raise ValueError
                    self.forward(message)
                except (ValueError, TypeError):
                    self.error(None, -32600, "Invalid MCP request.")
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            reader.join(timeout=2)
            self.process.stdout.close()
            self.finish_pending()
        return 0


def main():
    command, env, secrets = upstream_config()
    return JudgeProxy(command, env, sys.stdin, sys.stdout, secrets).run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, ValueError, TypeError):
        print("Jev setup unavailable: check private paths, permissions and runtime dependencies.", file=sys.stderr)
        raise SystemExit(2)
