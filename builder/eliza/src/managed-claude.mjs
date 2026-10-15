#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only
// Credentials load only in the managed Python child, never in an Eliza character.
import { readFileSync, lstatSync, realpathSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { spawn } from 'node:child_process';
import { safeProcessEnv } from './process.mjs';
const root = resolve(import.meta.dirname, '../../..');
try {
  const file = process.env.ELIZA_BUILDER_CONFIG ?? join(root, '.builder/eliza/config.json');
  const info = lstatSync(file);
  if (realpathSync(file) !== file || !info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw Error('private config required');
  const config = JSON.parse(readFileSync(file, 'utf8'));
  if (config.workersEnabled !== true) throw Error('workers disabled');
  const child = spawn('python3', [join(root, 'scripts/deepseek-acp.py')], { env: safeProcessEnv(), stdio: 'inherit' });
  let closing = false;
  const terminate = () => { if (closing) return; closing = true; child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 3000).unref(); };
  process.on('SIGTERM', terminate); process.on('SIGINT', terminate);
  child.on('error', () => process.exit(70));
  child.on('exit', code => process.exit(code ?? 70));
} catch {
  process.stderr.write('Managed direct DeepSeek worker unavailable; inspect private configuration.\n'); process.exit(78);
}
