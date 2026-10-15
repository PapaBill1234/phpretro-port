// SPDX-License-Identifier: GPL-3.0-only
// Test owner: acquires the real OS runtime lock and stays alive holding it,
// reporting its pid and helper pid so the test can drive release/death paths.
import { singleton } from '../../src/process.mjs';
import { writeFileSync } from 'node:fs';
const [lockPath, readyPath] = process.argv.slice(2);
let release;
try {
  release = await singleton(lockPath);
} catch (error) {
  writeFileSync(readyPath, JSON.stringify({ blocked: error.message }));
  process.exit(3);
}
writeFileSync(readyPath, JSON.stringify({ pid: process.pid, helperPid: release.helperPid }));
process.on('SIGTERM', async () => { await release(); process.exit(0); });
setInterval(() => {}, 1000);
