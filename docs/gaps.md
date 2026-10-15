# Known gaps against the requested PHPRetro port

Last reviewed/updated: 2026-10-14. Target: pinned PHPRetro feature behavior, Polaris/Octane compatibility, and the current Habbo public-site layout recreated with original assets. This structure is inspired by [PhotoCraft's gaps ledger](https://github.com/storytold/photocraft/blob/52e130d481c9017b6ec8321784776b33b04ad2c4/docs/gaps.md); project facts and estimates are specific to this port.

This is the work queue. Every entry has one owner/claim in the durable task store, evidence and an exit condition. Implementation coverage is **not yet measured**. A planning document is not a closed application feature. Priority is release risk and user impact, then dependency order. Estimates remain uncalibrated until a real vertical slice is complete.

| ID | Gap | Priority / gate | State | Next evidence needed |
|---|---|---|---|---|
| G01 | Actual Go/React/MariaDB/Redis foundation | P0 / alpha | Verified development foundation | production deployment and recovery remain G20 |
| G02 | Polaris identity/password/schema adapter | P0 / alpha | Partial: real identity adapter verified | registration/reset and full emulator migration |
| G03 | SSO expiry, one-use/races, Octane launch/reconnect | P0 / alpha | Upstream discrepancy; port missing | real emulator expiry/replay/concurrency tests |
| G04 | Authentication, staff MFA/RBAC, CSRF and revocation | P0 / alpha | Partial: existing-account login, staff MFA and settings authorization verified | reset/registration and remaining module permissions |
| G05 | Direct DeepSeek workers and Codex subscription coordinator | P0 / builder | Partial: direct routes and separate native Sol readiness/help verified | full Eliza main-model handler; enforced provider budgets and account-specific cloud features |
| G06 | Eliza orchestration, reviewed worker integration and budget enforcement | P0 / builder | Partial: real companion runtime/SQL/ACP fixture boundary; pinned full app checked separately | reviewed full-app plugin/dashboard integration; live worker/queue acceptance and pricing enforcement |
| G07 | Original/authorized assets for CMS and Octane | P0 / public release | Missing | provenance catalogue plus replaced embedded/client resources |
| G08 | Structural theme ABI and third-party theme adapters | P1 / beta | Specified, missing | both themes and a sample imported theme use same DTOs |
| G09 | Housekeeping locale controls/content/catalogue fallback | P1 / beta | Partial: audited enabled/default en/nl controls | content/catalogue fallback, plural and RTL fixtures |
| G10 | Modern public-page layout/state fidelity | P1 / beta | Homepage observed; implementation missing | pinned route/state visual catalogue and original artwork |
| G11 | Legacy public-page layout/state fidelity | P1 / beta | Source inventoried; implementation missing | characterization fixtures, both desktop/mobile behavior |
| G12 | Avatar/badge/Trax/promotional SWF replacements | P1 / beta | 8-file inventory complete; implementation missing | widget contract tests and approved assets/audio |
| G13 | Content/news/RSS/profiles/friends/support | P1 / beta | Partial: tested profile/account projections | actual endpoints, feature parity fixtures and real DB tests |
| G14 | Homes/widgets/guestbooks/groups/forums/minimail | P1 / full parity | Missing | ownership/moderation/state and XSS fixtures |
| G15 | Housekeeping's 22 original modules and new controls | P1 / full parity | Missing | audited permission matrix and real Polaris commands |
| G16 | Club/vouchers/credits/shop and game-domain mappings | P1 / full parity | Missing | wallet ownership/idempotency and capability evidence |
| G17 | Reversible legacy migration and game-data mapping | P0 / migration release | Missing | dry run, collisions, hash upgrade/reset, restore rehearsal |
| G18 | ts-rust toolchain fit and same-upstream compiler oracle | P2 / foundation | Verified for current DTO/JSX slices | broaden fixtures when toolchain or contracts change |
| G19 | Aggregate token/cost analytics and matched Jev experiments | P1 / builder efficiency | Specified, missing | per-run usage, pricing provenance, matched correctness |
| G20 | Performance, accessibility, observability and disaster recovery | P1 / public release | Missing | measured budgets, theme/locale matrix, restore report |
| G21 | Authenticated current-Habbo reference states | P2 / measured fidelity | External reference missing | an authorized reference session or independently approved spec |

## Highest-risk evidence

- **G02/G03:** Legacy `users.name`/`ticket_sso` differs from Polaris `username`/`auth_ticket`. Polaris's game consumer ignores expiry and clears ticket state by user ID. A CMS-side timeout is insufficient. See [emulator contract](emulator-contract.md).
- **G07:** PHPRetro headers separately attribute images/scripts/layouts to Sulake; Octane explicitly embeds some bitmaps extracted from `HabboAir.swf`. Code license does not establish asset ownership. A placeholder is not a completed original-art replacement.
- **G12:** Bundled SWFs are website widgets; the old hotel embed is an external Director DCR. See [Flash modernization](flash-modernization.md).
- **G17:** Old installer drops CMS tables and can truncate game users. New migration starts read-only and runs only on explicit disposable/rehearsal targets until validated.
- **G19:** `/doctor` is not an all-session savings report. Jev calls have been used for evidence triage; no savings are claimed without matched comparisons.

## G05/G06 Eliza boundary checkpoint — 2026-10-13

Eliza-first work currently pauses further CMS/theme/game-port implementation. The root companion uses actual published Eliza runtime/SQL/orchestrator packages with controlled ACP and bridge fixtures. The full upstream CLI/API/UI is pinned separately as a submodule at `28fef47c9d9d3acc7792be0fdaa3070020293cc5`; source installation, first-run browser rendering and later owner-login/DeepSeek chat checks passed in a separate reference checkout. This is not proof of a reproducible full-app launch from the root integration or a configured Sol main-chat handler. Project plugin/dashboard fixes and live worker/queue acceptance remain pending. See [integration evidence and launch boundary](eliza-integration.md). No full-port percentage or 100% completion is claimed.

## G05 native qualification correction — 2026-10-14

The installed raw ELF `codex-cli 0.162.0` matches the official release from source commit `c1382380de69521303b416720a52f42d51af6248`. The experimental schema defines the isolation fields that the earlier stable-only check missed. This confirms metadata support; generation remains blocked because the effective toolset is unqualified. Source inspection found an empty `ToolPolicy` ceiling, but no supported stock startup input for it. The host path, build provenance, runtime registry proof and hard-parent cleanup remain open. All 31 generator tests and 19 controlled JS fixtures pass; six GitHub jobs passed for both push and PR on `e540a9c`. Root upstream installation and CLI help passed. Configured full-host startup, browser mounting, live queue and main-Sol acceptance remain open. **G05 and G06 stay Partial.** See [qualification evidence and acceptance plan](eliza-native-qualification.md).

The G06 `proc <X> dead` terminate-group assertion repair (`2ac672d`) is **test-only** and does not close G06.

The native provider captures one caller deadline before metadata and caps its subprocesses and account/thread/turn RPCs by the remaining budget. Hard-parent cleanup and the complete native tool-registry proof remain unqualified, so native generation stays blocked.

**2026-10-15 provider scheduling boundary:** fixed-provider Codex subscription
generation and pinned Eliza source/runtime setup are provider-free with respect
to DeepSeek and use bounded caller deadlines. Direct DeepSeek calls retain their
off-peak gate. The full Eliza CLI remains gated because configured behavior may
dispatch inference; the dashboard and acceptance host retain their existing
DeepSeek controls. Focused mock tests do not establish hosted/browser acceptance.

**2026-10-15 provider-free build scheduling:** the native `build` command and
hosted qualification now capture one bounded caller deadline, independent of
the DeepSeek off-peak schedule. The standalone `source`/`toolchain` preparation
commands retain their existing gate. RAM/disk preflight, source/patch/lock
provenance, supervised process-group cleanup and `native_admission=false` are
unchanged. Hosted run `38717087713` on `a54f491` was refused by the old wrapper
gate before compilation or tests; this change has not yet had a hosted run.
Compiled qualification remains unverified and G05 stays Partial.

**2026-10-15 direct worker preflight:** Eliza's `deepseek-acp.py` now checks the
canonical coding window before querying DeepSeek balance. The later supervised
`deepseek-slots.py` gate still owns worker launch cutoff and four-worker
admission. Offline behavior tests prove a closed window prevents funding and
adapter launch, and an open window preserves both in order. This does not prove
live worker acceptance or enforce a spend ceiling; G05/G06 remain Partial.

## Entry update format

For each completed slice append: task/issue ID; exact changed behavior; source/reference revision; checks run and results; commit/PR; remaining limitation; status (`missing`, `partial`, `verified`, `blocked-external`). A passing mock or a code-text assertion cannot close an integration gap. Store verbose test artifacts privately or in a sanitized evidence index rather than pasting transcripts here.

New findings become a gap before their corresponding work is declared done. Reopen a verified gap when its contract or upstream revision changes and acceptance evidence no longer applies.

## G06.1 process-cleanup race checkpoint — 2026-10-12

The continuation runner's operational test now treats only ENOENT/ESRCH while reading a grandchild's `/proc/<pid>/stat` as a gone process. Permission/I/O failures and malformed stat content still fail the test; live process states remain rejected. The real leader/grandchild launch and production `terminate_group` remain unchanged. Zombie state or disappearance establishes that the grandchild is not running, not that it was reaped. This test-only repair does not close G06; automated integration and pricing enforcement remain open. Evidence/check results are in [validation](validation.md).

The dashboard's corresponding real-process-group test accepts `X` in addition to `Z` and `gone`, matching Linux's documented dead state for `/proc/<pid>/stat`; this only corrects the test's terminal-state observation.

## G02/G04/G09 implementation checkpoint — 2026-10-11

Existing Polaris identities are read through column-scoped grants, with exact epoch/bcrypt/ban semantics. CMS staff enrollment is independent of game rank; fixed-lifetime Redis sessions observe live password/version and staff credential revisions. TOTP replay is enforced durably in MariaDB. Housekeeping updates site/theme/languages using locked authorization, revision conflicts and atomic audit. React implements login/logout, staff settings and conflict/error handling with memory-only CSRF.

Evidence: [auth design](auth-design.md), [real integration](auth-integration.md), [validation](validation.md). This is partial port coverage: no game ticket, account creation, email reset, full CMS module authorization or measured layout parity. The native Sol review's session-reset finding was addressed by deleting old staff sessions; follow-up evidence is retained privately. Public commit/PR identifiers are obtained after checks and publication rather than guessed here.

## G05/G06 operator workflow amendment — 2026-10-11

The desktop launcher now uses the active native project, colours and operator-requested bypassPermissions. Project-only Doctor context and saved native builder sessions are indexed on demand by `/port-context`; old failed-project histories are excluded. Builders cannot disable session persistence. A guarded30-minute local continuation schedule is configured; native provider price/cash enforcement and full game/account integration remain open. Session availability does not establish savings or complete migration coverage. Acceptance evidence for Doctor, history registration and the continuation lock is recorded separately after actual runs/tests.

Doctor acceptance: actual native /doctor completed installation/settings/extensions checks; /doctor prompt-audit completed a static project guidance audit with an advisory report retained privately. Same-project ListAgents/SendMessage produced a real Luna acknowledgement. 63 operational tests pass, including actual child/grandchild cleanup, separate-process scheduler lock, concurrent history registration, complete Unicode pagination and failed-project exclusion. Cash budgets/prices, cloud account authentication, and full CMS/game parity remain unresolved.

## G05 reviewed guardian and host source artifact — 2026-10-14

The Linux guardian prerequisite now passed independent review after repairing observation helpers and deterministic startup-race evidence. Root verified 83 Python checks (45 generator, 25 guardian, 13 unchanged bridge) and 19 model-handler fixtures. The central immutable empty-ceiling source patch, provenance, Apache license and upstream NOTICE are published; clean-pin apply and file digests were independently verified. The patch remains **unbuilt and unqualified**, and the runtime guard remains false. Native bootstrap preparation is not accepted for installation/build yet; complete Rust startup/registry proof, configured full-host startup, browser mounting and funded live queue/escalation remain open. **G05/G06 remain Partial.**

## G05 native bootstrap and qualification sources — 2026-10-14

The reviewed native-host bootstrap verifies the actual public source/patch contract, HTTPS Rust manifest sidecar/archive hashes, private paths and resources, and bounds download, archive decompression, installation and child-group cleanup by the original cutoff. Three real acceptance failures triggered a small Sol extraction-timer repair; 64 focused and 30 upstream checks passed. Real pinned-source inspection succeeded. The first private Rust toolchain installation is in progress; no native binary or compiled qualification result is claimed. Fifteen source-reviewed startup/registry/adversarial tests are published as ordered patches, explicitly uncompiled/unrun. Full MCP/contributor and compiled CLI admission, actual full-host/browser and live queue checks remain open. G05/G06 stay Partial.

## G05 release lock mismatch — 2026-10-14

Task G05-release-lock; pinned Codex source
`c1382380de69521303b416720a52f42d51af6248`. A real bounded build exposed a
release-local `Cargo.lock` version mismatch before compilation. The bootstrap
now performs an exact input/output-hash-bound correction of 161 local package
versions in its private build copy, retaining `--locked` and all external
package versions/checksums/dependencies. Independent Sol source comparison
verified the diagnosis; persisted lock digests are checked before/after Cargo
and bound to build provenance. Exact source comparison and focused regression
checks are recorded in [validation](validation.md) and
[bootstrap evidence](eliza-native-host.md). Native build, compiled qualification,
MCP/contributor coverage and full-host/browser/live queue admission remain open.
**G05/G06 remain Partial.**

## G06 isolated host adapter and actual loading boundary — 2026-10-14

Task G06-acceptance-takeover; Eliza pin
`28fef47c9d9d3acc7792be0fdaa3070020293cc5`. The corrected adapter passed
independent Sol source review and all six candidate CI jobs on `6453aa0`.
Actual full private overlay setup and provenance checks passed; Bun resolves the
patched overlay UI, the pinned loader discovers both physical companion/native
packages, and the actual config schema accepts the generated private workspace.
Caller deadlines reach setup and launch validation; unknown source changes,
foreign symlinks, wrapper tampering and missing provenance are refused. The
supervisor now executes the same source it validates, preserving the dashboard
route and isolated-source callers. See [host evidence](eliza-acceptance-host.md)
and [validation](validation.md). Configured full-host startup, authenticated
owner/user/browser mounting, native text admission and funded live queue remain
open. **G05/G06 stay Partial.**

G05 resource evidence: two bounded repaired-lock builds reached actual dependency
and workspace compilation; the first timed out at 1,200 seconds, and the second
reached `codex-core` before root stopped it when available RAM fell below the
3 GiB desktop floor. These are time/resource checkpoints, not passing build or
Rust qualification evidence. Native guard remains false; no completed CLI binary,
compiled qualification or main-model admission is claimed.

G06 actual startup exposed a package-dependency gap: the supervised production
entry stopped before runtime initialization because the private core package
could not resolve `markdown-it`. Both children were cleaned up. The reviewed
repair now binds all 131 actual installed dependency locations, remaps workspace
imports into the overlay and verifies exact members/scopes/targets. Actual Bun
resolution of the missing dependency and overlay UI passed; 24 adapter boundary
checks pass. The original acceptance cutoff expired and was not extended.
A fresh bounded configured-host/browser run is still required. Source bindings
are verified; **G06 remains Partial**, with no runtime or native admission claim.

## G05 resource-fit build contract — 2026-10-14

Task G05-native-resource-fit; reviewed worker `2f6f192`, integrated on the feature
branch as `8fdc2be`. A fixed release profile now disables optimization/LTO and
incremental compilation, uses 256 codegen units and no debuginfo, and binds all
profile values to build provenance/freshness. The build supervisor checks available
RAM before launch and throughout compilation; low or unknown RAM stops its owned
group and prevents successful provenance publication. Root verified 87 focused
tests and 30 upstream tests; worker CI `38612139952` passed. These tests include
real process cleanup and a subprocess argument/environment fixture, not a Rust
compile. Actual resource fit, compiled qualification and native admission remain
open. The generation guard remains false. See [validation](validation.md).

The first real resource-fit compile exited 1 at `codex-app-server-protocol`
without a completed binary or a compiler error diagnostic. Its supervisor does
not retain a distinct resource-stop reason, so the precise cause is unproven.
The build and continuation locks were released. Stop-reason evidence is a new
bounded prerequisite before another justified attempt; compiled qualification
remains open.

## G06 fresh run isolation and runtime vault boundary — 2026-10-14

Task G06-fresh-acceptance-runs; worker `077dcdb`, feature integration `2221edd`.
Validated run IDs select isolated state, overlay, database and home directories.
Saved cutoff bytes remain immutable; a shared admission lock covers setup and
foreground supervision. A source-review finding about shorter launch deadlines
was repaired before integration. Root ran 107 adapter/dashboard tests with no
skips (19.277 seconds); worker CI `38616073837` passed. Independent Sol re-review
accepted the final source. See [validation](validation.md).

Actual setup of a fresh run succeeded. Production startup progressed past the
dependency boundary and stopped because the headless local vault had no master
key. Per-run local credentials are now a separately reviewed prerequisite;
actual authenticated OWNER/USER transport, browser mounting, two-room new-chat
persistence/reload and live queues remain unverified. A privately installed
Chromium launched successfully, which establishes browser availability only.
The expired original run and operator state were preserved. G05/G06 stay Partial.

## G05/G06 reviewed continuation — 2026-10-14

The accepted stop-evidence repair (`3d0f203`, feature `bb46b53`) now records
atomic, sanitized failure receipts for pre-launch RAM refusal, launch/log I/O,
cleanup and child failures, with explicit compiler-child scope. Root verified
103 focused tests; worker CI `38623378307` passed. It does not establish the
cause of the earlier failure or a completed native binary.

The extension/hosted contributor artifact (`4456779`, feature `0961dfe`) passed
independent source review and actual ordered apply/hash checks. Invalid
`Arc::make_mut` calls on non-Clone Session/TurnContext were repaired. Its four
tests remain uncompiled/unrun; real MCP catalog injection, executor dispatch
and nested mappings remain open.

Per-run local credentials (`7a2615a`, feature `305b94c`) passed independent
review after FIFO-safe loading and complete parent validation fixes. Worker CI
`38622569264` passed; root verified 121 candidate tests and actual pinned vault
recovery across fresh processes. A fresh isolated production host reported
runtime-ready. Live HTTP checks returned owner/no-Origin status 200, absent
credentials 401, foreign Origin 403, and a 29-view catalog. The configured UI
Origin was refused with `origin-not-configured`; its separate companion setting
still needs wiring. USER/gateway acceptance, browser mounting and two-room
new-chat persistence/reload remain open. No native model admission follows from
the application's ready status. The bounded host stopped; its test namespace
was retired after an uncaught browser connection error exposed its ephemeral
test token. No credential was committed.

The CI build design was reviewed for a larger hosted runner. Candidate `d59f8b8`
was rejected for result parsing, unused parent-environment filtering, missing
stop evidence/final preservation checks and stale artifact contents. A repair
`d7fc668` now addresses all five findings (module-qualified Rust result matching
for the 19 published tests, full child-environment isolation, truthful
stop/build receipts, post-test preservation rechecks, fresh atomic artifact
publication); a **DeepSeek** bounded review passed all four checked scopes with
no confirmed defects, `tests.ops.test_native_host_ci` Ran 59 tests OK and
`tests.ops.test_native_host_setup` Ran 103 tests OK, and Verify CI passed on the
worker branch. It is integrated as `bb9dc77`, and exact-SHA standard Verify CI
passed for that push (`38641368485`). The push-only workflow then dispatched its
first real hosted Ubuntu 24.04 run (`38641368461`), which refused in 9s with
`"qualified": false, "reason": "os error during preflight"`: the wrapper called
`shutil.disk_usage` on a private build root that a fresh checkout has not
created yet. Creating that root (idempotent, before any heavy work) is repaired
in the following commit `fbad50b`, together with a regression test for exactly
this fresh-root case (`tests/ops/test_native_host_ci.py`, class
`OrchestrationFixtureTests`, stubbing `preflight_resources` to raise
`FileNotFoundError` on an absent path). Removing the `mkdir` makes that test fail
with the hosted reason string; an independent read-only DeepSeek review of the
test commit found no confirmed defects. `tests.ops.test_native_host_ci` Ran 60
tests OK at the integrated tip and
`tests.ops.test_native_host_setup` Ran 103 tests OK against the repaired source,
and an independent read-only DeepSeek review of the repair found no confirmed
defects (it also flagged, as a non-blocking latent pattern, the same
missing-directory assumption at `scripts/native-host.py:1527`, which
`ensure_toolchain` currently masks). The repaired tip `fbad50b` is pushed;
exact-SHA standard Verify CI passed (`38643807820`). Two hosted runs then reached
the **next** stage and failed there: `38643808015` (`fbad50b`) and `38645240448`
(`a09be6d`) each ran ~29 minutes and refused with
`"reason": "locked online test preparation failed (exit 101)"` — the supervised
`cargo test --no-run -p codex-core --lib` at `scripts/native-host-ci.py:776-788`.
Because the failure artifact is receipt-only, the compiler text never leaves the
runner. The fix therefore makes the diagnostic observable rather than guessing:
the failure receipt now carries a bounded, sanitized excerpt of the failing
stage's own log (`DIAGNOSTIC_MAX_LINES=40`, `DIAGNOSTIC_MAX_CHARS=4000`, URL →
`$HOME` → absolute-path → `KEY=value` redaction, ≤256 KiB tail read), and
`stage_log` is now assigned per stage boundary so a `test-patches` failure can
never inherit the build log. An independent DeepSeek review **rejected the first
revision** for exactly that stale-`stage_log` mis-attribution and passed the
amended revision; `tests.ops.test_native_host_ci` Ran 72 tests OK and
`tests.ops.test_native_host_setup` Ran 103 tests OK. No compiled binary,
artifact import or compiled qualification has been verified, and no admission
follows. **G05/G06 remain Partial.**

The next hosted run, `38655519222` on `0631a53`, reached `offline-tests` and
exited 101. Its receipt showed no test outcome or summary lines and only Cargo's
generic `test failed` message; the available artifact/logs do not establish which
test or assertion failed. Source review found the receipt's stage diagnostic
always used the compiler-only line filter, which discarded the Rust test
harness's failure name, summary and panic location. The bounded offline-test
excerpt now retains those structural lines while dropping assertion/stdout
payloads. This fixes future failure evidence only; it does not recover the
missing hosted log or establish that any qualification test passes. Admission
remains false and G05/G06 remain Partial.

Hosted run `38727894425` on `6c8be60` again exited 101 in `offline-tests` after
about 24 minutes. Its published artifact contains only `qualification-receipt.json`;
the receipt has Cargo's generic failure line but no harness result, and no stage
log was available for inspection. The exact failure cause therefore remains
unknown. The stage capture source still records combined stdout/stderr to its
own bounded log, but the offline-test receipt filter previously discarded Cargo
process-launch details that could explain a failure before the test harness
started. The filter now retains only normalized process exit status, signal,
OS-error number, and a narrowly matched missing shared-library basename after
Cargo's generic failure line. It still drops raw commands, paths, loader text,
and arbitrary test stdout. Fixture tests cover these cases. This improves
future evidence only; it does not recover run `38727894425`'s log or identify its
cause. Admission remains false and G05/G06 remain Partial.

Hosted run `38764959443` on `d0daf32` again failed in `offline-tests`. Its
receipt records only Cargo's generic test failure, `Caused by:`, and normalized
child signal 6 (`SIGABRT`); the selected-test outcomes are all absent. The
receipt-only artifact has no raw stage log, so this proves an abort but does not
identify an assertion, the active test, or a pre-main initialization fault. The
bounded sanitizer also discarded libtest's partial `test <name> ...` progress
line, which Rust 1.95 flushes before a serial test runs. It now retains only
that constrained qualified name, labeled as started with no result, including
when Cargo's exact generic failure line shares the physical line; arbitrary
suffix text, test stdout and assertion payloads remain excluded. This may
identify the last started test on a future failure, but it cannot establish the
cause of this archived abort. Admission remains false and G05/G06 remain
Partial.

Companion trusted-origin wiring (G06, 2026-10-14, direct DeepSeek):
`scripts/eliza-dashboard.py` `build_env` now exports
`PHP_RETRO_COMPANION_TRUSTED_ORIGINS` into the API child environment only, from
the immutable `profile.allowed_origins`, on both return paths; ambient values
cannot override it and the UI child never receives it. Worker commit `8e9e4b7`
plus a doc-qualifier `5e0e863`, integrated as `29dc04b`/`1586212`. DeepSeek
independent source review found no defects; standard Verify CI passed for both
worker-branch SHAs (`38638478078`, `38638840061`); root `tests.ops.test_eliza_dashboard`
Ran 66 tests OK and `scripts/repo-check.py` passed. Live verification followed on
2026-10-14 in a fresh namespace `acceptance-20261014-v41a` against `db95e6d`: an
isolated acceptance host reached runtime-ready with `api`/`ui` running, and the
API on `127.0.0.1:18831` answered unauthenticated `/api/status` and `/api/views`
with 401, an empty bearer with 401, a foreign `Origin` with 403, the owner bearer
with 200, and **owner bearer + the same UI origin** with 200 — the case that
previously returned 403 `origin-not-configured` — while the foreign origin stayed
403. The run was stopped cleanly (`terminated: ["api","ui","supervisor"]`) and no
listener remained. A second probe in the same window found the **UI never served
a document** (`mounted:false`, one failed request, `TimeoutError`) because the
overlay copies only pinned tracked source (`_tracked_paths` → `git ls-files`), so
a workspace member whose `exports` declare no source condition resolved its
`./register` leaf to a `dist/*.js` the overlay never contains
(`ui.log:101` `Failed to resolve import
"@elizaos/plugin-native-settings/register"`). `dependency_bindings` now keeps
such a built-output member on its verified canonical binding while every
source-complete member still binds to the private overlay copy; a focused
regression test fails against the pre-fix binding. An independent DeepSeek review
passed the direction and narrowed behaviour but recorded two **latent**
predicate gaps — a bare `"source"` condition (never passed by the children) and
array/string-form `exports` are not covered — so the docstring overstates the
case. A live re-probe in the next namespace (`acceptance-20261014-v41c`) then
**confirmed the first of those predictions**: the original
`plugin-native-settings` resolution error was gone, but the identical failure
appeared for `@elizaos/plugin-trajectory-logger/register` (`ui.log`). A second,
final revision resolves `exports` the way the pinned children do (only
`eliza-source` is a source condition; string, array and nested-map shapes are
followed) and keeps every reviewed overlay-patched member on the overlay copy; an
independent DeepSeek review passed it while noting the condition-order mirror and
some export shapes remain unexercised, and lives the resolution class is now
clean: in `acceptance-20261014-v41d` the pre-transform errors are **zero** and
Vite reports `ready in 12979 ms`. That exposed a **separate, still-open blocker**:
the document request hangs rather than failing — in `acceptance-20261014-v41e`
`curl --max-time 150 http://127.0.0.1:18832/` received **0 bytes after 150 s**
(`mounted:false`, `requestFailures:1`) while the API matrix stayed correct
(unauth 401, empty bearer 401, foreign origin 403, owner 200,
owner+same-UI-origin 200). Each probe stopped its host cleanly
(`terminated ["api","ui","supervisor"]`). Still unqualified: browser/UI mounting
(blocked on that hang), the USER (non-owner) boundary, chat/two-room history and
reload (generation is not admitted), and any native/compiled claim; no
native/generation admission follows. The native CI-wrapper repair is integrated
separately (see the paragraph above).

## G06 UI dependency scan boundary — 2026-10-15

The pinned acceptance overlay's workspace links form normal dependency cycles.
A native filesystem trace identified `@tailwindcss/oxide` 4.3.3 traversing those
cycles under the desktop platform's `node_modules`, blocking the Vite main
thread. Before the change, `/`, `/site.webmanifest` and `/@vite/client` all
returned no bytes before a 20-second timeout; UI RSS reached approximately
2.6 GiB after 90 seconds. A short trace counted 108,036 filesystem/signal calls
in four seconds. These are local diagnostic observations, not a benchmark.

The reviewed overlay patch explicitly excludes dependency directories from
Tailwind class discovery with `@source not "../../../../**/node_modules"`.
Existing app, shared UI and plugin source registration stays intact. The
canonical upstream checkout is unchanged; the stylesheet joins the exact
changed-file allowlist and the patch digest is updated. In a controlled UI-only
run, the manifest returned 200 in 0.08s, the HTML document 200 (26,336 bytes) in
0.19s, and the Vite client 200 in 0.08s. This establishes request responsiveness;
real browser mounting, authenticated views and room persistence still require
full-host acceptance. Private traces remain under `.builder/evidence/`.

## G06 physical drop-in browser assets — 2026-10-15

Real browser pairing and launcher rendering now pass after the source-scan fix.
A guest machine session receives 403 for the owner-only companion route and
bundle, and the companion is absent from that guest's view catalog. Owner
pairing persists across browser reloads. Actual DynamicViewLoader navigation
then exposed a separate packaging defect: the installation-scoped companion
bundle returned 404 because the physical drop-in contained only a JavaScript
wrapper, not its declared `dist/views/bundle.js` asset.

The acceptance installer now copies that fixed browser asset into the physical
drop-in, without moving the canonical module or changing its relative imports.
Setup provenance records the asset hash; readiness checks both canonical and
installed bytes and refuses missing, modified or linked installed assets.
Legacy namespaces without asset provenance refuse setup before installation and
require a fresh run id; their saved provenance and credentials are preserved.
127 acceptance/dashboard checks pass (two canonical-source checks skipped in the
isolated worker checkout). Real browser mounting of the copied bundle remains
pending; this change alone does not close G06 or enable native generation.
