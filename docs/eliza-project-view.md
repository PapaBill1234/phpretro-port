# Eliza project companion view (G06)

Read-only, owner-gated companion plugin for the pinned upstream elizaOS app
(`28fef47c9d9d3acc7792be0fdaa3070020293cc5`). This document separates what is
verified by offline/controlled tests from what still needs an actual host.

## Deliverable

- Package `@phpretro/plugin-project-companion` under `builder/eliza/upstream-plugin/`:
  an ordinary physical ESM package (`name`/`version`/`type: module`/`exports`) with a
  default-exported real Plugin carrying `name`, `description`, three private `routes`,
  and one owner-gated `views` declaration.
- `dist/views/bundle.js`: a real single-module React view bundle built with the
  pinned Vite toolchain. React and the other host externals stay bare and resolve
  through `DynamicViewLoader`'s host-external bridge; React is not re-bundled.

## Accepted architecture (as implemented)

- **Discovery**: `scanDropInPlugins` scans immediate children of a load path; the
  package is a normal child directory (never a symlinked plugin dir).
  `findRuntimePluginExport` selects the default export.
- **Dispatch**: routes use the legacy `handler(req,res,runtime)` shape served by
  `tryHandleRuntimePluginRoute`. Every route is private; the dispatcher's coarse gate
  runs first and each handler then re-resolves a strict OWNER.
- **Authority**: `resolveStrictOwner` denies an untrusted `Origin`, the gateway/service
  shared token, a missing authority resolver, an unauthenticated caller, and any
  non-OWNER role. It calls the real host bridge
  (`resolveHttpRequestAuthorization`) with `allowTrustedLocalBypass:false` and enables
  the host-verified cookie path only for a same-origin (or Origin-less) request. It
  never trusts `req.user`, the runtime character, model/agent metadata, the mere
  presence of an `Authorization` header, or a bare `Host`/`X-Forwarded-Proto` authority.
- **Origin allowlist**: a request with no `Origin` is treated as a desktop/CLI owner
  (host cookie path allowed). A request with an `Origin` must be an exact http/https
  match against `PHP_RETRO_COMPANION_TRUSTED_ORIGINS` (comma-separated origins). `null`,
  malformed, FTP/other schemes, scheme mismatches and unconfigured origins are refused,
  so a matching `Host` header alone grants nothing.
- **Data**: fixed loopback `127.0.0.1:18791` only; server-side control token loaded by
  the host (`O_NOFOLLOW`, owned 0600 in a private parent, bounded read); response
  bodies bounded during streaming; no redirects; bounded timeout. Allowlist projection
  keeps counts, opaque ids, a coarse closed state enum, receipt kind/hash and escalation
  facts; any state/phase text outside the enum (paths, stack/error text, provider
  messages) collapses to `unknown`. It strips goals/prompts/plans/outputs/paths/
  credentials/raw errors/account identity/provider metadata. Loading and polling perform
  zero dispatch, help, mutation, or inference.
- **View transport**: the view loads through the pinned host API client
  (`client` from `@elizaos/ui`, left host-external for `DynamicViewLoader`) so cookie and
  bearer owner auth use the host's existing connection transport. The bundle authors no
  `fetch`, `credentials` or token of its own.

## Checks run (offline / controlled)

- `node --conditions=eliza-source <pinned tsx> --test tests/` → 20 pass, 0 fail.
- Real pinned loader: `scanDropInPlugins` + `findRuntimePluginExport`
  (`@elizaos/agent/runtime/plugin-types`) discover the package from a temp directory.
- Real pinned dispatcher: `tryHandleRuntimePluginRoute`
  (`@elizaos/agent/api/runtime-plugin-routes`) runs an OWNER handler (200, redacted
  body) and rejects a USER caller (403) before the companion is contacted.
- Owner authority: untrusted/`null`/FTP/scheme-mismatch/unconfigured Origin, gateway,
  missing-resolver, unauthenticated and non-OWNER denials; trusted-origin cookie OWNER
  success; a bare matching `Host` header grants nothing.
- View transport: the view calls the host API client (invocation asserted) and authors
  no `fetch`/`credentials`/token; the built bundle imports only `@elizaos/ui` and `react`.
- Redaction: identity, paths, quota, goals and raw errors dropped; counts/ids/state and
  receipt kind/hash kept.
- Companion client: outage, missing token and oversize-streamed body reported
  truthfully as unavailable (never empty success); 0600 file + private parent required.
- Escalation: third failure derived from receipt kinds / task fields as
  required/attempted/unavailable/completed, never guessed as queued.
- View bundle: built with pinned Vite; named export `ProjectCompanionView` importable;
  bundle carries no token/authorization strings.

## Remaining acceptance (not yet proven) — do not claim 100%

These require an actual host and are **not** covered by the controlled tests above:

1. Loading the plugin through a **fresh-DB** full elizaOS host boot, with the real
   `resolveHttpRequestAuthorization` resolver installed, proving the OWNER success and
   USER/gateway/ambient/foreign-Origin rejections end-to-end through the live server.
2. Mounting `ProjectCompanionView` through the live `DynamicViewLoader` against a real
   `/api/views` catalog (nav-tab presence alone is not success).
3. A live operator deployment with a real `.builder/eliza/control.token` and a running
   companion, proving status/tasks/receipts against real companion state.

Offline/controlled tests are not full-host proof. No feature is declared production
ready from them alone.
