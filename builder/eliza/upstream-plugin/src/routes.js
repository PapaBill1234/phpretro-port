// Owner-gated, read-only companion routes.
//
// Every route is private (no `public: true`), so the pinned dispatcher's coarse gate
// runs first; each handler then re-resolves a strict OWNER via resolveStrictOwner and
// refuses before touching the companion. No route mutates, dispatches, or starts work.

import { resolveStrictOwner } from "./owner-authority.js";

const REPORT_ID = /^[A-Za-z0-9._:-]{1,64}$/;
const MODES = ["local", "local-only"];
const MODE_REASON = "Project companion reads the operator-local builder companion; never routable in cloud/remote topologies.";

function sendJson(res, status, body) {
  if (res.headersSent) return;
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
}

function param(req, key) {
  const query = req && req.query;
  if (!query) return undefined;
  const value = query[key];
  return Array.isArray(value) ? value[0] : value;
}

export function createProjectCompanionRoutes(provider) {
  function guarded(fn) {
    return async (req, res, runtime) => {
      const decision = await resolveStrictOwner(req, runtime, provider.authorityDeps);
      if (!decision.ok) return sendJson(res, decision.status, { ok: false, error: decision.reason });
      try {
        return await fn(req, res, runtime);
      } catch {
        return sendJson(res, 503, { ok: false, error: "companion-unavailable" });
      }
    };
  }

  return [
    {
      type: "GET",
      name: "project-companion-status",
      path: "/api/project-companion/status",
      rawPath: true,
      modes: MODES,
      modeReason: MODE_REASON,
      handler: guarded(async (req, res) => sendJson(res, 200, { ok: true, status: await provider.getStatus() })),
    },
    {
      type: "GET",
      name: "project-companion-tasks",
      path: "/api/project-companion/tasks",
      rawPath: true,
      modes: MODES,
      modeReason: MODE_REASON,
      handler: guarded(async (req, res) => sendJson(res, 200, { ok: true, ...(await provider.getTasks()) })),
    },
    {
      type: "GET",
      name: "project-companion-receipts",
      path: "/api/project-companion/receipts",
      rawPath: true,
      modes: MODES,
      modeReason: MODE_REASON,
      handler: guarded(async (req, res) => {
        const id = param(req, "id");
        if (typeof id !== "string" || !REPORT_ID.test(id)) {
          return sendJson(res, 400, { ok: false, error: "invalid-id" });
        }
        return sendJson(res, 200, { ok: true, ...(await provider.getReceipts(id)) });
      }),
    },
  ];
}
