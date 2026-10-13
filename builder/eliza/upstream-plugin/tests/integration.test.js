// Integration tests against the pinned upstream loader and dispatcher.
//
// These import real pinned modules (@elizaos/agent/runtime/plugin-types,
// @elizaos/agent/api/runtime-plugin-routes, @elizaos/host/protocol) resolved with
// the `eliza-source` condition. They prove the plugin is discovered as an ordinary
// physical package and that a real route handler runs through the real private
// dispatcher, gated by strict OWNER re-resolution.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { PLUGIN_NAME } from "../src/plugin.js";
import { createProjectCompanionPlugin } from "../src/plugin.js";
import { createCompanionProvider } from "../src/companion-client.js";

const OK_AUTH = { getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: true, role: "OWNER" }) }), isServerTokenAuthorized: () => false };

function fakeRes() {
  return { statusCode: 0, headersSent: false, body: undefined, setHeader() {}, end(b) { this.body = b; this.headersSent = true; } };
}

test("real drop-in loader discovers the plugin package", async () => {
  const { scanDropInPlugins, findRuntimePluginExport } = await import("@elizaos/agent/runtime/plugin-types");
  const dir = await mkdtemp(path.join(tmpdir(), "dropin-"));
  const child = path.join(dir, "project-companion");
  await mkdir(child, { recursive: true });
  await writeFile(path.join(child, "package.json"), JSON.stringify({ name: PLUGIN_NAME, version: "0.1.0" }));
  const pluginUrl = pathToFileURL(path.resolve("src/plugin.js")).href;
  await writeFile(path.join(child, "index.js"), `export { default } from ${JSON.stringify(pluginUrl)};\n`);

  const records = await scanDropInPlugins(dir);
  assert.ok(records[PLUGIN_NAME], "loader must register the physical child package");
  assert.equal(records[PLUGIN_NAME].source, "path");

  const mod = await import(pathToFileURL(path.join(child, "index.js")).href);
  const found = findRuntimePluginExport(mod);
  assert.equal(found.name, PLUGIN_NAME);
  assert.equal(found.views[0].componentExport, "ProjectCompanionView");
  assert.equal(found.routes.length, 3);
});

test("real dispatcher runs an OWNER handler and rejects non-owner before the companion", async (t) => {
  let dispatcher;
  let getHttpRuntime;
  try {
    dispatcher = await import("@elizaos/agent/api/runtime-plugin-routes");
    getHttpRuntime = (await import("@elizaos/host/protocol")).getHttpRuntime;
  } catch (error) {
    t.skip(`pinned dispatcher import unavailable offline: ${error.message}`);
    return;
  }

  function build(authorityDeps, calls) {
    const provider = createCompanionProvider({
      fetchImpl: async () => { calls.n += 1; return { status: 200, text: async () => JSON.stringify({ activeWorkers: [{ id: "w1", status: "running", workdir: "/x" }], sessions: [], plugins: ["p"], services: [] }) }; },
      loadToken: () => "b".repeat(32),
      authorityDeps,
    });
    const plugin = createProjectCompanionPlugin({ provider });
    const runtime = {};
    getHttpRuntime(runtime).routes = plugin.routes;
    return runtime;
  }

  const url = new URL("http://127.0.0.1/api/project-companion/status");
  const calls = { n: 0 };
  const res = fakeRes();
  const handled = await dispatcher.tryHandleRuntimePluginRoute({
    req: { method: "GET", headers: {} }, res, method: "GET", pathname: url.pathname, url,
    runtime: build(OK_AUTH, calls), isAuthorized: () => true,
  });
  assert.equal(handled, true);
  assert.equal(res.statusCode, 200);
  const body = JSON.parse(res.body);
  assert.equal(body.ok, true);
  assert.deepEqual(body.status.workers, [{ id: "w1", state: "running" }]);
  assert.equal(JSON.stringify(body).includes("/x"), false);

  const denied = { n: 0 };
  const res2 = fakeRes();
  await dispatcher.tryHandleRuntimePluginRoute({
    req: { method: "GET", headers: {} }, res: res2, method: "GET", pathname: url.pathname, url,
    runtime: build({ getBridge: () => ({ resolveHttpRequestAuthorization: async () => ({ ok: true, role: "USER" }) }), isServerTokenAuthorized: () => false }, denied),
    isAuthorized: () => true,
  });
  assert.equal(res2.statusCode, 403);
  assert.equal(denied.n, 0, "companion must not be contacted for a non-owner");
});

test("view bundle uses the host API client and carries no credentials", async () => {
  const bundlePath = path.resolve("dist/views/bundle.js");
  const mod = await import(pathToFileURL(bundlePath).href);
  assert.equal(typeof mod.ProjectCompanionView, "function");
  const source = await (await import("node:fs/promises")).readFile(bundlePath, "utf8");
  assert.equal(/control\.token|authorization|bearer|credentials\s*:/i.test(source), false);
  assert.equal(/\bfetch\s*\(/.test(source), false, "view must not roll its own fetch");
  assert.equal(/from\s*["']@elizaos\/ui["']/.test(source), true, "view must use the host API client transport");
});
