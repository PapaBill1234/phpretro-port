# Account gateway prototype

This slice implements in-memory profile authorization state and optional cryptographic ID-token validation against a trusted JWKS snapshot: cryptographically random state, nonce and S256 PKCE; exact loopback callback and client binding; short-lived, one-use transactions; injected exchange and verified-identity validation; explicit profile selection.

The OpenAI helper uses `https://auth.openai.com/api/accounts/authorize`, resource `https://api.openai.com/v1`, and scopes `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`. It sends `ext_agent_host_id` on every attempt and `agent_name_hint` only during initial dynamic registration. UUIDv4 host IDs receive structural validation; RFC 9278 thumbprints (`urn:ietf:params:oauth:jwk-thumbprint:<algorithm>:<base64url>`), and `did:key` identifiers currently receive shape checks only. These opaque local identifiers are not identity proof or JWT claims; complete thumbprint/did:key encoding validation remains unfinished.

Returning callbacks may omit `client_id`; the transaction's stored client is used, while a conflicting supplied client is rejected. State and exact redirect are checked before provider denial; matching denial consumes only its pending attempt. A configured validator may use the actual RS256 implementation described below; tests also inject synthetic validators. Nil validators fail closed.

The bounded token-exchange transport supports caller-trusted HTTPS endpoints and a fixed OpenAI endpoint. It sends the authorization-code/PKCE form without a client secret, rejects redirects, caps requests and response parsing, and exposes returned access/refresh credentials only in a redacted in-memory bundle; they are neither persisted nor consumed by the gateway. Only synthetic TLS endpoint tests establish transport behavior. No live OAuth authorization, provider identity, credential lifecycle, listener, discovery or inference integration is claimed.

## ID-token verification

The independent gateway now provides NewJWTValidator(issuer, jwks, now), implementing Validator with github.com/golang-jwt/jwt/v5 v5.3.1. The caller supplies a trusted HTTPS issuer and an immutable public JWKS snapshot. This slice performs no discovery or network requests: unknown key IDs fail closed, and token-supplied discovery URLs cannot choose a key. A future transport must obtain the issuer and JWKS from trusted provider discovery and refresh the snapshot before using it.

Supported keys are RS256 RSA public keys of 2048–8192 bits, with odd exponent 3–65537; at most 64 keys and a 1 MiB document are accepted. Duplicate key IDs, duplicate JSON members (including case aliases), private key parameters, incompatible key operations and unsupported critical JWS extensions are rejected. Tokens are limited to 16 KiB. The verifier requires the exact issuer, issued-client audience, subject, expiry, issued-at and transaction nonce, checks not-before when present, and checks authorized party when supplied or when multiple audiences exist. No clock leeway is applied. Returning authorization must retain the previously verified subject.

Host identity comes from the pending local transaction; no host-ID JWT claim is invented. Granted scopes come from token-response metadata supplied by the exchange transport after a successful provider response. The validator requires openid and chatgpt.tokens.use.direct and returns a copied scope list. An ID token does not itself establish plan usage permission.

Generated-key tests exercise actual signatures, algorithm-confusion rejection, claim failures, parser/key bounds, concurrent verification and Gateway.Callback with two accounts. They prove this isolated verifier contract, not live authorization. The listener, JWKS discovery/refresh, private durable storage, refresh/revoke, inference adapters and human two-account authorizations remain unfinished.

References: https://developers.openai.com/siwc/website and https://developers.openai.com/siwc/token-sharing-open-source/sign-in.
