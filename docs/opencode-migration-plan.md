# OpenCode 2 builder migration plan — OC2-M01

Status: **high-level decomposition independently reviewed; runtime request
mechanism and live dispatch not approved**. Sol review OC2-M01-R1 approved
offline admission-core work with the boundaries below.
Baseline: `d094160`; runtime: official OpenCode **2.0.29**. Evidence checked
2026-10-15. The executable is already installed; reinstalling it is not a
migration milestone. Current gaps and acceptance are tracked in
[the migration checklist](opencode-migration-checklist.md).

## Scope and non-goals

Replace the builder coordinator with native OpenCode 2. Keep PHPRetro
CMS/themes/game development paused. Eliza orchestration, its native-subscription
bridge, old qualification hosts and V1 plugin research are retired, not repair
targets. Preserve checkpoints, unrelated `CLAUDE.md`, STOP files and runtimes.
Do not build a desktop client, authentication bridge or new credential proxy.
The historical checkout path stays stable.

No account login, logout, switch, reset credit or credential copying is
automated. Credentials, account identifiers, provider transcripts and private
handoffs stay out of Git. Public progress contains only redacted task evidence.

## Evidence and version boundary

Live V2 documentation can move ahead of the pin. Implementation must check
the tagged contracts and the installed server's `/openapi.json`, not infer V2
fields from the public V1 schema or reuse V1 server/plugin interfaces.

The official [`v2.0.29` tag][tag] resolves to
`042d0832f5f3a73eaa039a58a7d565776f6d4290`.
Use that immutable revision for source checks:

| Ref | Contract and observed implication |
| --- | --- |
| S1 | [`packages/core/src/plugin/provider/openai.ts`][s1]: native OpenAI OAuth methods, ChatGPT routing and a `gpt-6.1-sol` special case exist. Source support is **not** proof of this account's entitlement or a successful turn. |
| S2 | [`packages/ai/src/providers/deepseek.ts`][s2]: canonical provider ID is `deepseek`; native OpenAI-chat route defaults to `https://api.deepseek.com/v1`. A route ID named `deepseek-chat` does not establish the requested Flash model ID. |
| S3 | [`packages/plugin/src/promise/session.ts`][s3]: provider-scoped model/HTTP/retry hooks and mutable `Request`/`Response`; four request kinds: primary, title, compaction and generate. Retry exposes `{retry:false}`. |
| S4 | [`packages/core/src/plugin/hooks.ts`][s4]: provider filters match `event.model.providerID`; session hooks have no typed failure channel. Do not claim that throwing is a supported denial API. |
| S5 | [`packages/core/src/session/model-request.ts`][s5], lines 337–365: native request hook runs before `handler(sent)`, but converts Web Request to Effect request and reconstructs bodies. A replacement Request's abort signal is **not proven** to cancel transport. Response stream replacement exists, but must prove underlying cancellation. |
| S6 | [`packages/core/src/session/runner/retry.ts`][s6]: retry decision can be stopped by hook; transport retries are not durable task acceptance failures. |
| S7 | [`packages/core/src/session/usage.ts`][s7]: token accounting and calculated USD costs, not an account-allowance or spend-enforcement contract. S1 zeroes subscription catalog cost. Zero displayed cost is not unlimited quota. |
| D1 | [V2 migration guide][migration]: server/plugin contracts break compatibility; retained supported configuration need not be rewritten wholesale. |
| D2 | [Plugin guide][plugins]: native HTTP hooks, scoped retries, session interrupt, storage and worktrees. HTTP hooks keep matching providers on HTTP. Direct `ctx.generate.text` is a separate path; it must not become a DeepSeek bypass. |
| D3 | [Policies][policies]: `provider.use` is static availability control, not a clock or cash limit. Policies do not sandbox plugin code. |
| D4 | [Providers][providers], [API][api], [client][client]: native account/model discovery and V2 client. Current public OpenAPI search exposed no quota/allowance endpoint; absence here is not proof that provider metadata cannot report it. Verify the installed contract and selected connection. |
| S8 | [`packages/app/src/providers/connect/chatgpt-usage-limit.tsx`][s8]: the native usage-limit dialog opens `https://chatgpt.com/settings/usage`. This provides an operator-facing check, not an automated quota API. |
| L1 | `scripts/opencode-builder.py`, `tests/ops/test_opencode_builder.py`: two isolated HOME/XDG profiles and standalone launches; three existing tests, no schedule/admission/spend enforcement. |
| L2 | `scripts/coding-window.py`: canonical UTC decision, +5-minute opening / -30-minute cutoff, concrete `stop_at`, conservative unknown-year handling. Reuse it; do not duplicate its holiday calendar in TypeScript. |

## Proposed architecture

### 1. Accounts, models and dispatch roles

Keep `primary` and `secondary` as two manually selected **account profiles**,
not model/provider roles. Retain the existing private HOME/XDG and standalone
server boundaries. Anchor private stores to the canonical checkout so workers
do not accidentally create alternative profile universes from worktrees.
Inspect ancestor/global configuration as well as project configuration.

Use native `/connect` and `/models` per selected profile. Record only safe
provider/model/variant IDs and timestamped acceptance results. Never resolve
credentials into prompts or copy existing Codex tokens. An actual native turn
is necessary; a model appearing in a catalog is insufficient.

- Sol coordinates architecture, important decisions and independent review.
- Prefer Luna 6 high for source audits, test writing, documentation, ordinary
  implementation and evidence summaries after qualification.
- Up to four bounded direct DeepSeek Flash workers, **only after** request
  guards and admission are qualified. Never put a Sol/Luna model ID under
  DeepSeek. No guessed Flash alias and no silent fallback.
- Catalog discovery in this session lists `openai/gpt-6-luna` with `high`.
  `openai/gpt-6-luna#high` is a candidate for delegation, not proof of a
  successful delegated turn or separate quota savings.

### 2. Quota and private continuation

Before new dispatch, review or integration, require fresh verified metadata
bound to the **selected OpenCode account/profile and connection**. Check every
actually reported applicable allowance. At <=10% remaining in any applicable
allowance: checkpoint, stop new dispatch and hand off privately. Unknown,
malformed, stale or wrong-account data blocks dispatch. A reset timestamp
passing does not independently verify replenishment.

Preserve window durations/identifiers as reported; 300 and 10080 minutes do
not mean daily allowances. Never promote Desktop Codex figures to OpenCode
selected-account evidence. Provider token counts, model catalog visibility,
positive cash balances and subscription cost `[]` are not quota evidence.

First qualify the supported source of this metadata. If V2 exposes no suitable
native metadata contract, document the gap and require freshly verified manual
selected-account evidence from the usage page linked by S8; the operator must
verify the browser account matches the selected OpenCode connection. Do not
recreate a subscription bridge or declare
an autonomous quota guard complete. A proposed freshness bound of five minutes
for admission must be reviewed and recorded with its source semantics.

Private state needs profile, safe connection binding, observation time, original
windows, decision, last task receipt and resume instructions. Never activate
another credential or spend a reset credit. Report unavailable usage as unknown.

### 3. DeepSeek request-boundary scheduling

Prefer a minimal V2 plugin using native provider-scoped hooks, **subject to
OC2-M02 proving fail-closed blocking and cancellation**. A launch-time check,
prompt instruction, model catalog transform or permission gate is insufficient.

For every DeepSeek request, including title, compaction, generate and retry:

1. Run canonical `coding-window.py` with bounded execution and parse its
   decision. Missing script, invalid JSON, nonzero failure, missing/expired
   cutoff or clock uncertainty blocks sending.
2. Block before the network send when closed. Recheck immediately before
   every retry; disable a retry whose wait would cross cutoff.
3. Arm a deadline for pending headers **and the entire active response body**.
   Cancel underlying transport at cutoff, even for stalled streams. Recheck
   wall time after suspend/resume. Dispose timers and readers on all exits.
4. Affect only canonical DeepSeek requests. Sol/Luna HTTP/WebSocket calls
   remain unaffected. Unknown aliases/packages that might reach DeepSeek
   are disallowed in the supervised worker route until audited.

S4/S5 prevent assuming that `throw` or `new Request(...,{signal})` is enough.
The first probe must demonstrate no network send when denied and server-side
disconnect at cutoff. Qualify session interrupt against all request kinds and
concurrent sessions; cancelling the whole mixed-provider server is unacceptable.
Do not deploy speculative hook code if these contracts cannot support it.
Escalate to a small upstream-supported V2 change/design review, not an auth or
proxy rebuild. Keep DeepSeek autonomous dispatch disabled meanwhile.

Direct plugin generation, AI-SDK routes, custom endpoints, shell HTTP clients
and missing-plugin startups are explicit bypass tests. The supervised route
must fail closed if the guard is absent; it is not a sandbox for arbitrary
programs or hostile plugins. Do not claim machine-wide egress enforcement.

### 4. Shared four-worker admission and isolation

Use one canonical private transactional ledger (candidate: stdlib SQLite),
shared across both profiles and all servers. Per-plugin storage is not proof
of cross-profile atomic admission. Atomic claims enforce **four active workers
total**, not four per profile; reviews must not become uncounted parallel
workers. Coordinator itself is not an editing-worker slot.

Each task has ID, branch/worktree, owned paths, selected provider/model,
acceptance commands, bounded time/output budget, lease/heartbeat, session ID
and durable receipt. Reject overlapping path scopes (including parent/child
paths), duplicate task claims and unregistered worktrees. Native worktree
operations handle creation; the ledger handles admission. Permissions and
diff scope checks are defense in depth, not an OS filesystem sandbox.

Reserve before dispatch; persist the session binding before prompting. On
restart reconcile ledger, sessions and worktrees. An expired heartbeat alone
must not free a slot while inference/tools may still run. Ambiguous claims
stay quarantined until absence/termination is verified. Never delete dirty
worktrees automatically or replay an ambiguous prompt as a new task.

### 5. Acceptance failures, reviews and progress

Count a failure only after real declared acceptance ran and failed because of
the task's implementation. Persist unique attempt/evidence IDs; repeated
processing cannot increment the counter. After three such failures, block the
routine route and request Sol help with a concise source-backed root cause.
Stop unchanged retries after two identical failures.

Funding, quota, schedule cutoff, infrastructure, model unavailability, elapsed
time and turn-budget interruptions are non-acceptance outcomes. Funding errors
pause affected dispatch; they do not trigger coding-failure escalation.
Partial results remain recoverable and are never marked accepted.

Use one task/branch/worktree per editing worker. Parent receives <600-word
receipts: changed paths, evidence references, checks, commit, gaps and usage.
Reviewers receive bounded diffs and evidence pointers, not full provider logs.
Sol review is required before architecture lock and integration. A self-check
does not replace independent review. Push worker branches only; integration
requires independent review and CI, never an automatic merge into main.
GitHub task progress must be idempotent and redact private evidence.

### 6. Spend enforcement and Jev

There is **no verified live spend cap**. Do not equate positive balance, token
estimates or displayed USD with one. Before paid autonomous work, select and
qualify a real enforcement mechanism. Candidate local reservation accounting
requires a configured cap, bounded maximum input/output cost including retries,
atomic cross-worker reservations, conservative unknown-usage handling and
reconciliation. If the provider cannot guarantee bounds, label it an estimate
and keep autonomous paid dispatch blocked unless a real provider-side cap is
verified. No arbitrary cap amount is invented in this plan.

Before any autonomous paid send, require **selected-account quota, schedule
and guard health, shared worker admission, and verified spend enforcement
together**. A separate spend-design task is not permission to dispatch first.
Unknown reconciliation never releases spending capacity optimistically.

Jev remains optional typed judge-only over small already retrieved facts; never
a permission/admission gate. Handle unavailable/escalate locally. Do not claim
token savings without matched same-task/model measurements (input, cache,
output, time, Jev calls/cost and correctness).

## Bounded implementation order

Each row gets its own task/branch/worktree, declared paths, tests and receipt.
Dependencies are gates, not permission to launch all rows concurrently.

| Task | Owner preference / bounded scope | Gate to proceed |
| --- | --- | --- |
| OC2-M01 | Sol: this plan, gap/checklist and documentation links only | High-level Sol review completed; runtime enforcement mechanism remains unqualified. |
| OC2-M02 | Luna-high: read-only pinned-contract audit and local fake-provider probe design; Sol reviews blocking/cancellation conclusions | Fresh selected-account quota; exact supported denial/cancel mechanism proven in 2.0.29. No paid DeepSeek request. |
| OC2-M03 | Luna-high: quota metadata acquisition/validation and private checkpoint tests | Supported source identified; wrong/stale/missing account and reserve tests; native manual login/model/delegation evidence. |
| OC2-M04 | Luna-high: canonical shared task ledger and atomic admission, no provider dispatch | Concurrent claims across profiles, path overlap, crash/restart/quarantine and failure classification tests. |
| OC2-M04a | Luna-high: offline SQLite claim/admission core only; no native sessions or dispatch | May proceed independently of M02/M03 runtime qualification after fresh quota authorization; targeted concurrency/path/crash/quarantine tests and independent review. |
| OC2-M05 | Luna-high: native DeepSeek-only request guard | M02 architecture reviewed; fake transport proves deny/retry/active cutoff; Sol non-interference; fail-closed plugin startup. |
| OC2-M06 | Luna-high: V2 session/worktree dispatch adapter, receipts and escalation | M03–M05 reviewed; four-worker supervision and restart acceptance; verified spend enforcement before any autonomous paid send. |
| OC2-M07 | Luna-high: real-route acceptance evidence and redacted GitHub progress; Sol independent final review | Verified live route, spend protection, all checklist gates and CI. No PHPRetro unpause inferred. |

Luna work starts with M02 once quota permits; don't spend Sol tokens on routine
editing/tests. Keep evidence summaries small. Routine delegation is a cost
strategy, not a measured savings claim. No further routine-choice questions are
needed, but missing account metadata, manual sign-in, actual cap configuration
or unavailable requested models are honest blockers.

## Rollback and release

Keep new enforcement non-dispatching until reviewed. Rollback means disable
new autonomous dispatch, checkpoint and preserve receipts/worktrees; do not
restore Eliza or revert unrelated user changes. A partial rollout cannot remove
the safety guard while leaving DeepSeek workers active. No fresh worker claims
after a failed guard health check.

The builder migration is ready only when all live acceptance gates in the
checklist have evidence, review and CI. Fixtures alone cannot close them.
PHPRetro remains paused until the agreed migration release and explicit scope
decision; no CMS feature is marked complete by this work.

[tag]: https://api.github.com/repos/anomalyco/opencode/git/ref/tags/v2.0.29
[s1]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/core/src/plugin/provider/openai.ts
[s2]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/ai/src/providers/deepseek.ts
[s3]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/plugin/src/promise/session.ts
[s4]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/core/src/plugin/hooks.ts
[s5]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/core/src/session/model-request.ts
[s6]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/core/src/session/runner/retry.ts
[s7]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/core/src/session/usage.ts
[s8]: https://github.com/anomalyco/opencode/blob/042d0832f5f3a73eaa039a58a7d565776f6d4290/packages/app/src/providers/connect/chatgpt-usage-limit.tsx
[migration]: https://opencode.ai/v2/docs/migrate-v1
[plugins]: https://opencode.ai/v2/docs/build/plugins
[policies]: https://opencode.ai/v2/docs/policies
[providers]: https://opencode.ai/v2/docs/providers
[api]: https://opencode.ai/v2/docs/api
[client]: https://opencode.ai/v2/docs/build/client
