# Polaris / Octane integration contract

Status: proposed full game integration contract. The existing-account bridge/MFA slice has real MariaDB/Redis tests recorded in auth-integration.md; no full emulator build or game handshake tests have run. Source inspection performed 2026-10-11. Pin these revisions before development:

| Dependency | Revision | Role |
|---|---|---|
| Polaris | `0101ce955069af1bcf1dd4299b03250679d9b1e1` | Java game server, game identity and world persistence |
| Octane | `67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1` | React/TypeScript game client |
| Octane-Renderer | Not inspected or pinned yet | Required separate rendering/protocol dependency; resolve in the first integration spike |

## Ownership and storage

Use MariaDB for both applications, with separate databases and database principals. Do not port Polaris to PostgreSQL. Polaris constructs a `jdbc:mariadb://` connection; its SQL and migrations remain its responsibility. The current POM enforces Java 25–26, although its README still names Java 21. [DatabasePool.java:58](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/database/DatabasePool.java#L58), [pom.xml:22](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/pom.xml#L22).

| Principal | Database access |
|---|---|
| `cms_app` | CMS database only: website sessions, content, theme/locale settings, roles, migration mappings, audit events, jobs |
| `polaris_app` | Polaris database; emulator-owned runtime and migration policy |
| `cms_polaris_bridge` | Explicit adapter grants on agreed game fields; no unrestricted SQL or DDL |
| Migration principals | Temporary, separate, operator-scoped; unavailable to runtime agents and web requests |

CMS `accounts.game_user_id` references the immutable Polaris `users.id` logically. Do not join identities by display name or duplicate balances/passwords as independently authoritative records. Store migration source IDs and collision decisions in CMS tables. Separate databases here are separate MariaDB schemas; their users are database service principals, not duplicated human identities.

CMS owns website sessions, staff MFA and CMS authorization. Polaris owns in-game identities, bans, world state, currencies, and game permission ranks. Redis belongs to the Go CMS for bounded caches, session/rate-limit state and job coordination; it is not a replacement for game persistence. A Redis failure must deny privileged/session operations requiring it and allow explicitly safe uncached public reads.

## Go boundary

The current `internal/polaris` package alone accesses the bridge pool; future control/API operations must remain behind the same typed adapter boundary. API handlers, themes and domain services receive typed ports, never SQL or emulator secrets.

```go
// Illustrative boundary: no implementation or final generated API implied.
type PolarisIdentity struct { ID int64; Username string; Rank int; Banned bool }
type GameLaunch struct { Ticket string; ExpiresAt time.Time; ClientURL string }
type PolarisPort interface {
    FindIdentity(ctx context.Context, id int64) (PolarisIdentity, error)
    VerifyCredentials(ctx context.Context, username, password string) (PolarisIdentity, error)
    ProvisionIdentity(ctx context.Context, input NewIdentity) (PolarisIdentity, error)
    IssueLaunch(ctx context.Context, gameUserID int64, requestID string) (GameLaunch, error)
    RevokeLaunch(ctx context.Context, gameUserID int64) error
    ExecuteCommand(ctx context.Context, command AllowedCommand) (CommandResult, error)
    Health(ctx context.Context) (CompatibilityReport, error)
}
```

Freeze adapter grants and DTOs before implementation. Credentials are verified by the Go adapter against the existing Polaris bcrypt contract; raw hashes never leave that module. The current website session checks account/IP/known-machine bans. Maintenance eligibility is an additional game-launch requirement before issuing a game ticket and is not yet implemented; website authentication does not claim full native game-login parity. Registration must create one game identity, its required defaults, and a CMS mapping with an idempotency record. Use a real transaction where both databases share a connection/principal that permits it, or a durable provisioning workflow with explicit recovery; do not call two independent commits an atomic transaction. Do not assume defaults from one SQL dump without a real pinned-emulator registration fixture. Start the pilot with fixture accounts; provisioning ships after that fixture passes. Go runtime must not apply Polaris schema migrations.

The current logout clears only the CMS session, because this slice issues no emulator credentials. When game launch is implemented, logout must also invoke the emulator revocation/logout path required to clear tickets, revoke Polaris HTTP tokens, and disconnect the client. Password/security changes revoke both systems' sessions. Clear only the matching ticket when revoking a particular launch; administrative revoke-all is a separate action. Do not modify Polaris-owned tables outside adapter operations documented and tested here.

## Exact schema and credential compatibility

Pinned baseline fields are `users.id`, **`users.username`**, `users.password`, `users.mail`, `users.rank`, `users.auth_ticket`, `users.auth_ticket_expires_at`, and `users.ip_current`. There is no `usernname` field in the inspected contract. `username` is unique `varchar(25)` with a case-insensitive collation; `password` is `varchar(64)`; `auth_ticket` is `varchar(256)` with a case-insensitive collation; expiry is nullable. `ip_current` explicitly expects CMS updates for IP bans. [Base migration:30464](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/resources/db/migration/V20260518000000__base_database.sql#L30464).

The native verifier accepts bcrypt; native registration uses cost 12. Do not write Argon2 or legacy PHPRetro hashes into this field. Legacy accounts require an explicit transition: verify their old hash in a narrowly scoped migration verifier, then persist compatible bcrypt on first successful login, or require a password reset. Remove the legacy verifier after the migration window. A stronger independent staff MFA credential can live in the CMS. [PasswordHasher.java:10](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/PasswordHasher.java#L10), [Registration:449](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/SessionEndpoints.java#L449).

## SSO: observed behavior and required tests

Three credentials are distinct: the CMS cookie session, the game SSO ticket, and Polaris HTTP access/remember tokens. Native login updates `auth_ticket`, expiry and IP, and returns `ssoTicket` plus an HTTP `accessToken`. Its ticket is 32 random bytes encoded with a `nitro-` prefix. Native TTL defaults to 60 seconds with a 15-second floor. [Login:261](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/SessionEndpoints.java#L261), [Minting:218](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/AuthHttpUtil.java#L218), [TTL:589](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/SessionEndpoints.java#L589).

Material gaps in the pinned implementation:

1. **Game login ignores `auth_ticket_expires_at`.** Setting expiry in Go does not make game tickets expire. A scheduled CMS cleanup is useful defense but cannot guarantee atomic expiry enforcement during emulator authentication. Record this as an open release gate; evaluate an upstream-compatible emulator-local fix in a separately reviewed patch rather than claim the CMS solved it.
2. Successful loading clears `auth_ticket` by **user ID**, in a separate DB operation. Test concurrent requests and issuance of a new ticket while the previous ticket is consumed; do not describe this as atomic compare-and-consume.
3. `debug_sso=true` disables consumption. Production preflight must reject that setting. Comments lower in `HabboManager` still describe retained tickets; executable behavior takes precedence.
4. `/api/auth/sso-token` checks expiry for database lookup, then may locate an online client by SSO and issue an HTTP token. A consumed database ticket is therefore not sufficient evidence that no other authentication path accepts it.
5. Reconnect supports parked sessions and a recovery token. A new launch ticket cannot blindly be substituted for every reconnect.

Sources: [HabboManager.java:115](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/habbohotel/users/HabboManager.java#L115), [SSO HTTP exchange:146](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/SessionEndpoints.java#L146), [SecureLoginEvent.java:125](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/messages/incoming/handshake/SecureLoginEvent.java#L125).

`POST /api/v1/game/launch` requires an authenticated CMS session and CSRF protection. Mint a cryptographically random ticket of at least 256 bits, within the strictest upstream length limit (128 characters for HTTP exchange), update the agreed game fields, and return `Cache-Control: no-store`. Keep the ticket out of query strings, analytics, audit text, error reports and persistent browser storage in the new integration. Use a same-origin bootstrap/bridge that assigns the runtime configuration before client initialization; determine the smallest Octane patch in the integration spike. Constrain origins and frame policies; do not send credentials using wildcard `postMessage`.

Upstream Octane currently reads `?sso=` into `window.OctaneConfig['sso.ticket']` and exchanges it for HTTP tokens. Its HTTP access tokens use localStorage. Treat these as source behaviors to review, not secure defaults to reproduce. If adopting upstream behavior temporarily, record the limitation and the tests/replacement required. [Bootstrap:117](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/src/bootstrap.ts#L117), [Exchange:15](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/src/api/auth/ssoTokenExchange.ts#L15), [Storage:4](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/src/api/auth/accessToken.ts#L4).

## Administrative commands and client dependencies

Use implemented `POST /api/cms/command` with `{key,data}` for live administrative effects. It is disabled by default and provides direct-peer allowlisting, HMAC authentication, replay checks and command scopes. Configure a least-privilege key available only to the adapter; require TLS off host. Map explicit Go command types to supported commands. Never accept an arbitrary command string or use generic execution as a fallback. Idempotency belongs in the CMS workflow for operations such as granting currency; a timeout does not prove failure, so reconcile before retrying.

Headers are `X-Cms-Key`, `X-Cms-Timestamp`, `X-Cms-Nonce`, and `X-Cms-Signature`. Implement the pinned wire contract and verify canonical signing against emulator fixtures. Keep legacy RCON internal and disabled when migration is complete. [CmsApiHandler.java:38](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/cms/CmsApiHandler.java#L38), [Authentication contract](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/docs/rcon-api-migration.md#L141).

Octane is the game UI; Octane-Renderer is required for rendering and the transport implementation. Build both with locked dependencies and pinned revisions. Reverse proxy the game WSS endpoint separately from Go APIs; configure asset hosts explicitly. Existing CSS-only Octane skins are separate from CMS themes. Both inspected repositories declare GPL-3.0; preserve code provenance and notices. Assets require an independent rights/provenance record. [Client role/dependency](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/README.md#L23), [Sibling dependency resolution](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/vite.config.mjs#L9), [Polaris license](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/LICENSE), [Octane license](https://github.com/duckietm/Octane/blob/67ec3ce1fd4d3f9d134c3b06ae137a2c49a313c1/LICENSE).

## Acceptance matrix

Run these against disposable MariaDB, Redis, the pinned Polaris build and the pinned Octane/renderer build. Store source revisions and outcomes in `docs/gaps.md`; mocks alone do not prove compatibility.

| Test | Required result |
|---|---|
| Schema/version preflight | Required fields/collations/limits and supported migrations reported; incompatible deployments fail before writes |
| Username migration | Case collisions, length violations and renamed names resolve deterministically; game IDs stay stable |
| Credential migration | Bcrypt works in both paths; migrated legacy login rehashes once; bad passwords do not mint tickets |
| Ban/maintenance gates | Banned or maintenance-restricted identities cannot launch through the CMS |
| Launch CSRF/auth | Missing/expired CMS session or CSRF fails; launch ticket is never logged or cached |
| Expired ticket | Real game handshake rejects it; current upstream behavior is a recorded failing release gate until resolved |
| Replay/concurrency | Second unauthorized use cannot authenticate; simultaneous attempts and new-ticket consumption race are characterized and resolved |
| Ticket/HTTP exchange | Ticket exchange ordering before/after game consumption behaves as designed without widening access |
| Refresh/recovery/logout | Reconnect works within its allowed window; logout/password reset revoke CMS and client HTTP/game access |
| Admin transport | Bad HMAC, replayed nonce, stale timestamp, denied peer or scope fails; double grant/retry cannot duplicate effects |
| Privilege boundaries | CMS runtime cannot run DDL or arbitrary game SQL; browser cannot call control API |
| Infrastructure failure | Timeouts, emulator/Redis outage and DB rollback cannot leave privileged partial state or duplicate identities |
| Client smoke | Login, enter room, navigator, chat, inventory, catalog and normal disconnect/reconnect work with the actual renderer |

## Next decisions to record

Pin Octane-Renderer; verify game launch bootstrap; freeze registration and revoke-all mappings; decide the emulator-local SSO remediation; reconcile MariaDB character/username behavior; inventory PHPRetro features against current game capabilities. None of these source findings is a claim that the system has no vulnerabilities.
