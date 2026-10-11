---
name: polaris-contract
description: Implement or audit the Go CMS boundary to Polaris identity, MariaDB schema, tickets and administrative commands.
---

Read `docs/emulator-contract.md` and only the task's schema/auth/command sources. Canonical identity is immutable Polaris `users.id`; the name is `users.username`, email `mail`, password bcrypt, game ticket `auth_ticket`.

Keep CMS sessions separate from game tickets and Polaris HTTP/reconnect tokens. The audited game consumer ignores ticket expiry and clears by user ID. A Go TTL or a mocked ticket test does not close that gap. Verify expiry, replay, atomic conditional consumption, concurrent issuance and reconnect against the actual pinned consumer.

Administrative mutations use scoped HMAC CMS commands where supported; do not fall back silently to raw RCON. Record command ownership, idempotency and reconciliation.

CMS and Polaris use MariaDB databases and separate principals. Themes never query either. Run real MariaDB fixtures for schema/collation/password changes. For legacy passwords follow `docs/security-and-migration.md`; do not copy SHA-1 values into the bcrypt field and claim compatibility.
