# Isolated production full-Eliza acceptance host

Status: **bounded adapter phase**. `check` is read-only. Real configured-host
startup and browser/owner-transport proof are **pending** (root, after Sol
review). No readiness is claimed from fixtures.

Helper: `scripts/eliza-acceptance-host.py` — a thin adapter over the reviewed
supervisor `scripts/eliza-dashboard.py`. It owns only `.builder/eliza-acceptance`.
Tests: `tests/ops/test_eliza_acceptance_host.py`.

## Adapter, not a second supervisor

The helper registers one immutable `ACCEPTANCE_PROFILE` and delegates
`check`/`start`/`stop`/`status` to `eliza-dashboard.cmd_*`, so the OS lock,
captured cutoff, partial-startup group cleanup, PID start-tick signalling and
private filesystem come from the reviewed dashboard, not a duplicate
supervisor. The dashboard default profile reads the live module constants, so
existing direct-DeepSeek dashboard behaviour is unchanged. There is no
arbitrary CLI path/root flag: the profile is constructed only in reviewed code.

## Commands

```
python3 scripts/eliza-acceptance-host.py check            # read-only
python3 scripts/eliza-acceptance-host.py setup  --yes     # build private copy + drop-ins
python3 scripts/eliza-acceptance-host.py start  --yes     # foreground (dashboard supervisor)
python3 scripts/eliza-acceptance-host.py stop
python3 scripts/eliza-acceptance-host.py status
```

`check` reports, separately: `prerequisites` (pin/runtimes/ports and existing-lock safety), `overlay_ready`, `dropins_ready`, and `host:"pending"` /
`browser:"pending"`.

## Private copy and provenance

`setup` builds `.builder/eliza-acceptance/source` as **regular copies** of the
pinned canonical regular files (`git ls-files --stage`), never hardlinks, so no source
inode is shared and the canonical checkout stays byte-clean. Three pinned
internal source aliases are preserved with exact relative targets that resolve
inside the overlay. The optional native-inference gitlink is omitted; this
profile does not qualify local native inference. `node_modules` is
materialized so `@elizaos/ui` resolves to the overlay's own `packages/ui` (the
canonical workspace link would resolve the *original unpatched* UI). Only the
reviewed `builder/eliza/patches/new-chat.patch` is applied; verification
compares **every** tracked file by sha256 and rejects any unknown extra overlay
file — reverse-apply alone is not sufficient. A cutoff is captured once
(`cutoff.json`) and never extended; each build stage is deadline-gated.

## Physical drop-ins

Drop-ins are **real package directories** under
`<ELIZA_STATE_DIR>/plugins/custom/<name>` (never symlinked directories, which discovery
skips). Each carries its original package id (`@phpretro/plugin-project-companion`,
`@phpretro/eliza-native-model`) and a small physical `index.mjs` that imports the
fixed verified **canonical** module in place via a `file://` URL — so
`native-model/transport.mjs`'s `../../../scripts/codex-generate.py` derivation
and the companion root layout stay valid. The whole module is never copied out
of its layout. The wrapper preserves the canonical default export and exposes
an empty context-provider list when the canonical plugin supplies only model
handlers, matching the pinned loader's capability predicate. Config uses the verified schema key
`plugins.entries.<pkg>.enabled` (pinned
`packages/agent/src/config/zod-schema.ts`); no fields are invented.

## Child environment

`build_env` builds an allowlist: no ambient DB URL, provider key or foreign
`ELIZA_*`; private HOME/state/config/PGlite/workspace; the API child holds no
model credential here and the UI receives none.

## Pending / not run here

The full regular-copy overlay build, configured host startup and browser
acceptance remain pending. Root verified all three new-chat patch outputs against
independent Git application on the actual pin. Actual pinned
`scanDropInPlugins`/`findRuntimePluginExport` discovered both physical wrappers
and recognized the companion view and four native text slots without invoking
handlers. The actual pinned `ElizaSchema.safeParse` accepted the generated config.
These source/loader/schema checks do not establish host startup or mounting. The native provider guard stays false; no model is called automatically.

## Root takeover checkpoint — 2026-10-14

The corrected source adapter passed independent final Sol review after Root
fixed two observed loader failures (lost companion default export and a rejected
model-only native plugin shape), aligned physical discovery with the exact child
`ELIZA_STATE_DIR`, added explicit private PGlite/workspace environment and a
schema-validated workspace config, and required exact drop-in wrapper bytes plus
canonical entry digests. Setup now checks the canonical gate before copying;
copying checks its shared deadline for every tracked file. Canonical source pin
and clean state are verified before copy/launch. All acceptance-path ancestors
and write targets are checked for symlinks before mutation. Launch verification
requires both overlay and drop-in provenance.

Twenty-two adapter checks pass; 59 dashboard checks pass with one existing skip.
Tests include parent-path symlinks, closed-gate setup with no writes, wrapper
modification and missing provenance. Private real-source/loader/schema proof is
under `.builder/current-workers/root-acceptance-takeover/` and the neighboring
root lock-repair proof directory. No host or model was launched by those probes.

The actual full private overlay setup passed against pin
`28fef47c9d9d3acc7792be0fdaa3070020293cc5`. Full overlay and drop-in provenance
checks passed, and actual Bun resolution selects the overlay UI source. No host
or model was launched. The caller's `--deadline` now reaches setup and launch;
Git/patch timeouts, hashes, copying, links, provenance, configuration and completion
use the original remaining budget. Start validators derive that budget from the
saved captured cutoff. Only the generated root `node_modules` is exempt from
source traversal; unexpected nested dependency links are rejected. Reads of
provenance/package metadata reject symlink ancestors and leaves. Default supervisor
execution directories and Vite resolve to the source that was actually validated,
covered by captured child-launch regressions.

Independent final Sol review accepted the source fixes. The actual configured
host and browser remain unqualified; source acceptance does not admit the native
provider or close G05/G06.

The adapter `check` reads existing prerequisites without creating directories,
lock files or a captured cutoff. Acquiring the lifecycle lock belongs to `start`.
Independent Sol review accepted this no-write adjustment; a successful-check
regression leaves a fresh private root empty.
