# Native-Sol main text provider (first safe slice)

Status: **implemented, controlled-fixture verified**. Real native / tool-isolation
/ host-SSE acceptance is a **root-reviewer** step and is not claimed here.

Target pin: upstream Eliza `28fef47c9d9d3acc7792be0fdaa3070020293cc5`
(`/home/ubuntu/Documents/Codex/2026-10-11/al/work/research/eliza`).

## What this slice is

A first, deliberately small vertical slice that makes the native ChatGPT
subscription model (`gpt-6.1-sol`) the **main text provider** of the Eliza
runtime, without ever running native filesystem/shell/web/MCP/agent tools.

Two owned artifacts plus tests and this note:

| Path | Role |
|---|---|
| `scripts/codex-generate.py` | Generic generation transport (stdin JSON → stdout JSONL). |
| `builder/eliza/native-model/index.mjs` | Physical ESM plugin registering the real model handlers + live stream. |
| `builder/eliza/native-model/transport.mjs` | Child-process transport (env allowlist, JSONL reader). |
| `builder/eliza/native-model/package.json` | ESM package manifest / loader entry (`openai-codex`). |
| `builder/eliza/native-model/native-model.fixture.mjs` | Controlled integration fixture (no native calls). |
| `tests/ops/test_codex_generate.py` | Fake-RPC process-boundary tests. |

`scripts/codex-bridge.py` and `scripts/coding-window.py` are **reviewed
dependencies**: the generator reuses their `RpcClient`, account inspection and
`normalize_limits` helpers but never inherits the source-review developer
prompt, thread registry or read-only review assumptions. They are not edited or
committed by this task.

## Plugin registration (real, not catalog-only)

`builder/eliza/native-model/index.mjs` exports `nativeSolPlugin` with
`name: "openai-codex"`. Core registers each `plugin.models` entry with
`plugin.name` as the provider (`runtime.ts`, pin 28fef47), so the four entries
become real registrations under the recognized provider **`openai-codex`**:

- `TEXT_SMALL`, `TEXT_LARGE`, `RESPONSE_HANDLER`, `ACTION_PLANNER`.

`modelMetadata` declares `displayModelDefault: "gpt-6.1-sol"` for every slot, so
RESPONSE_HANDLER / ACTION_PLANNER route to the same small model without a
separate catalog.

The plugin registers at the **neutral priority 0 and declares no automatic
takeover**: it does not outrank existing providers, so cheap default text calls
(e.g. DeepSeek via the guarded task API) are untouched. Sol serves explicitly
selected coordinator/important requests only — via
`ELIZA_BRAIN_PROVIDER=openai-codex` or a per-call provider argument.

Handlers honour the actual core contracts:

- **Input** `GenerateTextParams`: `prompt`, `system`, `messages`, `tools`,
  `toolChoice`, `responseSchema`, `stream`, `onStreamChunk`, `signal`.
- **Output**: a `string`, or a `TextStreamResult`
  (`textStream` async iterable + `text` / `usage` / `finishReason` promises).
  Tool calls are validated against the host-requested allowlist and returned to
  the Eliza pipeline — **never** executed natively.

## Generation request / stream contract

`scripts/codex-generate.py` reads **one** bounded JSON request from stdin
(≤256 KiB) and writes JSONL to stdout:

```
request : { system?, messages:[{role,content}], tools?, responseSchema?, stream? }
stream  : {"type":"delta","text":"…"}        (provisional)
          {"type":"final","text","usage","finishReason","toolCalls","model"}
          {"type":"error","code","message"}  (sanitized; no private auth/paths/raw errors)
```

A request may **not** select the model, provider, `cwd`, state directory,
executable or files — those are host-fixed. The final answer is the terminal
`item/completed` assistant message settled by an **exact matching**
`turn/completed` for the same thread and turn; deltas are never trusted.

## Isolation enforced *before* a turn

Isolation is set up before dispatch, not by rejecting events after execution:

- **Environment**: an allowlist only (`HOME`, `PATH`, `LANG`, `TERM`,
  `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `CODEX_HOME`). No API/OAuth credential
  is copied; the selected native `CODEX_HOME` login is managed by the CLI.
- **Pre-turn verification**: installed Codex version must equal the pinned
  `0.162.0` (offline `--version` probe); the thread response must report
  `model = gpt-6.1-sol`, `modelProvider = openai`.
- **Feature-key verification (fail closed)**: before a turn, an offline
  `features list` probe runs under an **empty** `HOME`/`CODEX_HOME` (metadata
  only — never a login, account read or model call). If a required isolation
  key (`shell_tool`, `multi_agent`, `hooks`) is not recognized, an unsupported
  key would be silently ignored, so dispatch **blocks** rather than proceeding.
- **Quota/off-peak**: a fresh off-peak window and fresh subscription quota per
  call. Unknown quota, `spendControlReached`, or any window at ≥90% used blocks
  dispatch. The off-peak gate is re-evaluated every generation.
- **Thread**: a unique, stateless, ephemeral thread per generation (never
  resumed), with `environments: []` (no default environment access),
  `dynamicTools: []`, `selectedCapabilityRoots: []`,
  `allowProviderModelFallback: false`, host-fixed
  `baseInstructions` / `developerInstructions`, `project_doc_max_bytes=0` and
  an empty host-derived workspace as `cwd`.
- **Config**: `sandbox_mode=read-only`, `approval_policy=never`, `mcp_servers={}`,
  `features.multi_agent=false`, `features.hooks=false`, `features.shell_tool=false`,
  `tools.web_search=false`, `tools.view_image=false`, `tools.apply_patch=false`,
  `tools.unified_exec=false`, `project_doc_max_bytes=0`. An installed active MCP
  connector is additionally rejected by the reused account inspection
  (`mcp_servers` must all be disabled).
- **Text only**: any non-text (`commandExecution`, patch, search, tool) item or
  any unrecognized turn event aborts the turn; server-initiated requests are
  refused by `RpcClient`. Cancellation/timeout/provider errors interrupt the
  turn and terminate the process group — no success, no provider fallback.

## Known limits and gaps (not closed)

- **Real native acceptance is not run in this worker.** The plugin and
  generator are verified against a deterministic fake app-server and a
  controlled runtime fixture. Actual native tool-isolation, real `features list`
  parsing and host SSE acceptance remain a root-reviewer step. **One text turn
  does not prove isolation**, and rejecting events is never claimed to prevent
  execution — prevention is attempted at launch/config, and unproven config
  blocks.
- **Exact delta method.** Only `item/agentMessage/delta` matching the current
  thread/turn is read as a provisional delta; any other event name fails closed.
  Deltas are provisional and never trusted as the answer.
- **Tool calling is intentionally unsupported** in this slice: a tool call the
  host did not request (or any tool call when none was requested) fails
  validation. Validated tool proposals are returned to the host pipeline only —
  never executed natively.
- **TextStreamResult is a live iterator**: `stream: true` yields deltas as they
  arrive and settles `text`/`usage`/`finishReason` on the exact completed turn.
  Cancellation kills the transport and rejects with no fallback.
- Installed-CLI feature-key names beyond the listed set are **not** confirmed;
  any key that proves unrecognized on the installed CLI must block, not be
  assumed away.

## Verification (this slice)

```
python3 -m unittest tests.ops.test_codex_generate   # 19 tests, process boundary + FakeRPC negatives
node builder/eliza/native-model/native-model.fixture.mjs   # 19/19 controlled checks
python3 -m unittest tests.ops.test_codex_bridge      # 11 tests, dependency unchanged
```

Gap ledger entry (to be added to `docs/gaps.md` by the integrator, G05):
native-Sol main text provider — first safe slice implemented and
fixture-verified; real native acceptance and full CMS/runtime parity remain
open.
