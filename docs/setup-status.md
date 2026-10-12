# Setup status — 2026-10-11 UTC

This records observed setup checks. Planning/configuration completion is separate from completing the PHPRetro port.

| Item | Observed state |
|---|---|
| GitHub access | Existing authenticated `PapaBill1234` account has repo/workflow scopes; root agent owns repository creation and commits |
| Native Claude Code | 2.1.287 installed; raw executable verified; existing shell wrapper belongs to a prior runtime |
| Native installation Doctor | Raw `claude doctor` exited 0; no configuration changes were applied |
| A6API credential | Existing private mode-0600 file found; no value printed or committed |
| OpenRouter credential | Existing private mode-0600 file found; no value printed or committed |
| A6API model catalog | HTTP 200, exact Luna/Sol/DeepSeek IDs present |
| Direct Messages text | All three models returned valid Anthropic message envelopes |
| Direct Messages SSE/tool/result | All three passed harmless synthetic tool/result continuation with complete SSE endings |
| Actual native Luna CLI | Read-only fixture succeeded; Read tool used once and correct final value |
| Project agent definitions | Native `claude plugin validate .claude/agents` passed |
| Launcher preflight | Redacted `--check` passed; provider values absent from checked-in files/logs |
| Native configured lead and worker | Earlier read-only fixtures passed with exact DS/Luna models; worker used a Git worktree. Current lead starts Sol through `sonnet`; updated default-tools handoff has preflight/tests only, no new inference |
| Native configured Sol reviewer | Sequential high-effort CLI run succeeded with one Read and exact Sol ID; initial 512-token concurrent fixture timed out and remains recorded |
| Independent native Sol source review | Builder and auth source reviews completed; no final auth P0/P1 blockers. Session hardening was source-verified after the focused native follow-up timed out at120 seconds without a verdict. Task-board fixes passed all seven regression tests |
| Subagent model dispatch | Actual lead→Explore passed: DS main plus Luna child observed in messages and usage; reviewer child dispatch not yet separately observed |
| Interactive `/doctor prompt-audit` | Available in installed version by official docs; no interactive run claimed |
| Doctor coordinator tools | Lead/backup use default built-in tools with no agent tool restriction; ordinary permissions, strict judge-only MCP and disabled hooks remain. Interactive Doctor execution is unverified |
| Interactive peer wiring | Prepared tmux helper starts one Sol coordinator and three named Luna worktree windows without an initial prompt. All share private configuration/history; cross-session messaging across all four peers remains unverified |
| Aggregate all-session analytics | Native `/doctor` is local; shared task/run telemetry and quota enforcement require the supervisor |
| Provider rates and exact cash costs | Unknown until confirmed against A6API's billing source; no savings claim |
| Jev | Project shim exposes judge only; actual native lead and worker each called judge successfully, with no gate present or invoked |
| Previous machine workflow | Existing prior runtime STOP is present and preserved; no service/global config edits |
| Rust toolchain | rustc/cargo not found on PATH during audit; only install if a measured WASM module is admitted |

The three initial text probes produced 22 provider-reported output tokens. Six subsequent synthetic tool/result requests produced 119. A native Luna fixture used 3,719 uncached input and 52 output tokens. These were capability checks, not a matched cost comparison or a sustained build run. Source: private sanitized capability evidence retained in the task's scratch research folder.

The direct bootstrap launcher protects secret logging and checked-in configuration. It does not implement a secret-isolated inference proxy, a cash budget, or a provider spending monitor. The separate Codex desktop heartbeat checks its five-hour reserve and reset every two hours; it does not cap A6API charges. Those are explicit operational work, not implied by successful API requests. Reviewer restrictions are tool allowlists; writable worker scope and commit acceptance need integration enforcement.

Official references: [gateway](https://code.claude.com/docs/en/llm-gateway), [subagents](https://code.claude.com/docs/en/sub-agents), [Doctor](https://code.claude.com/docs/en/skills), [monitoring](https://code.claude.com/docs/en/monitoring-usage).

After committing the complete checkpoint and preparing `.builder/handoff`, run `python3 scripts/claude-sessions.py start`, `python3 scripts/claude-sessions.py status`, then `python3 scripts/claude-sessions.py attach`. Existing unrelated tmux sessions are preserved. Worktrees start from the committed checkout; private handoff files are shared by an absolute `--add-dir`, not copied into worktrees. The helper checks ownership and avoids restarting existing windows; it is not a security sandbox or spending guard.

The startup model is already a6api `gpt-6.1-sol`: `sonnet` and `opus` both map to Sol; `haiku` maps to Luna. Use `/model sonnet` or `/model opus` to select Sol in the existing interactive session, `/model haiku` for Luna, or `/model deepseek-v4.1-flash` for the verified DeepSeek provider model. Model switching retains that session's role permissions. `python3 scripts/claude-builder.py --role backup` opens another writable Sol coordinator using the same private history. Native `/doctor` diagnoses setup/context; aggregate session usage is separate. A Codex identity/conversation cannot transfer into Claude Code; written handoff and repository state provide continuity.

ChatGPT account sign-in is not wired into this launcher. Supporting two Plus accounts from Claude Code requires a separately reviewed localhost Anthropic-Messages-to-Responses gateway and OAuth profile picker as a future task; each account owner must authorize its own sign-in. Plain Codex logout/login does not switch these native Claude sessions. See the official [open-source token-sharing guide](https://developers.openai.com/siwc/token-sharing-open-source) and [profiles and sessions](https://developers.openai.com/siwc/profiles-and-sessions). No OAuth gateway or global configuration changes were implemented here.
