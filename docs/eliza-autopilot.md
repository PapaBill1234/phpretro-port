# Eliza off-peak autopilot (local queue)

Reviewed 2026-10-14. `scripts/eliza-autopilot.py` is a **trusted, host-owned local
queue** that drives the existing real Eliza companion's loopback builder API
through the DeepSeek off-peak window. It is a companion control, not the full
Eliza app.

What it is **not**: it is not the full Eliza UI, not autonomous planning, and it
does not apply a Sol plan automatically. It never starts a model, spawns a
worker directly, logs into an account, changes quota/ownership state, installs a
cron/global service, or touches the CMS/theme/game port. Its default queue is
empty and therefore dispatches nothing.

## Boundary to the companion

The autopilot only talks to the companion through its canonical fixed loopback
API at `127.0.0.1` (default port 18791), read from the companion's private
`server.json` and `control.token` under `.builder/eliza`. It uses the bearer
`/status`, `/tasks` (a bare JSON array) and `/control` contract implemented in
[builder/eliza/src](../builder/eliza/package.json). It never uses an ambient
proxy, never follows a redirect, refuses any non-loopback host, bounds every
response and timeout, and redacts secret-like text from any error it records.

Canonical claims still create the isolated worktrees: the autopilot never
provisions a worktree itself, and the companion keeps its global four-worker
cap. The autopilot additionally refuses to dispatch while the reported
`activeWorkers` already reaches four.

Before reusing any existing task, the autopilot treats the `/tasks` rows as
candidate ids only and reads the authoritative metadata through `task-status`.
It reuses a task only when the trusted identity matches exactly -- the same
`boardId`, `goal`, `scope` and `acceptanceCommand` as the reviewed manifest
entry. A `boardId` that already belongs to a differently-scoped task (for
example a CMS/private task) fails closed: the autopilot records an
identity-conflict review and never creates, dispatches, rewrites or mutates the
foreign task.

## Controls

```sh
python3 scripts/eliza-autopilot.py check   # dry-run decision; read-only
python3 scripts/eliza-autopilot.py run     # bounded foreground daemon (singleton)
python3 scripts/eliza-autopilot.py stop    # set the private STOP flag (--clear removes it)
```

`check` performs no local or remote mutation. `run` holds a stable-inode OS
(`fcntl`) lock under `.builder/eliza-autopilot/autopilot.lock`, so two daemons
can never both run; a second start exits with a private busy result. `stop`
writes a private STOP marker that `run` observes and honors. There is no
self-install and no global service: the owner starts and stops it.

## Task manifest

The owner maintains reviewed declarative definitions in a private, owned,
non-symlink `manifest.json` (mode 0600 under `.builder/eliza-autopilot/`).
Validation is fail-closed: JSON schema/version, bounded size and task count,
secret rejection, and bounded goals, criteria and acceptance argv.

```json
{
  "version": 1,
  "tasks": [
    {
      "boardId": "AUTO-1",
      "title": "Bounded helper",
      "goal": "One small bounded change",
      "scope": ["scripts/eliza-"],
      "acceptanceCriteria": ["focused test passes"],
      "acceptanceCommand": ["python3", "-m", "unittest", "tests.ops.test_eliza_autopilot"],
      "depends": []
    }
  ]
}
```

Allowed scope is Eliza-owned only: `scripts/eliza-`, `builder/eliza/`,
`docs/eliza-` and `tests/ops/test_eliza_`. Declared scope and the **actual**
changed paths of a finished worker are both denied for the CMS/game port,
`apps/`, `cmd/`, `internal/`, `migrations/`, `themes/`, `assets/`, `config/`,
`infra/`, `packages/`, any `.env*`, `.git`, game/Db (`*.sql`, `*.db`,
`*.sqlite`) and `..` traversal. An out-of-scope change blocks acceptance and
raises review-needed instead of being accepted.

## Window, wake and cutoff

The autopilot imports the **single canonical** [coding window](../scripts/coding-window.py);
it never reimplements the peak calendar. When the current instant is inside a
peak/buffer block it idles and wakes exactly at the canonical `next_allowed_at`
(the `+5` opening). Before the canonical `stop_at` (peak start minus 30 minutes),
and inside a five-minute grace, it cancels its tracked workers and idles until
the next window opens; no new dispatch starts after the cutoff.

## Quota and queue state

Before each dispatch it re-checks the window and reads a fresh quota decision
from the companion. Private queue state (`qstate.json`) records pause,
ownership and a waiting-reserve flag; any of these, a missing/unknown quota, a
reserve (`>= 90%` used in any reported window) or a detected four-worker cap
blocks a new dispatch without spending. Weekly/5-hour windows are never relabelled
as a daily allowance, and the autopilot never consumes a reset credit or changes
an account.

The queue also honors the host coordinator's private `quota-state.json` when
present: foreign ownership, pause, unknown reported windows or a reserve blocks
reconciliation and dispatch. Startup preserves that ownership. Ownership checks
precede cutoff cancellation as well; a handed-off queue cannot cancel another
coordinator's tasks. Nested private, hidden and dependency paths are excluded
from every declared worker scope. This queue is not installed as a live service
and has not passed a funded end-to-end deployment check.

## Durable attempts and recovery

Every mutating control request is preceded by a durable attempt marker. If a
dispatch POST completes but no response is observed (an ambiguous outcome), the
marker is retained as ambiguous and the run enters **recovery-required**: it
pauses and never blindly re-POSTs the same mutation.

Every open-window cycle, the running daemon reconciles its tracked workers
before deciding anything new. Reconciliation compares a completed worker turn
against the protocol-ready session, verifies the worker's actual changed paths
against the declared scope, and treats an interrupted process as infrastructure,
not a code failure; acceptance is only taken for an actual completed worker turn.
It is idempotent across restarts: an attempt already resolved by a prior cycle
is closed from the durable receipts rather than re-checked, and no acceptance is
ever taken during a blocked window or while an ambiguity needs recovery.

## Failures, review and integration

Three durable genuine acceptance failures stop further dispatch for that task
and retain a review-needed handoff; the existing Builder already asks Sol for
help at three failures. API, timeout, cancellation and turn-budget outcomes are
never counted as code failures. A passed acceptance still requires an
independent review: the autopilot never pushes, integrates or merges `main`.

## Status

Focused tests pass offline with a local loopback fixture and real subprocess
singletons. **Actual deployment is still pending**: this queue has not been run
against the live companion for a real off-peak dispatch, and it does not provide
the full Eliza UI, autonomous planning, a Sol main-chat handler or automatic
plan application. See [integration](eliza-integration.md) and
[known gaps](gaps.md).
