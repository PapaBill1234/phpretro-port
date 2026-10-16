# OpenCode 2 migration gaps and acceptance

Current combined local status: [OC2-C01 evidence](opencode-local-candidate.md)
supersedes historical review-pending worker receipts for the exact reviewed
offline code. Live acceptance gates remain open; no main integration or CI.

Task: OC2-M01. Updated: 2026-10-15. Baseline `d094160`, OpenCode 2.0.29.
This is the **current builder migration** checklist. Historical CMS/Eliza
evidence in `gaps.md` is not authorization to resume those workstreams.
Architecture: [migration plan](opencode-migration-plan.md).

States: implemented/local means code or a fixture exists; qualified/live means
the real route passed declared acceptance. Neither source existence nor this
checklist is a live acceptance result.

## Current state

- [x] Official pinned CLI bootstrap and standalone profile launcher exist.
  Evidence: `scripts/opencode-builder.py`, `tests/ops/test_opencode_builder.py`;
  handoff records version/help and three tests. No reinstall required.
- [x] Two isolated account HOME/XDG layouts implemented locally. Full live
  account/server isolation is still open below.
- [x] Plan/checklist drafted against immutable 2.0.29 contracts S1–S8 and V2
  docs. This closes only plan creation, not architecture review or migration.
- [x] Independent Sol high-level architecture review OC2-M01-R1 completed.
  Native-hook mechanism and live dispatch remain unapproved; no runtime lock.
- [x] Initial manual selected-account quota evidence received: operator reports
  50% five-hour and 70% weekly at 2026-10-15T19:56:45Z. Used/remaining basis is
  unspecified; either interpretation clears the 10% reserve (conservative
  remaining bounds 50% and 30%). Reset times remain unknown. This admits the
  bounded audit/review, not indefinite dispatch or an automated quota guard.
  Saved Desktop windows do **not** satisfy selected-account evidence. Native
  UI source S8 links to `https://chatgpt.com/settings/usage` for manual checking;
  the browser must use the same account as the selected OpenCode connection.
- [ ] Native manual login and real requested Sol model turn, per profile.
  The current coordinator session alone does not qualify the launcher route.
- [x] One observed Luna 6 high delegated source audit completed using
  `openai/gpt-6-luna#high`. This proves that bounded child turn only.
- [x] OC2-M04a offline SQLite claim/admission core implemented and locally
  tested. This does **not** qualify live four-worker admission, native recovery,
  or acceptance-failure escalation; see evidence below.
- [x] OC2-M04b offline typed outcome ledger and idempotent escalation-intent
  recording implemented and locally tested. This does **not** qualify A09 live
  acceptance evidence or dispatch; see evidence below.
- [ ] Per-profile launcher/delegation and restart recovery qualified; automated
  allowance guard and measured savings remain unqualified.
- [ ] Native DeepSeek credentials, actual Flash model ID and real route.
- [ ] DeepSeek-only schedule enforcement before every request and at cutoff.
- [ ] Shared max-four workers qualified with real bounded workers and scoped
  worktrees. OC2-M04a has local-only four-slot/path-conflict evidence; A07 stays
  open until native dispatch qualification.
- [ ] Durable task receipts/recovery and three-acceptance-failure escalation.
- [ ] Selected-account reserve checkpoint and manual profile-switch recovery.
- [ ] Real configured spend enforcement. No cap is currently verified.
- [ ] Redacted, idempotent GitHub progress and independently reviewed CI.

## Acceptance matrix (all unperformed unless explicitly noted)

| Gate | Required evidence / negative cases | Assigned slice |
| --- | --- | --- |
| A01 Native identity/model | Human `/connect` in each profile; exact selected provider/model/variant; successful native Sol turn. No token import, logout, silent fallback or auth transcript in Git. | M03/M07 |
| A02 Luna delegation | Verified catalog reference plus successful bounded Luna-high child receipt; no substitute model; quota checked first. Capture actual usage when available, otherwise unknown. | M03/M07 |
| A03 Profile isolation | Separate stores and standalone servers; profile A cannot see B's sessions/connections; repeat after restart and from worker worktrees. Wrong-profile quota rejected. Keep private IDs private. Existing environment tests alone are insufficient. | M03/M07 |
| A04 Request denial | Fake server records zero denied sends for all four request kinds; malformed/missing schedule/guard fails closed. Prove supported blocking contract, not a hook exception assumption. | M02/M05 |
| A05 Schedule boundaries | Canonical +5 opening and -30 closing, exact edges; initial request, retries after waits, active stream, stalled headers/body, suspend/clock jump and reconnect. Server observes cancellation; no send/continued stream after cutoff. Local accelerated-clock and real native-route evidence both required. | M05/M07 |
| A06 Provider isolation | DeepSeek blocked/cancelled while concurrent Sol/Luna requests succeed; no global process kill. Audit direct generation, title/compaction, aliases, SDK routes and missing-plugin bypasses. | M05/M07 |
| A07 Four-worker admission | Concurrent primary/secondary claims share one ledger; fifth claim denied; reserve-before-prompt, duplicate claims and overlapping paths rejected. Verify with four real bounded workers after fake race tests; interruption cannot leak a slot. | M04/M06/M07 |
| A08 Recovery | Restart coordinator/server during reserved, prompting, running and completed tasks. Reconcile without duplicate prompt/commit; ambiguous/live stale claims quarantine, not automatic release. Preserve dirty worktrees. | M04/M06/M07 |
| A09 Escalation | Durable acceptance attempt IDs; three real implementation acceptance failures request Sol once and block routine retry. Duplicate receipt no double count. Funding/quota/time/turn budget/schedule/infrastructure outcomes never increment. Also test two-identical-failure stop. | M04/M06/M07 |
| A10 Quota reserve | Every actually reported applicable window checked; <=10% remaining checkpoints/stops dispatch. Missing/stale/wrong-account/invalid metadata blocks; no invented daily allowance. Fresh verified metadata required after reset; switching remains manual. | M03/M07 |
| A11 Spend | Configured real cap and enforcement owner; concurrent/retry reservations and unknown usage cannot bypass. Provider balance/estimated USD not accepted. If enforcement cannot be proven, keep autonomous paid dispatch blocked and gap open. | M06/M07 |
| A12 Review/progress | Scoped diff, actual acceptance commands and exit status, independent Sol review, task-ID commit and worker-branch push; CI before integration. Redacted GitHub update retry is idempotent. Never merge main automatically. | Every slice/M07 |

## Dispatch queue and current blocker

1. **OC2-M01**: docs-only proposed architecture and checklist; offline validation
   permitted. Owned paths: these two files, `docs/opencode-builder.md` and
   a current-status pointer in `docs/gaps.md`. No runtime/config changes.
2. **OC2-M02**, preferred `openai/gpt-6-luna#high`: bounded pinned-contract
   audit/probe design, no auth/key access and no provider inference probes.
   Return source locations for denial and cancellation, bypass inventory and
   smallest fake-provider tests; <600 words, save larger evidence separately.
   **Read-only Luna audit completed**, based on fresh manual quota evidence;
   contract gap remains blocking. No runtime probe or implementation performed.
3. Independent Sol review OC2-M01-R1 completed: high-level decomposition approved,
   but native request enforcement and paid dispatch remain blocked. M04a offline
   admission core may proceed independently of M02/M03 runtime qualification.
4. OC2-M04a offline core is implemented on its task branch; no provider calls,
   native session access, dispatch adapter or process control was used. A07–A09
   and independent review/integration remain open.

No paid DeepSeek request was dispatched. Luna audit used the native OpenAI route.
Runtime qualification, spend protection and account allowance remain unknown.
PHPRetro stays paused; Eliza and custom subscription bridge stay retired.

## OC2-M01 offline validation

- `python3 -m unittest discover -s tests/ops -p test_opencode_builder.py -v`:
  **3 passed**, 2026-10-15. Existing launcher regression checks only.
- Documentation reference definitions and **20 local links**: passed.
- `git diff --check`: passed.
- Independent review, CI, runtime changes and live qualification: **not run**.
  This is a proposed-plan checkpoint, not an integration approval.
- Token/cached-token usage and cost: unavailable in this tool interface;
  unknown, not zero. No measured savings claim.

## OC2-M02 read-only Luna audit evidence

Completed 2026-10-15 using requested `openai/gpt-6-luna#high`. Independent source
audit, not runtime acceptance; no edits, tests or provider probes by the worker.

- S4 `hooks.ts:21–26`: no typed session-hook rejection contract.
- S5 `model-request.ts:337–363`: hook precedes send, but request conversion and
  response wrapping do not establish abort-signal propagation or underlying
  stream cancellation. A replacement signal or hook exception is not a
  qualified guard. Session interruption still needs scoped cancellation proof.
- S6 `retry.ts:86–87`: explicit retry denial is supported; it does not solve
  initial-send denial or active-stream cutoff.
- S8 native dialog links the usage page. S1 and the inspected V2 API/CLI docs
  expose no allowance reader. Session usage S7 remains token/cost accounting.
- Next: narrow independent Sol architecture review, then local-only probes or
  upstream-supported design work. DeepSeek autonomous dispatch stays disabled.
- Probe design: zero denied arrivals for all four request kinds; disconnect
  during pending headers, streaming and stalled body; retry across cutoff;
  concurrent Sol/Luna non-interference and bypass tests. **Not executed.**
- Actual worker token/cached-token usage and cost unavailable: unknown.

## OC2-M01-R1 independent Sol review

High-level design approved, not runtime qualification. Applied corrections:
combined quota/schedule/guard/admission/spend gate before any autonomous paid
send; no optimistic release on unknown spend reconciliation; offline M04a may
proceed without unqualified native contracts. Remaining runtime policy work:
manual quota observation/verification timestamps, connection attestation,
freshness/future-time rejection; uncertain exit attribution stays unresolved.
Classification and deduplicated escalation remain a separate M04b slice.
Next owned paths for M04a: `scripts/opencode-ledger.py`,
`tests/ops/test_opencode_ledger.py`, this checklist and migration plan.
Review performed no tests/probes; actual token usage/cost unknown.

## OC2-M04a local admission-core evidence

Implemented with the bounded paths above. The ledger requires an explicit
existing absolute private state root, shared by callers across profile and
worktree locations. SQLite `BEGIN IMMEDIATE` serializes claims; four active or
quarantined claims consume the total capacity. Duplicate task IDs, traversal,
symlink aliases and overlapping canonical owned paths (including parent/child)
are rejected. Expired leases are durably quarantined and never free capacity.
Release requires explicit structured termination evidence, but this offline
core cannot verify that native OpenCode actually terminated; this is only a
caller attestation. No native session/dispatch integration, process killing,
worktree cleanup, or automatic release is present.

- `python3 -m unittest discover -s tests/ops -p test_opencode_ledger.py -v`:
  **10 passed** on 2026-10-15. Eight real child processes race on initial DB
  creation under umask 022, with distinct synthetic branches/worktrees; exactly
  four entered and four were denied, and the DB is mode 0600. Cross-profile
  active/quarantined branch/worktree conflicts are covered. An existing 0644
  DB is rejected without repair. A child process exits after its insert but
  before commit; SQLite exposes no partial claim.
- `python3 -m unittest discover -s tests/ops -p test_opencode_builder.py -v`:
  **3 passed** (launcher regression).
- `git diff --check`: passed.
- Evidence is LOCAL only. A07 live worker admission, A08 native restart/session
  reconciliation, and A09 failure classification/escalation remain open.
  Sol's review of `1665791` identified branch/worktree isolation and DB
  initialization mode races; this correction addresses both. Commit `3699b01`
  was independently Sol-reviewed per the assigned M04b handoff; review evidence
  is recorded in parent checklist commit `899e639`. M04b is separately
  implemented below; its independent review and CI have not run.
- Token/cached-token usage and cost unavailable in this tool interface;
  unknown, not zero.

## OC2-M04b local outcome-core evidence

The ledger records bounded typed outcomes against a durable task claim. Counted
acceptance failures require caller-attested acceptance execution and
implementation attribution, plus opaque evidence/signature labels. Unknown or
bare-exit outcomes stay unresolved. Funding is a durable paused indicator;
quota, schedule, infrastructure, model unavailability, elapsed time, turn
budget and interruption outcomes never count as acceptance failures. Success
does not erase prior failures.

Attempt IDs are immutable and globally unique; exact replays return the original
receipt, while conflicting payloads and cross-task claim bindings are rejected.
Acceptance evidence references are unique run IDs across pass/failure receipts;
reuse across attempts, outcome kinds, or tasks is rejected by a transactional
SQLite unique index. Status survives process restart and is read from one
consistent SQL snapshot. The second identical failure blocks unchanged routine
retry; the third counted failure persists one Sol-help intent and blocks
routine retry. This intent is not dispatch, and recording an outcome never
releases a claim. Evidence is caller-attested; the ledger cannot prove
acceptance ran or attribution is true.

- `python3 -m unittest discover -s tests/ops -p 'test_opencode*.py' -v`:
  **27 passed** on 2026-10-15. Includes typed positive/negative outcomes,
  excluded interruptions, bare-exit/unresolved handling, strict type checks,
  immutable replay/conflict, task binding, two-identical stop, three-failure
  one-intent durability, funding pause, restart, concurrent duplicate-evidence
  rejection, and a deterministic status-snapshot interleaving; existing
  admission/launcher regression tests also passed.
- `git diff --check`: passed.
- A09 remains open: no real acceptance runner/receipt verifier, native dispatch,
  actual Sol help request, or live qualification. Independent M04b review and CI
  have not run. Usage and cost unavailable; unknown, not zero.

## OC2-M06-R1 offline reconciliation adapter

The read-only adapter is documented in [recovery evidence](opencode-recovery-evidence.md).
It matches exact task/claim bindings in an explicit versioned evidence snapshot
against existing active/quarantined ledger rows, checks ledger-bound owned paths
without following worktree symlinks, and emits only advisory review
recommendations. Idle, missing, reserved, or active session state and expired
leases never establish termination or safe release. No ledger mutation, prompt
replay, server call, process control, deletion, or worktree cleanup occurs.

This remains local synthetic evidence, not native session reconciliation or
live qualification. The raw native session evidence adapter and authoritative
termination contract remain open; refer to the evidence document for scope and
limitations. The official [V2 API reference](https://opencode.ai/v2/docs/api)
lists session endpoints, but no termination semantics are inferred from that
listing. Local checks: `python3 -m unittest discover -s tests/ops -p
'test_opencode*.py' -v` (48 passed); `python3 scripts/repo-check.py` and
`git diff --check` passed. Independent review and live qualification remain
pending. Token usage/cost unavailable; unknown, not zero.

### OC2-M06-R1-F1 Sol review corrections

Follow-up `6cb51c9` findings are covered by read-only Git status
(`--no-optional-locks`, sanitized `GIT_*` environment, literal pathspecs) and a
linked-worktree index-hash regression test. Evidence now rejects a session ID
shared by claims, non-finite/negative clocks, and observations older than
generation time. Native session identity remains caller-attested; no real
native binding or termination proof is claimed. The full OpenCode suite passed
(54 tests), as did `python3 scripts/repo-check.py` and `git diff --check`; this
remains local evidence, not live qualification.

### OC2-M06-R1-F2 Sol review correction

Snapshot timestamps follow `observed_at <= generated_at <= now`, with both
evidence timestamps subject to the freshness bound. Git status explicitly
disables local fsmonitor execution and submodule recursion/inspection in
addition to the optional-lock and inherited-environment safeguards. System and
global Git config are disabled; local config is bounded by explicit command-line
overrides, not claimed disabled. A synthetic local fsmonitor executable marker
test verifies the hook is not run.

## OC2-M06-R2 offline durable claim-to-native-session binding

Added an additive SQLite binding table and explicit offline coordinator
attestation/read APIs. Binding requires the exact existing active, unexpired
claim and matching profile. Replays preserve the original durable receipt;
conflicts and historical scope/session reuse fail closed, including across
profiles. See [recovery evidence](opencode-recovery-evidence.md) for the
caller-attested nature of IDs and scope, API boundaries, and the future need
for collector verification. This is not native binding verification, prompt
recovery qualification, dispatch, or proof of server incarnation/termination.
The full focused OpenCode suite passed (63 tests), as did
`python3 scripts/repo-check.py` and `git diff --check`. These local checks do
not qualify dispatch, native binding verification, or live recovery. Token
usage/cost unavailable; unknown, not zero.

### OC2-M06-R2 review correction

The private binding reader now uses an existing-file read-only/query-only
connection, does not initialize or migrate, and reports absent database/schema
as unknown. The write path alone migrates. Clocks reject booleans and numeric
strings, and new binding times cannot predate claim heartbeat. Identical replay
is historical receipt retrieval even after expiry; it does not renew admission.
Read-only filesystem-preservation, empty-root, legacy-schema, and clock-order
regressions are covered in the updated focused suite.

## OC2-M06-A09a local execution receipts

Added a standalone local acceptance-command runner and private, one-shot run
receipts. It checks the exact active/unexpired task claim and canonical
worktree using a read-only ledger snapshot, then reserves a unique run ID and
fsynced intent before launching one coordinator-supplied argv array without a
shell. Existing or interrupted run IDs cannot replay. Private bounded logs and
receipt hashes can be verified after process restart. Exit zero means only
`command_completed_zero`; nonzero is `unclassified_nonzero`; timeout,
interruption, output limit, capture and infrastructure outcomes stay unresolved.
The runner does not mutate outcomes or claims, and does not supply independent
implementation-failure attribution. It cannot prove that arbitrary coordinator
argv is safe, prevent concurrent claim release, or guarantee cleanup of
detached descendants. See [local execution receipt evidence](opencode-acceptance-evidence.md).

This is a local execution/evidence slice only, not A09 live/native qualification,
dispatch, a worker-admission reservation, feature acceptance, or a counted
implementation failure. Native discovery/auth, independent attribution, native
dispatch and live acceptance remain open. Focused/full-suite results are to be
recorded after implementation checks; token usage/cost unavailable, unknown,
not zero.
