// SPDX-License-Identifier: GPL-3.0-only
import { AgentRuntime, stringToUuid } from '@elizaos/core';
import sqlPlugin, { createDatabaseAdapter } from '@elizaos/plugin-sql';
import { createAgentOrchestratorPlugin } from '@elizaos/plugin-agent-orchestrator';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { legacySqlBoundary, Receipts } from './storage.mjs';
import { Builder } from './builder.mjs';

export async function startRuntime(config) {
  const originalCwd = process.cwd();
  // beta.7's public workdir validation uses cwd, rather than its newer roots
  // setting. The standalone host runs at the project root explicitly.
  process.chdir(config.root);
  await mkdir(config.stateDir, { recursive: true, mode: 0o700 });
  // These upstream features perform automatic model calls or respawns. The
  // project boundary owns authorization, acceptance, reserve and escalation.
  const settings = {
    PGLITE_DATA_DIR: join(config.stateDir, 'db'), ELIZA_ACP_STATE_DIR: join(config.stateDir, 'acp'),
    ELIZA_ACP_MAX_SESSIONS: '4', ELIZA_ACP_TRANSPORT: 'native', ELIZA_ACP_DEFAULT_AGENT: 'claude',
    ELIZA_ACP_MAX_NESTING_DEPTH: '0', ELIZA_ORCHESTRATOR_AUTO_GOAL_VERIFY: '0',
    ELIZA_REQUIRE_GOAL_CONTRACT: '0', ELIZA_ORCHESTRATOR_INDEPENDENT_VERIFY: '0',
    ELIZA_ORCHESTRATOR_WATCHDOG: '0', ELIZA_ORCHESTRATOR_SUPERVISOR: '0',
    ACPX_SUB_AGENT_ROUTER_DISABLED: '1', TASK_AGENT_WORKDIR_ROOTS: config.root,
    ELIZA_CLAUDE_ACP_COMMAND: config.fixtureCommand ?? `node ${join(import.meta.dirname, 'managed-claude.mjs')}`,
  };
  Object.assign(process.env, settings); // Upstream goal-policy helpers read env.
  const runtime = new AgentRuntime({
    character: { id: stringToUuid('phpretro-builder-v1'), name: 'PHPRetro Builder', bio: ['Local companion builder'], settings },
    settings, logLevel: 'error', enableAutonomy: false, enableDocuments: false,
    enableRelationships: false, enableTrajectories: false,
  });
  const adapter = createDatabaseAdapter({ dataDir: settings.PGLITE_DATA_DIR }, runtime.agentId);
  runtime.registerDatabaseAdapter(adapter);
  // Published beta.7 reads the old alias and uses SQLite-style upserts. This
  // scoped facade adapts its SQL dialect; no upstream lifecycle is reimplemented.
  runtime.databaseAdapter = legacySqlBoundary(adapter);
  const receipts = new Receipts(adapter);
  let builder;
  const projectPlugin = {
    name: 'phpretro-builder-boundary', description: 'Task-board claims and offline project controls',
    init: async () => {
      runtime.registerTaskWorker({ name: 'PHPRETRO_RECONCILE', execute: async () => { if (builder) await builder.reconcile(); } });
    },
  };
  try {
    await runtime.registerPlugin(sqlPlugin);
    await runtime.registerPlugin(createAgentOrchestratorPlugin());
    await runtime.registerPlugin(projectPlugin);
    await runtime.initialize();
    const services = await Promise.all(['ACP_SUBPROCESS_SERVICE', 'ORCHESTRATOR_TASK_SERVICE', 'ACPX_SUB_AGENT_ROUTER', 'CODING_WORKSPACE_SERVICE', 'task'].map(s => runtime.getServiceLoadPromise(s)));
    await receipts.init(); await receipts.verify();
    builder = new Builder({ ...config, originalCwd, runtime, adapter, receipts, acp: services[0], tasks: services[1] });
    await builder.init();
    if (!(await runtime.getTasksByName('PHPRETRO_RECONCILE')).length) {
      await runtime.createTask({ name: 'PHPRETRO_RECONCILE', description: 'Reconcile actual worker state; never dispatch inference', agentId: runtime.agentId,
        tags: ['queue', 'repeat'], metadata: { updateInterval: 30000, updatedAt: Date.now(), maxFailures: -1 } });
    }
    return builder;
  } catch (error) { await runtime.stop(); await adapter.close(); process.chdir(originalCwd); throw error; }
}
