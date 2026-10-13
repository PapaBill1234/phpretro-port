// SPDX-License-Identifier: GPL-3.0-only
import { startRuntime } from '../../src/runtime.mjs';
import { serve } from '../../src/http.mjs';
import { readFile, writeFile } from 'node:fs/promises';
const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
const b = await startRuntime(config);
const server = await serve(b, { token: 'fixture-token', port: 0 });
await writeFile(config.readyFile, JSON.stringify({ port: server.address().port, pid: process.pid }));
process.on('SIGTERM', async () => { server.close(); await b.close(); process.exit(0); });
