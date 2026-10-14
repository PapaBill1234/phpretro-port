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

The native provider now captures one deadline before metadata and caps its subprocesses and account/thread/turn RPCs by the remaining budget. Five seconds are reserved from the gate for normal cleanup; a short caller timeout remains usable. Forty-three generator tests pass, including capped metadata arguments and no subprocess launch after expiry. Hard-parent cleanup and the complete native tool-registry proof remain unqualified, so generation stays blocked.

## Entry update format

For each completed slice append: task/issue ID; exact changed behavior; source/reference revision; checks run and results; commit/PR; remaining limitation; status (`missing`, `partial`, `verified`, `blocked-external`). A passing mock or a code-text assertion cannot close an integration gap. Store verbose test artifacts privately or in a sanitized evidence index rather than pasting transcripts here.

New findings become a gap before their corresponding work is declared done. Reopen a verified gap when its contract or upstream revision changes and acceptance evidence no longer applies.

## G06.1 process-cleanup race checkpoint — 2026-10-12

The continuation runner's operational test now treats only ENOENT/ESRCH while reading a grandchild's `/proc/<pid>/stat` as a gone process. Permission/I/O failures and malformed stat content still fail the test; live process states remain rejected. The real leader/grandchild launch and production `terminate_group` remain unchanged. Zombie state or disappearance establishes that the grandchild is not running, not that it was reaped. This test-only repair does not close G06; automated integration and pricing enforcement remain open. Evidence/check results are in [validation](validation.md).

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
in the following commit; `tests.ops.test_native_host_ci` Ran 59 tests OK and
`tests.ops.test_native_host_setup` Ran 103 tests OK against the repaired source.
No compiled binary, artifact import or compiled qualification has been verified,
and the hosted runner has not re-run since the repair. **G05/G06 remain
Partial.**

Companion trusted-origin wiring (G06, 2026-10-14, direct DeepSeek):
`scripts/eliza-dashboard.py` `build_env` now exports
`PHP_RETRO_COMPANION_TRUSTED_ORIGINS` into the API child environment only, from
the immutable `profile.allowed_origins`, on both return paths; ambient values
cannot override it and the UI child never receives it. Worker commit `8e9e4b7`
plus a doc-qualifier `5e0e863`, integrated as `29dc04b`/`1586212`. DeepSeek
independent source review found no defects; standard Verify CI passed for both
worker-branch SHAs (`38638478078`, `38638840061`); root `tests.ops.test_eliza_dashboard`
Ran 66 tests OK and `scripts/repo-check.py` passed. This is a source-only change:
live same-UI-Origin acceptance, browser mounting and USER/gateway boundaries
remain unqualified and no native/generation admission follows. The native
CI-wrapper repair is integrated separately (see the paragraph above).
