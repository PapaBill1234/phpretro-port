# Pinned upstream Eliza source setup and CLI

`scripts/eliza-upstream.py` is the reproducible, fail-closed control surface for
the full pinned Eliza upstream source checkout. It owns only these files:
`scripts/eliza-upstream.py`, `tests/ops/test_eliza_upstream.py` and this
document. It never edits CMS, theme, port or other source paths.

## Pinned identity (non-overridable)

| Item | Value |
| --- | --- |
| Upstream repo | `https://github.com/elizaOS/eliza.git` (MIT) |
| Source path | `builder/eliza/upstream` |
| Exact pin | `28fef47c9d9d3acc7792be0fdaa3070020293cc5` |
| Node | `24.15.0` linux-x64, SHA256 `472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6` |
| Bun | `@oven/bun-linux-x64@1.4.2`, registry SRI `sha512-9/E/UXOTpSo3YsV5g+FhtTd/qTpiWoKuxS12cqtuYA1ssu9fRAoPQnipFgGyck3tWO63iUdxBiygq+kELFawng==` |

Runtime archives are only fetched from `nodejs.org` and `registry.npmjs.org`;
any other host is refused. There is no flag, environment variable or argument
that can change these URLs or the pin.

## Private layout

Everything installs under `ROOT/.builder/upstream` and never touches global
runtimes or ancestor directories:

```
.builder/upstream/runtimes/node-v24.15.0-linux-x64/bin/node
.builder/upstream/runtimes/bun/package/bin/bun
.builder/upstream/cache/       # verified download archives (reused)
.builder/upstream/logs/        # bounded, truncating build/CLI logs
.builder/upstream/state/       # private ELIZA_STATE_DIR (not an old profile)
.builder/upstream/setup.lock   # one stable fcntl advisory lock
.builder/upstream/provenance.json
```

## Commands

```sh
python3 scripts/eliza-upstream.py check
python3 scripts/eliza-upstream.py setup
python3 scripts/eliza-upstream.py cli -- --help
python3 scripts/eliza-upstream.py cli start
```

- **check** is read-only and truthful. It reports the source pin versus the
  expected SHA, tracked modifications, runtime versions, whether `node_modules`
  is installed and whether provenance recorded the install. It never installs,
  never downloads and never runs inference. A runtime is only reported ready
  when its bytes match the recorded digest, so an unverified binary is never
  executed. Readiness is `true` only when all of pin, runtimes and dependencies
  are verified.
- **setup** is explicit and refuses to run while the off-peak gate is closed. It
  verifies the platform is linux-x64, downloads and verifies the pinned Node
  archive (SHA256 against the official `SHASUMS256.txt`), verifies the Bun
  tarball against both its registry SRI and pinned SHA256, extracts both with
  bounded member/path/symlink checks, clones the exact pin if the checkout is
  absent, then runs the upstream frozen install
  (`bun install --ignore-scripts --frozen-lockfile`), the workspace symlink
  script and the metadata-selected `appRegister` packages' own `build:js`.
  Environment is built from scratch (private PATH/cache, `LANG=C.UTF-8`); no
  arbitrary provider or cloud credential is inherited. Verified archives are
  reused from the private cache; a cached runtime binary is re-hashed against its
  recorded digest before reuse, but only after the recorded manifest path is
  confirmed equal to the exact derived canonical binary (`runtimes/node/…/bin/node`
  or `runtimes/bun/package/bin/bun`). A manifest that names any other path is
  rejected rather than hashed-and-returned, and a hash mismatch re-installs or
  fails closed. Every phase — clone, checkout, each HTTP read and each
  `--version` probe — receives the original off-peak cutoff and a freshly
  recomputed remaining budget; the cutoff is captured once and never extended by
  a later phase, and work is refused once it has elapsed.
- **cli** runs the actual pinned Bun entry point
  `bun --no-install --conditions=eliza-source packages/app/src/entry.ts <argv>`
  with a clean private environment (`ELIZA_STATE_DIR` under `.builder/upstream`,
  local embeddings/voice/fused warmup disabled, loopback API bind). It requires a
  pinned, clean checkout, verifies the Bun binary bytes before execution, and
  refuses to launch while peak or without a verified off-peak cutoff. No
  model/provider key is added and none is inherited.

Optional `--node-path`/`--bun-path` accept a trusted operator-supplied runtime
only after a matching `--version` check; otherwise setup fails closed. There is
no fallback to global `node`/`bun`.

Every delete or install first asserts the target stays inside `.builder/upstream`
and crosses no symlinked ancestor, beginning at `ROOT/.builder` itself: a
symlinked `.builder` or `.builder/upstream` is rejected before any write or
delete, so a planted ancestor symlink cannot delete or write outside the private
tree. Long-running steps run in their own process group and are TERM/KILL bounded
with orphan cleanup on failure, cancellation or the off-peak cutoff; a helper that
dies by signal is reported as a failure (`128+signum`), never success. Setup holds
one `fcntl` advisory lock with no PID file and no stale-PID unlink.

The off-peak decision is imported from the canonical `scripts/coding-window.py`.
That file is intentionally **not** committed or edited here — it is owned by the
integration branch (Root `cd69db8`), so this worker's checkout carries it only as
an untracked dependency until integration provides it. If it is absent the script
fails closed rather than guessing a schedule.

## Integration seam (future work, not this task)

`scripts/eliza-dashboard.py` is a separate, protected dashboard candidate owned
by another worker and is deliberately not started here. This task documents the
integration but does not claim it. Injecting reviewed provider credentials,
starting the dashboard and registering the project plugin remain separate,
explicitly-reviewed steps.

## Acceptance status

The reference checkout at `work/research/eliza` (pin `28fef47…`) proved a real
source API + renderer first-run render with the exact launch components above.
This control script is verified by its own isolated tests; a **full root setup,
install and CLI launch acceptance from the integration tree is still pending**.
No full-app launch, provider-configured readiness or production packaging is
claimed here.
