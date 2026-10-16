# OC2-C01 — reviewed local builder candidate

Current continuation: [OC2-C02 reviewed local chain and qualification
boundary](opencode-reviewed-local-chain.md). OC2-C02 consolidates the later
R1/R2/A09a/A09b local slices and records the parked native-contract blocker;
it does not change the scope or live-readiness status below.

Status: **local coexistence/regression checked, not live-qualified or merged
into main**. Continuous goal: qualify the native OpenCode 2 builder with Luna
routine work and Sol review. PHPRetro remains paused; Eliza and the custom
subscription bridge remain retired.

This document supersedes the historical "review pending" worker receipts in
the plan/checklist for the exact local code below. The coordinator's complete
source-audit/review history is on `build/oc2-m01-plan` at `3bcf1cb`.

## Provenance and independent review

| Slice | Reviewed source | Scope |
| --- | --- | --- |
| OC2-M04a | `1665791` corrected by `3699b01` | Offline shared four-slot SQLite claims; branch/worktree/path conflicts; quarantine and caller-attested release. |
| OC2-M04b | `6063b28` corrected by `54cc822` | Offline typed outcomes, unique acceptance-run evidence, idempotent replay, consistent status and durable escalation intent. |
| OC2-M03a | `ac20fd8` corrected by `2b342d9` | Offline manually attested quota evidence, conservative threshold decision and private checkpoint writer. |

Sol reviewed each implementation and correction, independently reproduced the
initial findings, and reran targeted tests. Luna authored the fixes. Source
evidence/cost is distinct from runtime acceptance; unknown tokens/cached tokens
and cost are not reported as zero or claimed savings.

Candidate code checkpoint: `9637215` on `build/oc2-local-candidate`.
M03a was cherry-picked onto reviewed M04b without conflict. Exact content checks:

```sh
git diff --exit-code 2b342d9 HEAD -- scripts/opencode-quota.py tests/ops/test_opencode_quota.py docs/opencode-quota-evidence.md
git diff --exit-code 54cc822 HEAD -- scripts/opencode-ledger.py tests/ops/test_opencode_ledger.py tests/ops/test_opencode_outcomes.py
```

Both pass: no code was changed during candidate assembly. Candidate assembly
owns this evidence document and documentation status pointers only.

## Combined checks

Executed locally, 2026-10-15:

- `python3 -m unittest discover -s tests/ops -p 'test_opencode*.py' -v`:
  **43 passed** — 3 launcher, 10 admission, 14 outcome, 16 quota tests.
- `python3 scripts/repo-check.py`: passed.
- `git diff --check d094160..HEAD`: passed.

The tests include real subprocess/SQLite contention and crash rollback, plus
deterministic identity/evidence/read-snapshot and private-path regressions.
They use synthetic state, not credentials or native account databases.

## Not qualified

There is no launcher/plugin/native-session wiring for these new cores. The
ledger requires one trusted canonical state root; caller-attested termination
is not proof of native session settlement. Outcome receipts do not run actual
acceptance or request Sol help. The quota helper does not measure provider
allowances, prove identity, authorize paid dispatch or automatically resume.
Its five-minute freshness rule is an offline policy candidate, not native
quota reporting. Read [the quota evidence contract](opencode-quota-evidence.md).

Open A04–A12 gates include supported before-send DeepSeek denial and pending/
stream transport cancellation, real four-worker admission and restart recovery,
live failure escalation, all-window selected-account quota checks, actual spend
enforcement and reviewed GitHub CI. Standalone process supervision alone is
not proven sufficient. The inspected 2.0.30 hook contracts do not establish a
fix; no installed-runtime upgrade was performed. DeepSeek remains disabled.

No push or GitHub CI has run: authentication is unavailable in the isolated CLI
environment. No main update, account action, reset credit or PHPRetro unpause
occurred. Before another agent batch, obtain fresh selected-account quota
metadata; before push/CI, the operator must authenticate GitHub manually. No
checkpoint or reset timestamp substitutes for fresh evidence.
