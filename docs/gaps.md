# Known gaps against the requested PHPRetro port

Last reviewed/updated: 2026-10-11. Target: pinned PHPRetro feature behavior, Polaris/Octane compatibility, and the current Habbo public-site layout recreated with original assets. This structure is inspired by [PhotoCraft's gaps ledger](https://github.com/storytold/photocraft/blob/52e130d481c9017b6ec8321784776b33b04ad2c4/docs/gaps.md); project facts and estimates are specific to this port.

This is the work queue. Every entry has one owner/claim in the durable task store, evidence and an exit condition. Implementation coverage is **not yet measured**. A planning document is not a closed application feature. Priority is release risk and user impact, then dependency order. Estimates remain uncalibrated until a real vertical slice is complete.

| ID | Gap | Priority / gate | State | Next evidence needed |
|---|---|---|---|---|
| G01 | Actual Go/React/MariaDB/Redis foundation | P0 / alpha | Partial: pure Go/frontend contract subset | clean-checkout app, real migrations, Redis outage fixture |
| G02 | Polaris identity/password/schema adapter | P0 / alpha | Specified, missing | live MariaDB contract tests, bcrypt interoperability |
| G03 | SSO expiry, one-use/races, Octane launch/reconnect | P0 / alpha | Upstream discrepancy; port missing | real emulator expiry/replay/concurrency tests |
| G04 | Authentication, staff MFA/RBAC, CSRF and revocation | P0 / alpha | Missing | negative role/IDOR/session tests |
| G05 | a6api/Claude Code transport and model-role compatibility | P0 / builder | Readiness audit in progress | tool-call and subagent smoke tests, truthful usage metadata |
| G06 | Coordinated worker claims, reviewed integration and budget enforcement | P0 / builder | Partial: atomic local task board; scheduler/price limiter missing | crash/resume and automated integration checks; pricing enforcement |
| G07 | Original/authorized assets for CMS and Octane | P0 / public release | Missing | provenance catalogue plus replaced embedded/client resources |
| G08 | Structural theme ABI and third-party theme adapters | P1 / beta | Specified, missing | both themes and a sample imported theme use same DTOs |
| G09 | Housekeeping locale controls/content/catalogue fallback | P1 / beta | Missing | locale-switch/cache/plural/RTL fixtures |
| G10 | Modern public-page layout/state fidelity | P1 / beta | Homepage observed; implementation missing | pinned route/state visual catalogue and original artwork |
| G11 | Legacy public-page layout/state fidelity | P1 / beta | Source inventoried; implementation missing | characterization fixtures, both desktop/mobile behavior |
| G12 | Avatar/badge/Trax/promotional SWF replacements | P1 / beta | 8-file inventory complete; implementation missing | widget contract tests and approved assets/audio |
| G13 | Content/news/RSS/profiles/friends/support | P1 / beta | Partial: tested profile/account projections | actual endpoints, feature parity fixtures and real DB tests |
| G14 | Homes/widgets/guestbooks/groups/forums/minimail | P1 / full parity | Missing | ownership/moderation/state and XSS fixtures |
| G15 | Housekeeping's 22 original modules and new controls | P1 / full parity | Missing | audited permission matrix and real Polaris commands |
| G16 | Club/vouchers/credits/shop and game-domain mappings | P1 / full parity | Missing | wallet ownership/idempotency and capability evidence |
| G17 | Reversible legacy migration and game-data mapping | P0 / migration release | Missing | dry run, collisions, hash upgrade/reset, restore rehearsal |
| G18 | ts-rust toolchain fit and same-upstream compiler oracle | P2 / foundation | Inspected, untested here | JSX/DTO/project-reference fixtures with pinned versions |
| G19 | Aggregate token/cost analytics and matched Jev experiments | P1 / builder efficiency | Specified, missing | per-run usage, pricing provenance, matched correctness |
| G20 | Performance, accessibility, observability and disaster recovery | P1 / public release | Missing | measured budgets, theme/locale matrix, restore report |
| G21 | Authenticated current-Habbo reference states | P2 / measured fidelity | External reference missing | an authorized reference session or independently approved spec |

## Highest-risk evidence

- **G02/G03:** Legacy `users.name`/`ticket_sso` differs from Polaris `username`/`auth_ticket`. Polaris's game consumer ignores expiry and clears ticket state by user ID. A CMS-side timeout is insufficient. See [emulator contract](emulator-contract.md).
- **G07:** PHPRetro headers separately attribute images/scripts/layouts to Sulake; Octane explicitly embeds some bitmaps extracted from `HabboAir.swf`. Code license does not establish asset ownership. A placeholder is not a completed original-art replacement.
- **G12:** Bundled SWFs are website widgets; the old hotel embed is an external Director DCR. See [Flash modernization](flash-modernization.md).
- **G17:** Old installer drops CMS tables and can truncate game users. New migration starts read-only and runs only on explicit disposable/rehearsal targets until validated.
- **G19:** `/doctor` is not an all-session savings report. Jev calls have been used for evidence triage; no savings are claimed without matched comparisons.

## Entry update format

For each completed slice append: task/issue ID; exact changed behavior; source/reference revision; checks run and results; commit/PR; remaining limitation; status (`missing`, `partial`, `verified`, `blocked-external`). A passing mock or a code-text assertion cannot close an integration gap. Store verbose test artifacts privately or in a sanitized evidence index rather than pasting transcripts here.

New findings become a gap before their corresponding work is declared done. Reopen a verified gap when its contract or upstream revision changes and acceptance evidence no longer applies.
