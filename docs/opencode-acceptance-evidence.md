# OC2-M06-A09 local acceptance evidence

This slice adds a local-only runner for explicitly supplied acceptance/test
argv. It is **not** native OpenCode qualification, dispatch, a sandbox, or proof
that an arbitrary command is safe. The coordinator is trusted to select a test
command; `shell=False` prevents shell parsing but does not restrict what the
child can do. The runner itself invokes no provider, model, server, native
session, or ledger outcome API. A separate local reviewed-outcome adapter is
described below.

## Contract

`scripts/opencode-acceptance.py run` validates one task/claim/worktree against
one read-only SQLite snapshot of the existing ledger. The claim must be active,
unexpired, and bound to the exact canonical, no-symlink worktree supplied.
This is a preflight snapshot only: it does not reserve worker admission, prevent
concurrent claim release, or establish ownership of the entire filesystem.
Inherited `GIT_*` variables are removed before the child starts.

The caller provides an explicit argv array, bounded run ID and positive finite
timeout (maximum 3600 seconds). The runner creates a unique private run
directory and durably writes its intent with exclusive creation and fsyncs
before starting one child. Any existing run directory—including an intent-only
one after a crash—blocks replay. Logs are private mode 0600, capped at 16 MiB
combined, and represented in the completion receipt by SHA-256 and byte length.
Nonblocking multiplexed pipe reads remain under the same monotonic deadline as
the child; the leader exiting does not complete a run while inherited pipes or
in-group descendants remain. Timeout cleanup of the group and pipe closure are
bounded; detached pipe holders yield unresolved status rather than hanging or
false completion. The verifier checks the exact receipt schema, every immutable
receipt/intent binding (including timestamps and timeout), canonical worktree
path, bounded argv/hash, log file type/permissions, total size and log hashes.
An incomplete intent verifies as unresolved; no recovery path reruns it.
This is integrity checking against the saved receipt, not cryptographic
authenticity: a local operator able to rewrite the private intent, receipt, and
logs together can forge a consistent set.

The CLI emits only a status and redacted receipt/verification marker; structured
IDs, worktree and argv are available only from the local Python API/private
receipt. Ledger path URIs are generated with `Path.as_uri()` so URI-special
filesystem characters are encoded safely.

`command_completed_zero` means only that the supplied command returned zero;
it is not feature acceptance. `unclassified_nonzero` is not an implementation
failure. Timeouts, output limits, interruption and infrastructure/capture errors
remain unresolved. The runner does not set `acceptance_ran`,
`implementation_attributed`, or `acceptance_failure`, and does not
write/release/quarantine a ledger claim.

Timeout sends TERM to the new process group, waits a bounded grace period, then
sends KILL and waits only a bounded time for the direct child. This cannot prove
that detached descendants are stopped. A SIGKILL of the runner itself cannot
perform cleanup; the durable intent remains non-replayable and unresolved. Do
not treat these synthetic process tests as live worker or restart-recovery
qualification.

## Local evidence

`tests/ops/test_opencode_acceptance.py` uses only tiny synthetic local child
processes. Coverage includes zero/nonzero exits, hash verification, duplicate
and racing run IDs, claim/task/worktree and expiry denial, strict inputs,
redacted CLI output, URI-special state roots, receipt mutation/truncation,
symlink/tamper rejection, leader-exit inherited-pipe supervision, timeout group
cleanup, bounded detached pipe-holder behavior, output limits, runner SIGKILL
after intent, and byte-identical read-only ledger preflight. These checks
establish local receipt behavior only; A09 native/live acceptance and
implementation-failure attribution remain open.

## OC2-M06-A09b explicit local reviewed outcomes

`scripts/opencode-acceptance-outcome.py` is a separate local adapter. Its API
requires state root, run ID, task ID, original claim ID, attempt ID, verdict
(`pass` or `failure`), and a strict `review_confirmed=True` assertion. A pass
requires a freshly verified `command_completed_zero` receipt and explicit review
confirmation. A failure requires a freshly verified `unclassified_nonzero`
receipt, explicit review confirmation, strict `implementation_attributed=True`,
and a bounded safe failure signature. Exit status alone never classifies a
failure. Every unresolved runner status and incomplete intent is rejected even
if the caller asserts review or attribution; this adapter does not record
unresolved outcomes.

The adapter re-verifies the receipt and logs on every invocation—including an
exact attempt replay—then checks task, claim, and canonical worktree against the
original claim in a read-only ledger snapshot before any write. It permits a
historical released/expired claim only when that exact original binding remains;
it does not reassign claims. It calls the existing outcome API unchanged and
uses `acceptance/<run_id>` as evidence reference, so another attempt or verdict
for the same execution collides with ledger uniqueness. `attempt_id` remains
the ledger's idempotency key. The CLI emits only a redacted outcome summary and
sanitized rejection; no reviewer identity is collected or authenticated.

These reviewer and implementation assertions are caller-attested local inputs,
not proof of reviewer identity, feature correctness, native session state, or
provider/authentication truth. A local operator able to rewrite all private
intent, receipt, logs, and ledger data can forge a consistent history. Tests in
`tests/ops/test_opencode_acceptance_outcome.py` exercise the local chain with
tiny real child processes, schema-valid unresolved fixtures for hard-to-induce
statuses, tamper checks, replay/uniqueness, historical claim handling, and
durable local ledger behavior. This is not native A09 dispatch or live
acceptance qualification.
