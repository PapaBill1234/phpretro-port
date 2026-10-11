# Security and migration acceptance

This is the implementation contract, not a claim that the controls already exist.

## Authentication boundaries

Game identity stays in Polaris; website sessions and staff elevation are CMS-owned. Verify bcrypt interoperability with Polaris before migrating or registering users. Cookie sessions use random opaque values, secure attributes, server-side revocation, inactivity/absolute limits and session rotation after login/elevation. Hash reset/remember secrets at rest where compatible; separate game tickets, CMS session secrets, HTTP access tokens and recovery tokens.

Housekeeping authorization lives in Go middleware and domain operations. Every endpoint checks action and object ownership; hiding a React button never grants or denies authority. Staff MFA/elevation protects account/rank/credit changes. Map game ranks to explicit CMS capabilities; an unknown rank grants none. Audit who, what, target, outcome and idempotency key, redacting credentials and sensitive payloads.

Polaris's current ticket behavior is a release blocker until replay/expiry/race tests pass at the game consumer. Keep credentials out of query strings, history, referrers, error reporting and access logs. A launch flow uses a same-origin POST followed by a memory-only bootstrap, with explicit browser-origin checks if a frame bridge is required. Octane's existing query/bootstrap/localStorage behavior needs a small maintained integration patch and separate review.

## Threat model and evidence

| Surface | Failure to test | Required evidence |
|---|---|---|
| Login/reset/registration | enumeration, credential stuffing, reset reuse, collation mismatch | rate limits, neutral errors, one-use reset, bcrypt fixture |
| Website/staff session | fixation, CSRF, stale privilege, remembered login after revoke | session rotation/revocation, CSRF/origin negative tests, MFA |
| User-facing objects | IDOR and mass assignment | ownership tests for profiles/messages/homes/groups/support |
| Content/themes/uploads | stored XSS, arbitrary execution, SVG scripts, ZIP traversal | sanitization/render tests, type/size checks, build-time trusted theme installation |
| Emulator commands | replay/HMAC scope bypass, unintended wallet writes | command allowlist, nonce/time/signature tests, private transport |
| SSO/reconnect | expired/replayed/racing tickets or stolen reconnect tokens | actual consumer concurrency and reconnect tests |
| Migration | destructive table ops, name collisions, partial writes, ID mismatch | read-only dry run, stable mapping, checkpoints, restore rehearsal |
| Builder/public Git | secret disclosure, shared-tree corruption, incomplete review | explicit staged files, secret scan, worktree ownership and reviewed integration |

Use parameterized SQL. Validate typed request bodies and resource limits. Compile themes from reviewed packages; uploading arbitrary PHP/JS from housekeeping is not the install mechanism. Serve security headers/CSP with a deliberate Octane exception policy. Enforce origin/TLS limits for WebSockets. Keep MariaDB, Redis, emulator administration and the provider bridge private. Secrets enter processes from private files/environment, never frontend bundles or agent briefs.

A Redis outage must not disable security silently. Website sessions can use MariaDB authority with cache; cached permissions carry revision and are invalidated on changes. Rate-limit fallback is bounded and conservative; fail closed on staff/ticket issuance if the required authoritative check is unavailable.

Run code/dependency/secret scans and relevant Go concurrency/fuzz checks as features land, plus targeted browser/API negative tests. Security review is a specialized Sol task at the three checkpoints, with source-backed findings; no LLM verdict guarantees absence of defects.

## Reversible migration

1. Snapshot source versions and schemas from a read-only export. Do not invoke the original installer. Validate backup restoration in a disposable environment.
2. Inventory user IDs, name/collation collisions, encoding, groups/messages/content and asset rights. Record counts and checksums without committing user data.
3. Create an explicit mapping from legacy account/game IDs to Polaris IDs. Preserve IDs only where constraints permit; preserve foreign-key relationships through the mapping. Do not deduplicate by email automatically.
4. Migrate CMS content independently of rooms/items/wallets. Define game-domain maps per supported Polaris capability; unsupported data gets a report and does not silently disappear.
5. Legacy SHA-1 cannot be converted directly to bcrypt. Prefer reset. If continuity requires verification, keep a time-limited isolated legacy verifier and upgrade after a successful password check; do not allow legacy verification in normal Polaris auth.
6. Apply idempotent staged batches to a copy, record checkpoints and reconciliation reports, and rehearse rollback. No schema changes are owned by themes.
7. Rehearse final cutover with a write freeze/delta plan, measured downtime and health checks. Production writes/deploys are not part of the initial planning package.

Release requires reproducible real-service evidence and a restore report. Remaining risks stay in `gaps.md`; do not advertise "no vulnerabilities".
