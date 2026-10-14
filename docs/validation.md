## G06.1 process-cleanup race — 2026-10-12

The cleanup regression check uses the real leader/grandchild launch and unchanged production `terminate_group`. Test-local `/proc/<pid>/stat` observation maps only `FileNotFoundError`/`ProcessLookupError` to `gone`; other I/O/permission errors and malformed contents propagate, and non-zombie live states remain rejected. Accepted `Z`/`gone` means not running; it does not prove the grandchild was reaped. Focused, complete ops-suite and predetermined repeated real-cleanup test results are recorded in the worker handoff after execution. This does not enable the game or resolve full-port/budget limits.

## G05 resource-fit contract — 2026-10-14

Independent architecture and final native Sol source reviews accepted worker
`2f6f192dcdff17462baa1380d7c79feec2769943`; feature integration is `8fdc2be`.
Root independently ran 87 native-host setup tests (23.746 seconds), 30 upstream
launcher tests (5.029 seconds), and public-file/document/configuration checks;
all passed. Diff checks passed. Worker CI run `38612139952` succeeded.
Evidence is retained privately under `.builder/current-workers/native-resource-fit`.
Profile mutation/removal rejects stale provenance; a real Python Cargo fixture
observes actual flags/environment, while actual leaders and TERM-resistant
descendants exercise low/unknown-RAM cleanup and absence of successful provenance.
These fixtures do not establish Rust compilation. The cleanup fixture has a
timing-dependent startup interval; source review retained that nonblocking
limitation. Actual native build, compiled tests and registry/contributor/MCP
qualification remain open; generation remains disabled.

# Checkpoint validation — 2026-10-11

This is a runnable development foundation, not the completed CMS/game port.

| Check | Result |
|---|---|
| Builder/coordination/Jev protocol tests | 27 passed, including independent-review and scope-rename regressions |
| Imported TS contracts | 32 passed; ts-rust and matching official TS oracle diagnostics agreed |
| React application | 5 tests passed; both pinned compilers and Vite production build passed |
| Go | Unit tests, race tests, vet and binary build passed |
| Disposable MariaDB/Redis integration | Passed real probes, migration seeding, settings preservation on rerun and checksum mismatch rejection |
| Clean Docker images | Go image built; React image built from source with library build, checks and Vite build |
| Compose runtime | Migration exited 0; CMS and web running; MariaDB and Redis healthy |
| Public HTTP through nginx | health=ok; readiness database=true/redis=true; site.v1 settings revision=1, modern, en/nl |
| Browser | Modern shell rendered and displayed “Site service connected”; desktop screenshot inspected |
| Public-file scan | Known private credential values absent from 111 candidate files before validation document addition; ignore/config/document checks passed |
| Native a6api/Claude | See setup-status.md for real role/model/MCP probes and exact unverified items |
| GitHub Actions | Foundation workflow passed in run38147440561; auth checkpoint adds a real Docker integration job, pending publication |

Startup issues caught and corrected: migrator and CMS now use one explicit image, and nginx joins a separate edge network so Docker can bind 127.0.0.1:8789 while the database/cache/CMS remain on the internal network. No database or cache host port is published.

The native Sol review found two task-board defects; both were source-verified and fixed, with passing regressions. Independent post-fix task-board review passed; all seven coordination tests passed. Coordination is not a process security sandbox; sustained provider spending limits and secret isolation remain gaps.

The foundation checkpoint did not include authentication or housekeeping writes. The later auth checkpoint below records that evidence. Game SSO consumption, full old/current layout parity, production deployment and CMS widget replacements remain unverified. Browser testing of the classic shell and mobile/responsive behavior remains open. Do not count a pure validator, mock, preview or plan as a finished game/CMS feature.

Codex reached the user-requested reserve: 91% consumed in the five-hour window. A private ignored handoff/state checkpoint was saved; the desktop heartbeat checks every two hours for a new reset window and restored allowance. It requires the host/app to remain awake. No reset credit was used and no unlimited provider swarm was launched.

## Authentication and housekeeping checkpoint

All evidence is limited to the bounded existing-account/staff/settings slice, using a separate `phpretro-auth-dev` project on port8790. Previous runtime/theme data and STOP state were preserved.

| Check | Observed result |
|---|---|
| Go | Unit/RFC6238/Java bcrypt vectors, HTTP negative fixtures, race and vet passed, including the changed staff-session hardening |
| React | 18 controller/site tests passed; both pinned TypeScript compilers agreed; Vite production build passed |
| Builder/config | 30 tests passed, including private development-key upgrade and credential preservation |
| Fixture guards | Five tests passed for exact upstream DDL, pinned fixtures and target isolation |
| Real authentication | MariaDB11.8/Redis8.2 suite passed twice, then the changed-source suite passed with staff cookie/Redis deletion and same-role fresh-MFA re-enrollment; bans, grants, audit rollback, concurrency and durable TOTP replay passed |
| Native Sol | Architecture findings fixed; final source review found no P0/P1 blocker and one session-reset hardening item, subsequently fixed and independently checked in source. The focused native Sol follow-up timed out at120 seconds after tool-schema errors without a final verdict; private evidence records actual models/usage and limitations |
| Operator startup | Compose operator enrollment passed with local UID/GID and mode0600; no overwrite and CLI revocation passed |
| Browser | No enabled browser surface was available for this checkpoint. HTTP/controller/SSR checks do not establish interactive browser acceptance |

MFA/role reset now deletes a prior staff session instead of retaining member authentication. Revoke/re-enroll and deletion evidence is recorded in the integration report after the changed-source rerun. Ordinary members never gain administration from game rank. New identity data and enrollment remain explicit operator work; the stack ships no default accounts.

Native CLI token counters and its displayed cost are diagnostic evidence, not verified a6api billing. A configured native turn limit was exceeded by the CLI; only externally bounded wall time was observed. Price enforcement, cross-session doctor improvements and matched Jev savings remain open builder work.

Public-file integrity and local links passed; a scan of all136 candidate public files found none of the known provider/development credential values. Provider transcripts and enrollment files remain ignored/private.

## G05-release-lock — 2026-10-14

The actual bounded private `cargo build --release --locked -p codex-cli --bin
codex --jobs 1` stopped before compilation on the pinned release's lockfile
mismatch. A disposable offline diagnostic resolved only 161 local package
versions from `0.0.0` to `0.162.0`. Independent Sol review and root comparison
verified all 1,473 package records; all other fields and external dependencies
are identical. The deterministic repair reproduces the exact output digest
listed in [native bootstrap evidence](eliza-native-host.md). Trusted source
stays clean at `c1382380de69521303b416720a52f42d51af6248`.

The focused bootstrap suite has 69 checks, including refusal of unknown/symlinked
locks, external-entry preservation, old/wrong provenance rejection, and an actual
controlled compiler that must not record provenance if it mutates the lockfile.
Controlled compiler success does not establish a real Rust build. Runtime guard
remains false; compiled qualification and full-host/browser/live queue acceptance
are pending. Private review/check logs and usage are retained under
`.builder/current-workers/root-lock-repair/`.

Results: 69 bootstrap tests passed in 13.048 seconds; public-file/document/config
and whitespace checks passed. Independent final Sol review accepted the five-file
change without a P0/P1 finding. The repaired `--locked` build has reached actual
Rust/C compilation, but completion and runtime qualification are not yet claimed.

## G06-acceptance-takeover — actual source/loader/schema boundary

Source: Eliza `28fef47c9d9d3acc7792be0fdaa3070020293cc5`; corrected worker
`6453aa07867d2d0473f8d29df7194d2cecf60600`; six CI jobs passed in
run `38570097604`. Independent scoped Sol reviews found deadline, execution-source
and symlink gaps; root repaired each finding and final source review accepted.

Actual evidence, with no host/model call:

- All three reviewed new-chat patch outputs equal independent Git application.
- Full regular-file overlay setup and complete source/wrapper provenance passed.
  Three pinned internal aliases retain exact private targets; optional native
  inference gitlinks are omitted and remain unqualified.
- Actual Bun resolution selects overlay UI source.
- Pinned `scanDropInPlugins` / `findRuntimePluginExport` discover and accept both
  physical packages; companion has one view, native provider exposes four text slots.
- Actual pinned `ElizaSchema.safeParse` accepts private config/workspace.
- Adapter boundary checks cover deadlines, no writes after expired setup,
  captured launch cutoff, canonical aliases, unexpected nested dependency links,
  read-target symlinks, wrapper tampering and missing provenance. Dashboard checks
  capture both child working directories and the Vite path to prevent validation/
  execution mismatch. Configured host and browser acceptance remain pending.

Private receipts live under `.builder/current-workers/root-acceptance-takeover/`
and the neighboring root lock-repair loader-proof directory. Source/loader/schema
success does not establish host startup, browser mounting, a native model turn,
queue recovery or complete CMS/game coverage.

Integrated-root results: 22 adapter tests passed in 0.527 seconds and 59 dashboard
tests passed in 11.337 seconds. The actual root prerequisite check returned 0;
snapshots of 32,918 acceptance paths showed identical namespace, inode, mode,
size and modification time before/after. Independent Sol review accepted the
no-write check adjustment (`d40fc9f`). Public-file/document/config and whitespace
checks passed. No host or model was started.

Native build continuation: repaired locked build reached compilation, timed out
at 1,200 seconds, then a second bounded attempt reached `codex-core` and was
stopped at 2,806 MiB available RAM, below the 3,072 MiB project floor. Owned group
cleanup completed and cache/logs were retained. No native build/test success is
claimed; a reviewed build plan within the resource floor is pending.

G06 dependency-binding correction: the actual first supervised production entry
failed before runtime initialization on missing package-local `markdown-it`.
API/UI children were cleaned up. Candidate `3f0be34` repairs all 131 installed
Bun dependency locations, mapping workspace links to the private overlay and
retaining external installed cache targets. Exact members/scopes/link targets
are checked; only known old canonical links can migrate. Independent Sol review
accepted the repair, and a noted empty-scope gap was additionally closed with a
passing regression. Actual full binding verification and Bun resolution passed
without modifying the captured host cutoff. Root adapter suite: 24 tests passed
in 0.522 seconds. A fresh bounded runtime/browser acceptance remains open.
Private evidence: `package-resolution.result.json`, `dependency-review.json`,
`live-start.json.log` and owned supervisor logs under the existing private
acceptance state. The failed startup is not a passing full-host qualification.
