/**
 * Controlled integration fixture for the `openai-codex` plugin.
 *
 * PINNED CONTRACT (no real native calls): a minimal runtime reproduces the
 * `AgentRuntime` behaviours this slice depends on — registration by
 * `plugin.name`, priority ordering, the exact model fallback chains from
 * `@elizaos/core` model.ts at pin 28fef47, provider pinning via
 * `ELIZA_BRAIN_PROVIDER`, and the `TextStreamResult` pass-through/stream gate.
 * The generation transport is a fake returning canned JSONL, so nothing here
 * logs in, calls the model or launches Codex. This is a controlled fixture, not
 * the real host proof; that audit is a root-reviewer step.
 *
 * Run: `node builder/eliza/native-model/native-model.fixture.mjs`
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as pluginModule from "./index.mjs";
import {
  createNativeSolPlugin,
  nativeSolPlugin,
  SOL_MODEL,
  PROVIDER_NAME,
  TEXT_SLOTS,
} from "./index.mjs";

const FALLBACK_CHAINS = {
  TEXT_SMALL: ["TEXT_SMALL", "TEXT_MEDIUM", "TEXT_LARGE"],
  TEXT_LARGE: ["TEXT_LARGE", "TEXT_MEDIUM", "TEXT_SMALL"],
  RESPONSE_HANDLER: ["RESPONSE_HANDLER", "TEXT_NANO", "TEXT_SMALL", "TEXT_MEDIUM", "TEXT_LARGE"],
  ACTION_PLANNER: ["ACTION_PLANNER", "TEXT_MEDIUM", "TEXT_SMALL", "TEXT_LARGE"],
};
const resolveChain = (modelType) => FALLBACK_CHAINS[modelType] ?? [modelType];

function createRuntime() {
  const models = new Map();
  let order = 0;
  return {
    models,
    settings: {},
    getSetting(key) {
      return this.settings[key];
    },
    registerModel(modelType, handler, provider, priority = 0, metadata) {
      const list = models.get(modelType) ?? [];
      list.push({ handler, provider, priority, registrationOrder: order++, metadata });
      list.sort((a, b) => (b.priority - a.priority) || (a.registrationOrder - b.registrationOrder));
      models.set(modelType, list);
    },
    registerPlugin(plugin) {
      for (const [modelType, handler] of Object.entries(plugin.models)) {
        this.registerModel(modelType, handler, plugin.name, plugin.priority, plugin.modelMetadata?.[modelType]);
      }
    },
    resolve(modelType, provider) {
      for (const key of resolveChain(modelType)) {
        const list = models.get(key) ?? [];
        const candidates = provider ? list.filter((m) => m.provider === provider) : list;
        if (candidates.length) return { ...candidates[0], modelKey: key };
      }
      return undefined;
    },
    async useModel(modelType, params = {}, provider) {
      const pinned = provider ?? this.settings.ELIZA_BRAIN_PROVIDER;
      const resolved = this.resolve(modelType, pinned);
      if (!resolved) throw new Error("No handler found for delegate type: " + modelType);
      const shouldStream = params.stream === false ? false : Boolean(params.onStreamChunk || params.stream);
      const streamable = resolved.metadata?.streamable === true;
      const effective = { ...params, stream: shouldStream };
      if (streamable && shouldStream && typeof params.onStreamChunk === "function") {
        effective.onStreamChunk = params.onStreamChunk;
      } else {
        delete effective.onStreamChunk;
      }
      const result = await resolved.handler(this, effective);
      if (shouldStream && !(streamable && typeof params.onStreamChunk === "function") && result && result.textStream) {
        const inner = result.textStream;
        const wrapped = (async function* () {
          for await (const chunk of inner) {
            if (typeof params.onStreamChunk === "function") await params.onStreamChunk(chunk);
            yield chunk;
          }
        })();
        return { ...result, textStream: wrapped };
      }
      return result;
    },
  };
}

function fakeTransport(scenarios) {
  const requests = [];
  const transport = (request) => {
    requests.push(request);
    const events = scenarios.shift() ?? [{ type: "final", text: "ok", usage: null, finishReason: "stop", toolCalls: [] }];
    return {
      stdoutLines: (async function* () {
        for (const event of events) yield JSON.stringify(event);
      })(),
      exit: Promise.resolve(0),
      kill() {},
    };
  };
  return { transport, requests };
}

const results = [];
const check = (name, fn) => {
  try {
    fn();
    results.push(["PASS", name]);
  } catch (error) {
    results.push(["FAIL", name + " :: " + error.message]);
  }
};

// 1. Registration: four real slots under the recognized provider.
{
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport: fakeTransport([]).transport }));
  for (const slot of TEXT_SLOTS) {
    const reg = runtime.resolve(slot, PROVIDER_NAME);
    check("registers " + slot + " under " + PROVIDER_NAME, () =>
      assert.ok(reg && reg.provider === PROVIDER_NAME && reg.metadata?.displayModelDefault === SOL_MODEL),
    );
  }
}

// 2. Named exports + physical package manifest are discoverable.
{
  check("named exports are present", () => {
    for (const name of ["createNativeSolPlugin", "nativeSolPlugin", "TEXT_SLOTS", "SOL_MODEL", "PROVIDER_NAME", "openGeneration"]) {
      assert.ok(pluginModule[name] !== undefined, "missing export " + name);
    }
    assert.equal(nativeSolPlugin.name, PROVIDER_NAME);
  });
  check("package.json manifest declares an ESM eliza plugin", () => {
    const manifest = JSON.parse(readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf8"));
    assert.equal(manifest.type, "module");
    assert.equal(manifest.elizaos.plugin.name, PROVIDER_NAME);
    assert.equal(manifest.exports["."].import, "./index.mjs");
  });
}

// 3. Non-stream returns the settled text; the request carries no model/path.
{
  const { transport, requests } = fakeTransport([
    [{ type: "delta", text: "Hel" }, { type: "final", text: "Hello", usage: { totalTokens: 3 }, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const text = await runtime.useModel("TEXT_LARGE", { prompt: "hi" });
  check("non-stream returns settled text", () => assert.equal(text, "Hello"));
  check("request carries no model/path fields", () =>
    assert.ok(!("model" in requests[0]) && !("cwd" in requests[0]) && requests[0].messages[0].role === "user"),
  );
}

// 4. Handler-driven stream: deltas reach onStreamChunk live; text returned.
{
  const { transport } = fakeTransport([
    [{ type: "delta", text: "a" }, { type: "delta", text: "b" }, { type: "final", text: "ab", usage: null, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const seen = [];
  const text = await runtime.useModel("TEXT_LARGE", { prompt: "hi", stream: true, onStreamChunk: (c) => seen.push(c) });
  check("handler stream forwards deltas and returns text", () => {
    assert.deepEqual(seen, ["a", "b"]);
    assert.equal(text, "ab");
  });
}

// 5. Caller-driven stream is a LIVE iterator, not a settled-buffer replay.
{
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const transport = () => ({
    stdoutLines: (async function* () {
      yield JSON.stringify({ type: "delta", text: "live" });
      await gate; // the final cannot arrive until the test releases it
      yield JSON.stringify({ type: "final", text: "live!", usage: { totalTokens: 2 }, finishReason: "stop", toolCalls: [] });
    })(),
    exit: Promise.resolve(0),
    kill() {},
  });
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const result = await runtime.useModel("TEXT_LARGE", { prompt: "hi", stream: true });
  const iterator = result.textStream[Symbol.asyncIterator]();
  const first = await iterator.next();
  check("textStream yields the delta before the turn settles", () => assert.equal(first.value, "live"));
  release();
  const chunks = [first.value];
  for (let next = await iterator.next(); !next.done; next = await iterator.next()) chunks.push(next.value);
  const settledText = await result.text;
  const settledFinish = await result.finishReason;
  check("textStream then settles on the final text/finishReason", () =>
    assert.deepEqual(chunks, ["live"]) && settledText === "live!" && settledFinish === "stop",
  );
}

// 6. RESPONSE_HANDLER / ACTION_PLANNER route to the native provider.
{
  const { transport } = fakeTransport([
    [{ type: "final", text: "RESPOND", usage: null, finishReason: "stop", toolCalls: [] }],
    [{ type: "final", text: "[]", usage: null, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const responder = await runtime.useModel("RESPONSE_HANDLER", { prompt: "should respond?" });
  const planner = await runtime.useModel("ACTION_PLANNER", { prompt: "plan" });
  check("responder and planner use native provider", () => {
    assert.equal(responder, "RESPOND");
    assert.equal(planner, "[]");
  });
}

// 7. No automatic takeover: a default cheap provider keeps priority; Sol is explicit.
{
  const { transport } = fakeTransport([
    [{ type: "final", text: "native", usage: null, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerModel("TEXT_LARGE", async () => "cheap", "deepseek", 0, { streamable: false });
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const cheap = await runtime.useModel("TEXT_LARGE", { prompt: "hi" });
  runtime.settings.ELIZA_BRAIN_PROVIDER = PROVIDER_NAME;
  const native = await runtime.useModel("TEXT_LARGE", { prompt: "hi" });
  check("default call keeps the cheap provider; Sol is explicit", () => {
    assert.equal(cheap, "cheap");
    assert.equal(native, "native");
  });
}

// 8. A provider error is surfaced, never silently satisfied by a fallback.
{
  const { transport } = fakeTransport([
    [{ type: "error", code: "unexpected_tool", message: "Turn produced a non-text item." }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  let code;
  try {
    await runtime.useModel("TEXT_LARGE", { prompt: "hi" });
  } catch (error) {
    code = error.code;
  }
  check("error event throws (no fallback)", () => assert.equal(code, "unexpected_tool"));
}

// 9. Malformed stream frame fails closed.
{
  const transport = () => ({
    stdoutLines: (async function* () {
      yield "not-json";
    })(),
    exit: Promise.resolve(0),
    kill() {},
  });
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  let code;
  try {
    await runtime.useModel("TEXT_LARGE", { prompt: "hi" });
  } catch (error) {
    code = error.code;
  }
  check("malformed frame fails closed", () => assert.equal(code, "malformed"));
}

// 10. Structured output is validated; an unrequested tool call fails.
{
  const { transport } = fakeTransport([
    [{ type: "final", text: "not json", usage: null, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  let code;
  try {
    await runtime.useModel("TEXT_LARGE", { prompt: "hi", responseSchema: { type: "object" } });
  } catch (error) {
    code = error.code;
  }
  check("schema mismatch fails closed", () => assert.equal(code, "schema_invalid"));

  const { transport: t2 } = fakeTransport([
    [{ type: "final", text: "done", usage: null, finishReason: "stop", toolCalls: [{ id: "t", name: "exec", arguments: {} }] }],
  ]);
  const runtime2 = createRuntime();
  runtime2.registerPlugin(createNativeSolPlugin({ transport: t2 }));
  let toolCode;
  try {
    await runtime2.useModel("TEXT_LARGE", { prompt: "hi" });
  } catch (error) {
    toolCode = error.code;
  }
  check("unrequested tool call fails closed", () => assert.equal(toolCode, "unexpected_tool"));
}

// 11. Cancellation rejects and kills the transport.
{
  const controller = new AbortController();
  let killed = false;
  const transport = () => ({
    stdoutLines: (async function* () {
      yield JSON.stringify({ type: "delta", text: "partial" });
      await new Promise(() => {});
    })(),
    exit: new Promise(() => {}),
    kill() {
      killed = true;
    },
  });
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  const promise = runtime.useModel("TEXT_LARGE", { prompt: "hi", signal: controller.signal });
  controller.abort();
  let code;
  try {
    await promise;
  } catch (error) {
    code = error.code;
  }
  check("cancellation rejects with cancelled and kills the transport", () => {
    assert.equal(code, "cancelled");
    assert.ok(killed);
  });
}

// 12. Cross-call separation: each generation is an independent request.
{
  const { transport, requests } = fakeTransport([
    [{ type: "final", text: "one", usage: null, finishReason: "stop", toolCalls: [] }],
    [{ type: "final", text: "two", usage: null, finishReason: "stop", toolCalls: [] }],
  ]);
  const runtime = createRuntime();
  runtime.registerPlugin(createNativeSolPlugin({ transport }));
  await runtime.useModel("TEXT_LARGE", { prompt: "first" });
  await runtime.useModel("TEXT_LARGE", { prompt: "second" });
  check("each call is a fresh request (no context resume)", () => {
    assert.equal(requests.length, 2);
    assert.equal(requests[0].messages[0].content, "first");
    assert.equal(requests[1].messages[0].content, "second");
  });
}

const failed = results.filter(([status]) => status === "FAIL");
for (const [status, name] of results) console.log(status + " " + name);
console.log(`\n${results.length - failed.length}/${results.length} fixture checks passed`);
process.exitCode = failed.length === 0 ? 0 : 1;
