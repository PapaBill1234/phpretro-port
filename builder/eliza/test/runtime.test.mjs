// SPDX-License-Identifier: GPL-3.0-only
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, mkdir, copyFile, chmod, symlink, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { startRuntime } from '../src/runtime.mjs';
import { quotaDecision, rejectSecrets, Builder } from '../src/builder.mjs';
import { serve } from '../src/http.mjs';
import { runProcess, singleton, safeProcessEnv, bridgeEnv } from '../src/process.mjs';
const exec = promisify(execFile);
const root = resolve(import.meta.dirname, '../../..');
const fixture = join(import.meta.dirname, 'fixtures/acp-agent.mjs');
const delay = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = await fn(); if (value) return value; await delay(50); }
  throw Error('Timed out waiting for actual runtime/process state');
}
async function create(b, name, code = 0) {
  return b.create({ boardId: name, scope: [`builder/eliza/test/scope-${name}`], goal: 'Exercise actual offline runtime boundary',
    acceptanceCriteria: ['Actual acceptance command succeeds'], acceptanceCommand: [process.execPath, '-e', `process.exit(${code})`] });
}
async function repository() {
  const dir = await mkdtemp(join(tmpdir(), 'phpretro-eliza-repo-'));
  await mkdir(join(dir, 'scripts')); await copyFile(join(root, 'scripts/task-board.py'), join(dir, 'scripts/task-board.py'));
  await exec('git', ['init', dir]); await exec('git', ['-C', dir, 'add', 'scripts/task-board.py']);
  await exec('git', ['-C', dir, '-c', 'user.name=Offline Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Offline task-board source fixture']);
  return dir;
}
async function cleanupWorktrees(b) {
  for (const t of await b.tasks.listTasks()) { const d = await b.get(t.id); if (d.metadata.workdir) await b.git(['worktree', 'remove', '--force', d.metadata.workdir]); }
}

test('real runtime, canonical SQL task/session persistence, HTTP controls, exact third failure and quota handoff', { timeout: 120000 }, async () => {
  const repo = await repository(), stateDir = join(repo, '.builder/eliza');
  let solCalls = 0;
  const b = await startRuntime({ root: repo, stateDir, boardDb: join(stateDir, 'board.sqlite'), fixtureCommand: `node ${fixture} complete`,
    bridgeCall: async command => { if (command === 'help') { solCalls++; throw Error('No turns allowed'); } return { schema: 'phpretro.codex.bridge.v1', ok: true, readiness_fresh: true, model_available: true, limits: { dispatch_allowed: false, windows: [{duration_minutes: 300, used_percent: 90, resets_at: 2000000000}, {duration_minutes: 10080, used_percent: 2, resets_at: 2000000000}] } }; } });
  const server = await serve(b, { token: 'fixture-token', port: 0 });
  const url = `http://127.0.0.1:${server.address().port}`;
  async function control(body) { const r = await fetch(`${url}/control`, { method: 'POST', headers: { Authorization: 'Bearer fixture-token' }, body: JSON.stringify(body) }); const value = await r.json(); assert.equal(r.status, 200, JSON.stringify(value)); return value; }
  try {
    const status = await (await fetch(`${url}/status`, { headers: { Authorization: 'Bearer fixture-token' } })).json();
    assert.ok(status.plugins.includes('@elizaos/plugin-agent-orchestrator'));
    assert.ok(status.plugins.includes('phpretro-builder-boundary'));
    assert.ok(status.services.includes('ACP_SUBPROCESS_SERVICE'));
    assert.equal((await fetch(`${url}/status`)).status, 401);
    assert.equal((await fetch(`${url}/status`, { headers: { Authorization: 'Bearer fixture-token', Origin: 'http://example.com' } })).status, 401);
    assert.equal(status.quota.dailyAvailable, false);
    const input = { boardId: 'G06-eliza-third', scope: ['builder/eliza/test/offline-failure'], goal: 'Offline failure acceptance',
      acceptanceCriteria: ['Exit zero'], acceptanceCommand: [process.execPath, '-e', 'process.exit(7)'] };
    const task = await control({ command: 'task-create', task: input });
    for (let i = 1; i <= 3; i++) {
      const job = await control({ command: 'task-dispatch', id: task.id, revision: `Correction plan ${i}` });
      await until(async () => (await b.get(task.id)).sessions.some(s => s.sessionId === job.sessionId && s.status === 'completed'));
      const check = await control({ command: 'fail', id: task.id });
      assert.equal(check.kind, 'acceptance_failed'); assert.equal(check.receipt.data.code, 7);
      assert.equal((await b.failures(task.id)).length, i);
      assert.equal((await b.receipts.list(task.id)).filter(r => r.kind === 'escalation_required').length, i === 3 ? 1 : 0);
      await assert.rejects(b.check(task.id), /unchecked actual worker/);
      if (i < 3) await assert.rejects(b.dispatch(task.id), /Unchanged failed/);
    }
    await assert.rejects(b.dispatch(task.id, 'Fourth must not run'), /Third acceptance/);
    assert.equal(solCalls, 0);
    assert.equal(JSON.parse(await readFile(join(stateDir, 'HANDOFF.json'))).quota.reason, 'quota_reserve');
    const report = await control({ command: 'report', id: task.id }); assert.equal(report.board[0].status, 'claimed');
    assert.equal(report.receipts.filter(r => r.kind === 'acceptance_failed').length, 3);
    assert.ok(report.task.messages.some(m => m.content.startsWith('acceptance_failed')));
    const rows = await b.adapter.getRawConnection().query('SELECT document FROM orchestrator_tasks WHERE id=$1', [task.id]);
    assert.equal(JSON.parse(rows.rows[0].document).task.metadata.boardId, input.boardId);
    const sessions = await b.adapter.getRawConnection().query('SELECT * FROM acp_sessions'); assert.equal(sessions.rows.length, 3);
    assert.ok((await b.runtime.getTasksByName('PHPRETRO_RECONCILE')).length === 1);
    assert.ok(await b.receipts.verify());
  } finally { server.close(); await cleanupWorktrees(b); await b.close(); await rm(repo, { recursive: true, force: true }); }
});

test('canonical ACP concurrent reservation caps at four and actual cancellation closes fixture processes', { timeout: 120000 }, async () => {
  const repo = await repository(), stateDir = join(repo, '.builder/eliza');
  const b = await startRuntime({ root: repo, stateDir, boardDb: join(stateDir, 'board.sqlite'), fixtureCommand: `node ${fixture} hang` });
  try {
    const tasks = [];
    for (let i = 0; i < 6; i++) { const t = await create(b, `G06-cap-${i}`); await b.claim(t.id); tasks.push(t); }
    const results = await Promise.allSettled(tasks.map(t => b.dispatch(t.id)));
    assert.equal(results.filter(r => r.status === 'fulfilled').length, 4);
    assert.equal((await b.status()).activeWorkers.length, 4);
    for (let i = 0; i < tasks.length; i++) if (results[i].status === 'fulfilled') {
      const sid = results[i].value.sessionId;
      await b.cancel(tasks[i].id);
      assert.equal((await b.acp.getSession(sid)).status, 'stopped');
      assert.equal((await b.failures(tasks[i].id)).length, 0);
    }
    assert.equal((await b.status()).activeWorkers.length, 0);
    // The guard rejects upstream action-layer alternative model/framework paths.
    await assert.rejects(b.acp.spawnSession({ agentType: 'codex', model: 'gpt-6.1-sol' }), /Only managed/);
  } finally { await cleanupWorktrees(b); await b.close(); await rm(repo, { recursive: true, force: true }); }
});

test('fresh OS runtime process recovers canonical task and orphaned session without redispatch', { timeout: 120000 }, async () => {
  const repo = await repository(), stateDir = join(repo, '.builder/eliza'); await mkdir(stateDir, { recursive: true });
  const config = { root: repo, stateDir, boardDb: join(stateDir, 'board.sqlite'), fixtureCommand: `node ${fixture} hang`, readyFile: join(stateDir, 'ready.json') };
  const configFile = join(stateDir, 'test-config.json'); await writeFile(configFile, JSON.stringify(config));
  let child;
  async function launch() {
    await rm(config.readyFile, { force: true });
    child = spawn(process.execPath, [join(import.meta.dirname, 'fixtures/runtime-host.mjs'), configFile], { stdio: 'ignore' });
    const ready = await until(async () => { try { return JSON.parse(await readFile(config.readyFile)); } catch { return null; } }, 30000);
    return async body => { const r = await fetch(`http://127.0.0.1:${ready.port}/control`, { method: 'POST', headers: { Authorization: 'Bearer fixture-token' }, body: JSON.stringify(body) }); assert.equal(r.status, 200); return r.json(); };
  }
  let workdir, branch;
  try {
    let control = await launch();
    const task = await control({ command: 'task-create', task: { boardId: 'G06-restart', scope: ['builder/eliza/test/restart-owned'], goal: 'Crash recovery fixture', acceptanceCriteria: ['exit zero'], acceptanceCommand: [process.execPath, '-e', 'process.exit(0)'] } });
    const job = await control({ command: 'task-dispatch', id: task.id }); workdir = job.task.metadata.workdir; branch = job.task.metadata.branch;
    const before = await control({ command: 'report', id: task.id });
    child.kill('SIGKILL'); await new Promise(r => child.once('exit', r));
    // Fixture exits on stdin EOF, even when its runtime parent crashes.
    control = await launch();
    const after = await control({ command: 'report', id: task.id });
    assert.equal(after.task.metadata.boardId, 'G06-restart');
    assert.equal(after.task.sessions.length, 1);
    assert.equal(after.receipts.filter(r => r.kind === 'dispatch_started').length, 1);
    assert.ok(after.receipts.some(r => r.kind === 'attempt_interrupted' && r.data.category === 'process_recovered'));
    assert.equal(after.receipts.filter(r => r.kind === 'acceptance_failed').length, 0);
    assert.equal(after.receipts[0].hash, before.receipts[0].hash);
    child.kill('SIGTERM'); await new Promise(r => child.once('exit', r)); child = null;
  } finally {
    if (child) { child.kill('SIGTERM'); await new Promise(r => child.once('exit', r)); }
    if (workdir) await exec('git', ['-C', repo, 'worktree', 'remove', '--force', workdir]);
    if (branch) await exec('git', ['-C', repo, 'branch', '-D', branch]);
    await rm(repo, { recursive: true, force: true });
  }
});

test('process acceptance timeout kills real grandchild and singleton denies concurrent runtime', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'eliza-process-'));
  try {
    const release = await singleton(join(dir, 'lock'));
    await assert.rejects(singleton(join(dir, 'lock')), /already running/); await release();
    const pidFile = join(dir, 'grandchild.pid');
    const result = await runProcess([process.execPath, '-e', `const {spawn}=require('child_process'); const {writeFileSync}=require('fs'); const p=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'inherit'}); writeFileSync(${JSON.stringify(pidFile)},String(p.pid)); setInterval(()=>{},1000)`], { timeout: 400 });
    assert.equal(result.timedOut, true);
    const pid = (await readFile(pidFile, 'utf8')).trim();
    await until(async () => { try { const stat = await readFile(`/proc/${pid}/stat`, 'utf8'); return stat.slice(stat.lastIndexOf(')') + 2).startsWith('Z'); } catch (e) { if (e.code === 'ENOENT' || e.code === 'ESRCH') return true; throw e; } });
  } finally { await rm(dir, { recursive: true, force: true }); }
});


test('Sol wire rejects cached/unknown limits and reserves every actually reported window', () => {
  const wire = {schema: 'phpretro.codex.bridge.v1', ok: true, readiness_fresh: true, model_available: true, limits: {dispatch_allowed: true, windows: [{duration_minutes: 300, used_percent: 4, resets_at: 2000000000}, {duration_minutes: 10080, used_percent: 5, resets_at: 2000000000}]}};
  assert.equal(quotaDecision(wire).allowed, true);
  assert.equal(quotaDecision(wire).dailyAvailable, false);
  assert.equal(quotaDecision({...wire, readiness_fresh:false}).allowed, false);
  assert.equal(quotaDecision(null).allowed, false);
  const blocked=structuredClone(wire); blocked.limits.windows[1].used_percent=90;
  assert.equal(quotaDecision(blocked).reason, 'quota_reserve');
  const daily=structuredClone(wire); daily.limits.windows.push({duration_minutes:1440,used_percent:91,resets_at:2000000000});
  assert.equal(quotaDecision(daily).dailyAvailable, true);
  assert.equal(quotaDecision(daily).allowed, false);
  const invalid=structuredClone(wire);invalid.limits.windows[0].used_percent=null;
  assert.equal(quotaDecision(invalid).allowed, false);
});


test('actual runtime prevents overlapping dispatch and identifies unchanged failed worker output', {timeout:120000}, async () => {
  const repo=await repository(),stateDir=join(repo,'.builder/eliza');
  const b=await startRuntime({root:repo,stateDir,boardDb:join(stateDir,'board.sqlite'),fixtureCommand:`node ${fixture} complete`});
  try {
    const t=await create(b,'G06-overlap-tree',7);await b.claim(t.id);
    const attempts=await Promise.allSettled([b.dispatch(t.id),b.dispatch(t.id)]);
    assert.equal(attempts.filter(r=>r.status==='fulfilled').length,1);
    const job=attempts.find(r=>r.status==='fulfilled').value;
    await until(async()=>(await b.get(t.id)).sessions.some(s=>s.sessionId===job.sessionId&&s.status==='completed'));
    const workdir=(await b.get(t.id)).metadata.workdir;
    await writeFile(join(workdir,'worker-new-file.txt'),'first worker output');
    assert.equal((await b.check(t.id)).kind,'acceptance_failed');
    await assert.rejects(b.dispatch(t.id),/Unchanged failed/);
    assert.equal((await b.receipts.list(t.id)).filter(r=>r.kind==='dispatch_started').length,1);
    await writeFile(join(workdir,'worker-new-file.txt'),'corrected worker output');
    const retry=await b.dispatch(t.id);
    await until(async()=>(await b.get(t.id)).sessions.some(s=>s.sessionId===retry.sessionId&&s.status==='completed'));
    const fingerprint=b.codeFingerprint.bind(b);let calls=0,release,reached;
    const gate=new Promise(r=>release=r),waiting=new Promise(r=>reached=r);
    b.codeFingerprint=async task=>{calls++;if(calls===2){reached();await gate;}return fingerprint(task);};
    const checking=b.check(t.id);await waiting;await b.cancel(t.id);release();
    assert.equal((await checking).kind,'attempt_interrupted');
    assert.equal((await b.failures(t.id)).length,1);
    const records=await b.receipts.list(t.id);
    assert.equal(records.filter(r=>r.data.attemptId===retry.attemptId&&['acceptance_failed','acceptance_passed','attempt_interrupted'].includes(r.kind)).length,1);
  } finally {await cleanupWorktrees(b);await b.close();await rm(repo,{recursive:true,force:true});}
});

test('reconcile preserves an in-flight spawn and acceptance refuses a changing worktree', { timeout: 120000 }, async () => {
  const repo = await repository(), stateDir = join(repo, '.builder/eliza');
  const b = await startRuntime({ root: repo, stateDir, boardDb: join(stateDir, 'board.sqlite'), fixtureCommand: `node ${fixture} complete` });
  let release;
  try {
    const task = await create(b, 'G06-inflight-reconcile'); await b.claim(task.id);
    const spawnAgent = b.tasks.spawnAgentForTask.bind(b.tasks);
    let reached;
    const entered = new Promise(r => { reached = r; });
    const gate = new Promise(r => { release = r; });
    b.tasks.spawnAgentForTask = async (...args) => { reached(); await gate; return spawnAgent(...args); };
    const dispatch = b.dispatch(task.id);
    await entered; await b.reconcile();
    assert.equal((await b.receipts.list(task.id)).filter(r => r.kind === 'dispatch_recovery_required').length, 0);
    release(); const job = await dispatch;
    b.tasks.spawnAgentForTask = spawnAgent;
    await until(async () => (await b.get(task.id)).sessions.some(s => s.sessionId === job.sessionId && s.status === 'completed'));
    assert.equal((await b.check(task.id)).kind, 'acceptance_passed');

    const started = join(stateDir, 'acceptance-started'), proceed = join(stateDir, 'acceptance-proceed');
    const changed = await b.create({ boardId: 'G06-changing-tree', scope: ['builder/eliza/test/changing-tree'], goal: 'Refuse evidence from a concurrently changing tree',
      acceptanceCriteria: ['Tree remains unchanged while the real command runs'],
      acceptanceCommand: [process.execPath, '-e', `const fs=require('node:fs'); fs.writeFileSync(${JSON.stringify(started)},'started'); const timer=setInterval(()=>{if(fs.existsSync(${JSON.stringify(proceed)})){clearInterval(timer); process.exit(0)}},20)`] });
    const next = await b.dispatch(changed.id);
    await until(async () => (await b.get(changed.id)).sessions.some(s => s.sessionId === next.sessionId && s.status === 'completed'));
    const workdir = (await b.get(changed.id)).metadata.workdir;
    const checking = b.check(changed.id);
    await until(async () => { try { return await readFile(started); } catch { return null; } });
    await mkdir(join(workdir, 'builder/eliza/test/changing-tree'), { recursive: true });
    await writeFile(join(workdir, 'builder/eliza/test/changing-tree/changed.txt'), 'Changed during acceptance');
    await writeFile(proceed, 'proceed');
    const result = await checking;
    assert.equal(result.receipt.data.code, 0);
    assert.equal(result.receipt.data.treeStable, false);
    assert.equal(result.kind, 'attempt_interrupted');
    assert.equal((await b.failures(changed.id)).length, 0);
    assert.ok(await b.receipts.verify());
  } finally {
    release?.(); await cleanupWorktrees(b); await b.close(); await rm(repo, { recursive: true, force: true });
  }
});

test('bridge forwards the selected CODEX_HOME to its Python child but no provider credential', async () => {
  const repo = await mkdtemp(join(tmpdir(), 'phpretro-bridge-'));
  const keys = ['CODEX_HOME', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY'];
  const prior = Object.fromEntries(keys.map(k => [k, process.env[k]]));
  try {
    await mkdir(join(repo, 'scripts'));
    await copyFile(join(import.meta.dirname, 'fixtures/fake-bridge.py'), join(repo, 'scripts/codex-bridge.py'));
    process.env.CODEX_HOME = '/tmp/selected-codex-home';
    process.env.ANTHROPIC_AUTH_TOKEN = 'placeholder-not-a-real-token';
    process.env.OPENAI_API_KEY = 'placeholder-not-a-real-key';
    const wire = await new Builder({ root: repo }).bridge('limits');
    assert.equal(wire.received.CODEX_HOME, '/tmp/selected-codex-home');
    assert.equal(wire.received.hasCredential, false);
    // DeepSeek/acceptance environment never carries the bridge login or a credential.
    assert.equal(safeProcessEnv().CODEX_HOME, undefined);
    assert.equal(safeProcessEnv().ANTHROPIC_AUTH_TOKEN, undefined);
    assert.equal(bridgeEnv().CODEX_HOME, '/tmp/selected-codex-home');
    assert.equal(bridgeEnv().ANTHROPIC_AUTH_TOKEN, undefined);
    assert.throws(() => rejectSecrets({ token: 'sk-' + 'fixture'.repeat(5) }), /Secret-like/);
    assert.throws(() => rejectSecrets('openai_api_key=supersecretvalue'), /Secret-like/);
  } finally {
    for (const k of keys) { if (prior[k] === undefined) delete process.env[k]; else process.env[k] = prior[k]; }
    await rm(repo, { recursive: true, force: true });
  }
});

test('OS advisory singleton lock admits exactly one owner across a stale-content inode', { timeout: 60000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'eliza-lock-'));
  const lockPath = join(dir, 'runtime.lock');
  const owner = join(import.meta.dirname, 'fixtures/lock-owner.mjs');
  const probe = join(import.meta.dirname, 'fixtures/lock-try.mjs');
  const children = [];
  const spawnOwner = async name => {
    const ready = join(dir, `${name}.ready`);
    children.push(spawn(process.execPath, [owner, lockPath, ready], { stdio: 'ignore' }));
    return ready;
  };
  const acquire = async () => { try { await exec(process.execPath, [probe, lockPath]); return true; } catch { return false; } };
  try {
    // Leftover legacy file: its PID content must never be trusted or unlinked.
    await writeFile(lockPath, JSON.stringify({ pid: 999999 }), { mode: 0o600 });
    await chmod(lockPath, 0o600);
    const inode = (await stat(lockPath)).ino;
    const readyA = await spawnOwner('a'), readyB = await spawnOwner('b');
    const states = await until(async () => {
      const a = await readFile(readyA, 'utf8').catch(() => null), b = await readFile(readyB, 'utf8').catch(() => null);
      return a && b ? [JSON.parse(a), JSON.parse(b)] : null;
    });
    assert.equal(states.filter(s => s.pid).length, 1, 'exactly one owner must hold the lock');
    assert.equal(states.filter(s => s.blocked).length, 1);
    assert.match(states.find(s => s.blocked).blocked, /already running/);
    const winner = children[states.findIndex(s => s.pid)];
    winner.kill('SIGTERM'); await new Promise(r => winner.once('exit', r));
    await until(acquire);
    assert.equal((await stat(lockPath)).ino, inode, 'lock inode must never be unlinked or recreated');
  } finally {
    for (const c of children) { try { c.kill('SIGKILL'); } catch {} }
    await rm(dir, { recursive: true, force: true });
  }
});

test('singleton reacquires after a killed owner and fails closed on helper death', { timeout: 60000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'eliza-lock-'));
  const lockPath = join(dir, 'runtime.lock');
  const owner = join(import.meta.dirname, 'fixtures/lock-owner.mjs');
  const probe = join(import.meta.dirname, 'fixtures/lock-try.mjs');
  const children = [];
  const spawnOwner = async name => {
    const ready = join(dir, `${name}.ready`);
    const child = spawn(process.execPath, [owner, lockPath, ready], { stdio: 'ignore' });
    children.push(child);
    return { child, ready };
  };
  const readyState = ready => until(async () => { try { return JSON.parse(await readFile(ready, 'utf8')); } catch { return null; } });
  const acquire = async () => { try { await exec(process.execPath, [probe, lockPath]); return true; } catch { return false; } };
  try {
    // SIGKILL the holder: the helper sees pipe EOF and releases, so a restart reacquires.
    const first = await spawnOwner('kill');
    await readyState(first.ready);
    first.child.kill('SIGKILL'); await new Promise(r => first.child.once('exit', r));
    await until(acquire);

    // Helper death must not leave a live unprotected runtime: the owner exits.
    const second = await spawnOwner('death');
    const state = await readyState(second.ready);
    assert.ok(state.pid && state.helperPid);
    process.kill(state.helperPid, 'SIGKILL');
    const exit = await Promise.race([
      new Promise(r => second.child.once('exit', (code, signal) => r({ code, signal }))),
      delay(15000).then(() => null),
    ]);
    assert.ok(exit, 'owner must terminate when its OS lock helper dies');
    await until(acquire);
  } finally {
    for (const c of children) { try { c.kill('SIGKILL'); } catch {} }
    await rm(dir, { recursive: true, force: true });
  }
});

test('singleton fails closed on symlink, FIFO, loose mode and foreign-owner lock paths', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'eliza-lock-'));
  try {
    const symlinkPath = join(dir, 'link');
    await symlink(join(dir, 'missing-target'), symlinkPath);
    await assert.rejects(singleton(symlinkPath), /symlink/);

    const fifoPath = join(dir, 'fifo');
    await exec('mkfifo', [fifoPath]);
    await assert.rejects(singleton(fifoPath), /regular file/);

    const loosePath = join(dir, 'loose');
    await writeFile(loosePath, 'legacy', { mode: 0o644 }); await chmod(loosePath, 0o644);
    await assert.rejects(singleton(loosePath), /0600/);

    // A regular file owned by another user is refused (read-only lstat only).
    if (typeof process.getuid === 'function' && process.getuid() !== 0) {
      const foreign = await stat('/etc/passwd').catch(() => null);
      if (foreign && foreign.uid !== process.getuid()) await assert.rejects(singleton('/etc/passwd'), /owned by the runtime user/);
    }
  } finally { await rm(dir, { recursive: true, force: true }); }
});
