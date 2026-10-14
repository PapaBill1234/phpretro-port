# Known gaps against the requested PHPRetro port

Last reviewed/updated: 2026-10-13. Target: pinned PHPRetro feature behavior, Polaris/Octane compatibility, and the current Habbo public-site layout recreated with original assets. This structure is inspired by [PhotoCraft's gaps ledger](https://github.com/storytold/photocraft/blob/52e130d481c9017b6ec8321784776b33b04ad2c4/docs/gaps.md); project facts and estimates are specific to this port.

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
