// SPDX-License-Identifier: GPL-3.0-only
import { strict as assert } from "node:assert";
import test from "node:test";
import { loadSite, mergeSiteResult, parseSiteSettings, resolveSiteLocale, resolveTheme } from "../src/site.js";
import { messages } from "../src/locales.js";

const settings = { version: "site.v1", siteName: "Synthetic site", activeTheme: "legacy", defaultLocale: "nl", enabledLocales: ["en", "nl"], settingsRevision: 1, health: "ok" };

test("late public reads cannot undo a newer confirmed settings revision, while service failures stay visible", () => {
  const latest = { status: "available" as const, settings: parseSiteSettings({ ...settings, settingsRevision: 8 }) };
  const stale = { status: "available" as const, settings: parseSiteSettings({ ...settings, settingsRevision: 7 }) };
  assert.equal(mergeSiteResult(latest, stale), latest);
  assert.equal(mergeSiteResult(stale, latest), latest);
  assert.equal(mergeSiteResult(undefined, stale), stale);
  assert.deepEqual(mergeSiteResult(latest, { status: "unavailable", reason: "network" }), { status: "unavailable", reason: "network" });
});

test("API settings select the configured theme and language with deterministic fallbacks", () => {
  const parsed = parseSiteSettings(settings);
  assert.equal(resolveTheme(parsed.activeTheme), "legacy");
  assert.equal(resolveTheme("future-theme"), "modern");
  assert.equal(resolveSiteLocale(undefined, parsed), "nl");
  assert.equal(resolveSiteLocale("nl-BE", parsed), "nl");
  assert.equal(resolveSiteLocale("unknown", parsed), "nl");
  assert.equal(resolveSiteLocale("nl", { ...parsed, defaultLocale: "en", enabledLocales: ["en"] }), "en");
});

test("site settings reject malformed payloads and project unrelated fields", () => {
  assert.deepEqual(parseSiteSettings({ ...settings, internalNotes: "synthetic-internal" }), settings);
  for (const value of [null, [], { ...settings, version: "site.v2" }, { ...settings, enabledLocales: [] }, { ...settings, health: "pretend" }, { ...settings, settingsRevision: 0 }]) {
    assert.throws(() => parseSiteSettings(value), /invalid/);
  }
});

test("controller fetches the actual shared endpoint and preserves degraded availability", async () => {
  const fetcher: typeof fetch = async (url, options) => {
    assert.equal(url, "/api/v1/site");
    assert.equal(options?.cache, "no-store");
    assert.equal(options?.credentials, "same-origin");
    return new Response(JSON.stringify({ ...settings, health: "degraded" }), { status: 200 });
  };
  const result = await loadSite(fetcher);
  assert.equal(result.status, "available");
  if (result.status === "available") assert.equal(result.settings.health, "degraded");
});

test("network, HTTP and malformed JSON failures are unavailable rather than synthetic success", async () => {
  assert.deepEqual(await loadSite(async () => { throw new TypeError("offline"); }), { status: "unavailable", reason: "network" });
  assert.deepEqual(await loadSite(async () => new Response("unavailable", { status: 503 })), { status: "unavailable", reason: "http" });
  assert.deepEqual(await loadSite(async () => new Response("not JSON", { status: 200 })), { status: "unavailable", reason: "invalid" });
  assert.deepEqual(await loadSite(async () => new Response(JSON.stringify({ siteName: "invalid" }), { status: 200 })), { status: "unavailable", reason: "invalid" });
});

test("English and Dutch presentation catalogues have complete distinct labels", () => {
  assert.deepEqual(Object.keys(messages.nl).sort(), Object.keys(messages.en).sort());
  assert.notEqual(messages.en.title, messages.nl.title);
  assert.notEqual(messages.en.offline, messages.nl.offline);
  assert.notEqual(messages.en.retry, messages.nl.retry);
});
