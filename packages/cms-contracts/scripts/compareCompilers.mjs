// SPDX-License-Identifier: GPL-3.0-only
import { spawnSync } from "node:child_process";

function run(command, args) {
  const result = spawnSync(command, args, { encoding: "utf8", timeout: 60000 });
  if (result.error) throw result.error;
  return { status: result.status, diagnostics: `${result.stdout ?? ""}${result.stderr ?? ""}`.trim() };
}

const rustVersion = run("tsc-rs", ["--version"]);
const oracleVersion = run("tsc", ["--version"]);
// The Rust port prints the development family; the matching dated oracle prints its package suffix.
// Verify both exact pinned strings rather than mistaking that reporting difference for a compiler failure.
if (rustVersion.status !== 0 || oracleVersion.status !== 0 ||
    rustVersion.diagnostics !== "Version 7.1.0-dev" ||
    oracleVersion.diagnostics !== "Version 7.1.0-dev.20260929.1") {
  throw new Error(`compiler version mismatch: ${JSON.stringify({ rustVersion, oracleVersion })}`);
}
const rust = run("tsc-rs", ["-p", "tsconfig.json", "--noEmit"]);
const oracle = run("tsc", ["-p", "tsconfig.json", "--noEmit"]);
if (rust.status !== 0 || oracle.status !== 0 || rust.diagnostics !== oracle.diagnostics) {
  throw new Error(`compiler check failed or differed: ${JSON.stringify({ rust, oracle })}`);
}
console.log(`Compiler check: Rust ${rustVersion.diagnostics}; oracle ${oracleVersion.diagnostics}; both pass with identical diagnostics.`);
