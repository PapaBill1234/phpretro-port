// SPDX-License-Identifier: GPL-3.0-only
import { localeIds, themeIds, type LocaleId, type SiteSettings, type ThemeId } from "./site.js";

export type Principal = Readonly<{ id: number; username: string; role: "member" | "admin" }>;
type Session = { version: "session.v1"; authenticated: false } |
  { version: "session.v1"; authenticated: true; user: Principal; csrfToken: string };
export type SessionState = { status: "loading" | "guest" | "unavailable" } |
  { status: "authenticated"; user: Principal };
export type AdminSiteSettings = Omit<SiteSettings, "activeTheme" | "defaultLocale" | "enabledLocales"> & {
  activeTheme: ThemeId; defaultLocale: LocaleId; enabledLocales: LocaleId[];
};
export type SiteDraft = Pick<AdminSiteSettings, "siteName" | "activeTheme" | "defaultLocale" | "enabledLocales">;
export type FailureCode = "network" | "invalid_response" | "invalid_request" | "invalid_credentials" |
  "unauthenticated" | "forbidden" | "invalid_origin" | "invalid_csrf" | "settings_conflict" |
  "rate_limited" | "dependency_unavailable" | "http";
export type Failure = Readonly<{ code: FailureCode; retryAfter?: number }>;
export type AuthSnapshot = Readonly<{
  session: SessionState; busy: boolean; error: Failure | null;
  site: AdminSiteSettings | null; siteError: Failure | null;
}>;
type Result<Value> = { ok: true; value: Value } | { ok: false; failure: Failure };

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid contract");
  return value as Record<string, unknown>;
}
function exact(row: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(row).length !== keys.length || keys.some((key) => !Object.hasOwn(row, key))) throw new Error("invalid contract");
}
function positiveRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
function validLocales(value: unknown, defaultLocale: unknown): value is LocaleId[] {
  return Array.isArray(value) && value.length > 0 && value.length <= localeIds.length &&
    value.every((locale) => (localeIds as readonly unknown[]).includes(locale)) &&
    new Set(value).size === value.length && value.includes(defaultLocale);
}

export function parseSession(value: unknown): Session {
  const row = record(value);
  if (row.version !== "session.v1") throw new Error("invalid session");
  if (row.authenticated === false) {
    exact(row, ["version", "authenticated"]);
    return { version: "session.v1", authenticated: false };
  }
  exact(row, ["version", "authenticated", "user", "csrfToken"]);
  const user = record(row.user);
  exact(user, ["id", "username", "role"]);
  if (row.authenticated !== true || !positiveRevision(user.id) || user.id > 2147483647 ||
      typeof user.username !== "string" || !user.username.trim() || user.username.length > 25 ||
      /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/.test(user.username) ||
      (user.role !== "member" && user.role !== "admin") || typeof row.csrfToken !== "string" ||
      !/^[a-f0-9]{64}$/.test(row.csrfToken)) throw new Error("invalid session");
  return { version: "session.v1", authenticated: true,
    user: Object.freeze({ id: user.id, username: user.username, role: user.role }), csrfToken: row.csrfToken };
}

export function parseAdminSite(value: unknown): AdminSiteSettings {
  const row = record(value);
  exact(row, ["version", "siteName", "activeTheme", "defaultLocale", "enabledLocales", "settingsRevision", "health"]);
  if (row.version !== "site.v1" || typeof row.siteName !== "string" || !row.siteName.trim() || new TextEncoder().encode(row.siteName).length > 100 ||
      /[\u0000-\u001f\u007f-\u009f]/u.test(row.siteName) ||
      !(themeIds as readonly unknown[]).includes(row.activeTheme) || !(localeIds as readonly unknown[]).includes(row.defaultLocale) ||
      !validLocales(row.enabledLocales, row.defaultLocale) || !positiveRevision(row.settingsRevision) ||
      (row.health !== "ok" && row.health !== "degraded")) throw new Error("invalid settings");
  return { version: "site.v1", siteName: row.siteName, activeTheme: row.activeTheme as ThemeId,
    defaultLocale: row.defaultLocale as LocaleId, enabledLocales: [...row.enabledLocales],
    settingsRevision: row.settingsRevision, health: row.health };
}

export function createSiteUpdate(draft: SiteDraft, expectedRevision: number) {
  const row = record(draft);
  exact(row, ["siteName", "activeTheme", "defaultLocale", "enabledLocales"]);
  const safe = parseAdminSite({ ...row, version: "site.v1", settingsRevision: expectedRevision, health: "ok" });
  return { version: "site-update.v1" as const, siteName: safe.siteName, activeTheme: safe.activeTheme,
    defaultLocale: safe.defaultLocale, enabledLocales: [...safe.enabledLocales], expectedRevision: safe.settingsRevision };
}

async function responseFailure(response: Response, login: boolean): Promise<Failure> {
  let code: FailureCode = response.status === 401 ? (login ? "invalid_credentials" : "unauthenticated") :
    response.status === 403 ? "forbidden" : [400, 413, 415].includes(response.status) ? "invalid_request" :
    response.status === 409 ? "settings_conflict" : response.status === 429 ? "rate_limited" :
    response.status === 503 ? "dependency_unavailable" : "http";
  // Never display server prose or retain an unknown response field.
  try {
    const body = record(await response.json()); exact(body, ["error"]);
    const error = record(body.error); exact(error, ["code", "message"]);
    if (response.status === 403 && (error.code === "invalid_origin" || error.code === "invalid_csrf") && typeof error.message === "string") code = error.code;
  } catch { /* The HTTP status remains a safe fallback. */ }
  const retry = response.headers.get("Retry-After");
  const retryAfter = retry && /^\d+$/.test(retry) ? Number(retry) : undefined;
  return Object.freeze({ code, ...(code === "rate_limited" && retryAfter && retryAfter <= 3600 ? { retryAfter } : {}) });
}

export function createAuthController(fetcher: typeof fetch = fetch, requestTimeoutMs = 5000) {
  let csrf: string | undefined;
  let snapshot: AuthSnapshot = Object.freeze({ session: Object.freeze({ status: "loading" as const }), busy: false, error: null, site: null, siteError: null });
  const listeners = new Set<() => void>();
  function publish(change: Partial<AuthSnapshot>) {
    const next = { ...snapshot, ...change };
    Object.freeze(next.session);
    if (next.site) { Object.freeze(next.site.enabledLocales); Object.freeze(next.site); }
    snapshot = Object.freeze(next);
    for (const listener of listeners) listener();
  }
  function applySession(session: Session) {
    csrf = session.authenticated ? session.csrfToken : undefined;
    publish({ session: session.authenticated ? { status: "authenticated", user: session.user } : { status: "guest" },
      site: null, siteError: null });
  }
  function deny(failure: Failure, settings: boolean) {
    if (failure.code === "unauthenticated") { csrf = undefined; publish({ session: { status: "guest" }, site: null, error: failure }); }
    // A forbidden response is no longer evidence that this browser may use housekeeping.
    if (["forbidden", "invalid_origin", "invalid_csrf"].includes(failure.code)) publish({ site: null });
    publish(settings ? { siteError: failure } : { error: failure });
  }
  async function request<Value>(path: string, init: RequestInit, parser: (value: unknown) => Value, login = false): Promise<Result<Value>> {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), requestTimeoutMs);
    try {
      const response = await fetcher(path, { ...init, credentials: "same-origin", cache: "no-store", redirect: "error", signal: abort.signal });
      if (!response.ok) return { ok: false, failure: await responseFailure(response, login) };
      try { return { ok: true, value: parser(await response.json()) }; }
      catch { return { ok: false, failure: { code: "invalid_response" } }; }
    } catch { return { ok: false, failure: { code: "network" } }; }
    finally { clearTimeout(timeout); }
  }
  function admin(): boolean { return snapshot.session.status === "authenticated" && snapshot.session.user.role === "admin" && Boolean(csrf); }
  async function readSettings(): Promise<void> {
    const result = await request("/api/v1/housekeeping/site", {}, parseAdminSite);
    if (result.ok) publish({ site: result.value, siteError: null });
    else deny(result.failure, true);
  }
  async function acceptSession(result: Result<Session>) {
    if (result.ok) {
      applySession(result.value);
      if (admin()) await readSettings();
    } else {
      csrf = undefined;
      publish({ session: { status: result.failure.code === "invalid_credentials" || result.failure.code === "unauthenticated" ? "guest" : "unavailable" },
        site: null, siteError: null, error: result.failure });
    }
  }
  return {
    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    async refreshSession(): Promise<void> {
      if (snapshot.busy) return;
      publish({ busy: true, error: null, siteError: null });
      await acceptSession(await request("/api/v1/auth/session", {}, parseSession));
      publish({ busy: false });
    },
    async login(username: string, password: string, otp: string): Promise<void> {
      if (snapshot.busy || snapshot.session.status === "authenticated") return;
      publish({ busy: true, error: null, siteError: null });
      await acceptSession(await request("/api/v1/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, ...(otp ? { otp } : {}) }) }, (value) => {
          const session = parseSession(value);
          if (!session.authenticated) throw new Error("invalid login response");
          return session;
        }, true));
      publish({ busy: false });
    },
    async logout(): Promise<void> {
      if (snapshot.busy || snapshot.session.status !== "authenticated" || !csrf) return;
      publish({ busy: true, error: null });
      const result = await request("/api/v1/auth/logout", { method: "POST", headers: { "X-CSRF-Token": csrf } }, parseSession);
      if (result.ok && !result.value.authenticated) applySession(result.value);
      else deny(result.ok ? { code: "invalid_response" } : result.failure, false);
      publish({ busy: false });
    },
    async loadSettings(): Promise<void> {
      if (snapshot.busy || !admin()) return;
      publish({ busy: true, siteError: null });
      await readSettings();
      publish({ busy: false });
    },
    async saveSettings(draft: SiteDraft): Promise<AdminSiteSettings | undefined> {
      if (snapshot.busy || !admin() || !snapshot.site || !csrf) return;
      let update;
      try { update = createSiteUpdate(draft, snapshot.site.settingsRevision); }
      catch { publish({ siteError: { code: "invalid_request" } }); return; }
      publish({ busy: true, siteError: null });
      const result = await request("/api/v1/housekeeping/site", { method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(update) }, (value) => {
          const site = parseAdminSite(value);
          if (site.settingsRevision !== update.expectedRevision + 1) throw new Error("invalid saved revision");
          return site;
        });
      if (result.ok) publish({ site: result.value, siteError: null });
      else deny(result.failure, true);
      publish({ busy: false });
      return result.ok ? result.value : undefined;
    }
  };
}
export type AuthController = ReturnType<typeof createAuthController>;
