// Allowlist projection of companion payloads.
//
// The companion returns rich builder state. Only typed counts, opaque identifiers,
// coarse state, receipt kind/hash, escalation facts, and availability/staleness
// booleans may cross into the host or the view bundle. Goals, prompts, plans,
// task/output text, filesystem paths, credentials, raw errors, quota/account identity
// and model/provider metadata are dropped.

import { deriveEscalation } from "./escalation.js";

const ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const KIND_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/;
const HASH_PATTERN = /^[A-Fa-f0-9]{16,128}$/;

function opaqueId(value) {
  return typeof value === "string" && ID_PATTERN.test(value) ? value : undefined;
}

// Coarse, closed state vocabulary. Any value outside it (a path, an error string, a
// provider message, an unknown future phase) collapses to "unknown" rather than being
// forwarded as free text.
const KNOWN_STATES = new Set([
  "running", "starting", "active", "busy", "ready", "idle", "queued", "waiting",
  "paused", "blocked", "stopping", "stopped", "completed", "succeeded", "done",
  "failed", "error", "cancelled", "canceled", "archived", "unknown",
]);

function stateOf(entry) {
  const raw = entry && (entry.state ?? entry.status ?? entry.phase);
  if (typeof raw !== "string") return "unknown";
  const normalized = raw.trim().toLowerCase();
  return KNOWN_STATES.has(normalized) ? normalized : "unknown";
}

/** Project a raw companion `/status` document. Never throws on shape drift. */
export function projectStatus(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  const workers = Array.isArray(src.activeWorkers) ? src.activeWorkers : [];
  const sessions = Array.isArray(src.sessions) ? src.sessions : [];
  const jobIds = Array.isArray(src.acceptanceJobs) ? src.acceptanceJobs : [];
  return {
    availability: "available",
    counts: {
      activeWorkers: workers.length,
      sessions: sessions.length,
      plugins: Array.isArray(src.plugins) ? src.plugins.length : 0,
      services: Array.isArray(src.services) ? src.services.length : 0,
      acceptanceJobs: jobIds.length,
      workersEnabled: src.workersEnabled === true,
    },
    workers: workers
      .map((entry) => ({ id: opaqueId(entry && entry.id), state: stateOf(entry) }))
      .filter((entry) => typeof entry.id === "string"),
    sessions: sessions
      .map((entry) => ({ id: opaqueId(entry && entry.id), state: stateOf(entry) }))
      .filter((entry) => typeof entry.id === "string"),
  };
}

/** Project one raw companion task row into `{ id, state, escalation }`. */
export function projectTask(raw, receipts = []) {
  const src = raw && typeof raw === "object" ? raw : {};
  const id = opaqueId(src.id ?? src.taskId);
  if (typeof id !== "string") return null;
  const rows = Array.isArray(receipts) && receipts.length ? receipts : Array.isArray(src.receipts) ? src.receipts : [];
  return { id, state: stateOf(src), escalation: deriveEscalation(src, rows) };
}

/** Project a companion `/tasks` array. Non-array input yields an empty list. */
export function projectTasks(raw) {
  const list = Array.isArray(raw) ? raw : Array.isArray(raw && raw.tasks) ? raw.tasks : [];
  return list.map((entry) => projectTask(entry)).filter(Boolean);
}

/** Project companion receipt rows: opaque id, kind, and chain hash only. */
export function projectReceipts(raw) {
  const list = Array.isArray(raw) ? raw : Array.isArray(raw && raw.receipts) ? raw.receipts : [];
  return list
    .map((entry) => {
      const rec = entry && typeof entry === "object" ? entry : {};
      const id = opaqueId(rec.id ?? rec.receiptId);
      const kind = typeof rec.kind === "string" && KIND_PATTERN.test(rec.kind) ? rec.kind : undefined;
      const hash = typeof rec.hash === "string" && HASH_PATTERN.test(rec.hash) ? rec.hash : undefined;
      return { id, kind, hash };
    })
    .filter((entry) => typeof entry.id === "string" && entry.kind && entry.hash);
}

export const REDACTION_PATTERNS = { ID_PATTERN, KIND_PATTERN, HASH_PATTERN };
