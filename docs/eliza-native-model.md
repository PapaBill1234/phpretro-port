# Native-Sol main text provider (first safe slice)

Status: **draft — generation BLOCKED on the installed release.** The pure-text
Eliza handlers and transport are implemented and controlled-fixture verified, but
the installed native `0.162.0` contract cannot prove tool isolation, so
`scripts/codex-generate.py` refuses to start the app-server. Real native
tool-isolation / host-SSE acceptance is a **root-reviewer** step and is not
claimed here. Evidence: `~/…/.builder/isolation-audit/findings.md`.

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

## Isolation *attempted* before a turn (currently unprovable → blocked)

Tool isolation must be established before dispatch, never by rejecting events
after execution. On the installed `0.162.0` release it cannot be proven, so the
generator blocks. The controls it evaluates:

- **Environment**: an allowlist only (`HOME`, `PATH`, `LANG`, `TERM`,
  `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `CODEX_HOME`). No API/OAuth credential
  is copied; the selected native `CODEX_HOME` login is managed by the CLI.
- **Pre-turn version**: installed Codex version must equal the pinned `0.162.0`
  (offline `--version` probe).
- **Contract qualification (fail closed)**: the offline probe runs the **raw
  installed ELF** (not the PATH wrapper, which rewrites `CODEX_HOME`) under an
  **empty** `HOME`/`CODEX_HOME` (metadata only — no login/account/model). It
  runs `features list` twice — a baseline and one with the candidate
  `features.<name>=false` overrides applied — and requires every relevant
  capability to read `false` in the **candidate** output. It never manufactures
  a "false": config storage (`config/read`) is not capability proof. An
  unrecognized key (silent no-op), a capability that stays active despite its
  override (observed: `unified_exec` and the extra capabilities
  `code_mode_host`, `skill_search`, `sleep_tool`, `goals`, `worktrees`,
  `workspace_dependencies`), an unknown active side-effecting feature, or an
  unsupported ThreadStartParams field all **block** without starting the
  app-server.
- **Quota/off-peak**: a fresh off-peak window and fresh subscription quota per
  call. Unknown quota, `spendControlReached`, or any window at ≥90% used blocks
  dispatch.
- **Thread (per-generation)**: a unique, stateless thread per generation (never
  resumed) with host-fixed `baseInstructions` / `developerInstructions` and an
  empty host-derived workspace as `cwd`. The installed generated
  `ThreadStartParams` schema (v2) does **not** define `environments`,
  `dynamicTools`, `selectedCapabilityRoots` or `allowProviderModelFallback`, so
  they are omitted rather than sent as empty no-ops: **empty/ignored thread
  fields do not isolate a turn** and are not relied upon.
- **Config (recognized keys only)**: `sandbox_mode=read-only`,
  `approval_policy=never`, `mcp_servers={}`, `project_doc_max_bytes=0`,
  `web_search=disabled`, and an attempted `features.<name>=false` for each
  relevant capability. The unknown `tools.*` namespace is removed. An inherited
  MCP connector is additionally rejected by the reused inspection (`config/read`
  must show every `mcp_servers` entry disabled — the `{}` override does not
  erase inherited entries; the effective config is checked, never echoed).
- **Text only**: any non-text item or unrecognized turn event aborts the turn;
  server-initiated requests are refused by `RpcClient`. Cancellation/timeout/
  provider errors interrupt the turn and terminate the process group — no
  success, no fallback.

## Known limits and gaps (not closed)

- **Generation is BLOCKED on the installed release.** The installed `0.162.0`
  contract cannot prove tool isolation: the candidate `features.unified_exec=false`
  override does not take effect (and the extra relevant capabilities above stay
  active), and the ThreadStartParams isolation fields do not exist. The
  generator therefore refuses to start the app-server (`error:
  "unsupported_contract"`). Nothing was logged in, read or called to establish
  this — it is offline metadata only.
- **The pure-text handlers are a DRAFT, with no bypass.** The plugin's four
  handlers remain an implementation draft exercised only by the controlled
  fixture; they are not wired to a live native turn. No flag, prompt or
  post-execution event rejection is treated as isolation.
- **Real native acceptance is not run in this worker.** Actual native
  tool-isolation, tool-temptation behaviour and host SSE acceptance remain a
  root-reviewer step. **One text turn would not prove isolation** even if it
  ran.
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
python3 -m unittest tests.ops.test_codex_generate   # 25 tests, process boundary + FakeRPC + contract qualification
node builder/eliza/native-model/native-model.fixture.mjs   # 19/19 controlled checks (draft handlers)
python3 -m unittest tests.ops.test_codex_bridge      # 11 tests, dependency unchanged
```

Offline qualification evidence (raw ELF, empty HOME/CODEX_HOME): baseline and
candidate `features list` both show `unified_exec` etc. active;
`qualify_native_contract` returns `qualified=False` with reasons
`active:unified_exec, …, thread_params_unsupported`. Raw findings:
`~/…/.builder/isolation-audit/findings.md`.

Gap ledger entry (to be added to `docs/gaps.md` by the integrator, G05):
native-Sol main text provider — pure-text handlers/transport implemented as a
draft; **native generation blocked** because the installed release cannot prove
tool isolation. Real native acceptance remains open.
