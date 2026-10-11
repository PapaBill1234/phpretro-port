# Disposable authentication integration

This evidence belongs to the auth worktree. It never opens the earlier theme project's databases, containers or stopped state. The harness hard-codes `phpretro-auth-dev`, checks each container's Compose project/service and checkout labels, and uses only `http://127.0.0.1:8790`.

## Exact Polaris contract

Pinned source: [`0101ce955069af1bcf1dd4299b03250679d9b1e1`](https://github.com/duckietm/Polaris-Emulator/tree/0101ce955069af1bcf1dd4299b03250679d9b1e1). The fixture includes only the exact `CREATE TABLE users` and `CREATE TABLE bans` blocks, with SHA256, source path and line ranges recorded in `tests/integration/polaris-auth-origin.json`. It includes no upstream INSERT statements, default accounts or emulator data.

| Field | Exact contract used by CMS |
|---|---|
| `users.id` | Signed INT; immutable identity, require positive value. |
| `users.username` | VARCHAR(25), utf8mb3_general_ci, UNIQUE; case/accent equivalence comes from the database. Return canonical stored name. |
| `users.password` | VARCHAR(64); bcrypt string. No SHA1 or plaintext compatibility claim. |
| `users.account_created` | Signed INT Unix epoch seconds; never scan into `time.Time`. Reject negative account timestamp. |
| `users.access_token_version` | Signed BIGINT default0; reject negative value. Observe live version and stored password on every session use. |
| `users.machine_id` | VARCHAR(64), empty default. Check matching known-machine bans; the web browser does not supply a trustworthy game machine identity. |
| `bans` | Signed INT `user_id`, `timestamp`, `ban_expire`; IP VARCHAR(50), machine VARCHAR(255), type enum account/ip/machine/super. |

Source: [users DDL](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/resources/db/migration/V20260518000000__base_database.sql#L30464), [bans DDL](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/resources/db/migration/V20260518000000__base_database.sql#L779).

HTTP account/IP bans use `ban_expire >= now`. Game account checks also use `>=`, while game IP/machine checks use `>`. The CMS deliberately uses the conservative inclusive boundary for all matching types. Zero or past expiry is expired; there is no zero-as-permanent exception. More than 30 years remaining only changes the upstream presentation label. Source: [HTTP ban checks](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/AuthHttpUtil.java#L241), [game checks](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/habbohotel/modtool/ModToolManager.java#L692).

Polaris access JWTs carry the current `access_token_version` and a password binding, and verify both against live identity. CMS cookies are separate opaque sessions and follow that invalidation requirement without issuing emulator tokens. Source: [token issue and verification](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/AccessTokenService.java#L53).

## Java bcrypt interoperability

Polaris uses `at.favre.lib:bcrypt:0.10.2`, `BCrypt.withDefaults()` and native registration cost12. Defaults produce `$2a$`, encode characters as UTF8 and enforce 72 bytes. The CMS rejects longer input before Go comparison. Source: [PasswordHasher](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/PasswordHasher.java#L10), [registration](https://github.com/duckietm/Polaris-Emulator/blob/0101ce955069af1bcf1dd4299b03250679d9b1e1/Emulator/src/main/java/com/eu/habbo/networking/gameserver/auth/SessionEndpoints.java#L449), [favre defaults](https://github.com/patrickfav/bcrypt/blob/v0.10.2/modules/bcrypt/src/main/java/at/favre/lib/crypto/bcrypt/BCrypt.java#L59).

`java-bcrypt-fixtures.json` contains synthetic native-cost12, UTF8, 72-byte and Java-verified `$2y$` vectors. The generator fetches the exact pinned Polaris helper and checksum-pinned [Maven Central bcrypt artifact](https://repo.maven.apache.org/maven2/at/favre/lib/bcrypt/0.10.2/) plus bytes1.5.0. Compilation and execution happen in a digest-pinned official Temurin21 Docker container with no network, dropped capabilities and a temporary directory. It does not run Polaris or install Java on the host. Random salts change when fixtures are regenerated; verification is reproducible.

```sh
python3 tests/integration/regenerate_bcrypt_fixture.py
python3 tests/integration/verify_bcrypt_fixture.py
python3 -m unittest discover -s tests/integration -p 'test_*.py' -v
```

Verified on 2026-10-11: exact Java helper positive/wrong-password/2y/72-byte checks and 73-byte/malformed rejection passed. Go x/crypto v0.58.0 verified all four Java fixtures; Java verified a separately generated Go hash. Five provenance, RFC6238 and harness scope checks passed. No production passwords or hashes were used.

## Running the HTTP integration

Use only a freshly provisioned auth development stack, with an owned mode0600 `.env` from the project setup script. Configure `COMPOSE_PROJECT_NAME=phpretro-auth-dev`, `WEB_PORT=8790`, `CMS_PUBLIC_ORIGIN=http://127.0.0.1:8790` and explicit insecure loopback cookies. The stack must already be running; the harness does not start unrelated infrastructure. Runtime uses `cms_app`, the bridge uses `cms_polaris_bridge`, and enrollment uses `cms_migrator`.

```sh
python3 tests/integration/auth_e2e.py --prepare-schema
python3 tests/integration/auth_e2e.py --execute
```

Preparation creates the exact tables only when both are absent, checks key field contracts, then runs only the new project's `configure-auth` service with `--no-deps` to install column grants. It never replaces an existing table. Execution generates random synthetic usernames, inserts only its own fixture accounts, and enrolls a rank1 admin through the operator CLI into a temporary mode0600 file. A rank99 account remains a CMS member. No default staff user exists. Secrets and responses remain in memory/private temporary files; command failure output is withheld. Database passwords are stdin data or child environment, never shell source or command arguments.

Checks cover login/cookie replacement, canonical name, strict Origin/CSRF/JSON, bcrypt byte limit, malformed and excessive-cost hashes, negative account epoch/token version, every ban type, expiry zero/past, live password/version invalidation and prevention of restored-hash cookie revival. Two concurrent staff logins with the same valid OTP must yield exactly200+401 and persist that step; sequential and post-restart replay must also fail. Operator file preservation/revocation, least privileges, real theme/locale persistence, concurrent revision409, exact audit counts and a forced audit-insert failure prove the remaining contract. Nine case/accent-equivalent spellings must hit the shared immutable-ID login limit on attempt9, below the IP30/raw-name8 limits. For IP bans it derives the new web proxy's actual peer from its private network; it does not trust arbitrary forwarded headers. Runtime grants are proven by successful reads/last-step update and explicit permission-denied writes or reads of excluded fields.

Staff cookies become guests after removal or any role/MFA revision change: the cookie is cleared, the real Redis session key is deleted, and housekeeping returns401. The revoke/re-enroll regression keeps the same user and role revision1 while generating a new MFA revision/secret. A prior valid staff cookie is held until after re-enrollment to prove the fresh MFA revision alone invalidates it. Neither old cookie can revive, the old MFA secret is rejected, and only a new-secret OTP restores admin. Re-enrollment runs through the Compose `operator` profile into `.builder/enrollment`, verifying local UID/GID and mode0600, then removes only its own private file. Ordinary member sessions remain unaffected by this staff policy.

The outage fixture temporarily stops and restarts only the verified auth project's Redis container. Authentication and housekeeping must return503. Reusing an otherwise-valid TOTP afterward must still fail because MariaDB's last accepted step survived. An expired code cannot count as durability evidence. The harness restores initial settings, removes only its own account/staff/audit/ban rows and trigger (including operator audit events referencing its synthetic IDs), and clears only the disposable project's Redis. It leaves containers running and never deletes a volume.

Exact equality at ban expiry and TOTP windows needs a controlled clock in Go tests; an HTTP request crossing a wall-clock second is insufficient proof. Integration uses clearly active/future and expired timestamps. The fixture does not prove game SSO, emulator handshake, email reset or production readiness.

HTTP suite verified on 2026-10-11 against the new `phpretro-auth-dev` MariaDB11.8 and Redis8.2 stack on port8790: the initial suite passed twice, including concurrent TOTP CAS. After staff-session hardening, only the auth-dev CMS image was rebuilt and the updated suite passed once with removal/revision invalidation, real Redis deletion, same-role-revision re-enrollment and container enrollment ownership checks. All runs exited0 and left services running; the final run cleaned its own rows/trigger/private file and restored settings. No prior theme/STOP containers or data were opened or changed. The five guard tests and `git diff --check` also passed. This is evidence for this bounded auth/housekeeping slice, not completion of the entire CMS.

One small Jev batch classified the already-read ban/interop/fixture findings: inclusive-vs-strict expiry required explicit boundary tests; Java-only upstream tests did not establish Go interop; schema-only extraction was preferred. Important conclusions were checked against source and then actual runtime fixtures. Jev reported611 input/71 output tokens and736ms; main-model token usage and Jev monetary cost were not measured. No savings claim is made.
