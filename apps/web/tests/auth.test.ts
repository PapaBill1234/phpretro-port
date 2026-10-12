// SPDX-License-Identifier: GPL-3.0-only
import { strict as assert } from "node:assert";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AccountPanel } from "../src/AccountPanel.js";
import { createAuthController, createSiteUpdate, parseAdminSite, parseSession, type AdminSiteSettings, type SiteDraft } from "../src/auth.js";
import { messages } from "../src/locales.js";

const token = "a".repeat(64); // Synthetic test token; never a credential.
const guest = { version: "session.v1", authenticated: false };
const member = { version: "session.v1", authenticated: true, user: { id: 17, username: "synthetic-member", role: "member" }, csrfToken: token };
const admin = { ...member, user: { id: 23, username: "synthetic-admin", role: "admin" } };
const site: AdminSiteSettings = { version: "site.v1", siteName: "Synthetic hotel", activeTheme: "modern", defaultLocale: "en", enabledLocales: ["en", "nl"], settingsRevision: 7, health: "ok" };
const draft: SiteDraft = { siteName: "Changed hotel", activeTheme: "legacy", defaultLocale: "nl", enabledLocales: ["nl"] };
function json(value: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });
}
function error(status: number, code: string, headers?: HeadersInit): Response {
  return json({ error: { code, message: "Untrusted server prose must never enter the interface." } }, status, headers);
}
function checkRequest(options?: RequestInit) {
  assert.equal(options?.credentials, "same-origin"); assert.equal(options?.cache, "no-store");
  assert.equal(options?.redirect, "error"); assert.ok(options?.signal instanceof AbortSignal);
}

test("session contract rejects unknown fields, secrets, malformed identity and token values", () => {
  assert.deepEqual(parseSession(guest), guest);
  const parsed = parseSession(admin);
  assert.equal(parsed.authenticated, true);
  if (parsed.authenticated) { assert.deepEqual(parsed.user, admin.user); assert.ok(Object.isFrozen(parsed.user)); }
  for (const value of [null, [], { ...guest, csrfToken: token }, { ...guest, user: member.user },
    { ...admin, cookie: "synthetic-cookie" }, { ...admin, passwordHash: "synthetic-hash" },
    { ...admin, authenticated: 1 }, { ...admin, version: "session.v2" }, { ...admin, csrfToken: token.toUpperCase() },
    { ...admin, csrfToken: "a".repeat(63) }, { ...admin, user: { ...admin.user, password: "synthetic" } },
    { ...admin, user: { ...admin.user, id: 0 } }, { ...admin, user: { ...admin.user, id: 2147483648 } },
    { ...admin, user: { ...admin.user, id: "23" } }, { ...admin, user: { ...admin.user, id: 1.5 } },
    { ...admin, user: { ...admin.user, role: "owner" } }, { ...admin, user: { ...admin.user, username: " " } },
    { ...admin, user: { ...admin.user, username: "x".repeat(26) } }, { ...admin, user: { ...admin.user, username: "Control\u0001name" } },
    { ...admin, user: { ...admin.user, username: "Astral😀name" } }]) {
    assert.throws(() => parseSession(value), /invalid/);
  }
});

test("housekeeping settings are exact, supported and require a valid revision and locale set", () => {
  assert.deepEqual(parseAdminSite(site), site);
  assert.deepEqual(createSiteUpdate(draft, 7), { version: "site-update.v1", ...draft, expectedRevision: 7 });
  for (const value of [{ ...site, csrfToken: token }, { ...site, activeTheme: "future-theme" }, { ...site, defaultLocale: "fr" },
    { ...site, enabledLocales: ["en", "en"] }, { ...site, enabledLocales: ["nl"] }, { ...site, enabledLocales: [] },
    { ...site, settingsRevision: 0 }, { ...site, settingsRevision: 1.5 }, { ...site, siteName: " " }, { ...site, siteName: "x".repeat(101) },
    { ...site, siteName: "é".repeat(51) }, { ...site, siteName: "Control\u0001name" }]) {
    assert.throws(() => parseAdminSite(value), /invalid/);
  }
  assert.throws(() => createSiteUpdate({ ...draft, password: "synthetic" } as SiteDraft, 7), /invalid/);
});

test("member startup projects safe principal and never calls or writes housekeeping", async () => {
  const calls: string[] = [];
  const controller = createAuthController(async (url, options) => {
    calls.push(String(url)); checkRequest(options); return json(member);
  });
  const observations: string[] = [];
  const unsubscribe = controller.subscribe(() => observations.push(JSON.stringify(controller.getSnapshot())));
  await controller.refreshSession(); await controller.loadSettings(); await controller.saveSettings(draft);
  assert.deepEqual(calls, ["/api/v1/auth/session"]);
  const state = controller.getSnapshot(); assert.equal(state.session.status, "authenticated"); assert.equal(state.site, null);
  assert.equal(JSON.stringify(state).includes(token), false); assert.equal(observations.some((value) => value.includes(token)), false);
  assert.ok(Object.isFrozen(state)); assert.ok(Object.isFrozen(state.session)); unsubscribe();
});

test("login uses exact credentials in JSON only; logout uses CSRF and confirms guest state", async () => {
  let call = 0;
  const controller = createAuthController(async (url, options) => {
    checkRequest(options); call++;
    if (call === 1) { assert.equal(url, "/api/v1/auth/session"); return json(guest); }
    if (call === 2) {
      assert.equal(url, "/api/v1/auth/login"); assert.equal(options?.method, "POST");
      assert.deepEqual(JSON.parse(String(options?.body)), { username: "synthetic-member", password: "synthetic-password", otp: "123456" });
      assert.equal(new Headers(options?.headers).get("Content-Type"), "application/json");
      assert.equal(new Headers(options?.headers).has("X-CSRF-Token"), false); return json(member);
    }
    assert.equal(url, "/api/v1/auth/logout"); assert.equal(options?.method, "POST"); assert.equal(options?.body, undefined);
    assert.equal(new Headers(options?.headers).get("X-CSRF-Token"), token); return json(guest);
  });
  await controller.refreshSession(); await controller.login("synthetic-member", "synthetic-password", "123456");
  assert.equal(controller.getSnapshot().session.status, "authenticated");
  assert.equal(JSON.stringify(controller.getSnapshot()).includes("synthetic-password"), false);
  assert.equal(JSON.stringify(controller.getSnapshot()).includes("123456"), false);
  await controller.logout(); assert.equal(controller.getSnapshot().session.status, "guest"); assert.equal(controller.getSnapshot().error, null);
  await controller.logout(); assert.equal(call, 3);
});

test("verified admin save sends revision and CSRF, exposes new settings and reloads saved state", async () => {
  let persisted = site;
  const calls: string[] = [];
  const controller = createAuthController(async (url, options) => {
    checkRequest(options); calls.push(`${options?.method ?? "GET"} ${url}`);
    if (url === "/api/v1/auth/session") return json(admin);
    assert.equal(url, "/api/v1/housekeeping/site");
    if (options?.method === "PUT") {
      assert.equal(new Headers(options.headers).get("X-CSRF-Token"), token);
      assert.deepEqual(JSON.parse(String(options.body)), { version: "site-update.v1", ...draft, expectedRevision: 7 });
      persisted = { ...site, ...draft, settingsRevision: 8 };
    }
    return json(persisted);
  });
  await controller.refreshSession(); assert.deepEqual(controller.getSnapshot().site, site);
  assert.ok(Object.isFrozen(controller.getSnapshot().site)); assert.ok(Object.isFrozen(controller.getSnapshot().site?.enabledLocales));
  const result = await controller.saveSettings(draft); assert.deepEqual(result, persisted);
  assert.equal(result?.settingsRevision, 8); await controller.refreshSession();
  assert.deepEqual(controller.getSnapshot().site, persisted);
  assert.deepEqual(calls, ["GET /api/v1/auth/session", "GET /api/v1/housekeeping/site", "PUT /api/v1/housekeeping/site", "GET /api/v1/auth/session", "GET /api/v1/housekeeping/site"]);
});

test("conflict retains loaded settings, performs no automatic overwrite and permits explicit reload", async () => {
  let puts = 0, gets = 0;
  const controller = createAuthController(async (url, options) => {
    if (url === "/api/v1/auth/session") return json(admin);
    if (options?.method === "PUT") { puts++; return error(409, "settings_conflict"); }
    gets++; return json(gets === 1 ? site : { ...site, siteName: "Other edit", settingsRevision: 8 });
  });
  await controller.refreshSession(); const before = controller.getSnapshot().site;
  assert.equal(await controller.saveSettings(draft), undefined);
  assert.equal(controller.getSnapshot().site, before); assert.deepEqual(controller.getSnapshot().siteError, { code: "settings_conflict" });
  assert.equal(puts, 1); assert.equal(gets, 1);
  await controller.loadSettings(); assert.equal(controller.getSnapshot().site?.settingsRevision, 8); assert.equal(controller.getSnapshot().siteError, null);
});

test("unauthorized settings save clears identity; forbidden drops privileged settings until refresh", async () => {
  for (const status of [401, 403]) {
    let sessionCalls = 0, puts = 0;
    const controller = createAuthController(async (url, options) => {
      if (url === "/api/v1/auth/session") return json(++sessionCalls === 1 ? admin : member);
      if (options?.method === "PUT") { puts++; return error(status, status === 401 ? "unauthenticated" : "forbidden"); }
      return json(site);
    });
    await controller.refreshSession(); await controller.saveSettings(draft);
    assert.equal(controller.getSnapshot().site, null);
    assert.equal(controller.getSnapshot().session.status, status === 401 ? "guest" : "authenticated");
    if (status === 401) {
      assert.equal(controller.getSnapshot().error?.code, "unauthenticated");
      const html = renderToStaticMarkup(createElement(AccountPanel, { controller, t: messages.en, onSaved: () => {} }));
      assert.ok(html.includes(messages.en.sessionExpired));
    }
    await controller.saveSettings(draft); assert.equal(puts, 1);
    await controller.refreshSession(); const session = controller.getSnapshot().session;
    assert.equal(session.status, "authenticated"); if (session.status === "authenticated") assert.equal(session.user.role, "member");
    await controller.loadSettings(); await controller.saveSettings(draft); assert.equal(puts, 1);
  }
});

test("logout network or malformed confirmation never invents a logged-out state", async () => {
  for (const mode of ["network", "invalid", "authenticated"] as const) {
    const controller = createAuthController(async (url) => {
      if (url === "/api/v1/auth/session") return json(member);
      if (mode === "network") throw new TypeError("offline");
      return mode === "invalid" ? json({ ...guest, leakedSecret: "synthetic" }) : json(member);
    });
    await controller.refreshSession(); await controller.logout();
    assert.equal(controller.getSnapshot().session.status, "authenticated");
    assert.equal(controller.getSnapshot().error?.code, mode === "network" ? "network" : "invalid_response");
  }
});

test("unavailable startup and request timeout stay explicit; concurrent actions cannot replace a pending session", async () => {
  const offline = createAuthController(async () => { throw new TypeError("offline"); });
  await offline.refreshSession(); assert.equal(offline.getSnapshot().session.status, "unavailable");
  assert.equal(offline.getSnapshot().error?.code, "network");
  const timeout = createAuthController(async (_url, options) => new Promise<Response>((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  }), 5);
  await timeout.refreshSession(); assert.equal(timeout.getSnapshot().error?.code, "network"); assert.equal(timeout.getSnapshot().busy, false);
  let resolve: (response: Response) => void = () => {};
  let calls = 0;
  const pending = createAuthController(async () => { calls++; return new Promise<Response>((done) => { resolve = done; }); });
  const refresh = pending.refreshSession(); await pending.login("synthetic", "synthetic", ""); await pending.logout();
  assert.equal(calls, 1); assert.equal(pending.getSnapshot().busy, true); resolve(json(guest)); await refresh;
  assert.equal(pending.getSnapshot().session.status, "guest"); assert.equal(pending.getSnapshot().busy, false);
});

test("login failures use status and sanitized codes, never server prose or fake users", async () => {
  for (const [status, code, expected] of [[401, "invalid_credentials", "invalid_credentials"], [429, "rate_limited", "rate_limited"],
    [503, "dependency_unavailable", "dependency_unavailable"], [403, "invalid_origin", "invalid_origin"]]) {
    const controller = createAuthController(async () => error(status as number, String(code), { "Retry-After": "30" }));
    await controller.login("synthetic", "synthetic", "");
    assert.notEqual(controller.getSnapshot().session.status, "authenticated"); assert.equal(controller.getSnapshot().error?.code, expected);
    assert.equal(JSON.stringify(controller.getSnapshot()).includes("Untrusted server prose"), false);
    if (status === 429) assert.equal(controller.getSnapshot().error?.retryAfter, 30);
  }
  for (const response of [json(guest), json({ ...admin, password: "synthetic-secret" }), new Response("not JSON")]) {
    const controller = createAuthController(async () => response);
    await controller.login("synthetic", "synthetic", ""); assert.equal(controller.getSnapshot().error?.code, "invalid_response");
    assert.equal(controller.getSnapshot().session.status, "unavailable");
  }
});

test("invalid local edits and unadvanced save response cannot be reported as saved settings", async () => {
  let puts = 0;
  const controller = createAuthController(async (url, options) => {
    if (url === "/api/v1/auth/session") return json(admin);
    if (options?.method === "PUT") puts++;
    return json(site);
  });
  await controller.refreshSession();
  assert.equal(await controller.saveSettings({ ...draft, enabledLocales: [] }), undefined);
  assert.equal(puts, 0); assert.equal(controller.getSnapshot().siteError?.code, "invalid_request");
  assert.equal(await controller.saveSettings(draft), undefined); assert.equal(puts, 1);
  assert.equal(controller.getSnapshot().siteError?.code, "invalid_response"); assert.equal(controller.getSnapshot().site?.settingsRevision, 7);
});

test("real React panel renders localized account states and admin controls without secret values", async () => {
  for (const locale of ["en", "nl"] as const) {
    for (const session of [guest, member, admin]) {
      const controller = createAuthController(async (url) => json(url === "/api/v1/auth/session" ? session : site));
      await controller.refreshSession();
      const html = renderToStaticMarkup(createElement(AccountPanel, { controller, t: messages[locale], onSaved: () => {} }));
      assert.equal(html.includes(token), false);
      assert.equal(html.includes("name=\"password\""), !session.authenticated);
      assert.equal(html.includes(messages[locale].siteSettings), session === admin);
      assert.equal(html.includes(messages[locale].saveSettings), session === admin);
      assert.ok(html.includes(session.authenticated ? messages[locale].signOut : messages[locale].signIn));
    }
  }
});
