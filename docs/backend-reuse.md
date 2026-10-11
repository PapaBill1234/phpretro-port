# Go backend salvage audit

Audited source: [`PapaBill1234/phpretro-preservation`](https://github.com/PapaBill1234/phpretro-preservation/tree/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7), revision `af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7`, on 2026-10-11. Scope: Go backend, its SQL/API/test contracts and license. Source checkout remained unchanged; no credentials, production services, email delivery or migrations were used. Frontend and orchestration reuse have separate audits.

**Recommendation: salvage bounded read-domain code and its tests; rebuild production composition, auth/persistence and game launch against the new contracts.** The existing suite is green, but the HTTP app is intentionally a fixture demonstration and its database tests mask a concrete Polaris type mismatch. Do not label the previous backend production-ready or import it wholesale.

## Validation actually performed

The test harness was inspected first: SQL tests register fake `database/sql/driver` implementations, HTTP tests use `httptest`, and fixtures are synthetic. No real database driver or Redis client is present in `go.mod`. [Fake Polaris driver:17–89](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/polaris/store_test.go#L17), [HTTP tests:55](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/server/server_test.go#L55), [Dependency list](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/go.mod).

| Check | Actual outcome |
|---|---|
| Go toolchain | `go version go1.26.9 linux/amd64` |
| `go test -count=1 -json ./...` | Exit 0; 22 packages passed, 2 packages without tests; 284 passing test/subtest terminal events, zero failures/skips |
| `go test -race -count=1 -json ./...` | Exit 0; same package/test-event counts; no race reported in exercised tests |
| `go build ./...`, `go vet ./...` | Both exit 0 |
| Tracked Go files through `gofmt -l` | No unformatted files reported |
| `staticcheck ./...` | Exit 0, using `2026.1 (v0.7.0)` |
| `gosec -fmt=json ./...` | Exit 0; 32 files / 2,614 lines; zero findings; installed scanner reports version `dev`, so a release-version pin is not established |
| `govulncheck -json ./...` | Exit 0; zero finding messages; scanner `v1.7.0`, public vulnerability database updated 2026-10-08 |
| Independent integer timestamp scan probe | Confirmed `database/sql` cannot scan an integer epoch into `*time.Time`; see mismatch below |
| Real MariaDB/Redis/Polaris/Octane integration | **Not tested**; no deployed services or production credentials used |

Checks were run directly after reviewing `scripts/check.sh`; that wrapper was not invoked or imported. Audit logs and the standalone timestamp probe are in the disposable `work/research/` directory, not in the deliverable or source checkout. Security scanners and passing unit tests do not establish complete authentication, production compatibility or absence of vulnerabilities.

## Keep / adapt / reject map

“Keep” means retain useful domain logic/tests, still subject to integration contracts. “Reject unchanged” means replace runtime behavior; useful ideas/test cases can be retained deliberately.

| Area | Verdict | Concrete reuse or required change |
|---|---|---|
| `internal/profile/profile.go`, `view.go` and matching tests | **Keep first** | Small public projection and bounded tab normalization; separate storage from presentation. Add request context to repository ports and map to generated API DTOs; keep game IDs numeric. [Profile model:16](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/profile/profile.go#L16), [Tab normalization:31](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/profile/view.go#L31) |
| `internal/accountview` and tests | **Keep first** | Useful own-account projection check; derives no authenticated identity itself. New handlers must supply the principal from a real session. Map optional widgets to the typed theme-slot model. [Owner check:49](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/accountview/accountview.go#L49) |
| `internal/home`, `community`, `content` | **Adapt next** | Reuse privacy/list bounds and news button enums as domain starting points. Add durable repositories, complete homes/widgets behavior, pagination, safe link/content DTOs and deterministic ordering. The home contract is explicitly synthetic; community uses in-memory slices. [Home:48](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/home/home.go#L48), [Community:68](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/community/community.go#L68), [News:29](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/content/content.go#L29) |
| `internal/profile/settings.go` | **Adapt** | Reuse ownership/control-character tests; reconcile classic 32-rune motto limit with Polaris `varchar(127)` and actual look/preferences rules. No setting writes, audit or CSRF are implemented here. [Settings boundary:56](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/profile/settings.go#L56) |
| `internal/articles` | **Adapt** | Keep URL allowlist and nonmutation test ideas. Stop HTML-escaping plain API text before React escapes it again; introduce one explicit rich-content sanitization boundary. Excluding `/articles/archive` is a prior preservation choice, not established new parity policy. [Projection:24](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/articles/articles.go#L24) |
| `internal/polaris/store.go` | **Adapt; do not import as working adapter** | SQL uses bound values and correct `username`; queries align with several pinned fields. Fix epoch conversion, request cancellation/timeouts, normalized errors and complete ban/maintenance/auth policy. Add real MariaDB fixtures and compatibility preflight. [Queries:33](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/polaris/store.go#L33) |
| `internal/localization`, `cache` | **Adapt** | Useful locale fallback/cache-key ideas; locale set is fixed to four English/French variants, catalog permits only two keys and does not require complete coverage. Redis is only an interface with a serial in-memory double; add actual client, bounded TTLs, versioned settings/invalidation and configured languages. Never reuse “cache outage = miss” for session/authorization state. [Catalog:13](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/localization/localization.go#L13), [RedisLike:16](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/cache/cache.go#L16) |
| `internal/account`, `session`, `staff` | **Reject unchanged as auth runtime** | Preserve opaque-token, owner-binding and TOTP library tests where useful. No concrete bcrypt verifier/login endpoint exists; sessions and remember tokens are process-local. Step-up validates a code but records no elevation scope/expiry and does not compose durable replay checking. Build real middleware, durable stores, atomic rate/replay limits and revocation. [Account:29](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/account/account.go#L29), [StepUp:89](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/session/session.go#L89), [Replay boundary](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/staff/replay.go#L5) |
| `internal/registration`, `recovery` | **Reject unchanged as account lifecycle** | Registration accepts synthetic `.test` email, constructs IDs from usernames, takes no password and has no Polaris provisioning. Recovery tokens have no expiry; `Reset` consumes a token and returns a bool without changing any password. Retain transaction/privacy test concepts, implement the actual workflows anew. [Registration:35](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/registration/registration.go#L35), [Recovery:25](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/recovery/recovery.go#L25) |
| `internal/audit`, `guestbook` | **Adapt concepts/tests; reject synthetic persistence** | Audit SQL targets literal `synthetic.*` tables and string roles; memory replay rejection is not a durable SQL idempotency guarantee. Redaction rejects identifiers containing `password`/`token`, preventing legitimate named security audit events too. Guestbook storage/owner policy is synthetic, lacks durable history and uses unsorted map output. [Audit SQL:223](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/audit/audit.go#L223), [Redaction:16](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/audit/redact.go#L16), [Guestbook:25](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/guestbook/guestbook.go#L25) |
| `internal/server`, `cmd/phpretro`, `internal/authflow` | **Reject unchanged as production bootstrap/API** | Server explicitly creates fixtures, hardcodes principal/profile IDs and exposes response metadata through GET routes. No real cookie/auth/CSRF middleware or POST login flow. Root HTML references a source TSX file, and the server does not serve the compiled web bundle. Build typed `/api/v1` handlers, real composition/readiness, safe errors and static frontend delivery. Keep selected HTTP timeouts/port validation as ideas. [Demo server:19](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/server/server.go#L19), [Hardcoded account:34](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/server/server.go#L34), [Auth metadata](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/authflow/authflow.go#L45) |
| `stage3-run/02-stage3.sql` | **Reject** | Alters legacy HoloGraph `users` columns including `ticket_sso` for a disposable capture database. It is not a CMS schema migration or Polaris migration. No SQL migration directory implementing the new CMS schema was found. [Disposable SQL:1](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/stage3-run/02-stage3.sql#L1) |
| `tests/golden` | **Adapt comparator only** | Useful method/status/header/body comparison primitive. Existing public-root test compares a constructed fixture with itself; accepted capture hash is explicitly not its body hash. It does not demonstrate Go-vs-PHP or screenshot/layout equivalence. [Comparator](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/tests/golden/golden.go#L21), [Synthetic self-comparison:24](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/tests/golden/public_route_test.go#L24) |

## Verified Polaris mismatch

The old adapter selects `account_created` and scans directly into `profile.Profile.AccountCreated`, a `time.Time`. Both old fake profile rows return a `time.Time`, allowing the tests to pass. Actual pinned Polaris defines `account_created int(11)` representing epoch time. MariaDB integer decoding is not timestamp decoding.

- Old scan: [`store.go:51`](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/polaris/store.go#L51) and `:66`.
- Model type: [`profile.go:22`](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/profile/profile.go#L22).
- Mock value: [`store_test.go:59`](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/internal/polaris/store_test.go#L59).
- Target column: [Polaris baseline:30471](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/resources/db/migration/V20260518000000__base_database.sql#L30471).

A standalone standard-library driver returning `int64(1577934245)` produced:

```text
sql: Scan error on column index 0, name "account_created": unsupported Scan, storing driver.Value type int64 into type *time.Time
```

Fix the adapter by scanning a bounded integer epoch and converting explicitly to UTC, with documented handling for zero/invalid legacy values. Replace the unrealistic mock row and add a real MariaDB test using the pinned schema. The probe verifies the integer conversion failure; a real MariaDB driver/service was not run in this audit.

## Small first import, with provenance

The following dependency-closed read-model subset was imported after the audit under the user's reuse authorization, retaining its useful tests:

```text
internal/profile/profile.go
internal/profile/profile_test.go
internal/profile/view.go
internal/profile/view_test.go
internal/accountview/accountview.go
internal/accountview/accountview_test.go
```

Only `accountview` imports `profile`; these files require no external Go dependencies. Imports now target `github.com/PapaBill1234/phpretro-port`; every file carries `GPL-3.0-only`, upstream repository/file and source revision attribution. Stale artifact-local planning references in `view.go` were replaced with the pinned-source record here. A new standard-library-only `go.mod` declares Go `1.24.0` with toolchain `go1.26.9`; the old dependency and runtime tree was not imported. The new repository had no base commit at import time; integration/commit belongs to the root agent.

Validation of the **new imported subset**, distinct from validation of the complete old repository:

| Check | Result |
|---|---|
| `go test -count=1 -json ./...` | Exit 0; 2 packages / 26 passing test/subtest terminal events |
| `go test -race -count=1 -json ./...` | Exit 0; same counts, no exercised race reported |
| `go vet ./...`, `go build ./...` | Both exit 0 |
| Formatting | Imported files formatted with `gofmt` |

This is a tested read-model starting point, not a functioning web server, authentication system, database integration or game bridge. These unit tests use explicit synthetic sources; they do not close the new real-service acceptance gaps. No commit was made by the audit/import agent.

After the new request/DTO/database boundaries exist, port `home`, `community`, `content`, localization and selected cache helpers in separate reviewed commits. Each import must have a specific route/capability owner and fixtures under the new theme/API contract; bringing in an entire old internal tree would disguise synthetic implementations as completed features.

License: old repository explicitly declares `GPL-3.0-only` for its original implementation, while third-party code/assets retain their own provenance. Preserve attribution and GPL source/distribution obligations in the new repository, with the complete license text and dependency notices. No artwork or disposable captures are part of the proposed backend import. [Pinned LICENSE](https://github.com/PapaBill1234/phpretro-preservation/blob/af26734c8cd2f6fd42bf370a4daa7a9a52fdf2f7/LICENSE).

## Gaps against the new contracts

| New contract requirement | Existing state | Acceptance work |
|---|---|---|
| MariaDB CMS/Polaris databases with separate grants | Read-only injected DB handle; no real driver/bootstrap/CMS schema | Add migrations, least-privilege pools, schema preflight and rollback/provisioning fixtures |
| Real bcrypt + bans/maintenance | Interface verifier and fake success predicate only | Add compatible verifier, uniform credential errors and eligibility integration |
| CMS cookie, CSRF, staff MFA and revocation | Process-memory utilities; not composed into server | Implement middleware and durable/atomic session, MFA replay/rate and revoke-all flows |
| Polaris SSO issue/consume/recovery + HTTP tokens | No game ticket bridge in old Go backend | Implement new adapter; exercise all expiry/replay/race/logout gates in `emulator-contract.md` |
| Scoped HMAC CMS command API | Absent | Implement typed command mapping, HMAC fixtures, idempotency/reconciliation and least-privilege tests |
| Stable OpenAPI/typed React DTOs | Ad hoc maps and mixed JSON field casing; no OpenAPI file found | Freeze `/api/v1`, generate TS types and use both themes with the same fixtures |
| Housekeeping language/theme controls | Four hardcoded locales/two keys; no admin persistence | Implement configured catalogs/coverage, fallback/RTL, permissions and audited settings |
| Original-assets policy / Flash replacements | No backend guarantee; omitted from selected import | Carry separate asset/provenance inventory and widget parity tests |
| Evidence of actual feature/layout parity | Synthetic unit/golden tests | Add pinned real service integration and dated route/viewport comparison evidence |

Passing the existing suite is a useful quality baseline for isolated modules. Completion is measured by these new acceptance tests and real integration evidence, not old unit-count, roadmap or “done” labels.
