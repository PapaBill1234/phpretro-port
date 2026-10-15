# Builder contract

OpenCode is the native coding coordinator; connect ChatGPT accounts only through OpenCode's native provider flow. DeepSeek Flash is the separate direct API provider. Desired routine dispatch is off-peak and at most four disjoint workers, but the current bootstrap does not enforce those controls. Never route a Sol ID through DeepSeek. Provider funding failures pause dispatch and do not count toward task failures. CMS/theme/game-port work remains paused during migration.

Use OpenCode 2, not OpenCode 1. Eliza orchestration and the custom native-subscription bridge are retired workstreams; preserve their checkpoints but do not resume them. Read `docs/opencode-builder.md` and the assigned task brief; consult `.builder/handoff/OPENCODE-START.md` when present. `docs/gaps.md` retains historical port/Eliza evidence, not authorization to restart it. Load only the source contract and skill needed for the task. The full CMS/game port remains incomplete and paused.

- Stack: Go CMS, React/TypeScript, ts-rust tooling, MariaDB CMS + Polaris databases, Redis; Polaris + Octane/Octane-Renderer integration.
- Themes consume versioned DTOs and capabilities. No SQL, auth decisions, or SSO issuance in themes.
- Preserve behavior, not legacy security defects. Credentials, original Habbo assets, production data and provider transcripts never enter Git.
- One task, branch and worktree per editing worker. Declare owned paths. Commit with task ID after relevant checks; push worker branches only. The integrator alone updates main after independent review and CI.
- Sol reviews before architecture lock, for retry help after at least three durable task failures, and before integration. A verdict never substitutes for a test or source check.
- Report changed paths, evidence references, test results, commit SHA, remaining gaps and token usage. Keep returns under 600 words; save larger evidence separately.
- Use `rg` first. Narrow paths before reading. Do not forward full files/logs between agents. Stop unproductive retries after the second identical failure and return root-cause evidence.
- Jev answers typed choice/yes-no/score questions over already gathered, small, relevant facts. Batch questions for one state. Verify important conclusions in source. Do not send secrets, personal data or full files. Handle `escalate`/unavailable locally without unchanged retries. Never use `jev_gate` or a PreToolUse approval gate.
- Token savings require a matched same-task, same-main-model comparison, including input, cached input, output, time, Jev calls/cost and correctness. Unknown usage/cost is unknown, not zero.
- Update the gap and parity evidence with the change. Do not mark a feature done for a mock, a source-existence assertion, or a happy-path-only test.
- Before Codex dispatch/review/integration, inspect all actual reported account quota windows. At 10% remaining, checkpoint and stop new dispatch; resume from private handoff state only after fresh verified quota metadata. Unknown usage blocks dispatch; 300/10080-minute windows are not daily allowances. Never consume a reset credit automatically.
