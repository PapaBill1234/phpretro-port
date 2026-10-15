// Offline contract tests: strict owner resolution, redaction, companion client,
// control-token loading, and the route handlers called directly.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { resolveStrictOwner, requestHasOrigin, originDecision, parseTrustedOrigins } from "../src/owner-authority.js";
import { projectStatus, projectTasks, projectReceipts } from "../src/redact.js";
import { deriveEscalation } from "../src/escalation.js";
import { loadControlToken } from "../src/control-token.js";
import { createCompanionProvider } from "../src/companion-client.js";
import { createProjectCompanionRoutes } from "../src/routes.js";

const OK_AUTH = { getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: true, role: "OWNER" }) }), isServerTokenAuthorized: () => false };

function fakeRes() {
  return {
    statusCode: 0,
    headersSent: false,
    headers: {},
    body: undefined,
    setHeader(k, v) { this.headers[k] = v; },
    end(b) { this.body = b; this.headersSent = true; this.ended = true; },
  };
}

test("requestHasOrigin + originDecision classify Origin headers by scheme and allowlist", () => {
  assert.equal(requestHasOrigin({ headers: {} }), false);
  assert.equal(requestHasOrigin({ headers: { origin: "http://evil" } }), true);
  assert.deepEqual(originDecision({ headers: {} }), { present: false, sameOrigin: true });
  assert.deepEqual(parseTrustedOrigins("http://a.example, ftp://b.example, https://c.example, junk"), ["http://a.example", "https://c.example"]);

  const trusted = ["http://127.0.0.1:3000"];
  assert.equal(originDecision({ headers: { origin: "http://127.0.0.1:3000" } }, { trustedOrigins: trusted }).sameOrigin, true);
  // scheme mismatch, ftp, null, malformed and unconfigured origins are all refused
  assert.equal(originDecision({ headers: { origin: "https://127.0.0.1:3000" } }, { trustedOrigins: trusted }).reason, "untrusted-origin");
  assert.equal(originDecision({ headers: { origin: "ftp://127.0.0.1:3000" } }, { trustedOrigins: trusted }).reason, "scheme-not-allowed");
  assert.equal(originDecision({ headers: { origin: "null" } }, { trustedOrigins: trusted }).reason, "null-origin");
  assert.equal(originDecision({ headers: { origin: "not a url" } }, { trustedOrigins: trusted }).reason, "malformed-origin");
  assert.equal(originDecision({ headers: { origin: "http://127.0.0.1:3000" } }, { trustedOrigins: [] }).reason, "origin-not-configured");
  // a matching Host header alone grants nothing
  assert.equal(originDecision({ headers: { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000" } }, { trustedOrigins: [] }).sameOrigin, false);
});

test("resolveStrictOwner denies foreign Origin, gateway, missing resolver, non-owner", async () => {
  const req = () => ({ headers: {} });
  assert.equal((await resolveStrictOwner(req(), {}, { getBridge: () => ({}), isServerTokenAuthorized: () => false })).reason, "authority-resolver-missing");
  assert.equal((await resolveStrictOwner({ headers: { origin: "http://x" } }, {}, OK_AUTH)).reason, "origin-not-configured");
  assert.equal((await resolveStrictOwner({ headers: { origin: "ftp://x" } }, {}, OK_AUTH)).reason, "scheme-not-allowed");
  assert.equal((await resolveStrictOwner(req(), {}, { ...OK_AUTH, isServerTokenAuthorized: () => true })).reason, "gateway-token");
  const user = { getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: true, role: "USER" }) }), isServerTokenAuthorized: () => false };
  assert.equal((await resolveStrictOwner(req(), {}, user)).status, 403);
  const anon = { getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: false, role: "NONE" }) }), isServerTokenAuthorized: () => false };
  assert.equal((await resolveStrictOwner(req(), {}, anon)).status, 401);
});

test("resolveStrictOwner accepts a resolved OWNER for a trusted same-origin browser cookie GET", async () => {
  const cookieOwner = {
    getBridge: () => ({ resolveHttpRequestAuthorization: async (_req, _runtime, options) => ({ ok: options.allowCookieAuth === true, role: options.allowCookieAuth ? "OWNER" : "NONE" }) }),
    isServerTokenAuthorized: () => false,
    trustedOrigins: ["http://127.0.0.1:3000"],
  };
  const req = { headers: { origin: "http://127.0.0.1:3000" } };
  assert.deepEqual(await resolveStrictOwner(req, {}, cookieOwner), { ok: true, status: 200, reason: "owner" });
  assert.deepEqual(await resolveStrictOwner({ headers: {} }, {}, OK_AUTH), { ok: true, status: 200, reason: "owner" });
});

test("projectStatus strips identity, paths and quota; keeps typed counts/state", () => {
  const raw = {
    runtime: "@elizaos/core@2.0.3", plugins: ["a", "b"], services: ["s"], taskStore: "pglite/.builder/x",
    activeWorkers: [{ id: "w1", pid: 42, status: "running", workdir: "/secret/path" }],
    sessions: [{ id: "s1", pid: 7, status: "idle" }], acceptanceJobs: ["j1"], quota: { email: "a@b.c" },
    workersEnabled: true,
  };
  const out = projectStatus(raw);
  assert.equal(out.counts.activeWorkers, 1);
  assert.equal(out.counts.plugins, 2);
  assert.deepEqual(out.workers, [{ id: "w1", state: "running" }]);
  const text = JSON.stringify(out);
  assert.equal(text.includes("secret"), false);
  assert.equal(text.includes("a@b.c"), false);
  assert.equal(text.includes("pid"), false);
});

test("projectTasks keeps opaque id/state and honest escalation, drops goals", () => {
  const tasks = projectTasks([{ id: "G06", goal: "secret goal", status: "blocked", failures: 3 }]);
  assert.equal(tasks[0].id, "G06");
  assert.equal(tasks[0].state, "blocked");
  assert.equal(tasks[0].escalation.state, "required");
  assert.equal(JSON.stringify(tasks).includes("secret"), false);
});

test("deriveEscalation never reports a third failure as merely queued", () => {
  assert.equal(deriveEscalation({ failures: 2 }).state, "not-required");
  assert.equal(deriveEscalation({ failures: 3 }).state, "required");
  assert.equal(deriveEscalation({ failures: 3, helpAttempted: true }).state, "attempted");
  assert.equal(deriveEscalation({ helpCompleted: true }).state, "completed");
  assert.equal(deriveEscalation({}).state, "unknown");
});

test("projectReceipts keeps only id/kind/hash", () => {
  const out = projectReceipts({ receipts: [{ id: "r1", kind: "check", hash: "a".repeat(64), data: { prompt: "x" } }] });
  assert.deepEqual(out, [{ id: "r1", kind: "check", hash: "a".repeat(64) }]);
});

test("loadControlToken requires a private 0600 regular file in a private parent", async () => {
  const fs = await import("node:fs/promises");
  const root = await mkdtemp(path.join(tmpdir(), "pc-"));
  const dir = path.join(root, ".builder", "eliza");
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const tokenPath = path.join(dir, "control.token");
  await writeFile(tokenPath, "a".repeat(32) + "\n", { mode: 0o600 });
  await fs.chmod(tokenPath, 0o600);
  assert.equal(await loadControlToken({ root }), "a".repeat(32));

  await fs.chmod(tokenPath, 0o644);
  assert.equal(await loadControlToken({ root }), null, "group/other-readable token is refused");

  await fs.chmod(tokenPath, 0o600);
  await fs.chmod(dir, 0o755);
  assert.equal(await loadControlToken({ root }), null, "non-private parent is refused");
});

function provider(overrides = {}, calls = { n: 0 }) {
  const fetchImpl = async () => { calls.n += 1; return { status: 200, text: async () => JSON.stringify({ activeWorkers: [], sessions: [] }) }; };
  return {
    provider: createCompanionProvider({ fetchImpl, loadToken: () => "b".repeat(32), authorityDeps: OK_AUTH, ...overrides }),
    calls,
  };
}

test("status route: OWNER succeeds and calls the companion once", async () => {
  const { provider: p, calls } = provider();
  const [statusRoute] = createProjectCompanionRoutes(p);
  const res = fakeRes();
  await statusRoute.handler({ method: "GET", headers: {}, query: {} }, res, {});
  assert.equal(res.statusCode, 200);
  assert.equal(JSON.parse(res.body).ok, true);
  assert.equal(calls.n, 1);
});

test("status route: non-owner is rejected before any companion request", async () => {
  const { provider: p, calls } = provider({ authorityDeps: { getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: true, role: "USER" }) }), isServerTokenAuthorized: () => false } });
  const [statusRoute] = createProjectCompanionRoutes(p);
  const res = fakeRes();
  await statusRoute.handler({ method: "GET", headers: {}, query: {} }, res, {});
  assert.equal(res.statusCode, 403);
  assert.equal(calls.n, 0);
});

test("companion outage is reported truthfully, never as empty success", async () => {
  const p = createCompanionProvider({ fetchImpl: async () => { throw new Error("down"); }, loadToken: () => "b".repeat(32), authorityDeps: OK_AUTH });
  const status = await p.getStatus();
  assert.equal(status.availability, "unavailable");
  assert.equal(status.reason, "outage");
});

test("missing token is unavailable without an ambient fallback", async () => {
  const p = createCompanionProvider({ fetchImpl: async () => ({ status: 200, text: async () => "{}" }), loadToken: () => null, authorityDeps: OK_AUTH });
  assert.equal((await p.getStatus()).reason, "token-unavailable");
});

test("routes are private, GET-only, and reject malformed receipt ids", async () => {
  const { provider: p } = provider();
  const routes = createProjectCompanionRoutes(p);
  for (const route of routes) {
    assert.equal(route.public, undefined);
    assert.equal(route.type, "GET");
    assert.equal(route.rawPath, true);
  }
  const receipts = routes[2];
  const res = fakeRes();
  await receipts.handler({ method: "GET", headers: {}, query: { id: "bad id!" } }, res, {});
  assert.equal(res.statusCode, 400);
});

test("escalation can be derived from real receipt kinds", () => {
  const required = projectTasks([{ id: "T", status: "blocked", receipts: [{ id: "r1", kind: "task-failure" }, { id: "r2", kind: "task-failure" }, { id: "r3", kind: "task-failure" }] }]);
  assert.equal(required[0].escalation.state, "required");
  const attempted = projectTasks([{ id: "T", status: "blocked", receipts: [{ id: "r1", kind: "sol-help-attempted" }] }]);
  assert.equal(attempted[0].escalation.state, "attempted");
  const completed = projectTasks([{ id: "T", status: "done", receipts: [{ id: "r1", kind: "sol-help-completed" }] }]);
  assert.equal(completed[0].escalation.state, "completed");
  const unavailable = projectTasks([{ id: "T", status: "blocked", receipts: [{ id: "r1", kind: "sol-help-unavailable" }] }]);
  assert.equal(unavailable[0].escalation.state, "unavailable");
});

test("oversized companion responses are refused during streaming", async () => {
  const big = new Uint8Array(300000).fill(65);
  const fetchImpl = async () => ({ status: 200, body: { getReader: () => ({ read: (() => { let done = false; return async () => (done ? { done: true } : (done = true, { done: false, value: big })); })(), cancel: async () => {} }), text: async () => "x".repeat(300000) } });
  const p = createCompanionProvider({ fetchImpl, loadToken: () => "b".repeat(32), authorityDeps: OK_AUTH });
  const status = await p.getStatus();
  assert.equal(status.availability, "unavailable");
  assert.equal(status.reason, "oversize");
});

test("state fields collapse to a coarse enum; paths and errors become unknown", () => {
  const out = projectStatus({
    activeWorkers: [
      { id: "w1", status: "RUNNING" },
      { id: "w2", status: "/home/ubuntu/secret/path" },
      { id: "w3", status: "ENOENT: no such file" },
      { id: "w4", phase: "maybe later" },
    ],
    sessions: [],
  });
  assert.deepEqual(out.workers, [
    { id: "w1", state: "running" },
    { id: "w2", state: "unknown" },
    { id: "w3", state: "unknown" },
    { id: "w4", state: "unknown" },
  ]);
  assert.equal(JSON.stringify(out).includes("secret"), false);
  assert.equal(JSON.stringify(out).includes("ENOENT"), false);
});

test("view transport invokes the host API client and never sends credentials itself", async () => {
  const { loadCompanionJson } = await import("../views/companion-transport.ts");
  const calls = [];
  const client = {
    rawRequest: async (path, init, options) => {
      calls.push({ path, init, options });
      return { ok: true, status: 200, json: async () => ({ ok: true, status: { availability: "available" } }) };
    },
  };
  const body = await loadCompanionJson(client, "/api/project-companion/status");
  assert.deepEqual(body, { ok: true, status: { availability: "available" } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/project-companion/status");
  assert.equal(calls[0].init.method, "GET");
  // No credentials/token field is authored by the view; the host client owns transport auth.
  assert.equal(JSON.stringify(calls[0]).match(/credential|bearer|authorization|token/i), null);

  const failing = { rawRequest: async () => ({ ok: false, status: 401, json: async () => ({}) }) };
  await assert.rejects(() => loadCompanionJson(failing, "/api/project-companion/tasks"));
});
