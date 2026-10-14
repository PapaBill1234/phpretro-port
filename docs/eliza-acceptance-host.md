# Isolated production full-Eliza acceptance host

Status: **bounded first phase (setup + launch contracts)**. `check` runs
read-only against the real tree. Real host startup and browser/owner-transport
proof have **not** been run here and remain a root step after review. No
readiness is claimed from fixtures.

Helper: `scripts/eliza-acceptance-host.py` (owns only
`.builder/eliza-acceptance`). Tests: `tests/ops/test_eliza_acceptance_host.py`.

## What it is

A small, reproducible launcher for the **real** pinned Eliza host, isolated from
the canonical install and the operator's existing conversations. It uses the
accepted production startup path — the actual
`packages/app/src/entry.ts` → `cli/program/register.start.ts` →
`runtime/eliza.ts` host bridge — **not** the device harness
`createRealTestRuntime`/deterministic models. Those are not startup proof.

## Commands

```
python3 scripts/eliza-acceptance-host.py check     # read-only; no writes
python3 scripts/eliza-acceptance-host.py setup  --yes
python3 scripts/eliza-acceptance-host.py start  --yes   # foreground
python3 scripts/eliza-acceptance-host.py stop
python3 scripts/eliza-acceptance-host.py status
```

`check` verifies the fixed pin `28fef47…`, the reviewed patch digest, verified
Node/Bun runtimes and a clean canonical source; it never writes. `setup` and
`start` are gated behind `--yes`; `start` stays in the foreground.

Verified read-only today against the trusted root: pin matches, canonical source
clean, runtimes verified, patch digest `caf755be…04ffb9`. No overlay was built
and no host was launched in this worker.

## Fixed launch contract

Exact argv (entry is the overlay copy, never the canonical source):

```
<bun> --no-env-file --no-install --conditions=eliza-source <overlay>/packages/app/src/entry.ts start
```

Fixed, filtered child environment (inherited `DATABASE_URL`/`POSTGRES_URL`,
provider/cloud credentials and **all** foreign `ELIZA_*` values are dropped):

```
ELIZA_STATE_DIR          <acceptance>/state
ELIZA_CONFIG_PATH        <acceptance>/state/eliza.json
PGLITE_DATA_DIR          <acceptance>/state/db
ELIZA_API_BIND           127.0.0.1
ELIZA_API_PORT           18831
ELIZA_UI_PORT            18832
ELIZA_ALLOWED_ORIGINS    http://127.0.0.1:18832,http://127.0.0.1:18831
ELIZA_ALLOW_NULL_ORIGIN  0
```

Config (`eliza.json`, 0600): `database.provider = pglite`, isolated agent
workspace under the private state, plugin entries for ProjectCompanion and
nativeSol. No secrets are written or logged. No nginx or global service is
changed.

## Overlay and provenance

`setup` builds `.builder/eliza-acceptance/source` as a hardlink copy of the
canonical source (excluding `node_modules`/`.git`) and applies **only** the
reviewed `builder/eliza/patches/new-chat.patch`. The applied diff must
reverse-check equal to the patch, and the pin/patch digest is recorded in
`overlay-provenance.json`. `node_modules` is materialized so `@elizaos/ui`
re-points at the overlay's own `packages/ui` — the canonical
`@elizaos/ui -> ../../packages/ui` link would otherwise resolve the *original
unpatched* UI package. Import resolution is proven by
`test_patched_ui_resolves_into_overlay_not_canonical`.

## Boundaries

- Trusted root is a fixed constant; there is no arbitrary `--root` flag.
- All writes stay under `.builder/eliza-acceptance` with non-symlink ancestors.
- `setup` never wipes: a pre-existing overlay with unknown provenance is
  refused, not replaced; a partial build is removed only if this run created it.
  Persistent acceptance state is retained across stop/start.
- `stop` signals only the recorded pid group after matching that pid's command
  line to this overlay's entry; no global `pkill`.
- Shared setup deadline, a 1 GiB free-space floor, and bounded 0600 logs.

## Pending / not run here

- Applying the overlay and launching the real host (root, after review).
- Browser/owner-transport acceptance (New-chat creation, empty transcript,
  retained prior conversation, reload behaviour) on the real running app.
- Physical drop-in copies (ProjectCompanion, nativeSol) and the exact upstream
  plugin config schema fields — to be wired when the private plugin sources are
  confirmed; generation guard stays false and no brain/model call is automatic.
