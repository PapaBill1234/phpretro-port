# @phpretro/plugin-project-companion

A read-only, owner-gated companion view for the pinned elizaOS host. It discovers
through the ordinary drop-in loader, mounts one owner-only view through the real
`DynamicViewLoader`, and exposes three private GET routes that project bounded
builder state from the local companion. It never mutates, dispatches, helps, or
calls a model.

## Provenance

- Upstream host: `elizaOS` pinned at `28fef47c9d9d3acc7792be0fdaa3070020293cc5` (MIT).
- This package: GPL-3.0-only. No upstream source is copied or modified; host APIs
  are consumed as peer dependencies (`@elizaos/agent`, `@elizaos/core`, `@elizaos/host`).
- Companion: `builder/eliza` (GPL-3.0-only), spoken to only over fixed loopback.

## Operator install

1. Place this package as an **immediate child directory** of a configured
   `plugins.load.paths` entry (no symlinked plugin dir). The loader
   (`scanDropInPlugins`) reads the child `package.json` and the default export.
2. Provide the companion root so the host can load the control token at runtime
   init: set `PHP_RETRO_BUILDER_ROOT` to the builder root. The host reads
   `<root>/.builder/eliza/control.token` (owned, 0600, private parent) — never the
   view bundle, character settings, or logs.
3. Build the view bundle with the pinned toolchain:
   `PHP_RETRO_VITE_BIN=<pinned vite> node scripts/build-view.mjs`
   (React and `@elizaos/ui` stay external and are provided by the host bridge).
4. Set `PHP_RETRO_COMPANION_TRUSTED_ORIGINS` to a comma-separated http/https origin
   allowlist for browser owners (e.g. `https://hotel.example`). A request with no
   `Origin` is treated as a desktop/CLI owner; a present `Origin` must match exactly.
   The view loads through the host API client (`@elizaos/ui`), so cookie/bearer auth
   uses the host transport — no token is placed in the bundle.

## Routes (private, GET only)

- `/api/project-companion/status`
- `/api/project-companion/tasks`
- `/api/project-companion/receipts?id=<opaque-task-id>`

Each handler re-resolves a strict OWNER through the host bridge
(`resolveHttpRequestAuthorization`, `allowTrustedLocalBypass:false`), rejects a
foreign `Origin`, rejects the gateway/service token, and only enables the
host-verified cookie path for a same-origin request. Non-owner calls are refused
before the companion is contacted.

## Data boundary

Only typed counts, opaque task/session/receipt ids, coarse state, receipt
kind/hash, escalation facts and availability/staleness cross the boundary. Goals,
prompts, plans, outputs, paths, credentials, raw errors, quota/account identity and
provider/model metadata are stripped. The companion client is fixed to
`127.0.0.1:18791`, times out, follows no redirect, sends no `Origin`, and bounds the
response body while streaming.
