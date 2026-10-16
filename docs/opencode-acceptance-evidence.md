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
The verifier checks receipt/intent bindings, canonical worktree path, argv
hash, log file type/permissions and log hashes. An incomplete intent verifies
as unresolved; no recovery path reruns it.

`command_completed_zero` means only that the supplied command returned zero;
`unclassified_nonzero` is not an implementation failure. Timeouts, output
limits, interruption and infrastructure/capture errors remain unresolved.
Nothing here sets `acceptance_ran`, `implementation_attributed`, or
`acceptance_failure`, and it does not write/release/quarantine a ledger claim.
Independent review and a later explicit outcome-recording step remain separate.

Timeout sends TERM to the new process group, waits a bounded grace period, then
sends KILL and reaps the direct child. This cannot prove that detached
descendants are stopped. A SIGKILL of the runner itself cannot perform cleanup;
the durable intent remains non-replayable and unresolved. Do not treat these
synthetic process tests as live worker or restart-recovery qualification.

## Local evidence

`tests/ops/test_opencode_acceptance.py` uses only tiny synthetic local child
processes. Coverage includes zero/nonzero exits, hash verification, duplicate
and racing run IDs, claim/task/worktree and expiry denial, strict inputs,
symlink/tamper rejection, timeout group cleanup, output limits, runner SIGKILL
after intent, and no outcome-ledger mutation. These checks establish local
receipt behavior only; A09 native/live acceptance and implementation-failure
attribution remain open.
