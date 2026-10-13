/**
 * Native-Sol generation transport.
 *
 * Spawns `scripts/codex-generate.py` with a bounded JSON request on stdin and
 * streams its JSONL events back as an async iterable. The child environment is
 * an allowlist: no API/OAuth credential is copied into the generation process
 * (the selected native `CODEX_HOME` login is managed by the CLI itself).
 *
 * This is the ONLY place the plugin reaches outside the process, and it never
 * passes a caller-selected path, executable or model id — the request object
 * contains only text, roles, tool definitions and an optional schema.
 */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

/** Absolute path of the generation script, resolved from the plugin location. */
export const GENERATE_SCRIPT = fileURLToPath(
  new URL("../../../scripts/codex-generate.py", import.meta.url),
);

const ENV_ALLOWLIST = [
  "HOME",
  "PATH",
  "LANG",
  "LC_ALL",
  "TERM",
  "TMPDIR",
  "USER",
  "LOGNAME",
  "SHELL",
  "CODEX_HOME",
];

/** Filter the parent environment down to the reviewed allowlist. */
export function generationChildEnvironment(source = process.env) {
  const env = {};
  for (const key of ENV_ALLOWLIST) {
    if (typeof source[key] === "string") env[key] = source[key];
  }
  return env;
}

/**
 * Default transport: run the Python generator as a child process.
 *
 * Returns `{ stdoutLines, exit, kill }`. `stdoutLines` is an async iterable of
 * raw JSONL lines (unbounded only up to the generator's own output; the plugin
 * additionally caps each line before parsing). `exit` resolves with the exit
 * code. `kill` terminates the child (used on cancellation/timeout).
 */
export function spawnGenerationTransport({
  python = process.env.PHPRETRO_PYTHON ?? "python3",
  timeoutMs = 300_000,
} = {}) {
  return function transport(request) {
    const child = spawn(python, [GENERATE_SCRIPT], {
      stdio: ["pipe", "pipe", "pipe"],
      env: generationChildEnvironment(),
    });
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }, timeoutMs);
    if (typeof timer.unref === "function") timer.unref();

    child.stdin.on("error", () => {
      /* ignore EPIPE when the child exits early */
    });
    child.stdin.end(JSON.stringify(request));

    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    const exit = new Promise((resolve) => {
      child.on("error", () => resolve(-1));
      child.on("close", (code) => {
        clearTimeout(timer);
        resolve(typeof code === "number" ? code : -1);
      });
    });
    return {
      stdoutLines: lines,
      exit,
      kill: () => {
        clearTimeout(timer);
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      },
    };
  };
}
