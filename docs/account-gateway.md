# Account gateway state slice

This slice implements only in-memory profile authorization state: cryptographically random state, nonce and S256 PKCE; exact loopback callback and client binding; short-lived, one-use transactions; injected exchange and verified-identity validation; explicit profile selection.

The OpenAI helper uses `https://auth.openai.com/api/accounts/authorize`, resource `https://api.openai.com/v1`, and scopes `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`. It sends `ext_agent_host_id` on every attempt and `agent_name_hint` only during initial dynamic registration. Host IDs are validated as UUIDv4, RFC 9278 thumbprints (`urn:ietf:params:oauth:jwk-thumbprint:<algorithm>:<base64url>`), or `did:key` identifiers; they are opaque local identifiers, not proof or JWT claims.

Returning callbacks may omit `client_id`; the transaction's stored client is used, while a conflicting supplied client is rejected. State and exact redirect are checked before provider denial; matching denial consumes only its pending attempt. The injected validator is a synthetic boundary and nil validators fail closed.

It does not implement a live listener, provider credentials, JWT verification/JWKS, durable credential storage or refresh/revoke, Responses/Messages/SSE/tool adapters, or account-owner authorizations.
