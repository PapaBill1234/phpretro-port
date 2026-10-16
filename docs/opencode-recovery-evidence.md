# OC2-M06-R1 offline reconciliation evidence

`scripts/opencode-reconcile.py` is an offline, read-only adapter. It compares
the existing private ledger's active/quarantined claims with a caller-supplied
version-1 JSON snapshot and checks the ledger-bound worktree's owned paths using
read-only `git status`. It does not query an OpenCode server or interpret
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
or future-dated evidence fails closed. The ledger is opened via SQLite
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
semantics. Token usage/cost is unavailable here (unknown, not zero).
