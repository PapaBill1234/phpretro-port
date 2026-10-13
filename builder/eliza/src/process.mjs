// SPDX-License-Identifier: GPL-3.0-only
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstat, mkdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';

// The selected CODEX_HOME is the only bridge-specific value forwarded to the
// controlled Codex bridge. It (and any provider credential) must never reach
// DeepSeek workers or acceptance commands, which use safeProcessEnv().
const BRIDGE_ENV_ALLOWLIST = ['CODEX_HOME'];
const LOCK_STARTUP_TIMEOUT = 10000;
const LOCK_SHUTDOWN_TIMEOUT = 10000;

function waitExit(child, ms) {
  return new Promise(resolveExit => {
    if (child.exitCode !== null || child.signalCode !== null) return resolveExit(true);
    const timer = setTimeout(() => { child.removeListener('exit', onExit); resolveExit(false); }, ms);
    const onExit = () => { clearTimeout(timer); resolveExit(true); };
    child.once('exit', onExit);
  });
}

// Fast pre-check; the helper re-validates the same inode under O_NOFOLLOW so a
// swap between check and open cannot yield an untrusted lock. Fail closed on
// any malformed or unsafe path rather than taking a lock on foreign content.
async function assertSafeLockPath(path) {
  let info;
  try { info = await lstat(path); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
  if (info.isSymbolicLink()) throw Error('Runtime lock path must not be a symlink');
  if (!info.isFile()) throw Error('Runtime lock path must be a regular file');
  if (info.uid !== process.getuid()) throw Error('Runtime lock file must be owned by the runtime user');
  if (info.mode & 0o077) throw Error('Runtime lock file permissions must be 0600');
}

// Singleton runtime gate. A stable file inode is locked with an OS advisory
// lock (flock) held by a tiny owned Python helper for the whole runtime
// lifetime, so two recovery starters can never both succeed. The lock is never
// taken or released by PID inspection, and the inode is never unlinked, so a
// legacy PID-content file is only ever reused, never trusted or removed.
export async function singleton(file) {
  if (typeof file !== 'string' || !file || file.includes('\0')) throw Error('Runtime lock path is malformed');
  const path = resolve(file);
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  await assertSafeLockPath(path);
  const helper = join(import.meta.dirname, 'runtime-lock.py');
  const child = spawn('python3', [helper, path], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = '', err = '';
  child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
  child.stdout.on('data', chunk => { out += chunk; });
  child.stderr.on('data', chunk => { err += chunk; });
  child.stdin.on('error', () => {}); // Helper death closes our pipe; not fatal here.
  let released = false;
  let verdict;
  try {
    verdict = await new Promise((resolveVerdict, rejectVerdict) => {
      let settled = false;
      const finish = value => { if (settled) return; settled = true; clearTimeout(timer); resolveVerdict(value); };
      const fail = error => { if (settled) return; settled = true; clearTimeout(timer); rejectVerdict(error); };
      const timer = setTimeout(() => fail(Error('Runtime lock helper startup timed out')), LOCK_STARTUP_TIMEOUT);
      child.on('error', error => fail(Error(`Runtime lock helper unavailable: ${error.code ?? error.message}`)));
      child.on('exit', code => { if (!settled) fail(Error(`Runtime lock helper exited during startup (${code ?? 'signal'})${err.trim() ? `: ${err.trim()}` : ''}`)); });
      child.stdout.on('data', () => {
        const line = out.split('\n')[0].trim();
        if (line === 'LOCKED') finish('locked'); else if (line === 'BUSY') finish('busy');
        else if (line) fail(Error(`Runtime lock helper rejected the request: ${line}`));
      });
    });
  } catch (error) {
    try { child.kill('SIGKILL'); } catch {}
    await waitExit(child, 2000); throw error;
  }
  if (verdict === 'busy') { await waitExit(child, LOCK_SHUTDOWN_TIMEOUT); throw Error('Eliza runtime already running; use its loopback API'); }
  // Unexpected helper death after acquisition means the OS lock is gone; the
  // runtime would be unprotected, so it must fail closed instead of continuing.
  child.on('exit', () => {
    if (released) return;
    process.stderr.write('Eliza runtime lost its OS lock helper; terminating to fail closed.\n');
    process.exit(70);
  });
  if (child.exitCode !== null || child.signalCode !== null) throw Error('Runtime lock helper died after acquiring the lock');
  const release = async () => {
    if (released) return;
    released = true;
    const exit = waitExit(child, LOCK_SHUTDOWN_TIMEOUT);
    try { child.stdin.end(); } catch {}
    if (!(await exit)) { try { child.kill('SIGKILL'); } catch {} await waitExit(child, 2000); }
  };
  release.helperPid = child.pid ?? null;
  return release;
}

export function runProcess(argv, { cwd, env, timeout = 60000, signal } = {}) {
  if (!Array.isArray(argv) || !argv.length || argv.some(x => typeof x !== 'string' || x.includes('\0'))) throw Error('Expected command argv');
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0], argv.slice(1), { cwd, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let timedOut = false, cancelled = false, timer, killTimer;
    const hash = createHash('sha256'); let bytes = 0;
    for (const stream of [child.stdout, child.stderr]) stream.on('data', b => { bytes += b.length; hash.update(b); });
    function stop() {
      try { process.kill(-child.pid, 'SIGTERM'); } catch (e) { if (e.code !== 'ESRCH') throw e; }
      killTimer = setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL'); } catch (e) { if (e.code !== 'ESRCH') throw e; } }, 500);
    }
    const abort = () => { cancelled = true; stop(); };
    signal?.addEventListener('abort', abort, { once: true });
    child.on('error', error => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(error); });
    child.on('close', (code, exitSignal) => {
      clearTimeout(timer); clearTimeout(killTimer); signal?.removeEventListener('abort', abort);
      // A leader exiting can leave grandchildren holding the pipes. The timeout
      // kills the entire group; even normal completion cleans up remaining children.
      try { process.kill(-child.pid, 'SIGKILL'); } catch (e) { if (e.code !== 'ESRCH') return reject(e); }
      resolve({ pid: child.pid, code, signal: exitSignal, timedOut, cancelled, outputBytes: bytes, outputSha256: hash.digest('hex') });
    });
    timer = setTimeout(() => { timedOut = true; stop(); }, timeout);
    if (signal?.aborted) abort();
  });
}

export function safeProcessEnv() {
  const env = {};
  for (const key of ['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SHELL']) if (process.env[key]) env[key] = process.env[key];
  return env;
}

// Bridge-only environment: the base safe set plus the selected CODEX_HOME so
// the controlled Codex bridge can find its ChatGPT subscription login. No other
// provider credential is forwarded here or anywhere downstream.
export function bridgeEnv() {
  const env = safeProcessEnv();
  for (const key of BRIDGE_ENV_ALLOWLIST) if (process.env[key]) env[key] = process.env[key];
  return env;
}
