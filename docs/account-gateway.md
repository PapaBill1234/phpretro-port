# Local ChatGPT account manager

Run the standalone gateway with an absolute private directory and a loopback port:

    go run ./cmd/account-gateway --state-dir /absolute/private/account-auth --port 1456

The listener starts before sign-in can begin. Open http://127.0.0.1:1456/, sign in to Account 1 or Account 2, and click **Use this account**. Signing in does not automatically change selection. For a remote host, forward the same port over SSH before opening the page:

    ssh -N -L 127.0.0.1:1456:127.0.0.1:1456 phpretro

This manages authorizations held by this gateway; it does not change the account signed in to the Codex desktop app. No account is authorized until its owner completes OpenAI's browser flow. No CMS or Polaris runtime is required.

## Account isolation and recovery

Up to two registrations are stored separately with a stable UUIDv4 host ID, issued client ID, verified subject and their own token bundle. Callback completion verifies state, browser session, nonce, S256 PKCE, exact callback/client, signature, issuer, audience, expiry and granted scopes. A returning sign-in must retain its saved subject and client. Token response scopes must include the configured identity and direct-use scopes. A second account cannot overwrite the first account or implicitly become selected.

Credentials remain backend-only in the encrypted, process-locked private vault. Browser pages and normal JSON/Go formatting exclude tokens. The local HTTP manager enforces its exact Host, request Origin, one-session CSRF, bounded forms, duplicate callback rejection, no-store and no-referrer policies. One lifecycle mutex orders callback, selection, sign-out and backend credential use. Durable saves precede reported success; any persistence failure blocks all further account operations and credential use until restart/recovery.

Sign-out cancels pending authorization and writes a durable pending marker before attempting revocation. It clears local tokens while retaining registration and host identity. A network/server failure is reported as unconfirmed remote revocation; the owner can disconnect the app in ChatGPT Settings. A restart with a pending sign-out clears tokens and reports the uncertainty. Expired access credentials are not used or silently replaced with another account: sign in again with the same registration. Rotating refresh and scheduled renewal are not implemented; opaque earliest_refresh_at metadata is not assigned undocumented semantics.

## Trusted identity-provider configuration

Production uses the exact discovery URL https://auth.openai.com/.well-known/openid-configuration. Discovery must match issuer https://auth.openai.com, authorization https://auth.openai.com/api/accounts/authorize, token https://auth.openai.com/api/accounts/oauth/token, JWKS https://auth.openai.com/.well-known/jwks.json and revocation https://auth.openai.com/api/accounts/oauth/revoke. The trusted public discovery document was observed independently; observation does not authorize any account.

Provider requests use verified TLS, trusted cloned standard transports, bounded time/body sizes and disabled redirects. Token exchange is a single authorization-code/PKCE POST without client secrets or rewind/retry. JWKS snapshots are bounded and cached for five minutes; an unknown key ID can trigger at most one fixed-URL refresh per minute. Token-supplied URLs never choose an endpoint.

The maintained github.com/golang-jwt/jwt/v5 v5.3.1 verifier accepts only RS256 public RSA keys of 2048-8192 bits and canonical, bounded JSON. It rejects private keys, duplicate/case-aliased members, incompatible algorithms/operations, invalid critical headers and claim failures. Host identity comes from the local transaction rather than an invented JWT claim. UUIDv4 host IDs are structurally validated; other generic host forms retain earlier shape checks, while this manager generates UUIDv4 only.

## G05.6/G05.7 evidence and remaining scope

Tests run a real TLS provider with actual RSA-signed tokens, PKCE checking, HTTP browser/cookie flows, two-account switching, encrypted disk reopen, sign-out recovery, wrong state/cookie/subject, duplicate callbacks, denial/replay, expiration without fallback, bounded key refresh and persistence failures. Vault tests exercise actual files, process locks, permissions, authenticated encryption, tamper/missing-file failures and pre-/post-rename durability faults. These establish implemented behavior against a controlled provider, not real-account authorizations.

Actual owner authorization remains external until the browser flow is completed. This account-manager delivery does not implement Responses/Messages/SSE/tool inference adapters or route native Claude builder traffic through a ChatGPT account. Native builders continue to use their configured a6api route. Full port SSO and migration remain separate unfinished work; game launch remains disabled.

References: [registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions), [discovery and signature verification](https://developers.openai.com/siwc/website).
