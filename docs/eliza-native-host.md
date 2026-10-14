# Private native text-host bootstrap and build

`scripts/native-host.py` is the reproducible, fail-closed bootstrap/build control
surface for the pinned OpenAI Codex text host. It owns only these files:
`scripts/native-host.py`, `tests/ops/test_native_host_setup.py` and this
document. The public patch/provenance tree `builder/eliza/native-host` is owned
by another worker and is read-only here; this helper never edits it, the
CMS/theme/port trees, the tool guard or any account/OAuth material.

This is a **BOOTSTRAP/BUILD helper, not runtime admission**. A successful build
records build provenance only: it does **not** qualify the model or toolset, and
the helper never runs the host, a model, a turn or a service. Qualification
stays open per the [native qualification evidence](eliza-native-qualification.md)
and [known gaps](gaps.md).

## Pinned identity (non-overridable)

| Item | Value |
| --- | --- |
| Source repo | `https://github.com/openai/codex.git` |
| Exact pin | `c1382380de69521303b416720a52f42d51af6248` |
| Rust | `1.95.0` `x86_64-unknown-linux-gnu` from the source `rust-toolchain.toml` |
| Rust manifest | `https://static.rust-lang.org/dist/channel-rust-1.95.0.toml` |
| Build target | `codex-cli --bin codex` |

Toolchain archives and their SHA-256 manifest are only fetched from
`static.rust-lang.org`; any other host is refused. There is no flag, environment
variable or argument that can change the URLs, the pin or the Rust version.

## Private layout

Everything lives under `ROOT/.builder/native-host` and never touches global
runtimes, `~/.cargo`, `~/.rustup`, `~/.codex` or ancestor directories:

```
.builder/native-host/source/      # verified pinned clean clone (reused, never reset)
.builder/native-host/build/       # private checkout of source + reviewed patch
.builder/native-host/toolchain/   # private Rust prefix (bin/cargo, bin/rustc)
.builder/native-host/cargo/       # private CARGO_HOME (registry/git cache)
.builder/native-host/target/      # private CARGO_TARGET_DIR
.builder/native-host/home/        # private HOME (no operator credentials)
.builder/native-host/cache/       # verified toolchain manifest + archive
.builder/native-host/logs/         # bounded, truncating install/build logs
.builder/native-host/native-host.lock
.builder/native-host/provenance.json
```

## Commands

```sh
python3 scripts/native-host.py status
python3 scripts/native-host.py source
python3 scripts/native-host.py toolchain
python3 scripts/native-host.py build
```

- **status** is read-only and truthful. It reports the source pin versus the
  expected SHA, tracked modifications, the verified toolchain and the recorded
  build binary (bytes re-hashed). `ready` requires all three. `qualified` is
  always `false`: readiness is build provenance, not model/toolset admission.
- **source** verifies the pinned clean clone and the reviewed patch. An existing
  checkout is inspected and reused, **never reset**: a wrong revision or any
  tracked modification is refused rather than repaired. The source
  `rust-toolchain.toml` must pin exactly `1.95.0`.
- **toolchain** downloads `channel-rust-1.95.0.toml`, reads the exact
  `x86_64-unknown-linux-gnu` archive URL and SHA-256 from it, downloads and
  verifies that archive, extracts it with bounded member/path/symlink checks and
  runs the official `install.sh` with `--prefix <private> --disable-ldconfig
  --without=rust-docs`. It never touches a global path, `~/.rustup` or the system
  ld cache, and records the `cargo`/`rustc` digests.
- **build** applies the reviewed patch to a private checkout of the trusted
  source (`git clone --shared`, so the clean clone is untouched), preflights
  free disk and available RAM to protect XRDP, then runs exactly one
  low-priority release job:
  `nice -n 19 cargo build --release --locked -p codex-cli --bin codex --jobs 1`.
  Before Cargo, the pinned release-local lock mismatch is repaired as described
  below, while retaining `--locked`.
  The Cargo cache/target are private and the environment is built from scratch
  (no inherited provider or cloud credential, no account or OAuth copy), with
  the fixed low-memory release profile described below.

## Reviewed public patch (read-only dependency)

The reviewed patch and its digest are read from the accepted public schema in
`builder/eliza/native-host/provenance.json`: `upstream.commit` and
`patch.applies_to_commit` must both equal the pinned source revision, `patch.file`
must name a patch whose bytes match `patch.sha256`, and `patch.target_file`
declares `{path, base_sha256, patched_sha256}`. A missing manifest, an unknown
digest, an escaping or symlinked patch path, a digest mismatch or an unbound
commit all refuse before anything is applied. In the real build the declared
target bytes are verified against the private checkout `base_sha256` **before**
the patch is applied and `patched_sha256` **after**, so a wrong target is
rejected. The patch is applied only to the private build checkout, so the trusted
clone is never dirtied and `status` stays truthful; a recorded build whose
source, production patch, repaired lock digest or actual toolchain binary digests no longer match is
reported stale, never ready.

The official toolchain manifest is trusted only through its HTTPS `.sha256`
sidecar: the sidecar is read from the same host, the manifest bytes are
re-hashed against it **before** parsing or selecting an archive URL, and a
tampered or unverified cache is discarded and refetched (never accepted).
Downloads follow no redirect and use no ambient proxy, the final URL/scheme is
re-checked, manifest/sidecar and archive downloads are byte-bounded while
reading, and both manifest and archive bytes/hashes are recorded.

## Bounds, gate and cleanup

All expensive stages (`source`, `toolchain`, `build`) require the canonical
off-peak gate and share one captured cutoff; work is refused while peak or
without a verified cutoff. Every costly path parses the cutoff strictly and
refuses a **missing, naive, malformed, non-finite or expired** value *before*
any subprocess or network read, so no workload can run unbounded or past the
window. The cutoff is immutable: each stage recomputes its own remaining budget
from the same value and never extends it. Read-only `status` needs no cutoff and
still reports truthfully without dispatch (its git probes use a fixed bounded
probe timeout, never a naive `None` cap on a costly step).

Subprocesses run in their own supervised session. The cancellation handlers are
installed **before** the child is launched, so a startup signal cannot orphan it;
the leader is kept unreaped until its **whole group** is cleaned (its id cannot
be recycled under a `killpg`), and the group is TERM/KILL-cleaned even when the
leader has already exited — a surviving TERM-resistant grandchild cannot outlive
the step. Every wait, reap and log-drain is bounded by the original cutoff, the
log is capped *while* the child runs (a single finite overflow flag, never
per-chunk growth), and an unprovable group state is a failure, never success; a
helper that dies by signal is reported as `128+signum`. The runner's own process
group is never signalled.

Archive scanning and extraction share one absolute `SIGALRM` boundary around
the complete operation, including blocking `getmembers`, decompression and
member writes. Between-member checks alone are insufficient. The boundary uses
the original captured cutoff and restores an enclosing alarm's handler and
remaining time without extending it. An interrupted extraction cleans private
temporary files before any installer runs or toolchain provenance is recorded.

The gate comes from `scripts/coding-window.py`, which this worker does not commit
or edit (it is owned by the integration branch and fails closed if absent). Every
delete or install asserts its target stays inside `.builder/native-host` and
crosses no symlinked ancestor. One `fcntl` advisory lock guards each step. If the
budget stops mid-run, the worktree checkpoints at the precise stage (`source`,
`toolchain` or `build`).

## Acceptance status

The helper is verified by its own isolated tests (87 focused tests: source/patch/
digest/tar/path/deadline/resource refusal, containment dotdot/symlink refusal
before any write, bounded downloads and archive members, tampered-cache
refetch, stale-build binding, no-signal-after-reap ordering, cutoff-clamped
probes and extraction, blocked archive scanning/member extraction with no
installation or provenance publication, nested alarm restoration, strict
MemAvailable unit/value parsing, fixed release-profile env/contract and
per-field profile freshness rejection, a real controlled compiler that proves
the actual argv and profile environment it receives, owned-group resource stops
on low and unknown RAM after real children recorded their PIDs, and the real
public manifest schema
probed offline) against controlled fixtures; there are no public-network,
install or build calls, and the tests do not assert that a real build succeeded.
The upstream regression suite (30 tests) and the repository public-file/
document-link check still pass. A **real Root toolchain install and codex-cli
build is still pending** and is executed by Root after independent review; the
`source`, `toolchain` and `build` mutating commands are unaccepted until then.
No runtime qualification, main-Sol acceptance, live queue or configured host
startup is claimed here.

## G05 release lock correction — 2026-10-14

The real private build reached Cargo and failed before compilation because the
release workspace declares version `0.162.0`, while 161 local packages in the
pinned `Cargo.lock` still declare `0.0.0`. A disposable offline diagnostic build
produced a minimal correction. Independent Sol inspection and root semantic
comparison verified that every external package version, checksum, source and
dependency list is unchanged across all 1,473 packages.

The helper now accepts only the exact pinned input lock SHA-256
`49fe417bcafe3f69f1f6636e23274f90d37e75e388127a76888c39001a4f8117`,
updates only source-less local package versions, requires exactly 161 changes,
and requires the output SHA-256
`1f770c0e411d51770e6dd48e9f3f4eab847bbabc7949f82f3619414697d68b8d`.
It refuses unknown input, symlinks, repeated repair and unexpected output before
writing. The persisted bytes are verified before Cargo and rechecked afterward;
only the verified digest is recorded as a build input. Old build provenance
without this digest is stale. The trusted source and production ceiling patch
remain unchanged. This is a build-input repair, not native runtime admission.

Private diagnostic evidence: `.builder/native-host/logs/build.log`,
`minimal-lock-probe.log`, and
`.builder/current-workers/root-lock-repair/architecture.json`. Provider review
transcripts remain private. Tests exercise external-entry preservation, refusal
without writes, symlink refusal, stale provenance and a real controlled compiler
that mutates the lockfile and must not publish new provenance.

Actual continuation: the repaired `--locked` build reached Rust/C compilation.
The first bounded run hit its 1,200-second limit without a compiler diagnostic.
A second bounded run reused caches and reached `codex-core`; root stopped the
owned helper when available RAM fell to 2,806 MiB, below the 3,072 MiB project
floor (compiler RSS about 3 GiB, swap nearly full). The supervised group exited;
no build processes remain. Cache/logs are retained privately. No completed CLI
binary or compiled qualification is claimed. A reviewed build approach that
fits available resources remains necessary before native admission.

## G05 resource-fit contract — 2026-10-14

To fit the reviewed build into the available RAM, the `build` command now uses
**one fixed low-memory release-profile contract** defined once and consumed two
ways so they can never drift:

| Profile field | Value | Cargo environment override |
| --- | --- | --- |
| `opt-level` | `0` | `CARGO_PROFILE_RELEASE_OPT_LEVEL=0` |
| `lto` | `false` | `CARGO_PROFILE_RELEASE_LTO=false` |
| `codegen-units` | `256` | `CARGO_PROFILE_RELEASE_CODEGEN_UNITS=256` |
| `debug` | `0` | `CARGO_PROFILE_RELEASE_DEBUG=0` |
| `incremental` | `false` | `CARGO_PROFILE_RELEASE_INCREMENTAL=false` |

These override the source release profile (thin LTO, four codegen units) while
retaining `CARGO_INCREMENTAL=0`, `--release --locked --jobs 1` and `nice 19`.
The whole contract is also recorded under `build.inputs.profile`; `status`
requires **exact equality** against the current contract, so a missing setting,
an altered value or a different profile invalidates provenance even when the
binary hash still matches. Source, patch, lock and toolchain bindings are
unchanged. 256 codegen units is a candidate sizing setting, **not a memory
ceiling**: monitoring can still overshoot between samples.

RAM monitoring is **opt-in** on the existing supervisor, so every other caller
keeps its current semantics. When the build supplies the 3,072 MiB floor,
available RAM is checked immediately before launch and re-sampled at a fixed,
bounded interval while the owned child runs. `MemAvailable` is trusted only in
its canonical `MemAvailable: <non-negative int> kB` form: a missing line, a
non-integer or negative value, a wrong field count or an unrecognized unit all
fail closed. A reading below the floor, or any unusable reading, stops the owned
process group with the same unreaped-leader TERM/KILL cleanup and the original
cutoff, and reports a **nonzero resource stop even if the child later exits
zero**; no success provenance is written. The stop is bounded by the same
immutable cutoff as every other step.

A real Root build of the exact resulting binary is still pending, so `qualified`
stays `false` and the guard stays `false`; this is a build-input/resource-fit
change, not native runtime admission.
