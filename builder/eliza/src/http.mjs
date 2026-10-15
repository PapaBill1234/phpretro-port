// SPDX-License-Identifier: GPL-3.0-only
import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { rejectSecrets } from './builder.mjs';

export async function serve(builder, { token, port = 18791 }) {
  const server = createServer(async (req, res) => {
    const respond = (status, data) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(data)); };
    const auth = Buffer.from(req.headers.authorization ?? ''), expected = Buffer.from(`Bearer ${token}`);
    if (req.headers.origin || auth.length !== expected.length || !timingSafeEqual(auth, expected)) return respond(401, { error: 'Local control authentication required' });
    try {
      if (req.method === 'GET' && req.url === '/status') return respond(200, await builder.status());
      if (req.method === 'GET' && req.url === '/tasks') return respond(200, await builder.tasks.listTasks({ includeArchived: true }));
      if (req.method !== 'POST' || req.url !== '/control') return respond(404, { error: 'Unknown route' });
      let body = ''; for await (const chunk of req) { body += chunk; if (Buffer.byteLength(body) > 32768) return respond(413, { error: 'Control request too large' }); }
      const input = JSON.parse(body); rejectSecrets(input);
      let result;
      switch (input.command) {
        case 'task-create': result = await builder.serial(() => builder.create(input.task)); break;
        case 'task-import': result = await builder.serial(() => builder.importBoard(input.task)); break;
        case 'task-status': result = await builder.get(input.id); break;
        case 'task-claim': result = await builder.serial(() => builder.claim(input.id)); break;
        case 'task-dispatch': result = await builder.dispatch(input.id, input.revision); break;
        case 'fail': case 'task-check': result = await builder.check(input.id); break;
        case 'task-cancel': result = await builder.cancel(input.id); break;
        case 'help': result = await builder.help(input.id, input.reason, input.promptFile); break;
        case 'report': result = await builder.report(input.id); break;
        case 'chat':
          if (input.message === '/status') result = await builder.status();
          else if (input.message === '/tasks') result = await builder.tasks.listTasks();
          else result = await builder.help(input.id, 'architecture', input.promptFile);
          break;
        default: return respond(400, { error: 'Unknown control command' });
      }
      respond(200, result);
    } catch (e) {
      // Child stdout/stderr and exception messages can contain credentials or
      // provider transcripts. Return only explicitly authored boundary errors.
      const message = e?.cmd || e?.stdout || e?.stderr ? 'Boundary subprocess failed; inspect private prerequisites' : e.message;
      respond(409, { error: message });
    }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  return server;
}
