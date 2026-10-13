// SPDX-License-Identifier: GPL-3.0-only
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, realpath, stat, lstat, readlink } from 'node:fs/promises';
import { join, resolve, relative } from 'node:path';
import { runProcess, safeProcessEnv, bridgeEnv } from './process.mjs';
const exec = promisify(execFile);
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const terminal = new Set(['completed', 'stopped', 'errored', 'cancelled']);

export function quotaDecision(wire) {
  const source = wire?.limits;
  const windows = Array.isArray(source?.windows) ? source.windows : [];
  const valid = windows.length > 0 && windows.every(w =>
    Number.isFinite(w.used_percent) && w.used_percent >= 0 && w.used_percent <= 100 &&
    Number.isFinite(w.duration_minutes) && w.duration_minutes > 0 &&
    Number.isFinite(w.resets_at) && w.resets_at > Date.now() / 1000);
  const ready = wire?.schema === 'phpretro.codex.bridge.v1' && wire.ok === true &&
    wire.readiness_fresh === true && wire.model_available === true && source?.dispatch_allowed === true;
  const reserve = valid && windows.some(w => w.used_percent >= 90);
  return { allowed: Boolean(ready && valid && !reserve),
    reason: reserve ? 'quota_reserve' : ready && valid ? 'quota_available' : 'quota_unknown', windows,
    dailyAvailable: valid && windows.some(w => w.duration_minutes === 1440),
    remainingPercent: valid ? Math.min(...windows.map(w => 100 - w.used_percent)) : null };
}

export function rejectSecrets(value) {
  const text = JSON.stringify(value);
  if (/(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|Bearer\s+\S+|ANTHROPIC_AUTH_TOKEN\s*[=:]|api[_-]?key\s*[=:])/i.test(text)) throw Error('Secret-like input rejected');
}

export class Builder {
  constructor(config) { Object.assign(this, config); this.busy = new Map(); this.controllers = new Map(); this.dispatching = new Map(); this.cancelRequests = new Set(); this.tail = Promise.resolve(); }
  serial(fn) {
    const result = this.tail.then(fn);
    this.tail = result.catch(() => {}); return result;
  }
  async board(command, args = []) {
    const { stdout } = await exec('python3', [join(this.root, 'scripts/task-board.py'), '--db', this.boardDb, command, ...args],
      { env: safeProcessEnv(), timeout: 15000, maxBuffer: 1024 * 1024 });
    return command === 'list' ? JSON.parse(stdout) : true;
  }
  async git(args, cwd = this.root) {
    return (await exec('git', ['-C', cwd, ...args], { env: safeProcessEnv(), timeout: 15000, maxBuffer: 1024 * 1024 })).stdout.trim();
  }
  async get(id) { const task = await this.tasks.getTask(id); if (!task) throw Error('Task does not exist'); return task; }
  async init() {
    await mkdir(join(this.stateDir, 'prompts'), { recursive: true, mode: 0o700 });
    const spawn = this.acp.spawnSession.bind(this.acp);
    // Gate EVERY canonical spawn entry, including action-layer fallback paths.
    // Canonical AcpService still reserves slots atomically and owns processes.
    this.acp.spawnSession = opts => this.serial(async () => {
      if (opts.agentType !== 'claude' || opts.model !== 'deepseek-flash') throw Error('Only managed direct DeepSeek Claude workers are allowed');
      const task = await this.get(opts.metadata?.taskId);
      const rows = await this.board('list'), row = rows.find(r => r.id === task.metadata.boardId);
      if (!row || row.status !== 'claimed' || row.owner !== task.metadata.owner || row.lease_expired) throw Error('Authoritative task-board claim unavailable');
      if (resolve(opts.workdir) !== task.metadata.workdir || task.metadata.workdir === this.root) throw Error('Worker must use its claimed isolated worktree');
      if ((await this.failures(task.id)).length >= 3) throw Error('Third acceptance failure requires Sol review; dispatch stopped');
      const sessions = await this.acp.listSessions();
      if (sessions.filter(s => !terminal.has(s.status)).length >= 4) throw Error('Global four-worker cap reached');
      if (sessions.some(s => !terminal.has(s.status) && resolve(s.workdir) === task.metadata.workdir)) throw Error('Worktree already has a live session');
      return spawn(opts);
    });
    await this.reconcile();
  }
  async create(input) {
    rejectSecrets(input);
    if (!input.boardId || !/^[A-Za-z0-9._-]{1,100}$/.test(input.boardId) || !Array.isArray(input.scope) || !input.scope.length || !input.goal || !Array.isArray(input.acceptanceCriteria) || !input.acceptanceCriteria.length)
      throw Error('Task needs boardId, scope, goal and explicit acceptanceCriteria');
    if (!Array.isArray(input.acceptanceCommand) || !input.acceptanceCommand.length || input.acceptanceCommand.some(x => typeof x !== 'string')) throw Error('Task needs acceptanceCommand argv');
    const base = await this.git(['rev-parse', `${input.base ?? 'HEAD'}^{commit}`]);
    await this.board('add', [input.boardId, '--base', base, '--scope', ...input.scope, '--depends', ...(input.depends ?? [])]);
    // Two durable boundaries cannot share a transaction. An interrupted create
    // leaves a queued board row: import explicitly, never create a second claim.
    return this.importBoard(input, base);
  }
  async importBoard(input, base) {
    rejectSecrets(input);
    if (!input || !/^[A-Za-z0-9._-]{1,100}$/.test(input.boardId ?? '') || !input.goal || !Array.isArray(input.acceptanceCriteria) || !input.acceptanceCriteria.length ||
      !Array.isArray(input.acceptanceCommand) || !input.acceptanceCommand.length || input.acceptanceCommand.some(x => typeof x !== 'string' || x.includes('\0')))
      throw Error('Import requires a valid boardId, goal, criteria and acceptance argv');
    const rows = await this.board('list'), row = rows.find(r => r.id === input.boardId);
    if (!row) throw Error('Board row missing');
    for (const t of await this.tasks.listTasks({ includeArchived: true })) if ((await this.get(t.id)).metadata.boardId === row.id) throw Error('Board row already linked');
    const t = await this.tasks.createTask({ title: input.title ?? input.boardId, goal: input.goal, kind: 'coding', acceptanceCriteria: input.acceptanceCriteria,
      providerPolicy: { preferredFramework: 'claude', providerSource: 'direct-deepseek', model: 'deepseek-flash' },
      metadata: { boardId: row.id, base: base ?? row.base, scope: row.scope, acceptanceCommand: input.acceptanceCommand, phase: 'queued' } });
    await this.receipts.append(t.id, 'task_linked', { boardId: row.id, base: row.base, scope: row.scope }); return t;
  }
  async claim(id) {
    const task = await this.get(id), owner = `eliza:${task.metadata.boardId}`;
    if (task.metadata.owner) throw Error('Task already has a claim; inspect before recovery');
    await this.board('claim', [task.metadata.boardId, '--owner', owner, '--lease-seconds', '86400']);
    const branch = `builder/${task.metadata.boardId}`, workdir = join(this.stateDir, 'worktrees', task.metadata.boardId);
    await this.receipts.append(id, 'claim_reserved', { owner, branch, workdir });
    // A failed provision retains its claim for inspection, rather than releasing
    // potentially valuable worker changes or assigning another overlapping owner.
    await this.git(['worktree', 'add', '-b', branch, workdir, task.metadata.base]);
    await this.tasks.updateTask(id, { metadata: { ...task.metadata, owner, branch, workdir, phase: 'claimed' } });
    return this.get(id);
  }
  async failures(id) { return (await this.receipts.list(id)).filter(r => r.kind === 'acceptance_failed'); }
  async dispatch(id, revision) {
    if (this.dispatching.has(id) || this.busy.has(id) || this.cancelRequests.has(id)) throw Error('Task mutation already running');
    const job = this.performDispatch(id, revision);
    this.dispatching.set(id, job);
    try { return await job; } finally { this.dispatching.delete(id); }
  }
  async codeFingerprint(task) {
    const commit = await this.git(['rev-parse', 'HEAD'], task.metadata.workdir);
    const diff = await this.git(['diff', 'HEAD'], task.metadata.workdir);
    const binary = await this.git(['diff', '--raw', 'HEAD'], task.metadata.workdir);
    const untracked = (await this.git(['ls-files', '--others', '--exclude-standard', '-z'], task.metadata.workdir)).split('\0').filter(Boolean).sort();
    const hashes = [];
    for (const name of untracked) {
      const path = join(task.metadata.workdir, name), info = await lstat(path);
      if (info.isSymbolicLink()) hashes.push([name, digest(await readlink(path))]);
      else if (info.isFile() && info.size <= 8 * 1024 * 1024) hashes.push([name, createHash('sha256').update(await readFile(path)).digest('hex')]);
      else throw Error('Untracked artifact cannot be fingerprinted safely');
    }
    return { commit, fingerprint: digest([commit, diff, binary, hashes, task.metadata.revisionHash ?? null]) };
  }
  async performDispatch(id, revision) {
    if (!this.fixtureCommand && !this.workersEnabled) throw Error('Workers disabled; fund/configure direct DeepSeek and enable explicitly');
    let task = await this.get(id);
    if ((await this.failures(id)).length >= 3) throw Error('Third acceptance failure requires Sol review; dispatch stopped');
    if (!task.metadata.owner) task = await this.claim(id);
    if (revision) {
      rejectSecrets(revision);
      await this.tasks.createPlanRevision(id, { plan: { instruction: revision }, editSummary: 'Operator revised retry plan', createdBy: 'operator', makeCurrent: true });
      task = await this.get(id);
      await this.tasks.updateTask(id, { metadata: { ...task.metadata, revisionHash: digest(revision) } });
      task = await this.get(id);
    }
    const { commit, fingerprint } = await this.codeFingerprint(task);
    const records = await this.receipts.list(id), failed = records.filter(r => r.kind === 'acceptance_failed').at(-1);
    if (failed?.data.fingerprint === fingerprint) throw Error('Unchanged failed attempt; revise plan or code before retry');
    if (records.some(r => r.kind === 'dispatch_started' && !records.some(x => ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(x.kind) && x.data.attemptId === r.data.attemptId)))
      throw Error('Current attempt needs acceptance or cancellation before dispatch');
    const attemptId = randomUUID();
    await this.receipts.append(id, 'dispatch_started', { attemptId, fingerprint, commit, workdir: task.metadata.workdir });
    try {
      const detail = await this.tasks.spawnAgentForTask(id, { framework: 'claude', model: 'deepseek-flash', providerSource: 'direct-deepseek', workdir: task.metadata.workdir,
        task: `${revision ?? task.goal}\nOwned paths: ${task.metadata.scope.join(', ')}. No nested model processes. Preserve native /doctor. Run acceptance; report evidence, do not merge.`, approvalPreset: 'autonomous' });
      const sessionId = detail.latestSessionId;
      await this.receipts.append(id, 'worker_spawned', { attemptId, sessionId });
      return { attemptId, sessionId, task: detail };
    } catch (error) {
      await this.receipts.append(id, 'dispatch_unavailable', { attemptId, category: 'infrastructure_or_auth' });
      throw error;
    }
  }
  async check(id) {
    if (this.busy.has(id) || this.dispatching.has(id) || this.cancelRequests.has(id)) throw Error('Acceptance or task mutation already running');
    const controller = new AbortController(); this.controllers.set(id, controller);
    const job = this.performCheck(id, controller.signal); this.busy.set(id, job);
    try { return await job; } finally { this.busy.delete(id); this.controllers.delete(id); }
  }
  async performCheck(id, signal) {
    const task = await this.get(id), records = await this.receipts.list(id);
    const attempt = records.filter(r => r.kind === 'dispatch_started').at(-1);
    if (!attempt || records.some(r => ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(r.kind) && r.data.attemptId === attempt.data.attemptId)) throw Error('No unchecked actual worker attempt');
    const worker = records.find(r => r.kind === 'worker_spawned' && r.data.attemptId === attempt.data.attemptId);
    const session = worker && await this.acp.getSession(worker.data.sessionId);
    const completed = task.sessions.find(s => s.sessionId === worker?.data.sessionId)?.status === 'completed';
    if (!session || session.status !== 'ready' || !completed) throw Error('Worker has not completed an actual turn; API/transport/cancellation is not acceptance failure');
    const testedTree = await this.codeFingerprint(task);
    await this.receipts.append(id, 'acceptance_started', { attemptId: attempt.data.attemptId, commandHash: digest(task.metadata.acceptanceCommand) });
    let result;
    try { result = await runProcess(task.metadata.acceptanceCommand, { cwd: task.metadata.workdir, env: safeProcessEnv(), timeout: this.checkTimeout ?? 60000, signal }); }
    catch (e) {
      await this.receipts.append(id, 'attempt_interrupted', { attemptId: attempt.data.attemptId, category: 'infrastructure', errorCode: e.code ?? 'unknown' }); throw Error('Acceptance infrastructure unavailable');
    }
    const afterTree = await this.codeFingerprint(await this.get(id));
    const evidence = { ...result, attemptId: attempt.data.attemptId, fingerprint: testedTree.fingerprint, commit: testedTree.commit,
      treeStable: afterTree.fingerprint === testedTree.fingerprint, dispatchFingerprint: attempt.data.fingerprint };
    // Timeouts, cancellation, signals and spawn/API failures never increment code failures.
    const proposedKind = !evidence.treeStable || signal.aborted || this.cancelRequests.has(id) || result.cancelled || result.timedOut || result.signal || result.code === null ? 'attempt_interrupted' : result.code === 0 ? 'acceptance_passed' : 'acceptance_failed';
    const receipt = await this.receipts.append(id, proposedKind, evidence);
    const kind = receipt.kind;
    await this.tasks.addMessage(id, { senderKind: 'system', direction: 'system', content: `${kind}: ${receipt.id}`, metadata: { receiptId: receipt.id, hash: receipt.hash } });
    await this.acp.closeSession(worker.data.sessionId);
    if (kind === 'acceptance_failed' && (await this.failures(id)).length === 3) {
      await this.tasks.pauseTask(id);
      await this.receipts.append(id, 'escalation_required', { failures: 3, evidenceReceipt: receipt.id });
      // Persist before any bridge call. On unknown/reserve the pending review
      // survives restarts without spending or redispatching a coding worker.
      await this.escalate(id);
    }
    return { kind, receipt, independentReviewRequired: true };
  }
  async cancel(id) {
    if (this.cancelRequests.has(id)) throw Error('Cancellation already running');
    this.cancelRequests.add(id);
    this.controllers.get(id)?.abort();
    try {
    await this.dispatching.get(id)?.catch(() => {});
    const task = await this.get(id);
    for (const s of task.sessions) { const live = await this.acp.getSession(s.sessionId); if (live && !['stopped', 'errored', 'cancelled'].includes(live.status)) await this.tasks.stopTaskAgent(id, s.sessionId); }
    const records = await this.receipts.list(id), attempt = records.filter(r => r.kind === 'dispatch_started').at(-1);
    if (attempt && !records.some(r => ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(r.kind) && r.data.attemptId === attempt.data.attemptId))
      await this.receipts.append(id, 'attempt_interrupted', { attemptId: attempt.data.attemptId, category: 'cancelled' });
    return await this.tasks.pauseTask(id);
    } finally { this.cancelRequests.delete(id); }
  }
  async bridge(command, args = []) {
    if (this.bridgeCall) return this.bridgeCall(command, args); // Controlled wire fixture only, never HTTP-configurable.
    // The bridge alone receives the selected CODEX_HOME through bridgeEnv();
    // DeepSeek workers and acceptance commands stay on safeProcessEnv().
    const { stdout } = await exec('python3', [join(this.root, 'scripts/codex-bridge.py'), command, ...args], { env: bridgeEnv(), timeout: command === 'help' ? 300000 : 15000, maxBuffer: 256 * 1024 });
    const wire = JSON.parse(stdout); rejectSecrets(wire); return wire;
  }
  async quota() {
    try { return quotaDecision(await this.bridge('limits')); } catch { return quotaDecision(null); }
  }
  async checkpoint(quota, taskId) {
    const state = { status: 'waiting_for_quota_or_review', git: { branch: await this.git(['branch', '--show-current']), commit: await this.git(['rev-parse', 'HEAD']) },
      taskId, tasks: await this.tasks.listTasks({ includeArchived: false }), quota, observedAt: new Date().toISOString(),
      steps: ['Check actual native Codex limits', 'Human account switch if needed; never logout/refresh or consume reset credits', 'eliza-builder.py help TASK --reason retry'], usage: { input: null, cachedInput: null, output: null, cost: null } };
    const path = join(this.stateDir, 'HANDOFF.json'); await writeFile(`${path}.tmp`, JSON.stringify(state, null, 2) + '\n', { mode: 0o600 }); await rename(`${path}.tmp`, path);
    return path;
  }
  async help(id, reason = 'architecture', promptFile) {
    if (!['architecture', 'final-review', 'retry'].includes(reason)) throw Error('Unsupported Sol reason');
    const task = await this.get(id), failures = await this.failures(id);
    if (reason === 'retry' && failures.length < 3) throw Error('Retry review requires three durable acceptance failures');
    const quota = await this.quota();
    if (!quota.allowed) { const handoff = await this.checkpoint(quota, id); return { completedTurn: false, blocked: quota.reason, handoff }; }
    let path = join(this.stateDir, 'prompts', `${randomUUID()}.txt`);
    let prompt = promptFile ? await readFile(await this.privatePath(promptFile), 'utf8') : JSON.stringify({ goal: task.goal, acceptance: task.acceptanceCriteria,
      failures: failures.slice(-3).map(r => ({ receipt: r.id, commit: r.data.commit, exit: r.data.code, outputSha256: r.data.outputSha256 })), reason });
    rejectSecrets(prompt); if (prompt.length > 12000) throw Error('Review brief exceeds bounded evidence limit');
    await writeFile(path, `Read-only gpt-6.1-sol review. Do not edit, commit, dispatch workers or call DeepSeek.\n${prompt}`, { mode: 0o600 });
    // Receipt prevents unchanged automatic retries if quota/auth/turn fails.
    await this.receipts.append(id, 'sol_call_started', { reason, failures: failures.length, promptHash: digest(prompt) });
    try {
      const args = ['--prompt-file', path, '--task-id', task.metadata.boardId, '--reason', reason, '--timeout', '240'];
      if (reason === 'retry') {
        const ledger = join(this.stateDir, 'prompts', `${randomUUID()}.failures.json`);
        await writeFile(ledger, JSON.stringify({ tasks: { [task.metadata.boardId]: { failures: failures.length,
          evidence: failures.map(r => ({ receipt: r.id, hash: r.hash, commit: r.data.commit, code: r.data.code })) } } }) + '\n', { mode: 0o600 });
        args.push('--failures-file', ledger);
      }
      const wire = await this.bridge('help', args);
      if (wire.schema !== 'phpretro.codex.bridge.v1' || wire.ok !== true || wire.status !== 'completed' ||
        wire.completed_turn !== true || wire.read_only !== true || wire.model !== 'gpt-6.1-sol' || wire.model_access_verified !== true ||
        !wire.turn_id || !wire.thread_id || !wire.assistant_text) throw Error('Bridge did not establish a completed read-only actual turn');
      await this.receipts.append(id, 'sol_turn_completed', { reason, turnId: wire.turn_id, threadId: wire.thread_id,
        answerSha256: digest(wire.assistant_text), usage: wire.usage ?? null }); return wire;
    } catch {
      await this.receipts.append(id, 'sol_unavailable', { reason, category: 'bridge_auth_or_infrastructure' });
      return { completedTurn: false, blocked: 'bridge_auth_or_infrastructure' };
    }
  }
  async escalate(id) {
    const rows = await this.receipts.list(id);
    if (rows.some(r => r.kind === 'sol_call_started' && r.data.reason === 'retry')) return { blocked: 'review_already_attempted' };
    return this.help(id, 'retry');
  }
  async privatePath(path) {
    const full = resolve(this.root, path);
    if (!full.startsWith(resolve(this.root, '.builder') + '/')) throw Error('Prompt file must be private .builder state');
    const actual = await realpath(full), info = await stat(actual);
    if (!actual.startsWith(resolve(this.root, '.builder') + '/') || !info.isFile() || (info.mode & 0o077) || info.uid !== process.getuid())
      throw Error('Prompt file must be an owned private regular file');
    return actual;
  }
  async reconcile() {
    for (const t of await this.tasks.listTasks({ includeArchived: false })) {
      const task = await this.get(t.id), rows = await this.receipts.list(t.id);
      for (const r of rows.filter(x => x.kind === 'dispatch_started' && !this.dispatching.has(t.id))) {
        if (!rows.some(x => x.kind === 'worker_spawned' && x.data.attemptId === r.data.attemptId) &&
          !rows.some(x => ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(x.kind) && x.data.attemptId === r.data.attemptId)) {
          if (!rows.some(x => x.kind === 'dispatch_recovery_required' && x.data.attemptId === r.data.attemptId))
            await this.receipts.append(t.id, 'dispatch_recovery_required', { attemptId: r.data.attemptId, action: 'Inspect orphan processes and worktree, then explicitly cancel; never implicit redispatch' });
          await this.tasks.pauseTask(t.id);
        }
      }
      for (const r of rows.filter(x => x.kind === 'worker_spawned')) {
        const live = await this.acp.getSession(r.data.sessionId);
        if (!live || ['errored', 'cancelled', 'stopped'].includes(live.status)) {
          if (!rows.some(x => ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted'].includes(x.kind) && x.data.attemptId === r.data.attemptId))
            await this.receipts.append(t.id, 'attempt_interrupted', { attemptId: r.data.attemptId, category: 'process_recovered', sessionId: r.data.sessionId });
        }
      }
      if (task.metadata.owner) {
        try { await this.board('renew', [task.metadata.boardId, '--owner', task.metadata.owner, '--lease-seconds', '86400']); }
        catch { await this.tasks.pauseTask(t.id); }
      }
      // Scheduler reconciles metadata only. It never resurrects dead jobs or
      // makes a fresh Sol call implicitly after an auth/quota failure.
    }
  }
  async status() {
    const live = await this.acp.listSessions();
    return { runtime: '@elizaos/core@2.0.3-beta.7', orchestrator: '@elizaos/plugin-agent-orchestrator@2.0.3-beta.7',
      plugins: this.runtime.plugins.map(p => p.name), services: [...this.runtime.getAllServices().keys()],
      taskStore: 'canonical-runtime-db/PGlite', status: await this.tasks.getStatus(),
      activeWorkers: live.filter(s => !terminal.has(s.status)).map(s => ({ id: s.id, pid: s.pid ?? null, status: s.status, workdir: s.workdir })),
      sessions: live.map(s => ({ id: s.id, pid: s.pid ?? null, status: s.status })), acceptanceJobs: [...this.busy.keys()],
      quota: await this.quota(), workersEnabled: Boolean(this.workersEnabled), inferenceCalls: 'not inferred from process status' };
  }
  async report(id) { const task = await this.get(id); return { task, receipts: await this.receipts.list(id), chainHead: await this.receipts.verify(), board: (await this.board('list')).filter(r => r.id === task.metadata.boardId), usage: { input: null, cachedInput: null, output: null, cost: null } }; }
  async close() { for (const c of this.controllers.values()) c.abort(); await Promise.allSettled([...this.busy.values(), ...this.dispatching.values()]); await this.tail; await this.runtime.stop(); await this.adapter.close(); if (this.originalCwd) process.chdir(this.originalCwd); }
}
