# OC2-M06-R1 offline reconciliation evidence

`scripts/opencode-reconcile.py` is an offline, read-only adapter. It compares
the existing private ledger's active/quarantined claims with a caller-supplied
version-1 JSON snapshot and checks the ledger-bound worktree's owned paths using
`git --no-optional-locks status`, with inherited `GIT_*` variables removed,
system/global Git config disabled, explicit local overrides for fsmonitor and
submodule recursion, and owned paths passed as literal pathspecs. It does not
query an OpenCode server or interpret
provider/session payloads. The raw native OpenCode evidence adapter remains
**open**; the V2 API reference documents session routes, but this task does not
assert their semantics prove termination.

Evidence input has exactly this top-level shape:

```json
{
  "version": 1,
  "generated_at": "2026-10-16T12:00:00Z",
  "claims": [
    {
      "task_id": "opaque-task-reference",
      "claim_id": "opaque-claim-reference",
      "observed_at": "2026-10-16T12:00:00Z",
      "session": {"state": "active", "session_id": "opaque-session-reference"}
    }
  ]
}
```

Session states are `active`, `idle`, `missing`, or `reserved`; reserved requires
`session_id: null`. This data is caller attestation, not authenticated or
server-verified proof. The exact task/claim bindings must match every live
ledger row; duplicate, missing, extra, malformed, stale (over five minutes),
or future-dated evidence fails closed. The timestamp order is
`observed_at <= generated_at <= now`; both observation and generation must be
fresh. A non-null session ID cannot bind multiple claims. Those IDs remain
caller-attested and do not constitute verified native bindings. The clock value
must be finite and non-negative. The ledger is opened via SQLite
`mode=ro` and `query_only`; existing claims/schema are required (the adapter
does not initialize an absent ledger). Results omit raw task/session IDs and
filesystem paths and are stable for identical inputs and clock value.

`quarantine_review` and `unresolved_review` are recommendations only. This
tool never quarantines/releases claims, replays prompts, starts/stops sessions,
deletes or repairs files, or writes evidence. Active sessions, idle/missing
sessions, reserved tasks, expired leases, and dirty/missing worktrees cannot
establish termination or safe release. Dirty worktrees are preserved.

Local synthetic coverage exercises reserved/active/idle/missing sessions,
binding and freshness failures, expired leases, dirty/missing/symlink worktree
boundaries, deterministic repeats, and unchanged files. It is not live
qualification and does not establish native OpenCode session lifecycle
semantics. Linked-worktree tests exercise the real worktree gitfile and linked
index to ensure status does not refresh index metadata. Token usage/cost is
unavailable here (unknown, not zero).

## OC2-M06-R2 offline durable claim-to-session binding

The private SQLite ledger has an additive `session_bindings` table and two
explicit coordinator APIs: `bind_session(...)` and the bounded private reader
`get_session_binding(...)`. The write atomically matches exact task and claim
IDs to an active, unexpired claim with the same profile, then records opaque
session ID, project ID, profile, caller-attested server-scope identifier, a
stable receipt, and creation clock. No worktree or branch can be supplied by
the binder; those remain the claim's ledger-owned values. No claim, lease,
release, or outcome state is changed. Missing bindings remain unknown.

Exact replay returns its original durable receipt without refreshing its
timestamp. Conflicting task/claim rebinding is rejected. A server-scope/session
pair remains unique across profiles and historical released claims, preventing
automatic reassignment. A different scope is distinct only as a caller-supplied
configuration label; it is **not** proof of a different process or a new server
incarnation. The scope and session identity are coordinator attestations, not
authenticated OpenCode evidence. Future native collector work must verify the
native project/session correspondence and establish authoritative server-scope
identity independently. Restart recovery remains at-least-once; a binding does
not prove termination, safe prompt replay, or live recovery qualification.

The APIs perform no auth, quota, plugin, provider, server, or native API calls.
They do not launch/stop sessions or issue prompts. The raw read API is private
ledger access and the CLI intentionally provides no raw binding output. It
opens only an existing private database in SQLite read-only/query-only mode;
an absent database or legacy database without the bindings table returns
`None` (unknown) without initializing or migrating anything. Unsafe database
files produce a bounded `LedgerError`. Schema migration is performed only on
the write path. New bindings require an exact int/float non-negative finite
clock not earlier than the claim heartbeat; booleans and numeric strings are
rejected. An identical existing binding is historical receipt retrieval, not a
new admission: it returns the original receipt even if the claim has since
expired, and does not alter timestamps or claim state. Tests cover read-only
filesystem/DB preservation, legacy unknown then write-path migration, clock
validation/order, additive migration with existing claim/outcome rows,
persistence, replay, reject paths, cross-profile session collisions,
different-scope semantics, concurrent insertion, and rollback. No dispatch or
native binding verification is claimed.
