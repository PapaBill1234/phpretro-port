// Honest escalation state for a task at or past its third failure.
//
// The current companion really attempts Sol help automatically on the third failure,
// so a task past threshold can never be reported as merely "queued". State is derived
// from the actual receipt kinds when provided, else from explicit task fields; when
// neither is present the result is "unknown" rather than a fabricated status.

const THRESHOLD = 3;

function firstBoolean(...values) {
  for (const value of values) if (typeof value === "boolean") return value;
  return undefined;
}

const KIND_PATTERNS = [
  ["unavailable", /(help|escalation|sol).*(unavailable|failed|error)/i],
  ["completed", /(help|escalation|sol|review).*(complete|completed|resolved|done)/i],
  ["attempted", /(help|escalation|sol).*(attempt|request|dispatch)/i],
  ["failure", /(fail|failure|error)/i],
];

function classifyReceipt(kind) {
  if (typeof kind !== "string") return undefined;
  for (const [state, pattern] of KIND_PATTERNS) if (pattern.test(kind)) return state;
  return undefined;
}

/** Derive escalation from receipt rows (id/kind) when they are available. */
export function deriveEscalationFromReceipts(receipts, task = {}) {
  let failures = 0;
  let attempted = false;
  let completed = false;
  let unavailable = false;
  for (const receipt of Array.isArray(receipts) ? receipts : []) {
    const kind = receipt && typeof receipt === "object" ? receipt.kind : undefined;
    switch (classifyReceipt(kind)) {
      case "failure": failures += 1; break;
      case "attempted": attempted = true; break;
      case "completed": completed = true; break;
      case "unavailable": unavailable = true; break;
      default: break;
    }
  }
  if (failures === 0 && !attempted && !completed && !unavailable) return null;
  const state = completed ? "completed" : unavailable ? "unavailable" : attempted ? "attempted" : failures >= THRESHOLD ? "required" : "not-required";
  return { threshold: THRESHOLD, required: failures >= THRESHOLD || attempted || completed || unavailable, state };
}

export function deriveEscalation(task, receipts = []) {
  const src = task && typeof task === "object" ? task : {};
  const fromReceipts = deriveEscalationFromReceipts(receipts, src);
  if (fromReceipts) return fromReceipts;

  const failures =
    Number.isSafeInteger(src.failures) ? src.failures
      : Number.isSafeInteger(src.failureCount) ? src.failureCount
        : Number.isSafeInteger(src.attempts) ? src.attempts
          : undefined;

  const attempted = firstBoolean(src.helpAttempted, src.escalationAttempted);
  const completed = firstBoolean(src.helpCompleted, src.escalationCompleted);
  const unavailable = firstBoolean(src.helpUnavailable, src.escalationUnavailable);

  if (completed === true) return { threshold: THRESHOLD, required: true, state: "completed" };
  if (unavailable === true) return { threshold: THRESHOLD, required: true, state: "unavailable" };
  if (attempted === true) return { threshold: THRESHOLD, required: true, state: "attempted" };
  if (typeof failures === "number") {
    return { threshold: THRESHOLD, required: failures >= THRESHOLD, state: failures >= THRESHOLD ? "required" : "not-required" };
  }
  return { threshold: THRESHOLD, required: false, state: "unknown" };
}

export const ESCALATION_THRESHOLD = THRESHOLD;
