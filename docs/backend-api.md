# CMS foundation API

The Go service listens on port 8080. Public responses contain no account, SSO or emulator data. Settings live in the separate `phpretro_cms` MariaDB schema.

| GET route | Result |
|---|---|
| `/api/v1/health` | 200 `{ "status": "ok" }`; process liveness without dependency calls. |
| `/api/v1/ready` | Actual MariaDB `PingContext` and Redis `PING`; 200 when both pass, otherwise 503. `{ "status": "ok\|degraded", "database": true, "redis": true }`. Booleans reflect each probe. |
| `/api/v1/site` | Persisted settings below; 503 `site_unavailable` if the database row cannot be read/validated. Redis failure yields 200 with `health: "degraded"`. |

```json
{"version":"site.v1","siteName":"PHPRetro Port","activeTheme":"modern","defaultLocale":"en","enabledLocales":["en","nl"],"settingsRevision":1,"health":"ok"}
```

Currently supported themes are `modern` and `legacy`; languages are `en` and `nl`. The default locale must occur exactly once in enabled locales. Responses use JSON, `no-store`, and `nosniff`. Dependency requests have a four-second context budget. HTTP read/write/header/idle timeouts and ten-second graceful shutdown are configured.

Run `/app/cms --migrate` as a separate one-shot service with `CMS_MIGRATION_DSN`. The runtime accepts only `CMS_DATABASE_DSN`, `REDIS_ADDR` (default `redis:6379`) and `REDIS_PASSWORD`; it never executes DDL. Migration uses a dedicated connection/advisory lock, idempotent additive DDL, and a transaction for seed plus checksum/version record. MariaDB's implicit DDL commit means an interrupted create may leave an empty table, recoverable by rerunning. Checksum or unsupported-version mismatches fail closed. Do not alter applied migration files.

Runtime `cms_app` receives SELECT only. The migration principal owns DDL and seed writes. Polaris credentials/schema are never opened by this foundation. Root Compose provisions the principals and waits for the migration service before starting CMS.

`go test ./...`, `go test -race ./...`, and `go vet ./...` cover the reusable packages and interface-based HTTP checks. `TestDisposableIntegration` opts in only via `PHPRETRO_TEST_DISPOSABLE_DSN` plus `PHPRETRO_TEST_REDIS_ADDR`/`PHPRETRO_TEST_REDIS_PASSWORD`; use a new disposable schema because it mutates fixture settings and checksum. No authentication, housekeeping write API, or live Polaris integration is implemented yet.

Dependencies: [go-sql-driver/mysql v1.10.1](https://github.com/go-sql-driver/mysql/releases/tag/v1.10.1), [go-redis v9.23.0](https://github.com/redis/go-redis/releases/tag/v9.23.0). Go 1.26 is required by the selected Redis module; the repository toolchain is 1.26.9.

Verification on 2026-10-11: standard and race Go suites plus `go vet ./...` passed. The opt-in integration also passed against the new development MariaDB 11.8.9 and Redis 8.2 containers: real probes, seed defaults, preserved settings on rerun, and rejected checksum corruption. Its uniquely named fixture database was removed after the test. No production services or credentials were used.
