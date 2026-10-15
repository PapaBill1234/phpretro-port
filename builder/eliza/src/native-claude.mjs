#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-only
// Use installed native Claude Code, preserving native commands including Doctor.
// Limit SDK worker tools and settings; never silently pick a coordinator model.
const executable = process.env.ELIZA_NATIVE_CLAUDE;
const settings = process.env.ELIZA_DEEPSEEK_SETTINGS;
if (!executable || !settings || process.env.ANTHROPIC_BASE_URL !== 'https://api.deepseek.com/anthropic' || process.env.ANTHROPIC_MODEL !== 'deepseek-flash') process.exit(78);
const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--model') { args.splice(i, 2); i--; }
  else if (args[i].startsWith('--model=')) { args.splice(i, 1); i--; }
}
process.execve(executable, [executable, ...args, '--model', 'deepseek-flash', '--settings', settings, '--setting-sources', 'project',
  '--disallowedTools', 'Agent,Task', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'], process.env);
