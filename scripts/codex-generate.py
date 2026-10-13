#!/usr/bin/env python3
"""Generic native-Sol TEXT generation transport for the Eliza main model.

Contract (first safe slice):
  * stdin  : ONE bounded JSON request object (never a path, exec or model id).
  * stdout : JSONL events; provisional ``delta`` lines then exactly one terminal
             ``final`` or ``error`` line. Private auth/paths/raw errors never
             reach the caller.

This module is deliberately separate from the read-only source-review bridge
(``codex-bridge.py``). It REUSES the reviewed ``RpcClient``, account inspection
and ``normalize_limits`` helpers but never the review developer prompt, the
review thread registry, or native filesystem/shell tools.

Safety posture (isolation is enforced before a turn, not by rejecting events
after execution):
  * selected ``CODEX_HOME`` + an env allowlist (no API/OAuth credential copy);
  * exact installed Codex version, model and provider verified before a turn;
  * fresh off-peak window and fresh subscription quota each call (unknown quota
    or any exhausted window blocks);
  * a UNIQUE new stateless thread per generation (never resumed), with
    ``environments: []`` (no default environment access), ``dynamicTools: []``,
    ``selectedCapabilityRoots: []``, host-fixed instructions and an empty
    host-derived workspace;
  * ``allowProviderModelFallback: false`` — no provider/model fallback;
  * read-only sandbox, ``approval_policy=never``, no MCP connectors and the
    side-effecting builtin features disabled in the launch config.

Only text is supported. A tool/execution event, cancellation, timeout or
provider error interrupts the turn, terminates the process group and produces
NO success and NO fallback.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Reviewed bridge + window policy are dependencies; imported, never edited here.
_bridge_spec = importlib.util.spec_from_file_location(
    "codex_bridge", ROOT / "scripts/codex-bridge.py"
)
bridge = importlib.util.module_from_spec(_bridge_spec)
_bridge_spec.loader.exec_module(bridge)
_window_spec = importlib.util.spec_from_file_location(
    "coding_window", ROOT / "scripts/coding-window.py"
)
window_policy = importlib.util.module_from_spec(_window_spec)
_window_spec.loader.exec_module(window_policy)

SCHEMA = "phpretro.codex.generate.v1"
MODEL = bridge.MODEL  # exact model id, fixed host-side
PROVIDER = "openai"
CODEX_VERSION = "0.162.0"
MAX_REQUEST = 256 * 1024
MAX_FINAL_TEXT = 262144
ALLOWED_ROLES = ("system", "developer", "user", "assistant", "tool")
# Exact installed event method for a provisional assistant delta. Nothing is
# inferred from a substring: only this named method, matching the turn, is read.
DELTA_METHOD = "item/agentMessage/delta"
# Launch-config overrides that remove native side-effect authority BEFORE a turn
# (isolation is established at launch, not by rejecting events afterwards).
GENERATION_CONFIG_OVERRIDES = (
    ("model_provider", '"openai"'),
    ("forced_login_method", '"chatgpt"'),
    ("model", '"' + MODEL + '"'),
    ("sandbox_mode", '"read-only"'),
    ("approval_policy", '"never"'),
    ("mcp_servers", "{}"),
    ("features.shell_tool", "false"),
    ("features.multi_agent", "false"),
    ("features.hooks", "false"),
    ("tools.web_search", "false"),
    ("tools.view_image", "false"),
    ("tools.apply_patch", "false"),
    ("tools.unified_exec", "false"),
    ("project_doc_max_bytes", "0"),
)
# Feature keys whose absence on the installed CLI means isolation can NOT be
# established at the config layer. A missing/unverifiable key blocks dispatch.
REQUIRED_FEATURE_KEYS = ("shell_tool", "multi_agent", "hooks")
# Item types that are not text generation. Any other item type fails the turn,
# because this slice is text-only and must never let the model drive host
# execution/editing/search through a native tool.
BENIGN_ITEM_TYPES = ("agentMessage", "reasoning")
# Generation-only developer instructions. NOT the source-review prompt, and no
# tool/agent/hook/execution authority of any kind is granted here.
GENERATION_DEVELOPER_INSTRUCTIONS = (
    "You are a text generation backend for the PHPRetro Eliza runtime. "
    "Produce only the requested text or JSON. Do not use tools, do not run "
    "commands, do not edit or read files, do not browse, and do not launch "
    "agents. If a tool would be required, answer in text instead."
)


def generation_codex_command(binary: str = "codex"):
    """App-server launch config that disables side-effecting builtins.

    Every override here removes native side-effect authority BEFORE a turn:
    read-only sandbox, never-approve, no MCP connectors, and the shell/agent/
    hook/web/apply-patch/view-image/unified-exec tools off. A key the installed
    CLI does not recognize must not be silently ignored — ``verify_feature_keys``
    blocks dispatch when a required feature key is unverifiable.
    """
    binary = bridge.resolve_codex_binary(binary) if binary == "codex" else binary
    command = [binary, "app-server", "--listen", "stdio://"]
    for key, value in GENERATION_CONFIG_OVERRIDES:
        command.extend(["-c", key + "=" + value])
    return command


def _feature_probe_environment():
    """Return an allowlisted env with EMPTY HOME/CODEX_HOME (offline, no login)."""
    home = tempfile.mkdtemp(prefix="codex-empty-home-")
    env = bridge.child_environment()
    env["HOME"] = home
    env["CODEX_HOME"] = os.path.join(home, ".codex")
    return env


def probe_feature_keys(binary: str) -> str:
    """Offline `features list` metadata probe. Never authenticates or runs a model."""
    try:
        completed = subprocess.run(
            [binary, "features", "list"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            env=_feature_probe_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        raise bridge.BridgeError(
            "feature_policy", "Installed Codex feature list cannot be read; refusing to dispatch."
        ) from None
    text = (completed.stdout or b"").decode("utf-8", "replace") + (completed.stderr or b"").decode(
        "utf-8", "replace"
    )
    if completed.returncode != 0 or not text.strip():
        raise bridge.BridgeError(
            "feature_policy", "Installed Codex feature list is unavailable; refusing to dispatch."
        )
    return text


def verify_feature_keys(binary: str, probe=probe_feature_keys) -> bool:
    """Fail closed unless the installed CLI recognizes every required key.

    An unsupported key would be silently ignored, leaving a side-effecting
    builtin enabled, so this must block before a turn. ``probe`` is injectable
    for tests; the production probe is offline and never logs in or calls a model.
    """
    text = probe(binary)
    missing = [key for key in REQUIRED_FEATURE_KEYS if key not in text]
    if missing:
        raise bridge.BridgeError(
            "feature_policy",
            "Installed Codex does not recognize the required isolation features; refusing to dispatch.",
        )
    return True


def verify_version(binary: str, expected: str = CODEX_VERSION) -> str:
    """Offline metadata check: the installed executable must be the pinned one.

    Runs only ``--version`` (never a login/account/model turn). Raises when the
    version is missing, unparseable, or not the pinned release so an unknown CLI
    is never trusted to isolate the turn.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=bridge.child_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        raise bridge.BridgeError(
            "codex_unavailable", "Installed Codex version cannot be verified."
        ) from None
    text = (completed.stdout or b"").decode("utf-8", "replace").strip()
    if completed.returncode != 0 or not text:
        raise bridge.BridgeError(
            "codex_version", "Installed Codex version cannot be verified."
        )
    found = text.split()[-1].lstrip("v")
    if found != expected:
        raise bridge.BridgeError(
            "codex_version",
            "Installed Codex version is not the pinned release; refusing to dispatch.",
        )
    return found


def parse_request(raw: bytes) -> dict:
    """Validate and normalize ONE bounded JSON request from stdin."""
    if not raw or len(raw) > MAX_REQUEST:
        raise bridge.BridgeError("request_bound", "Generation request is empty or oversized.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise bridge.BridgeError("request_malformed", "Generation request is not valid UTF-8 JSON.") from None
    if not isinstance(data, dict):
        raise bridge.BridgeError("request_malformed", "Generation request must be a JSON object.")
    # Model, provider and every path are host-fixed; a request may not select them.
    for forbidden in ("model", "modelProvider", "cwd", "state_dir", "exec", "command", "HOME", "CODEX_HOME"):
        if forbidden in data:
            raise bridge.BridgeError("request_scope", "Request may not select model, provider or paths.")
    system = data.get("system")
    if system is not None and not isinstance(system, str):
        raise bridge.BridgeError("request_malformed", "system must be a string.")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise bridge.BridgeError("request_malformed", "messages must be a non-empty list.")
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise bridge.BridgeError("request_malformed", "Each message must be an object.")
        role = message.get("role")
        content = message.get("content")
        if role not in ALLOWED_ROLES or not isinstance(content, str) or not content:
            raise bridge.BridgeError("request_malformed", "Each message needs an allowed role and string content.")
        normalized.append({"role": role, "content": content})
    tools = data.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or any(
        not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"] for t in tools
    ):
        raise bridge.BridgeError("request_malformed", "tools must be a list of named definitions.")
    schema = data.get("responseSchema")
    if schema is not None and not isinstance(schema, dict):
        raise bridge.BridgeError("request_malformed", "responseSchema must be a JSON object.")
    stream = bool(data.get("stream", False))
    return {
        "system": system,
        "messages": normalized,
        "tools": tools,
        "tool_allowlist": [t["name"] for t in tools],
        "responseSchema": schema,
        "stream": stream,
    }


def serialize_input(system, messages):
    """Serialize the system prompt and ordered message roles explicitly.

    The native turn receives one text item per message with an explicit role
    label, so ordering and authorship can never be inferred or reordered by the
    app-server.
    """
    items = []
    if isinstance(system, str) and system:
        items.append({"type": "text", "text": "[system]\n" + system, "text_elements": []})
    for message in messages:
        items.append(
            {"type": "text", "text": "[" + message["role"] + "]\n" + message["content"], "text_elements": []}
        )
    return items


def _type_label(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def validate_final(text: str, response_schema, tool_calls, tool_allowlist):
    """Validate the final output against the host-requested schema/allowlist.

    Tool calls are only accepted for names the host requested; otherwise the
    turn fails. Structured output is parsed and structurally checked. Nothing
    is ever returned to native execution — the validated payload goes back to
    the Eliza pipeline.
    """
    names = [call.get("name") for call in tool_calls if isinstance(call, dict)]
    unknown = [name for name in names if name not in tool_allowlist]
    if unknown or (not tool_allowlist and tool_calls):
        raise bridge.BridgeError("unexpected_tool", "Model produced a tool call the host did not request.")
    if response_schema is not None:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            raise bridge.BridgeError("schema_invalid", "Structured response was not valid JSON.") from None
        expected = response_schema.get("type")
        if isinstance(expected, str):
            if _type_label(parsed) != expected:
                raise bridge.BridgeError("schema_invalid", "Structured response did not match the requested type.")
        if isinstance(expected, list):
            if _type_label(parsed) not in expected:
                raise bridge.BridgeError("schema_invalid", "Structured response did not match the requested type.")
    return text


def _delta_text(params):
    for key in ("delta", "text", "chunk"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return None


def run_turn(client, request, emit, workspace: Path):
    """Drive one native turn; yield deltas; return the validated final payload."""
    thread_params = {
        "model": MODEL,
        "modelProvider": PROVIDER,
        "cwd": str(workspace),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": "",
        "developerInstructions": GENERATION_DEVELOPER_INSTRUCTIONS,
        "environments": [],
        "dynamicTools": [],
        "selectedCapabilityRoots": [],
        "allowProviderModelFallback": False,
        "ephemeral": True,
    }
    thread = client.request("thread/start", thread_params)
    try:
        thread_id = thread["thread"]["id"]
    except (KeyError, TypeError):
        raise bridge.BridgeError("rpc_malformed", "Thread response is invalid.") from None
    if not isinstance(thread_id, str) or not thread_id:
        raise bridge.BridgeError("rpc_malformed", "Thread identity is invalid.")
    if thread.get("model") != MODEL or thread.get("modelProvider") != PROVIDER:
        raise bridge.BridgeError("model_mismatch", "App-server selected an unexpected model route.")

    start = client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "model": MODEL,
            "effort": "high",
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "input": serialize_input(request["system"], request["messages"]),
        },
    )
    try:
        turn_id = start["turn"]["id"]
    except (KeyError, TypeError):
        raise bridge.BridgeError("rpc_malformed", "Turn response is invalid.") from None
    if not isinstance(turn_id, str) or not turn_id:
        raise bridge.BridgeError("rpc_malformed", "Turn identity is invalid.")
    client.active = (thread_id, turn_id)

    messages = {}
    tool_calls = []
    usage = None
    pending = client.pending
    client.pending = []
    while True:
        row = pending.pop(0) if pending else client.next()
        if row is None:
            continue
        method = row.get("method")
        params = row.get("params", {})
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            continue
        if method == "turn/completed":
            turn = params.get("turn", {})
            client.active = None
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                continue
            status = turn.get("status")
            if status != "completed":
                code = "turn_" + str(status) if status in ("failed", "interrupted") else "turn_invalid"
                raise bridge.BridgeError(code, "Generation turn did not complete.")
            text = "\n".join(messages[key] for key in messages)
            if not messages or len(text) > MAX_FINAL_TEXT:
                raise bridge.BridgeError("missing_answer", "Completed turn contained no usable final message.")
            validate_final(text, request["responseSchema"], tool_calls, request["tool_allowlist"])
            return {
                "type": "final",
                "text": text,
                "usage": usage,
                "finishReason": "stop",
                "toolCalls": tool_calls,
                "model": MODEL,
            }
        if params.get("turnId") != turn_id:
            continue
        if method == DELTA_METHOD:
            # Deltas are provisional: emitted live but never trusted as the final
            # answer (settled only by the exact matching completed turn).
            text = _delta_text(params)
            if isinstance(text, str) and text:
                emit({"type": "delta", "text": text})
            continue
        if method in ("thread/tokenUsage/updated",):
            value = params.get("tokenUsage")
            if isinstance(value, dict):
                usage = {
                    part: {
                        key: counter
                        for key, counter in counters.items()
                        if key
                        in (
                            "inputTokens",
                            "cachedInputTokens",
                            "outputTokens",
                            "reasoningOutputTokens",
                            "totalTokens",
                        )
                        and isinstance(counter, int)
                        and not isinstance(counter, bool)
                        and counter >= 0
                    }
                    for part, counters in value.items()
                    if part in ("total", "last") and isinstance(counters, dict)
                }
            continue
        if method == "item/completed":
            item = params.get("item", {})
            if not isinstance(item, dict):
                raise bridge.BridgeError("unexpected_event", "Turn produced an invalid item.")
            item_type = item.get("type")
            if item_type not in BENIGN_ITEM_TYPES:
                # Text-only slice: a tool/exec/patch/search item must never run.
                raise bridge.BridgeError("unexpected_tool", "Turn produced a non-text item; aborting.")
            if item_type == "agentMessage":
                calls = item.get("toolCalls")
                if isinstance(calls, list) and calls:
                    tool_calls.extend(c for c in calls if isinstance(c, dict))
                if item.get("phase") in (None, "final_answer"):
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        messages[str(item.get("id"))] = text
            continue
        # Any other turn-scoped event on a text-only slice is unexpected.
        raise bridge.BridgeError("unexpected_event", "Turn produced an unrecognized event; aborting.")


def _empty_workspace(directory: Path) -> Path:
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    return workspace


def execute(args, command=None, emit=None):
    """Run one generation. ``command``/``emit`` are injectable for tests."""
    directory = bridge.private_directory(args.state_dir)
    _empty_workspace(directory)
    result = {"schema": SCHEMA, "ok": True, "provider": "codex-chatgpt-subscription", "model": MODEL}
    if emit is None:

        def emit(obj):
            sys.stdout.write(json.dumps(obj, allow_nan=False) + "\n")
            sys.stdout.flush()

    with bridge.state_lock(directory):
        try:
            raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
            request = parse_request(raw)
            # Fresh off-peak window and fresh subscription quota EVERY call.
            try:
                window_policy.require_allowed()
            except ValueError:
                raise bridge.BridgeError(
                    "off_peak_paused",
                    "Generation is paused outside the verified off-peak window.",
                ) from None
            if command is None:
                binary = bridge.resolve_codex_binary(args.codex_bin)
                verify_version(binary)
                verify_feature_keys(binary)
                command = generation_codex_command(binary)
            client = bridge.RpcClient(command, args.timeout)
        except bridge.BridgeError as error:
            emit({"type": "error", "code": error.code, "message": str(error)})
            return {"schema": SCHEMA, "ok": False, "error_code": error.code}
        success = False
        try:
            limits, available, _account = bridge.inspect_account(client)
            if not limits["dispatch_allowed"]:
                raise bridge.BridgeError(
                    limits["reason"], "New Sol generation is paused; inspect quota and checkpoint."
                )
            if not available:
                raise bridge.BridgeError("model_unavailable", "The pinned model is unavailable on this account.")
            final = run_turn(client, request, emit, _empty_workspace(directory))
            emit(final)
            result.update(final=final)
            success = True
        except bridge.BridgeError as error:
            emit({"type": "error", "code": error.code, "message": str(error)})
            result.update(ok=False, error_code=error.code)
        finally:
            # Interrupt on any non-success path; close() terminates the group.
            client.close(interrupt=not success)
        return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=ROOT / ".builder/codex-generate")
    p.add_argument("--codex-bin", default="codex", help="Trusted installed executable; no command string.")
    p.add_argument("--timeout", type=float, default=180.0)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
        print(json.dumps({"type": "error", "code": "arguments", "message": "Timeout must be 1-300s."}))
        return 2
    try:
        result = execute(args)
    except (bridge.BridgeError, OSError):
        print(json.dumps({"type": "error", "code": "local_failure", "message": "Generation setup failed."}))
        return 2
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
