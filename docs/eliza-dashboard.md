# Eliza dashboard launcher (`scripts/eliza-dashboard.py`)

A maintainable CLI that reproduces the reviewed two-process Eliza dashboard
launch design as four commands: `check`, `start`, `status`, `stop`. It is a
reproducibility extraction of the existing launcher
(`run-dashboard.py` in the reference tree), not a new provider or runtime
architecture. The live launcher is unchanged and is not started by this CLI in
tests.

## External prerequisites (no installation performed)

`check`/`start` validate an **explicitly registered config source**; they never
install anything:

- Pinned upstream checkout
  `/home/ubuntu/Documents/Codex/2026-10-11/al/work/research/eliza` at git pin
  `28fef47c9d9d3acc7792be0fdaa3070020293cc5` (exact `git rev-parse HEAD` match).
- Operator-provided runtimes under that checkout's `.phpretro-builder/runtimes`:
  Bun `1.4.2` and Node `v24.15.0` (exact `--version` match).
- Prerequisite source files: `packages/app/src/entry.ts` and
  `node_modules/vite/bin/vite.js`.
- The project coding-window gate `scripts/coding-window.py`. This script does
  **not** own or copy that gate (a pending readiness task owns it); when the
  gate is absent or closed, `check`/`start` **fail closed**.

The registered source path, pin and versions live in `CONFIG_SOURCE`. The
integration `ROOT` is resolved from the script location; there is no arbitrary
`--project-root` option — tests inject an isolated tree through the pure helper
functions instead.

## Commands

- `check` — validates the source pin/runtimes/prereqs, runs a **fresh**
  coding-window gate check, tests that the loopback ports are bindable, reports
  lock state, onboarding state, the local compat-module state and the saved
  model-routing compatibility. Routing verification **fails closed**: only a
  genuinely absent saved config (legitimate first-run) is compatible; an
  existing config that is unsafe/unreadable/a symlink, malformed JSON, or whose
  root is not a JSON object counts as incompatible. It never launches a child,
  never performs model inference, and never dumps or mutates the saved config.
- `start` — foreground supervisor. Acquires the non-blocking lock (a duplicate
  startup reports `busy` and does not dispatch), re-checks the gate **before
  every launch** (after the slow prerequisite work and immediately before each
  child), and never launches once it is within five seconds of the validated
  cutoff. The originally validated cutoff is preserved so a later gate re-check
  cannot extend the deadline; the API and UI children start in their own process
  groups. It stops at the coding window cutoff (five seconds early), on
  SIGTERM/SIGINT, or when a child exits, always cleaning partial startup with
  bounded TERM then KILL including descendants, with every cleanup deadline
  clamped so it cannot run past the known cutoff.
- `status` — truthful `awaiting-onboarding` vs `stopped` vs `runtime-ready`
  distinction from identity-verified PIDs. No inference and no arbitrary API
  payload or secret is printed.
- `stop` — verifies each stored PID start-tick (`/proc/<pid>/stat` field 22)
  immediately before **every** signal (including the final KILL), so a reused
  leader PID is spared. Original group members are captured before signalling so
  known original descendants are still reclaimed by identity-checked single
  signals even after the leader is reaped. Every stop deadline is clamped to the
  recorded `stopAt`. It signals only the recorded API/UI process groups and the
  recorded supervisor; it never uses a broad `pkill`.

## Security and state

All state is under `.builder/eliza-dashboard` (private, ignored by Git):

- The state directory is created/validated `0700`, owned, and symlink-rejected.
- `dashboard.lock`, `runtime.json` and `api.log`/`ui.log` are validated regular,
  non-symlink, `0600`, owned files.
- Secrets are read with a robust inert dotenv parser (`api.token`,
  `vault.passphrase` in the state dir; `secrets/deepseek.env`), each required
  `0600`, owned and non-symlink. Credential values are never printed and never
  passed on a command line.
- The **API** child receives the DeepSeek text key and the persistent vault
  passphrase. The **UI** child receives neither the provider key nor the vault
  passphrase nor the API token. The fixed endpoint is
  `https://api.deepseek.com/v1` with `deepseek-flash` on all text tiers,
  autonomy `false` and the unmanaged orchestrator off. Host/origin are exactly
  `phpretro.work.gd` / `https://phpretro.work.gd`; the reviewed environment
  skips are preserved. All bindings are loopback only.
- No scheduler/system-wide service change is made and no runtime ancestor is
  chmod-ed automatically.

## Temporary cutoff and incomplete features

`start` is foreground and bounded by the coding window's `stop_at`; an external
supervisor (the coding-window/continuation runner) provides the outer bound.
This is a **temporary** DeepSeek off-peak cutoff, not a permanent schedule.

This slice reproduces the two-process launch only. Local embeddings, voice
warm-up and fused-inference setup are disabled by the preserved skips; plugin
installation, native Sol review wiring and embeddings remain **incomplete**.
This does not mark full Eliza complete.

## Checks

`python3 -m unittest discover -s tests/ops -p test_eliza_dashboard.py` exercises
the pure helpers and real behaviour: registered-source pin/version/prereq
validation, missing gate, denied gate, gate re-check after slow validation and
before each spawn, no launch within five seconds of the cutoff, cleanup bounded
by the original recorded cutoff, unsafe secret/symlink rejection, a FIFO log
target rejected promptly without a reader, UI environment isolation, lock busy
in a separate process, busy loopback port, partial-startup cleanup with the log
descriptor closed on failure, invalid/native/unknown/empty/unsafe routing
fail-closed, real leader/grandchild group termination, PID-replacement-safe and
post-TERM-orphan-safe `stop`. Tests never run the live dashboard or model
inference and use only disposable owned child processes.

Readiness requires both identity-verified child processes and an authenticated loopback agent status with state running, canRespond true and startup phase ready. A listening/live process alone is starting. Records missing PID start ticks cannot authorize stop. No actual model inference is performed by the status probe.

Saved route acceptance uses the pinned upstream model-resolution contract: `agents.defaults.model.primary = "openai/deepseek-flash"`, or `serviceRouting.llmText` with backend `openai`, transport `direct`, and primaryModel `deepseek-flash`. An OpenAI label elsewhere or an unsupported simplified `{provider, model}` object does not establish a selected route. Other saved profiles require explicit compatibility review.
