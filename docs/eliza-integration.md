# Eliza integration checkpoint

Reviewed 2026-10-13. Eliza integration is the active priority; CMS, theme and game-port work is paused. The complete Eliza integration and PHPRetro port remain unfinished.

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

Local execution under actual Node `24.15.0` passed all seven companion tests using the existing locked dependencies. GitHub execution of the new CI job remains pending.

## Full app evidence and remaining boundary

A separate checkout of the same upstream pin was installed with private Bun `1.4.2` and Node `24.15.0`. The actual source CLI, API and Vite renderer passed first-run checks; a browser displayed the upstream runtime chooser. Later owner login and direct DeepSeek ordinary/streaming chat checks passed in that separately configured app. Those checks used private runtime state and a temporary API compatibility preload for DeepSeek tool-choice requests. Root's pinned submodule has not yet received that installation or a reviewed reproducible dashboard launch path. The companion launch above does not start this full app.

Native subscription Sol readiness and bounded read-only help were tested separately through the selected Codex wrapper/account profile. A tool-free health turn completed with the exact reply and matching turn; an earlier 180-second source-review probe timed out. These checks do not provide an Eliza main-chat model handler. Sol `TEXT_SMALL`/`TEXT_LARGE`, streaming/tool handling, upstream project-plugin registration and its dashboard view remain pending. Dashboard-launcher fixes, the NewChat/ChatOverlay integration, authenticated owner boundaries, durable queued escalation/resume and complete live-worker acceptance still need integration review. See [provider contracts](provider-migration.md) and [known gaps](gaps.md).

The shared runtime’s selected-profile environment and concurrent stale-lock recovery defects were repaired and independently accepted. Eleven integration/process tests pass under Node24.15.0, including actual child environment propagation, OS advisory ownership, owner-crash recovery and helper-loss shutdown. This verifies the companion boundary; the full-app milestones above remain open.
