// Strict OWNER resolution for the project-companion routes.
//
// The pinned legacy dispatcher (packages/agent/src/api/runtime-plugin-routes.ts)
// runs a private route handler only after its own coarse gate, which also admits the
// cloud gateway service token and credential-free trusted-local (ambient loopback)
// callers. None of those is the owner. This module re-resolves the real caller through
// the installed host bridge and accepts ONLY a cryptographically resolved OWNER.
//
// Authority always comes from the host bridge (packages/agent/src/runtime/host-bridge.ts:
// resolveHttpRequestAuthorization). We never trust req.user, the runtime character,
// agent/model metadata, the mere presence of an Authorization header, or an
// untrusted Host / X-Forwarded-Proto authority. Cookie-based owner sessions are
// handled by the host resolver; we only allow the cookie path for a request whose
// Origin is absent or exactly matches the operator's configured trusted-origin
// allowlist. Foreign schemes, "null", FTP and scheme mismatches are rejected.

import { getAgentHostBridge } from "@elizaos/agent/runtime/host-bridge";
import { isServerTokenAuthorized } from "@elizaos/agent/api/server-helpers-auth";

export const TRUSTED_ORIGINS_ENV = "PHP_RETRO_COMPANION_TRUSTED_ORIGINS";

// Ambient loopback and the gateway token are refused. Cookie auth is host-verified and
// enabled only for a trusted same-origin request; bearer owner credentials are allowed.
export const AUTHORIZATION_OPTIONS = Object.freeze({
  allowCookieAuth: true,
  allowTrustedLocalBypass: false,
  allowBearerAuth: true,
});

export function buildAuthorizationOptions(allowCookieAuth) {
  return { allowCookieAuth: Boolean(allowCookieAuth), allowTrustedLocalBypass: false, allowBearerAuth: true };
}

function headerValue(req, name) {
  const raw = req && req.headers ? req.headers[name] : undefined;
  return Array.isArray(raw) ? raw[0] : raw;
}

/** True when the request carries any Origin header. */
export function requestHasOrigin(req) {
  const origin = headerValue(req, "origin");
  return typeof origin === "string" && origin.length > 0;
}

/** Parse a comma-separated origin allowlist, keeping only http/https origins. */
export function parseTrustedOrigins(value) {
  return String(value ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .flatMap((entry) => {
      try {
        const url = new URL(entry);
        if (url.protocol !== "http:" && url.protocol !== "https:") return [];
        return [url.origin];
      } catch {
        return [];
      }
    });
}

/**
 * Classify the request Origin against the configured trusted allowlist.
 * An absent Origin is same-origin (desktop/CLI owner) and may use the host cookie path.
 * A present Origin must be an exact http/https allowlist match; "null", malformed,
 * FTP/other schemes, scheme mismatches and unconfigured origins are refused.
 */
export function originDecision(req, options = {}) {
  const origin = headerValue(req, "origin");
  if (typeof origin !== "string" || origin.length === 0) return { present: false, sameOrigin: true };
  if (origin === "null") return { present: true, sameOrigin: false, reason: "null-origin" };

  let parsed;
  try {
    parsed = new URL(origin);
  } catch {
    return { present: true, sameOrigin: false, reason: "malformed-origin" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { present: true, sameOrigin: false, reason: "scheme-not-allowed" };
  }

  const allowlist = Array.isArray(options.trustedOrigins)
    ? options.trustedOrigins
    : parseTrustedOrigins(options.trustedOrigins ?? process.env[TRUSTED_ORIGINS_ENV]);
  if (allowlist.length === 0) return { present: true, sameOrigin: false, reason: "origin-not-configured" };
  return allowlist.includes(parsed.origin)
    ? { present: true, sameOrigin: true }
    : { present: true, sameOrigin: false, reason: "untrusted-origin" };
}

function deny(status, reason) {
  return { ok: false, status, reason };
}

/** @returns {Promise<{ok: boolean, status: number, reason: string}>} */
export async function resolveStrictOwner(req, runtime, deps = {}) {
  const gatewayCheck = deps.isServerTokenAuthorized || isServerTokenAuthorized;
  let bridge;
  try {
    bridge = deps.getBridge ? deps.getBridge() : getAgentHostBridge();
  } catch {
    return deny(403, "authority-bridge-unavailable");
  }

  const origin = originDecision(req, deps);
  if (origin.present && !origin.sameOrigin) return deny(403, origin.reason || "untrusted-origin");
  try {
    if (gatewayCheck(req)) return deny(403, "gateway-token");
  } catch {
    return deny(403, "gateway-check-failed");
  }

  const resolver =
    bridge && typeof bridge.resolveHttpRequestAuthorization === "function"
      ? bridge.resolveHttpRequestAuthorization
      : undefined;
  if (!resolver) return deny(403, "authority-resolver-missing");

  let authorization;
  try {
    authorization = await resolver(req, runtime, buildAuthorizationOptions(origin.sameOrigin));
  } catch {
    return deny(403, "authority-resolution-failed");
  }
  if (!authorization || authorization.ok !== true) return deny(401, "unauthenticated");
  if (authorization.role !== "OWNER") return deny(403, "not-owner");
  return { ok: true, status: 200, reason: "owner" };
}
