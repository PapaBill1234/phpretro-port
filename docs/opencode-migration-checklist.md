# OpenCode 2 migration gaps and acceptance

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
- [ ] Independent Sol architecture review and architecture lock.
- [ ] Fresh selected OpenCode account quota evidence. Saved Desktop windows
  do **not** satisfy this. Unknown blocks worker and reviewer dispatch. Native
  UI source S8 links to `https://chatgpt.com/settings/usage` for manual checking;
  the browser must use the same account as the selected OpenCode connection.
- [ ] Native manual login and real requested Sol model turn, per profile.
  The current coordinator session alone does not qualify the launcher route.
- [ ] Real Luna 6 high delegation. Catalog lists
  `openai/gpt-6-luna#high`; request success and allowance remain unqualified.
- [ ] Native DeepSeek credentials, actual Flash model ID and real route.
- [ ] DeepSeek-only schedule enforcement before every request and at cutoff.
- [ ] Shared max-four workers and scoped worktrees.
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
   **Queued, not spawned: fresh selected-account quota unavailable.**
3. Independent Sol architecture review, then M03–M07 dependency order in plan.
   Do not start runtime implementation from an unreviewed proposed design.

No new worker/reviewer or paid DeepSeek request was dispatched by M01.
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
