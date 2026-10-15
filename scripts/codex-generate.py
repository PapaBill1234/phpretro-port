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
  * fresh subscription quota each call (unknown quota or any exhausted window
    blocks), with one caller-bounded operation deadline;
  * a UNIQUE new stateless thread per generation (never resumed), with
    host-fixed instructions and an empty host-derived workspace; when the
    installed EXPERIMENTAL schema is offline-confirmed to define them, it also
    pins ``environments: []`` (no default environment access), ``dynamicTools:
    []``, ``selectedCapabilityRoots: []`` and ``allowProviderModelFallback:
    false`` (no provider/model fallback), and pins ``environments: []`` on the
    turn;
  * read-only sandbox, ``approval_policy=never``, no MCP connectors and the
    side-effecting builtin features disabled in the launch config.

Only text is supported. A tool/execution event, cancellation, timeout or
provider error interrupts the turn, terminates the process group and produces
NO success and NO fallback.
"""
from __future__ import annotations

import argparse
import contextvars
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
# Side-effecting features that must be effectively DISABLED before a turn. Every
# name is a recognized feature in the installed CLI's `features list` (verified
# offline). We disable each with `-c features.<name>=false` (the documented
# equivalent of `features disable`); an unrecognized name would be a silent
# no-op, so `verify_tool_policy` refuses when any of these is not recognized.
SIDE_EFFECTING_FEATURES = (
    "shell_tool",
    "shell_snapshot",
    "unified_exec",
    "unified_exec_tty",
    "apply_patch_freeform",
    "view_image",
    "image_generation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "browser_annotation_api",
    "in_app_browser",
    "computer_use",
    "in_app_local_automation",
    "apps",
    "enable_mcp_apps",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "plugin_hooks",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
    "tool_call_mcp_elicitation",
    "standalone_web_search",
)
# Additional relevant capabilities the installed audit identified outside a
# keyword-based guard: they must ALSO be provably inactive before a turn.
EXTRA_RELEVANT_FEATURES = (
    "code_mode_host",
    "skill_search",
    "sleep_tool",
    "goals",
    "worktrees",
    "workspace_dependencies",
)
# Every capability that must be provably inactive before a turn.
RELEVANT_FEATURES = SIDE_EFFECTING_FEATURES + EXTRA_RELEVANT_FEATURES
# Candidate overrides probed against the real installed metadata (never assumed).
FEATURE_OVERRIDES = tuple(("features." + name, "false") for name in RELEVANT_FEATURES)
# ThreadStartParams isolation fields the design relies on. They are only present
# in the installed EXPERIMENTAL protocol schema; they are sent only when the
# actual installed schema is offline-confirmed to define them (see
# ``thread_params_supported``).
REQUIRED_THREAD_FIELDS = (
    "environments",
    "dynamicTools",
    "selectedCapabilityRoots",
    "allowProviderModelFallback",
)
# Offline experimental-schema detection bounds. The generator runs the RAW ELF
# with an EMPTY HOME/CODEX_HOME and asks it to emit the protocol schema; these
# bound the subprocess runtime, the single schema file it reads and its path.
SCHEMA_TIMEOUT = 60.0
SCHEMA_FILE_MAX_BYTES = 8 * 1024 * 1024
SCHEMA_OUTPUT_SUBDIR = "schema"
SCHEMA_THREAD_FILE = ("v2", "ThreadStartParams.json")
THREAD_SCHEMA_TITLE = "ThreadStartParams"
# One monotonic work deadline for the WHOLE call (metadata probes, app-server and
# the turn). Set once in ``execute`` from the caller timeout; every bounded
# subprocess and RPC operation is capped by what remains. The default (None)
# preserves the plain helper signatures: a direct call is uncapped as before.
_DEADLINE = contextvars.ContextVar("codex_generate_work_deadline", default=None)


def _remaining_budget():
    """Seconds left before the captured work deadline, or ``None`` when unset."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _budget_timeout(cap):
    """Cap a subprocess timeout by the remaining work budget; never extend it."""
    remaining = _remaining_budget()
    if remaining is None:
        return cap
    if remaining <= 0:
        raise bridge.BridgeError("timeout", "Work deadline reached before offline metadata.")
    return min(cap, remaining)


def _check_budget(stage):
    """Refuse to start ``stage`` once the captured work deadline has passed."""
    remaining = _remaining_budget()
    if remaining is not None and remaining <= 0:
        raise bridge.BridgeError("timeout", "Work deadline reached before " + stage + ".")


def capture_work_deadline(timeout, now_monotonic=None):
    """Capture one immutable monotonic deadline from the caller's timeout."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise bridge.BridgeError("arguments", "Caller timeout must be positive and finite.")
    start = time.monotonic() if now_monotonic is None else now_monotonic
    return start + timeout
# Exact declared shape each isolation field must have in the installed
# experimental schema. A field is trusted only when its declared ``type`` (and,
# for the array fields, its item ``$ref``) matches EXACTLY; anything else is an
# unknown schema and the turn stays blocked (fail closed).
THREAD_FIELD_SHAPES = {
    "environments": {"types": frozenset({"array", "null"}), "items": "TurnEnvironmentParams"},
    "dynamicTools": {"types": frozenset({"array", "null"}), "items": "DynamicToolSpec"},
    "selectedCapabilityRoots": {"types": frozenset({"array", "null"}), "items": "SelectedCapabilityRoot"},
    "allowProviderModelFallback": {"types": frozenset({"boolean"}), "items": None},
}
# Recognized, currently-true features that merely CONTAIN a side-effect keyword
# but are not model-invokable tools; excluding them avoids false blocks.
BENIGN_TRUE_FEATURES = frozenset({"skill_mcp_dependency_install", "compaction_image_budget"})
# Keyword patterns used to detect a NEW/unknown side-effecting feature that is
# active but not in the disabled set (an "unknown critical" flag => refuse).
_SIDE_EFFECT_PATTERNS = (
    "shell",
    "exec",
    "apply_patch",
    "view_image",
    "image_generation",
    "browser",
    "web_search",
    "computer_use",
    "mcp",
    "multi_agent",
    "hooks",
    "plugin",
    "apps",
)


def generation_config_overrides():
    """Launch-config overrides that remove native side-effect authority BEFORE a turn.

    These are recognized installed config keys only: the base sandbox/approval/
    MCP/project-doc controls, the top-level ``web_search`` mode, and a
    ``features.<name>=false`` override for every side-effecting feature.
    """
    overrides = [
        ("model_provider", '"openai"'),
        ("forced_login_method", '"chatgpt"'),
        ("model", '"' + MODEL + '"'),
        ("sandbox_mode", '"read-only"'),
        ("approval_policy", '"never"'),
        ("mcp_servers", "{}"),
        ("project_doc_max_bytes", "0"),
        ("web_search", '"disabled"'),
    ]
    for name in RELEVANT_FEATURES:
        overrides.append(("features." + name, "false"))
    return tuple(overrides)


GENERATION_CONFIG_OVERRIDES = generation_config_overrides()
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
    read-only sandbox, never-approve, no MCP connectors, ``web_search=disabled``,
    and a ``features.<name>=false`` override for every side-effecting feature.
    A key the installed CLI does not recognize must not be silently ignored —
    ``verify_tool_policy`` blocks dispatch when a required feature is unverifiable.
    """
    binary = bridge.resolve_codex_binary(binary) if binary == "codex" else binary
    command = [binary, "app-server", "--listen", "stdio://"]
    for key, value in GENERATION_CONFIG_OVERRIDES:
        command.extend(["-c", key + "=" + value])
    return command


GUARDIAN = str(Path(__file__).resolve().parent / "native-group-guardian.py")


def guardian_command(native_argv, deadline, parent_pid=None):
    """Wrap the host-fixed native argv in the Linux process-group guardian.

    ``parent_pid`` defaults to this process and ``deadline`` MUST be the ORIGINAL
    captured absolute monotonic work deadline; a caller request JSON can never
    supply either. ``RpcClient`` starts the guardian detached
    (``start_new_session=True``) so the guardian is the session/group leader and
    owns the native process group, letting it reclaim that group after a hard
    parent death. The guardian never writes to stdout (that channel is the native
    RPC stream) and its exit is never a successful JSON-RPC completion; the
    native argv is preserved verbatim after ``--``.
    """
    pid = os.getpid() if parent_pid is None else parent_pid
    return [
        sys.executable,
        GUARDIAN,
        "--parent-pid",
        str(pid),
        "--deadline",
        repr(float(deadline)),
        "--",
        *native_argv,
    ]


def _require_native_elf(binary: str) -> None:
    """Refuse any executable that is not the native ELF.

    The PATH ``codex`` entry point is a shell wrapper that rewrites
    ``CODEX_HOME``; running it would silently touch an ordinary profile. Only the
    raw installed ELF is trusted for offline qualification.
    """
    try:
        with open(binary, "rb") as executable:
            is_elf = executable.read(4) == b"\x7fELF"
    except OSError:
        is_elf = False
    if not is_elf:
        raise bridge.BridgeError(
            "unsupported_executable",
            "Offline qualification requires the native ELF; profile-changing wrappers are refused.",
        )


def probe_feature_list(binary: str, overrides=()):
    """Offline `features list` metadata probe (EMPTY HOME/CODEX_HOME, no login).

    Runs the RAW installed binary (never the PATH wrapper, which rewrites
    ``CODEX_HOME``) and parses STDOUT only with the installed CLI's exact line
    format ``<name>  <stage>  <true|false>``. stderr warnings are ignored so a
    key name mentioned only in a warning can never satisfy a check. ``overrides``
    are ``(key, value)`` pairs applied as ``-c key=value`` BEFORE the subcommand,
    so the returned states are the ACTUAL effective states, not assumed ones.
    The temp dir is always cleaned up.
    """
    _require_native_elf(binary)
    home = tempfile.mkdtemp(prefix="codex-empty-home-")
    try:
        codex_home = os.path.join(home, ".codex")
        os.makedirs(codex_home, mode=0o700, exist_ok=True)
        env = bridge.child_environment()
        env["HOME"] = home
        env["CODEX_HOME"] = codex_home
        command = [binary]
        for key, value in overrides:
            command += ["-c", key + "=" + value]
        command += ["features", "list"]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_budget_timeout(20),
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            raise bridge.BridgeError(
                "feature_policy", "Installed Codex feature list cannot be read; refusing to dispatch."
            ) from None
        if completed.returncode != 0:
            raise bridge.BridgeError(
                "feature_policy", "Installed Codex feature list is unavailable; refusing to dispatch."
            )
        _check_budget("feature list parsing")
        features = {}
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-1] in ("true", "false"):
                features[parts[0]] = {"stage": parts[1], "state": parts[-1] == "true"}
        if not features:
            raise bridge.BridgeError(
                "feature_policy", "Installed Codex feature list is empty; refusing to dispatch."
            )
        return features
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def _is_side_effecting(name: str) -> bool:
    return any(pattern in name for pattern in _SIDE_EFFECT_PATTERNS)


def _declared_types(property_schema) -> frozenset:
    value = property_schema.get("type")
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return frozenset(value)
    return frozenset()


def _item_reference(property_schema):
    items = property_schema.get("items")
    if isinstance(items, dict) and isinstance(items.get("$ref"), str):
        return items["$ref"].rsplit("/", 1)[-1]
    return None


def thread_params_supported(binary: str) -> bool:
    """Offline: does the installed EXPERIMENTAL schema define the isolation fields?

    Runs the RAW installed ELF (never the PATH wrapper, which rewrites
    ``CODEX_HOME``) with an EMPTY ``HOME``/``CODEX_HOME`` and generates the
    app-server protocol schema into a bounded temporary directory with
    ``--experimental``. There is no login, account, model, thread, turn or
    network use: this is offline protocol metadata only, and the ordinary
    profile is never searched. The ``v2/ThreadStartParams`` bundle is parsed and
    every ``REQUIRED_THREAD_FIELDS`` entry must be present with its EXACT declared
    shape (types + item ``$ref``); a non-ELF (wrapper), a schema-generation
    failure, or an unknown/renamed/mistyped schema yields ``False`` so the turn
    stays blocked. The temp dir is always cleaned up.
    """
    _require_native_elf(binary)
    home = tempfile.mkdtemp(prefix="codex-empty-home-")
    try:
        codex_home = os.path.join(home, ".codex")
        os.makedirs(codex_home, mode=0o700, exist_ok=True)
        out_dir = os.path.join(home, SCHEMA_OUTPUT_SUBDIR)
        env = bridge.child_environment()
        env["HOME"] = home
        env["CODEX_HOME"] = codex_home
        command = [binary, "app-server", "generate-json-schema", "--experimental", "--out", out_dir]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_budget_timeout(SCHEMA_TIMEOUT),
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            raise bridge.BridgeError(
                "thread_params_unsupported",
                "Installed Codex experimental schema cannot be generated; refusing to dispatch.",
            ) from None
        if completed.returncode != 0:
            raise bridge.BridgeError(
                "thread_params_unsupported",
                "Installed Codex experimental schema is unavailable; refusing to dispatch.",
            )
        _check_budget("schema parsing")
        base = Path(out_dir).resolve()
        path = base.joinpath(*SCHEMA_THREAD_FILE).resolve()
        if base not in path.parents:
            return False
        try:
            if path.stat().st_size > SCHEMA_FILE_MAX_BYTES:
                return False
            raw = path.read_bytes()
        except OSError:
            return False
        try:
            schema = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return False
        if not isinstance(schema, dict) or schema.get("title") != THREAD_SCHEMA_TITLE:
            return False
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return False
        for name, expected in THREAD_FIELD_SHAPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict):
                return False
            if _declared_types(prop) != expected["types"]:
                return False
            if expected["items"] is not None and _item_reference(prop) != expected["items"]:
                return False
        return True
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def runtime_toolset_verified(binary: str = None) -> bool:
    """Whether the installed build's exact builtin tool registry is qualified.

    The experimental schema only confirms that the isolation FIELDS exist; it
    does NOT prove which native tools the runtime actually exposes to a turn.
    The remaining ``unified_exec`` flag is transport-only: official config
    documents it as always enabled with user opt-outs ignored, while
    ``shell_tool`` controls command-execution tools (and is already disabled).
    Feature/config/transport flags are DIAGNOSTICS, not a per-thread effective
    native-tool-catalog proof, and the experimental method catalog exposes no
    native-tool-catalog/list API or ``thread/start`` dry-run. The exact builtin
    registry qualification is therefore still pending, so this is fail-closed
    ``False`` and generation MUST stay blocked even when every experimental
    field is present.
    """
    return False


def qualify_native_contract(
    binary: str,
    probe=probe_feature_list,
    thread_probe=thread_params_supported,
    runtime_probe=runtime_toolset_verified,
) -> dict:
    """Qualify the installed contract for a tool-free turn, or report why not.

    Probes the ACTUAL candidate overrides (never manufactures state): runs
    ``features list`` once at baseline and once with ``FEATURE_OVERRIDES``
    applied, then requires every ``RELEVANT_FEATURES`` capability to read
    ``false`` in the CANDIDATE output. Also rejects a recognized, active
    side-effecting feature outside the guarded set. Independently confirms the
    installed EXPERIMENTAL ``ThreadStartParams`` schema defines the isolation
    fields (``schema_supported``) and that the exact builtin runtime toolset is
    qualified (``runtime_probe`` — currently always unverified). ``qualified`` is
    True only when every condition holds; otherwise generation MUST block.
    """
    baseline = probe(binary, overrides=())
    candidate = probe(binary, overrides=FEATURE_OVERRIDES)
    reasons = []
    for name in RELEVANT_FEATURES:
        info = candidate.get(name)
        if info is None:
            reasons.append("unrecognized:" + name)
        elif info["state"]:
            reasons.append("active:" + name)
    for name, info in candidate.items():
        if (
            info["state"]
            and info["stage"] not in ("removed", "deprecated")
            and name not in RELEVANT_FEATURES
            and name not in BENIGN_TRUE_FEATURES
            and _is_side_effecting(name)
        ):
            reasons.append("unknown_active:" + name)
    schema_supported = thread_probe(binary)
    if not schema_supported:
        reasons.append("thread_params_unsupported")
    if not runtime_probe(binary):
        reasons.append("unverified_runtime_toolset")
    return {
        "qualified": not reasons,
        "schema_supported": schema_supported,
        "reasons": tuple(reasons),
        "baseline": baseline,
        "candidate": candidate,
    }


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
            timeout=_budget_timeout(10),
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


def run_turn(client, request, emit, workspace: Path, isolation_fields: bool = False):
    """Drive one native turn; yield deltas; return the validated final payload.

    ``isolation_fields`` is set only when the installed EXPERIMENTAL
    ``ThreadStartParams`` schema has been offline-confirmed to define them (see
    ``thread_params_supported``). When confirmed, the thread declares empty
    ``environments``/``dynamicTools``/``selectedCapabilityRoots`` and
    ``allowProviderModelFallback=False``, and the turn pins ``environments=[]``
    to disable environment access for that turn. When NOT confirmed the fields
    are omitted (they would be unknown to the app-server and imply isolation they
    do not provide). In either case tool isolation is established — or the turn
    is blocked — by the pre-turn contract qualification, never by these fields.
    """
    thread_params = {
        "model": MODEL,
        "modelProvider": PROVIDER,
        "cwd": str(workspace),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": "",
        "developerInstructions": GENERATION_DEVELOPER_INSTRUCTIONS,
        "ephemeral": True,
    }
    if isolation_fields:
        thread_params.update(
            {
                "environments": [],
                "dynamicTools": [],
                "selectedCapabilityRoots": [],
                "allowProviderModelFallback": False,
            }
        )
    _check_budget("thread start")
    thread = client.request("thread/start", thread_params)
    try:
        thread_id = thread["thread"]["id"]
    except (KeyError, TypeError):
        raise bridge.BridgeError("rpc_malformed", "Thread response is invalid.") from None
    if not isinstance(thread_id, str) or not thread_id:
        raise bridge.BridgeError("rpc_malformed", "Thread identity is invalid.")
    if thread.get("model") != MODEL or thread.get("modelProvider") != PROVIDER:
        raise bridge.BridgeError("model_mismatch", "App-server selected an unexpected model route.")

    turn_params = {
        "threadId": thread_id,
        "model": MODEL,
        "effort": "high",
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "input": serialize_input(request["system"], request["messages"]),
    }
    if isolation_fields:
        turn_params["environments"] = []
    _check_budget("turn start")
    start = client.request("turn/start", turn_params)
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


def execute(args, command=None, emit=None, qualify=None):
    """Run one generation. ``command``/``emit``/``qualify`` are injectable for tests."""
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
            # This fixed Codex subscription provider does not use DeepSeek. Capture
            # the caller's bounded operation deadline before offline metadata.
            work_deadline = capture_work_deadline(args.timeout)
        except ValueError:
            emit({"type": "error", "code": "arguments", "message": "Caller timeout is invalid."})
            return {"schema": SCHEMA, "ok": False, "error_code": "arguments"}
        except bridge.BridgeError as error:
            emit({"type": "error", "code": error.code, "message": str(error)})
            return {"schema": SCHEMA, "ok": False, "error_code": error.code}
        # ONE deadline for the whole call. Every probe and RPC operation is capped
        # by what remains; it is never recomputed, so earlier stages cannot extend it.
        token = _DEADLINE.set(work_deadline)
        client = None
        success = False
        try:
            _check_budget("metadata qualification")
            # Contract qualification runs BEFORE the app-server (account/model/
            # thread/turn) is ever started: an unprovable tool-isolation contract
            # must block without touching the account or a model.
            if qualify is not None:
                qualification = qualify()
            elif command is None:
                binary = bridge.resolve_codex_binary(args.codex_bin)
                verify_version(binary)
                qualification = qualify_native_contract(binary)
            else:
                # Caller-emulated qualified host: no offline schema detection is
                # performed, so isolation fields are NOT sent (fail closed).
                qualification = {"qualified": True, "schema_supported": False}
            _check_budget("contract qualification")
            if not qualification.get("qualified"):
                raise bridge.BridgeError(
                    "unsupported_contract",
                    "Native generation is blocked: the installed contract cannot prove tool isolation.",
                )
            if command is None:
                command = generation_codex_command(bridge.resolve_codex_binary(args.codex_bin))
            _check_budget("app-server launch")
            # The launched command is ALWAYS the guardian-wrapped native argv, so
            # a hard parent death still reclaims the detached native group. The
            # guardian receives the host PID and the ORIGINAL captured monotonic
            # work deadline — never a value from the request JSON.
            command = guardian_command(command, work_deadline)
            client = bridge.RpcClient(command, _budget_timeout(args.timeout))
            # Retain the ABSOLUTE clamp: the RPC client deadline can never exceed
            # the captured work deadline, even if the remaining budget shrank
            # between capture and launch.
            client.deadline = min(client.deadline, work_deadline)
            _check_budget("account inspection")
            limits, available, _account = bridge.inspect_account(client)
            if not limits["dispatch_allowed"]:
                raise bridge.BridgeError(
                    limits["reason"], "New Sol generation is paused; inspect quota and checkpoint."
                )
            if not available:
                raise bridge.BridgeError("model_unavailable", "The pinned model is unavailable on this account.")
            final = run_turn(
                client,
                request,
                emit,
                _empty_workspace(directory),
                isolation_fields=bool(qualification.get("schema_supported")),
            )
            emit(final)
            result.update(final=final)
            success = True
        except bridge.BridgeError as error:
            emit({"type": "error", "code": error.code, "message": str(error)})
            result.update(ok=False, error_code=error.code)
        finally:
            # Interrupt on any non-success path; close() terminates the group.
            if client is not None:
                client.close(interrupt=not success)
            _DEADLINE.reset(token)
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
