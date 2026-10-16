# OC2-M06-A09a local execution receipts

This slice adds a local-only runner for explicitly supplied acceptance/test
argv. It is **not** native OpenCode qualification, dispatch, a sandbox, or proof
that an arbitrary command is safe. The coordinator is trusted to select a test
command; `shell=False` prevents shell parsing but does not restrict what the
child can do. No provider, model, server, native session, or ledger outcome API
is invoked.

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
`unclassified_nonzero` is not an implementation failure. Timeouts, output
limits, interruption and infrastructure/capture errors remain unresolved.
Nothing here sets `acceptance_ran`, `implementation_attributed`, or
`acceptance_failure`, and it does not write/release/quarantine a ledger claim.
Independent review and a later explicit outcome-recording step remain separate.

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
