# Autonomous builder operations

The desired process is implemented in stages. Project instructions/skills and provider launchers are configuration; a fully enforced autonomous scheduler, integration controller and billing limiter remain tasks until their runtime tests pass. Do not confuse a Markdown instruction with an execution guarantee.

## Model roles

| Role | Verified provider ID target | Work | Default bound |
|---|---|---|---|
| Lead | `gpt-6.1-sol` | coordination, task decomposition and review; Luna implements suitable slices | one active lead; concise briefs; checkpoint at 18k context target |
| Workers | `gpt-6-luna` | discovery, docs lookup, mechanical/isolated slices | disjoint owned scopes and isolated worktrees; bounded implementation |
| Architect/reviewer | `gpt-6.1-sol` | auth/schema contracts, architecture, repeated failure and integration review | invoked at three checkpoints, not every search |
| Typed triage | Jev through OpenRouter | narrow choice/yes-no/score over supplied facts | one batch per relevant state, bounded excerpts |

Context targets are design budgets, not hard model limits. Enforce per-task request/output/time/price bounds through a runner before unattended implementation. Setup probes are small; there is no authorization to burn tokens merely to occupy available agents. The user authorized a6api use and repository commits, but unknown provider pricing cannot support a meaningful hard dollar guarantee. Record unknown costs and do not advertise savings or start an unlimited daemon.

The direct Messages endpoint has to pass text, SSE, tool result and actual Claude CLI probes. Set project-local model aliases to the verified IDs; do not force every subagent to Luna because that overrides the Sol reviewer too. Unsupported model-effort options are omitted rather than guessed. Preserve existing runtime STOP files.

## Task and Git lifecycle

1. Select the highest-priority unclaimed gap whose dependencies are verified. Create a task with exact base SHA, owned paths, evidence references, done criteria, checks, stop conditions and budget.
2. Claim it atomically in a private local task store. A claim has owner, lease and heartbeat; two CLIs may not own overlapping write paths. Expired leases require inspection of their worktree before reassignment.
3. Create a Git branch/worktree from the current integration base. Keep read-only researchers outside shared mutable build directories. Tests with mutable outputs also use task worktrees.
4. Worker performs the bounded slice, checks it and commits explicit files. Include task ID in the commit and a compact handoff. Push its feature branch when ready; all editing agents can make these commits.
5. Sol reviews the actual diff and its evidence. Two identical errors route here earlier. A substantive contract change goes back through architecture review.
6. The integrator combines reviewed work on an integration branch, resolves conflicts, reviews resolution changes and runs the required combined checks. Only then update main. Workers never push directly to main or force-push shared branches.
7. Update `gaps.md` and parity evidence with the accepted SHA. Release the claim and dispatch the next task. A partially working slice remains partial.

Use short task branches such as `codex/G03-ticket-consumption`. Worktree creation never copies credentials. Keep private runtime state in `.builder/` (ignored by Git). GitHub issues/PRs may mirror tasks, but avoid one API call per heartbeat.

## Brief and return format

```json
{
  "task": "G03-ticket-consumption",
  "base": "full SHA",
  "owned_paths": ["internal/polaris", "tests/contracts"],
  "evidence": ["docs/emulator-contract.md#authentication"],
  "acceptance": ["expired and replayed tickets fail at real consumer"],
  "checks": ["named concurrency and launch suite"],
  "stop": "second identical failure, budget exhausted, missing required contract",
  "budget": {"max_requests": 12, "max_output_tokens_per_request": 4096, "max_minutes": 25}
}
```

These starting limits are conservative operational choices; tune from accepted work, not intuition. Reports include `task`, `base`, `commit`, `paths`, `checks`, `evidence`, `open_gaps`, and `usage`, under 600 words. Keep large logs in private evidence storage with the useful error excerpt referenced by path.

## Token and cost observability

Each CLI run gets a run ID, task ID, role, worktree and provider model ID. Capture metadata-only provider usage: uncached input, cache-read, cache-write, output, attempts, elapsed time, exit status, check/review acceptance and rate card provenance. Keep unknown values null. Do not record prompts/tool bodies/keys in an OTEL exporter or public JSONL.

Native `/doctor` diagnoses installation/configuration and some local context overhead. The recent installed version can provide `/doctor prompt-audit`; this remains a local audit. A separate aggregate report joins run metadata and actual a6api billing records. Export/report first, then have Sol propose a narrow routing or skill change. Validate its effect on correctness and accepted cost.

Skills reduce repeated instruction loading by progressive disclosure. Stable short contracts and selective source retrieval matter more than aggressively shortening away invariants. Avoid a giant global CLAUDE.md, full-history forks, repeated complete test logs, or worker returns that reprint the source.

## Jev experiments

Batch independent judgments on one supplied state; use exact searches to reduce candidates first. Source-check important conclusions. No gate/permission use.

For a matched trial hold task/question, main model, settings, starting commit and correctness oracle constant. Run baseline without Jev and treatment with Jev. Report main input, cached input, output, wall time, Jev calls/usage/estimated cost, total estimated cost and answer correctness; include retries and rejected work. Use repeated trials before declaring savings. The initial research used Jev to select evidence; that is not an efficiency benchmark.

## Codex reserve and self-handoff

The user's 10% rule is interpreted as **10% remaining in the five-hour window** (`usedPercent >= 90` for a 300-minute window). Check before dispatch/review/integration milestones, not just after work. Account limits are shared across chats, so another session can consume reserve. Do not spend artificially until a threshold is reached.

When the reserve is reached: stop new Codex dispatch; checkpoint running workers safely; write a private `.builder/HANDOFF.md` and machine-readable waiting state with repo/base/branch/commits, verified work, active tasks, exact next commands, known blockers and observed reset timestamp. Keep credentials and transcripts out. The current local Codex continuation job checks every30minutes and skips when the quota reserve, readiness or project lock prevents dispatch. After a verified new window and restored allowance, resume the next bounded unfinished task once. Unknown usage does not imply a reset. Do not consume a reset credit automatically.

The current continuation cron is a local app job for this workspace and invokes the project runner through SSH. It needs its host/app available to execute, and it cannot guarantee an instantaneous threshold stop between checks. Active turns check at milestones. An OS cron with guessed token counters would not be authoritative.

Current project workflow: native Doctor and prompt-audit completed, all builders retain their shared native histories, and project-only session context is available via /port-context. Follow .claude/project-scope.md and private CURRENT-HANDOFF.md. Accepted revisions may overlap only an explicitly accepted prerequisite whose commit is present in the recorded base; concurrent claimed/submitted scopes remain exclusive. This does not mark a draft branch as merged into main.
