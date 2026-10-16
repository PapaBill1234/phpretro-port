# OC2-C02 — reviewed local chain and qualification boundary

Status: **reviewed local evidence only**. The cumulative candidate at
`46dac62` contains the exact independently accepted offline slices below. This
does not qualify native request enforcement, live dispatch, recovery, spend
control, or integration. See [OC2-C01 candidate provenance](opencode-local-candidate.md)
for the earlier combined candidate; this document extends rather than replaces
that history. PHPRetro remains paused; Eliza orchestration and the custom native
subscription bridge remain retired.

Task OC2-C02 is docs-only, claimed on branch `build/oc2-c02-reviewed-local-chain`
from base `46dac62ea5340dc92ac050a574979ac95e0020d8`. No code, credentials,
account settings, runtime configuration, or model/provider routing is changed.

## Provenance and accepted local slices

| Slice | Accepted source | Scope and boundary |
|---|---|---|
| OC2-M06-R1 | `337a12a` | Read-only advisory reconciliation of evidence snapshots with local claims. No native termination proof, mutation, release, or prompt replay. |
| OC2-M06-R2 | `3761391` | Durable caller-attested claim/session binding and private read-only reader. Does not verify native identity, server incarnation, or termination. |
| OC2-M06-A09a | `7eb8e1b` | Local command receipts and bounded process supervision. Caller-supplied command; receipt is not proof of feature correctness or an independently attributed failure. |
| OC2-M06-A09b | `46dac62` | Reverified local receipts plus explicit reviewer outcomes and stable run-evidence deduplication. Local ledger/API only; no authenticated reviewer identity, dispatch, release, or retry automation. |

These are cumulative in the current base. The coordinator reports that each
slice and its corrections received independent review; the complete focused
suite was rerun at `46dac62`: **101 tests passed** in 11.774 seconds. `repo-check`
and the full task diff check passed there as well. These checks establish local
behavior only, not A07–A12 live acceptance.

## Parked supported-contract blocker

OC2-M06-R3 remains parked at `3761391`, with no source changes. The pinned
OpenCode source is `042d0832f5f3a73eaa039a58a7d565776f6d4290`:

- `packages/client/src/promise/service.ts:124–127` calls service discovery;
- `packages/client/src/service-probe.ts:49–57` performs an authenticated
  probe before the caller's endpoint guard can run.

PID/version bracketing is only advisory and does not establish UUID continuity;
it cannot supply the required guard before that probe. No preflight/custom-fetch
mechanism was found or added. Proceed only with a supported upstream/native
contract or upstream change. Do not parse secrets, add an auth bridge, or infer
that a post-probe check prevents credential-bearing requests.

## What can qualify next

The endpoint-guard blocker cannot be qualified by a mock or a fixture of our
own wrapper. A bounded, credentials-free CLI smoke/fixture check may be possible
with the existing CLI and test harness, without new code or a costly framework,
but no supported fake-service contract has yet been established. First verify a
documented CLI contract and an observable negative case; if none exists, stop
and request the upstream/native contract rather than inventing a route. Any such
fixture qualifies only its tested local CLI behavior. It cannot close the
pre-request endpoint guard, native session termination, request cancellation,
native restart/recovery, dispatch, quota enforcement, or spend gates. Do not
claim qualification until a real supported route passes declared positive and
negative acceptance checks.

## Current blockers and evidence

- Native endpoint identity/pre-request guard and transport cancellation remain
  blocked on a supported contract/upstream change (A04–A06).
- Native four-worker admission, session restart/recovery, and termination-proof
  release remain open (A07–A08). Native restart is explicitly at-least-once in
  `packages/core/src/session/execution/restart.ts:37–61`.
- A09's local receipts/outcome API are not native acceptance-run verification,
  authenticated review, or real Sol escalation.
- Automated quota cutoff/checkpoint enforcement remains open. Existing multi-auth
  reporting is complete and a successful `openai/gpt-6-luna#high` turn is verified;
  neither establishes automated enforcement. Do not reopen completed reporting
  or authentication work.
- Spend enforcement, autonomous paid dispatch, redacted GitHub progress/CI, and
  integration remain open. GitHub CLI has no authenticated host; human login is
  needed before any push/CI attempt. Main is untouched.
- Token/cached-token use and cost are unavailable; unknown, not zero. No savings
  claim. Do not consume reset credits. PHPRetro remains paused.
