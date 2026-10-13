// Fixed-loopback companion client.
//
// Speaks ONLY to http://127.0.0.1:18791 (the pinned builder companion:
// builder/eliza/src/http.mjs) over its two read endpoints plus the single fixed
// report command. There is no configurable host, port, path prefix, or command
// passthrough. Requests time out, never follow redirects, send no Origin, and bound
// the response body while it streams. Loading and polling never dispatch, help,
// mutate, or call a model.

import { projectStatus, projectTasks, projectReceipts } from "./redact.js";
import { loadControlToken } from "./control-token.js";

export const COMPANION_HOST = "127.0.0.1";
export const COMPANION_PORT = 18791;
const MAX_BODY_BYTES = 262144;
const DEFAULT_TIMEOUT_MS = 4000;
const STALE_AFTER_MS = 10 * 60 * 1000;
const REPORT_ID = /^[A-Za-z0-9._:-]{1,64}$/;
const decoder = new TextDecoder("utf-8", { fatal: false });

function unavailable(reason) {
  return { availability: "unavailable", reason, counts: null, workers: [], sessions: [] };
}

/** Read the response body with a byte cap enforced during streaming. */
async function readBounded(response) {
  const body = response && response.body;
  if (body && typeof body.getReader === "function") {
    const reader = body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BODY_BYTES) {
        try { await reader.cancel(); } catch { /* ignore */ }
        return null;
      }
      chunks.push(value);
    }
    return decoder.decode(Uint8Array.from(chunks.flatMap((chunk) => Array.from(chunk))));
  }
  const text = typeof response.text === "function" ? await response.text() : null;
  return typeof text === "string" && Buffer.byteLength(text) <= MAX_BODY_BYTES ? text : null;
}

export function createCompanionProvider(options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const now = options.now ?? (() => new Date());
  const loadToken = options.loadToken ?? (() => loadControlToken(options.tokenOptions ?? {}));
  const authorityDeps = options.authorityDeps ?? {};

  let tokenPromise;
  let lastGoodStatus = null;
  let lastGoodAt = 0;

  function ensureToken() {
    if (!tokenPromise) {
      tokenPromise = Promise.resolve()
        .then(() => loadToken())
        .then((value) => (typeof value === "string" && value ? value : null))
        .catch(() => null);
    }
    return tokenPromise;
  }

  async function call(companionPath, init) {
    const bearer = await ensureToken();
    if (!bearer) return { ok: false, kind: "token-unavailable" };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(`http://${COMPANION_HOST}:${COMPANION_PORT}${companionPath}`, {
        method: init?.method ?? "GET",
        redirect: "error",
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${bearer}`,
          accept: "application/json",
          ...(init?.body ? { "content-type": "application/json" } : {}),
        },
        ...(init?.body ? { body: init.body } : {}),
      });
      if (!response || response.status !== 200) return { ok: false, kind: "outage" };
      const text = await readBounded(response);
      if (typeof text !== "string") return { ok: false, kind: "oversize" };
      return { ok: true, data: JSON.parse(text) };
    } catch {
      return { ok: false, kind: "outage" };
    } finally {
      clearTimeout(timer);
    }
  }

  async function getStatus() {
    const result = await call("/status");
    if (result.ok) {
      lastGoodStatus = projectStatus(result.data);
      lastGoodAt = now().getTime();
      return { ...lastGoodStatus, observedAt: new Date(lastGoodAt).toISOString(), stale: false };
    }
    if (lastGoodStatus && now().getTime() - lastGoodAt <= STALE_AFTER_MS) {
      return { ...lastGoodStatus, observedAt: new Date(lastGoodAt).toISOString(), stale: true, reason: result.kind };
    }
    return unavailable(result.kind);
  }

  async function getTasks() {
    const result = await call("/tasks");
    if (!result.ok) return { availability: "unavailable", reason: result.kind, tasks: [] };
    return { availability: "available", observedAt: now().toISOString(), tasks: projectTasks(result.data) };
  }

  async function getReceipts(taskId) {
    if (typeof taskId !== "string" || !REPORT_ID.test(taskId)) {
      return { availability: "unavailable", reason: "invalid-id", receipts: [] };
    }
    const result = await call("/control", { method: "POST", body: JSON.stringify({ command: "report", id: taskId }) });
    if (!result.ok) return { availability: "unavailable", reason: result.kind, receipts: [] };
    return { availability: "available", observedAt: now().toISOString(), receipts: projectReceipts(result.data) };
  }

  return { getStatus, getTasks, getReceipts, authorityDeps, _reset: () => { tokenPromise = undefined; } };
}
