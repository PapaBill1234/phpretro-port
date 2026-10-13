// SPDX-License-Identifier: GPL-3.0-only
import { readFile, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
const exec = promisify(execFile);
const dir = join(import.meta.dirname, '..');
const lock = JSON.parse(await readFile(join(dir, 'package-lock.json')));
const names = ['@elizaos/core', '@elizaos/plugin-sql', '@elizaos/plugin-agent-orchestrator', '@agentclientprotocol/claude-agent-acp', '@anthropic-ai/claude-agent-sdk', '@electric-sql/pglite'];
const packages = [];
for (const name of names) {
  const item = lock.packages[`node_modules/${name}`];
  const manifest = JSON.parse(await readFile(join(dir, 'node_modules', name, 'package.json')));
  const { stdout } = await exec('npm', ['view', `${name}@${item.version}`, 'version', 'dist.integrity', 'dist.tarball', 'gitHead', 'license', 'repository', '--json', '--cache', '/tmp/phpretro-eliza-npm'], { timeout: 15000 });
  const registry = JSON.parse(stdout);
  if (registry['dist.integrity'] !== item.integrity || registry.version !== item.version) throw Error(`Registry/lock mismatch: ${name}`);
  packages.push({ name, version: item.version, integrity: item.integrity, resolved: item.resolved, gitHead: registry.gitHead ?? null, license: registry.license ?? manifest.license ?? null, repository: registry.repository ?? null });
}
const result = { sourcePin: '28fef47c9d9d3acc7792be0fdaa3070020293cc5', sourcePinRole: 'full upstream app/CLI/UI submodule pin; separate source-checkout installation and first-run verified; companion published packages differ',
  authoredLicense: 'GPL-3.0-only', packages, runtime: { testedNode: process.version, packageRequirement: '>=24', upstreamRepositoryPins: { node: '24.15', bun: '1.4.2' } },
  discrepancies: ['beta.7 canonical task/session stores require flat SQLite-style SQL; project adapter translates upserts, placeholders and SQLite 64-bit INTEGER to real plugin-sql/PGlite SQL', 'core owns runtime.adapter; beta.7 stores read legacy runtime.databaseAdapter alias', 'core services load lazily; await getServiceLoadPromise', 'registry app 1.7.2 was not used; pinned source CLI/API/UI were installed and first-run tested in a separate checkout; root submodule installation and reviewed launcher integration remain pending', 'plugin-sqlite is unpublished and unused'] };
await writeFile(join(dir, 'provenance.json'), JSON.stringify(result, null, 2) + '\n');
process.stdout.write(`Recorded ${packages.length} exact registry/lock entries; missing gitHead is null.\n`);
