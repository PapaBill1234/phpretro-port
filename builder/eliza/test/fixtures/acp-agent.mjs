// SPDX-License-Identifier: GPL-3.0-only
// Controlled protocol/process fixture, not an inference provider or replacement
// orchestration service. Runs under the installed canonical native ACP client.
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { writeFileSync } from 'node:fs';
const mode = process.argv[2] ?? 'complete';
const pidFile = process.argv[3];
if (pidFile) writeFileSync(pidFile, String(process.pid));
const send = m => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...m }) + '\n');
let pending, timer;
createInterface({ input: process.stdin }).on('line', line => {
  const m = JSON.parse(line);
  if (m.method === 'initialize') send({ id: m.id, result: { protocolVersion: 1, agentCapabilities: {} } });
  else if (m.method === 'session/new') send({ id: m.id, result: { sessionId: randomUUID() } });
  else if (m.method === 'session/prompt') {
    pending = m;
    if (mode === 'malformed') { process.stdout.write('malformed-wire\n'); process.exit(65); }
    if (mode === 'complete') timer = setTimeout(() => {
      send({ method: 'session/update', params: { sessionId: m.params.sessionId, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'Controlled fixture turn completed.' } } } });
      send({ id: m.id, result: { stopReason: 'end_turn' } }); pending = undefined;
    }, 150);
  } else if (m.method === 'session/cancel') {
    clearTimeout(timer); if (pending) send({ id: pending.id, result: { stopReason: 'cancelled' } }); pending = undefined;
    if (m.id !== undefined) send({ id: m.id, result: {} });
  } else if (m.method === 'session/close') { send({ id: m.id, result: {} }); }
  else if (m.id !== undefined) send({ id: m.id, error: { code: -32601, message: 'Unsupported fixture method' } });
}).on('close', () => process.exit(0));
