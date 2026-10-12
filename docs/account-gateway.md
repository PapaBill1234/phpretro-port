# Account gateway state slice

This slice implements only in-memory profile authorization state: cryptographically random state, nonce and S256 PKCE; exact loopback callback and client binding; short-lived, one-use transactions; injected exchange and verified-identity validation; explicit profile selection. After callback claim, exchange and validation failures are sanitized, consume that claim, and preserve the prior registration; provider-declared denial occurs before claim and leaves the pending request available until expiry. Concurrent callbacks cannot replace a newer transaction.

It does not implement a live listener, provider credentials, JWT verification, durable credential storage or refresh/revoke, Responses/Messages/SSE/tool adapters, or account-owner authorizations. Those are subsequent reviewed slices.
