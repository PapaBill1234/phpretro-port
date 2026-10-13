// SPDX-License-Identifier: GPL-3.0-only
import { createHash, randomUUID } from 'node:crypto';

// beta.7's canonical stores speak SQLite SQL. Translate only their two tables;
// the actual core adapter and all execution stay in plugin-sql/PGlite.
export function legacySqlBoundary(adapter) {
  const query = async (text, values = []) => {
    if (!/\b(acp_sessions|orchestrator_tasks)\b/.test(text)) throw Error('Unexpected upstream SQL table');
    // SQLite INTEGER is 64-bit; PostgreSQL INTEGER is 32-bit. Epoch millis
    // require BIGINT in the canonical table's activity sort column.
    text = text.replace(/last_activity_at INTEGER/g, 'last_activity_at BIGINT');
    const replace = text.match(/^INSERT OR REPLACE INTO (\w+)\s*\(([^)]+)\)/i);
    if (replace) {
      const columns = replace[2].split(',').map(x => x.trim());
      text = text.replace(/^INSERT OR REPLACE/i, 'INSERT');
      text += ` ON CONFLICT (id) DO UPDATE SET ${columns.filter(x => x !== 'id').map(x => `${x}=EXCLUDED.${x}`).join(',')}`;
    }
    let index = 0;
    text = text.replace(/\?/g, () => `$${++index}`);
    if (index !== values.length) throw Error('Upstream SQL parameter mismatch');
    return adapter.getRawConnection().query(text, values);
  };
  return { execute: query, query, all: async (q, v) => (await query(q, v)).rows };
}

export class Receipts {
  constructor(adapter) { this.adapter = adapter; this.tail = Promise.resolve(); }
  query(sql, values = []) { return this.adapter.getRawConnection().query(sql, values); }
  async init() {
    await this.query(`CREATE TABLE IF NOT EXISTS phpretro_receipts (
      seq BIGSERIAL PRIMARY KEY, id TEXT UNIQUE NOT NULL, task_id TEXT NOT NULL,
      kind TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL,
      previous_hash TEXT NOT NULL, hash TEXT UNIQUE NOT NULL)`);
  }
  async list(taskId) {
    const { rows } = await this.query(`SELECT * FROM phpretro_receipts ${taskId ? 'WHERE task_id=$1' : ''} ORDER BY seq`, taskId ? [taskId] : []);
    return rows.map(r => ({ ...r, data: JSON.parse(r.data) }));
  }
  append(taskId, kind, data) {
    const work = this.tail.then(async () => {
      // One terminal result per attempt. Cancellation/recovery/acceptance can
      // race; the receipt queue makes conclusion atomic within the singleton.
      if (data.attemptId && ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(kind)) {
        const existing = (await this.list(taskId)).find(r => r.data.attemptId === data.attemptId &&
          ['acceptance_failed', 'acceptance_passed', 'attempt_interrupted', 'dispatch_unavailable'].includes(r.kind));
        if (existing) return existing;
      }
      const previous = (await this.query('SELECT hash FROM phpretro_receipts ORDER BY seq DESC LIMIT 1')).rows[0]?.hash ?? '';
      const id = randomUUID(), at = new Date().toISOString(), encoded = JSON.stringify(data);
      const hash = createHash('sha256').update(JSON.stringify([id, taskId, kind, at, encoded, previous])).digest('hex');
      await this.query('INSERT INTO phpretro_receipts (id,task_id,kind,at,data,previous_hash,hash) VALUES ($1,$2,$3,$4,$5,$6,$7)', [id, taskId, kind, at, encoded, previous, hash]);
      return { id, task_id: taskId, kind, at, data, previous_hash: previous, hash };
    });
    this.tail = work.catch(() => {});
    return work;
  }
  async verify() {
    let previous = '';
    for (const r of await this.list()) {
      const hash = createHash('sha256').update(JSON.stringify([r.id, r.task_id, r.kind, r.at, JSON.stringify(r.data), previous])).digest('hex');
      if (r.previous_hash !== previous || r.hash !== hash) throw Error('Receipt chain verification failed');
      previous = hash;
    }
    return previous;
  }
}
