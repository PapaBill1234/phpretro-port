# Eliza integration checkpoint

Reviewed 2026-10-14. Eliza integration is the active priority; CMS, theme and game-port work is paused. The complete Eliza integration and PHPRetro port remain unfinished.

## Two runtime layers

The project companion in [builder/eliza](../builder/eliza/package.json) uses the actual published `@elizaos/core`, SQL and canonical agent-orchestrator packages, pinned to `2.0.3-beta.7`. It runs an `AgentRuntime`, PGlite persistence, canonical ACP sessions and project task/receipt controls. Its loopback API and command controls are a builder companion, separate from the full upstream web app.

The [upstream submodule](../.gitmodules) preserves the full [Eliza source at `28fef47`](https://github.com/elizaOS/eliza/tree/28fef47c9d9d3acc7792be0fdaa3070020293cc5), including CLI, API, React app and first-party plugins. Its source manifests and the companion's published packages have different revisions. The public registry's older app package was not substituted for this source. Exact package integrities, upstream revisions and licenses are recorded in [provenance](../builder/eliza/provenance.json).

## Companion launch and verification

Use Node `24.15.0`, npm, Python 3 and Git. From the repository root:

```sh
python3 scripts/eliza-builder.py setup
python3 scripts/eliza-builder.py start
```

`start` stays in the foreground. In another terminal, `python3 scripts/eliza-builder.py status` reads its authenticated loopback status. Private state is under `.builder/eliza`; new setup defaults to `workersEnabled: false`. These commands do not dispatch inference. Dispatch/help are separate explicit operations and need reviewed provider configuration, task claims, funding, quota and the buffered off-peak policy. Native Claude workers retain their own Doctor/history; Codex history and aggregated usage are separate.

Offline integration tests exercise the real published runtime and SQL persistence, authenticated HTTP controls, task/session recovery, four ACP fixture sessions, process cancellation, receipts and failure/quota boundaries. Fixture agents and bridge responses avoid provider inference; they do not prove four live DeepSeek workers. The [CI companion job](../.github/workflows/verify.yml) uses Node `24.15.0`, lifecycle-disabled locked npm installation and `npm test`. It does not install the full source submodule, local models or native device components.

Local execution under actual Node `24.15.0` passed eleven companion/process tests. GitHub CI on `e540a9c` is green for all six jobs, for both push and pull_request. Later integration commits require their own CI result.

## Full app evidence and remaining boundary

A separate checkout of the same upstream pin passed actual source API, renderer, owner-login and direct DeepSeek ordinary/streaming chat checks. Those checks used private runtime state and a temporary API compatibility preload for DeepSeek tool-choice requests. The integration tree now also passed its own [verified source setup](eliza-upstream.md): clean pinned submodule, digest-checked Node `24.15.0` and Bun `1.4.2`, frozen lifecycle-disabled installation of 3,255 packages, workspace symlinks and twelve renderer registration builds. The actual source CLI `--help` succeeded from this tree and exposed `start`, `doctor`, `dashboard`, auth and configuration commands. This proves installation and CLI readiness; configured full-host startup is a separate milestone.

Native subscription Sol readiness and bounded read-only help were tested through the selected Codex profile. The [main text provider](eliza-native-model.md) implements real model handlers and passes 31 Python transport/policy tests plus 19 controlled streaming/response fixtures. Generation remains blocked before account or model dispatch: the installed raw ELF `codex-cli 0.162.0` is byte-identical to the official release, and the experimental schema **does** define the isolation fields, but that is metadata, not runtime isolation, and no accepted host policy path exists yet. See the [native qualification evidence and acceptance plan](eliza-native-qualification.md). Existing review-help access is a separate path and does not establish main-chat readiness.

The [project view](eliza-project-view.md) uses the actual pinned loader/dispatcher, host authenticated client, strict host-resolved OWNER authority and coarse private-state projection. Twenty controlled checks and its view bundle passed review; full-host owner transport and browser mounting remain unverified. The [fresh-chat patch](eliza-fresh-chat.md) is published for qualification and has not been applied to the clean submodule or live app. The [private usage reporter](eliza-usage.md) extracts real producer metadata, reports partial known subtotals explicitly and does not claim measured savings. Durable queue deployment, live project-view mounting, native coordinator qualification and end-to-end worker escalation/resume remain open. See [provider contracts](provider-migration.md) and [known gaps](gaps.md).

The shared runtime’s selected-profile environment and concurrent stale-lock recovery defects were repaired and independently accepted. Eleven integration/process tests pass under Node24.15.0, including actual child environment propagation, OS advisory ownership, owner-crash recovery and helper-loss shutdown. This verifies the companion boundary; the full-app milestones above remain open.
