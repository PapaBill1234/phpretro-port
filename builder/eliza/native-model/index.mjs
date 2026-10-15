/**
 * `openai-codex` — the native-Sol main text provider.
 *
 * Registers real `TEXT_SMALL`, `TEXT_LARGE`, `RESPONSE_HANDLER` and
 * `ACTION_PLANNER` handlers under the recognized provider name `openai-codex`,
 * backed by the pinned native ChatGPT-subscription model `gpt-6.1-sol` through
 * `scripts/codex-generate.py`.
 *
 * Contract (matches `@elizaos/core` ModelParamsMap / ModelResultMap at pin
 * 28fef47):
 *   * input  — `GenerateTextParams`: prompt / system / messages / tools /
 *              toolChoice / responseSchema / stream / onStreamChunk / signal.
 *   * output — a `string`, or a `TextStreamResult` whose `textStream` is a
 *              LIVE async iterator: provisional `item/agentMessage/delta`
 *              chunks are yielded as they arrive, and only the settled,
 *              exact-thread/turn final `text` / `usage` / `finishReason`
 *              promises resolve the answer. Tool calls are validated against
 *              the host-requested allowlist and returned to the Eliza pipeline
 *              — never executed natively.
 *
 * Routing: core registers each `models` entry with `plugin.name` as the
 * provider. The plugin stays at the NEUTRAL priority 0 and declares no
 * automatic takeover: Sol is selected explicitly (for example
 * `ELIZA_BRAIN_PROVIDER=openai-codex`, or a per-call provider argument), so the
 * cheap default text calls (e.g. DeepSeek) are untouched.
 *
 * The plugin never chooses a provider fallback: a generation error is thrown so
 * the runtime's own failover policy — not this plugin — decides what happens.
 */
export const PROVIDER_NAME = "openai-codex";
export const SOL_MODEL = "gpt-6.1-sol";
export const TEXT_SLOTS = Object.freeze([
  "TEXT_SMALL",
  "TEXT_LARGE",
  "RESPONSE_HANDLER",
  "ACTION_PLANNER",
]);
// Neutral: never outrank an existing provider, so a default cheap text call is
// not silently rerouted to the subscription model. Selection is explicit.
export const NATIVE_SOL_PRIORITY = 0;
export const MAX_LINE_BYTES = 512 * 1024;

import { spawnGenerationTransport } from "./transport.mjs";

/** Thrown when the native generation transport fails. Never carries private data. */
export class NativeSolError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "NativeSolError";
    this.code = code;
  }
}

function slotMetadata() {
  const metadata = {};
  for (const slot of TEXT_SLOTS) {
    metadata[slot] = {
      displayModelDefault: SOL_MODEL,
      displayModelSettings: ["OPENAI_CODEX_MODEL", "CODEX_MODEL"],
      streamable: true,
    };
  }
  return metadata;
}

/** Derive the generation request from `GenerateTextParams` (no path/model fields). */
export function toGenerationRequest(params = {}) {
  const messages = [];
  if (Array.isArray(params.messages)) {
    for (const message of params.messages) {
      if (!message || typeof message !== "object") continue;
      const content =
        typeof message.content === "string"
          ? message.content
          : Array.isArray(message.content)
            ? message.content
                .map((part) => (part && typeof part.text === "string" ? part.text : ""))
                .join("")
            : "";
      if (!content) continue;
      messages.push({ role: message.role ?? "user", content });
    }
  }
  if (messages.length === 0 && typeof params.prompt === "string" && params.prompt) {
    messages.push({ role: "user", content: params.prompt });
  }
  if (messages.length === 0) {
    throw new NativeSolError("empty_prompt", "Generation requires prompt or messages.");
  }
  const tools = Array.isArray(params.tools)
    ? params.tools
        .filter((tool) => tool && typeof tool.name === "string" && tool.name)
        .map((tool) => ({ name: tool.name }))
    : [];
  return {
    system: typeof params.system === "string" ? params.system : undefined,
    messages,
    tools,
    responseSchema:
      params.responseSchema && typeof params.responseSchema === "object"
        ? params.responseSchema
        : undefined,
    stream: params.stream === true,
  };
}

function typeLabel(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  return typeof value;
}

/**
 * Validate the settled output against the host-requested schema.
 *
 * Tool proposals are rejected here too: a name the host did not request (or any
 * tool call when none was requested) fails. Nothing is returned to native
 * execution — the validated payload goes back to the Eliza pipeline.
 */
export function validateOutput(final, request) {
  const allowlist = request.tools.map((tool) => tool.name);
  const calls = Array.isArray(final.toolCalls) ? final.toolCalls : [];
  for (const call of calls) {
    const name = call && typeof call === "object" ? call.name : undefined;
    if (typeof name !== "string" || !allowlist.includes(name)) {
      throw new NativeSolError("unexpected_tool", "Model proposed a tool the host did not request.");
    }
  }
  if (request.responseSchema) {
    let parsed;
    try {
      parsed = JSON.parse(final.text);
    } catch {
      throw new NativeSolError("schema_invalid", "Structured response was not valid JSON.");
    }
    const expected = request.responseSchema.type;
    if (typeof expected === "string" && typeLabel(parsed) !== expected) {
      throw new NativeSolError("schema_invalid", "Structured response did not match the requested type.");
    }
    if (Array.isArray(expected) && !expected.includes(typeLabel(parsed))) {
      throw new NativeSolError("schema_invalid", "Structured response did not match the requested type.");
    }
  }
  return final.text;
}

function parseLine(line) {
  if (typeof line !== "string" || line.length === 0) return null;
  if (Buffer.byteLength(line, "utf8") > MAX_LINE_BYTES) {
    throw new NativeSolError("line_oversized", "Generation stream frame exceeded its bound.");
  }
  let event;
  try {
    event = JSON.parse(line);
  } catch {
    throw new NativeSolError("malformed", "Generation stream contained malformed JSON.");
  }
  if (!event || typeof event !== "object") {
    throw new NativeSolError("malformed", "Generation stream contained an invalid event.");
  }
  return event;
}

/**
 * Open one generation and return a live handle:
 *   * `deltas` — async iterator of provisional delta strings (live);
 *   * `done`   — settles with the validated final payload, or rejects;
 *   * `text` / `usage` / `finishReason` — promises derived from `done`.
 *
 * The reader is started immediately, so deltas flow while the turn runs.
 */
export function openGeneration(request, transport, { signal } = {}) {
  const proc = transport(request);
  const queue = [];
  let wake = null;
  const push = (item) => {
    queue.push(item);
    if (wake) {
      const resume = wake;
      wake = null;
      resume();
    }
  };
  let failure = null;
  let final = null;
  let settleAbort;
  const aborted = new Promise((resolve) => {
    settleAbort = resolve;
  });
  const onAbort = () => {
    if (!failure) failure = { code: "cancelled", message: "Generation was cancelled." };
    proc.kill?.();
    push({ type: "end" });
    settleAbort("abort");
  };
  if (signal?.aborted) onAbort();
  else signal?.addEventListener?.("abort", onAbort, { once: true });

  const done = (async () => {
    const reader = (async () => {
      try {
        for await (const rawLine of proc.stdoutLines) {
          if (signal?.aborted) break;
          const event = parseLine(rawLine);
          if (!event) continue;
          if (event.type === "delta") {
            if (typeof event.text === "string" && event.text) push({ type: "delta", text: event.text });
          } else if (event.type === "final") {
            final = event;
          } else if (event.type === "error") {
            failure = { code: event.code || "generation", message: event.message || "Generation failed." };
          }
        }
        const code = await proc.exit;
        if (!failure && (code !== 0 || !final)) {
          failure = { code: "no_final", message: "Generator produced no settled result." };
        }
      } catch (error) {
        if (error instanceof NativeSolError) failure = { code: error.code, message: error.message };
        else failure = { code: "transport", message: error instanceof Error ? error.message : "Transport failed." };
      }
    })();
    // Cancellation settles the turn immediately even if the transport stream is
    // wedged; the child is killed by onAbort.
    await Promise.race([reader, aborted]);
    signal?.removeEventListener?.("abort", onAbort);
    push({ type: "end" });
    if (!failure && !final) failure = { code: "no_final", message: "Generator produced no settled result." };
    if (failure) {
      if (failure.code !== "cancelled") proc.kill?.();
      throw new NativeSolError(failure.code, failure.message);
    }
    validateOutput(final, request);
    return final;
  })();
  // Keep the base promise handled when only the delta iterator is consumed.
  done.catch(() => {});

  async function* deltas() {
    while (true) {
      while (queue.length) {
        const item = queue.shift();
        if (item.type === "end") return;
        yield item.text;
      }
      if (!queue.length) await new Promise((resolve) => { wake = resolve; });
    }
  }

  return {
    deltas: deltas(),
    done,
    text: settled(done, (value) => value.text),
    usage: settled(done, (value) => value.usage ?? undefined),
    finishReason: settled(done, (value) => value.finishReason ?? undefined),
    kill: () => proc.kill?.(),
  };
}

/**
 * Derive a settlement promise but keep the base marked handled so a caller that
 * only consumes the delta iterator never triggers an unhandled rejection. The
 * derived promise still rejects for a caller that awaits it.
 */
function settled(base, project) {
  const derived = base.then(project);
  derived.catch(() => {});
  return derived;
}

/** Build the four model handlers over an injected transport. */
export function createHandlers(transport) {
  const handler = async (runtime, params = {}) => {
    const request = toGenerationRequest(params);
    const open = openGeneration(request, transport, { signal: params.signal });
    const onStreamChunk = typeof params.onStreamChunk === "function" ? params.onStreamChunk : undefined;
    if (onStreamChunk) {
      // Runtime-driven stream: forward live deltas, then return settled text.
      for await (const chunk of open.deltas) await onStreamChunk(chunk);
      const settled = await open.done;
      return settled.text;
    }
    if (params.stream === true) {
      // Caller-driven stream: expose the LIVE iterator + settlement promises.
      return {
        textStream: open.deltas,
        text: open.text,
        usage: open.usage,
        finishReason: open.finishReason,
        model: SOL_MODEL,
      };
    }
    // Non-stream: drain provisional deltas, return the settled text.
    for await (const _chunk of open.deltas) void _chunk;
    const settled = await open.done;
    return settled.text;
  };
  const models = {};
  for (const slot of TEXT_SLOTS) models[slot] = handler;
  return models;
}

/** Build the plugin object. `transport` is injectable for controlled fixtures. */
export function createNativeSolPlugin({ transport = spawnGenerationTransport() } = {}) {
  return {
    name: PROVIDER_NAME,
    description:
      "Native ChatGPT-subscription Sol text provider (gpt-6.1-sol) for explicit main-model selection.",
    priority: NATIVE_SOL_PRIORITY,
    modelMetadata: slotMetadata(),
    models: createHandlers(transport),
  };
}

export const nativeSolPlugin = createNativeSolPlugin();
export default nativeSolPlugin;
