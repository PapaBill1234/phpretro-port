#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomBytes } from 'node:crypto';
import { singleton } from './process.mjs';
import { startRuntime } from './runtime.mjs';
import { serve } from './http.mjs';
const root = resolve(import.meta.dirname, '../../..');
const stateDir = resolve(process.env.ELIZA_BUILDER_STATE_DIR ?? join(root, '.builder/eliza'));
process.umask(0o077);
let release, builder;
try {
  if (process.argv[2] !== 'serve') throw Error('Use scripts/eliza-builder.py for setup/start/status/chat/task/fail/help/report');
  release = await singleton(join(stateDir, 'runtime.lock'));
  let config = {};
  const configFile = join(stateDir, 'config.json');
  try { config = JSON.parse(await readFile(configFile, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  process.env.ELIZA_BUILDER_CONFIG = configFile;
  let token;
  try { token = (await readFile(join(stateDir, 'control.token'), 'utf8')).trim(); } catch (e) {
    if (e.code !== 'ENOENT') throw e;
    token = randomBytes(32).toString('hex'); await writeFile(join(stateDir, 'control.token'), token + '\n', { mode: 0o600 });
  }
  builder = await startRuntime({ root, stateDir, boardDb: resolve(root, config.boardDb ?? '.builder/tasks.sqlite'), workersEnabled: config.workersEnabled === true });
  const server = await serve(builder, { token, port: config.port ?? 18791 });
  const address = server.address();
  await writeFile(join(stateDir, 'server.json'), JSON.stringify({ pid: process.pid, port: address.port, host: '127.0.0.1' }) + '\n', { mode: 0o600 });
  process.stdout.write(JSON.stringify({ ready: true, port: address.port, inference: false }) + '\n');
  let stopping = false;
  const stop = async () => {
    if (stopping) return; stopping = true;
    server.close(); await builder.close(); await release(); process.exit(0);
  };
  process.on('SIGTERM', stop); process.on('SIGINT', stop);
} catch {
  if (builder) await builder.close(); if (release) await release();
  process.stderr.write('Eliza startup failed; verify local package/runtime configuration.\n'); process.exit(1);
}
