# Builder contract

Read `docs/gaps.md` and the assigned task brief. Load only the source contract and skill needed for that task. This is a planning repository until application implementation is explicitly recorded.

- Stack: Go CMS, React/TypeScript, ts-rust tooling, MariaDB CMS + Polaris databases, Redis; Polaris + Octane/Octane-Renderer integration.
- Themes consume versioned DTOs and capabilities. No SQL, auth decisions, or SSO issuance in themes.
- Preserve behavior, not legacy security defects. Credentials, original Habbo assets, production data and provider transcripts never enter Git.
- One task, branch and worktree per editing worker. Declare owned paths. Commit with task ID after relevant checks; push worker branches only. The integrator alone updates main after independent review and CI.
- Sol reviews before architecture lock, after the same failure twice, and before integration. A verdict never substitutes for a test or source check.
- Report changed paths, evidence references, test results, commit SHA, remaining gaps and token usage. Keep returns under 600 words; save larger evidence separately.
- Use `rg` first. Narrow paths before reading. Do not forward full files/logs between agents. Stop unproductive retries after the second identical failure and return root-cause evidence.
- Jev answers typed choice/yes-no/score questions over already gathered, small, relevant facts. Batch questions for one state. Verify important conclusions in source. Do not send secrets, personal data or full files. Handle `escalate`/unavailable locally without unchanged retries. Never use `jev_gate` or a PreToolUse approval gate.
- Token savings require a matched same-task, same-main-model comparison, including input, cached input, output, time, Jev calls/cost and correctness. Unknown usage/cost is unknown, not zero.
- Update the gap and parity evidence with the change. Do not mark a feature done for a mock, a source-existence assertion, or a happy-path-only test.
