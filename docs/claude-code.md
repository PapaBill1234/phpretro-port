# Project-local Claude Code builder

The verified A6API route accepts Anthropic Messages directly. The project uses the native Claude Code executable as its harness; A6API supplies the requested non-Claude models. Anthropic does not officially support that model substitution. Compatibility is backed by this project's bounded protocol tests. [Official gateway guidance](https://code.claude.com/docs/en/llm-gateway)

## Start a session

From the repository, inspect the redacted local setup without inference:

```bash
python3 scripts/claude-builder.py --role lead --check
```

Start one session per terminal:

```bash
python3 scripts/claude-builder.py --role lead
python3 scripts/claude-builder.py --role worker --worktree-name auth-contract
python3 scripts/claude-builder.py --role reviewer
```

Forward ordinary native CLI arguments after `--`, for example a print-mode prompt, resume UUID or session name. At the operator's explicit request, sessions use `bypassPermissions`, including print mode. This changes tool approval prompts; operating-system permissions still apply.

```bash
python3 scripts/claude-builder.py --role lead -- --resume SESSION_UUID
python3 scripts/claude-builder.py --role lead -- -p 'Summarize the next open gap without editing.'
```

Workers require an initial Git commit and get their own native worktree. `worktree.baseRef=head` starts a new worktree at the current checkout's commit. Uncommitted files are not copied. A reused name reopens its existing checkout; confirm its branch and actual starting SHA. Give each editing worker a distinct task claim and worktree. [Worktree semantics](https://code.claude.com/docs/en/worktrees)

The current `~/.local/bin/claude` wrapper and desktop entrypoint use this builder. The older wrapper is preserved in the private launcher backups. The builder selects the raw pinned executable, currently `~/.local/share/claude/versions/2.1.287`. `PHPRETRO_CLAUDE_BIN` can select a reviewed replacement raw executable. A script wrapper is rejected. Project sessions use ignored `.builder/claude` as `CLAUDE_CONFIG_DIR`, load project settings, and leave previous machine workflows and STOP files untouched.

## Desktop launcher and permissions

The Ubuntu **Claude Code Sol Chat** and **Claude Code Colour Session** entries run `/home/ubuntu/bin/claude-phpretro.sh`. The entrypoint starts or reuses the owned `phpretro-builder` tmux session, then attaches to it. Use Ctrl-b then n to change windows. A new window requires a clean committed checkpoint; existing open sessions can be attached while a task is in progress.

The Windows **Claude Code - Ubuntu** shortcut opens Windows Terminal and connects with `ssh -t phpretro /home/ubuntu/bin/claude-phpretro.sh`. Both routes use the current auth project and shared `.builder/claude` history. The prior installed runtime and its STOP files remain preserved.

`permissions.defaultMode` is `bypassPermissions` in the operator's user settings and the private shared configuration. The launcher also passes the explicit CLI mode because recent Claude versions ignore that default when it comes only from project settings, and CLI flags override defaults. Reviewer tool scope remains read-only. Provider credentials stay outside checked-in configuration.

## Models and agent scopes

| Role | Claude alias | A6API ID | Effort | Scope |
|---|---|---|---|---|
| Lead | sonnet | gpt-6.1-sol | high | Task coordination, integration, bounded delegation |
| Worker / Explore / researcher | haiku | gpt-6-luna | medium | One implementation scope or read-only discovery |
| Reviewer | opus | gpt-6.1-sol | high | Read-only contract/security review |

`config/models.json` is the role registry. The launcher checks it against `.claude/settings.json`; it selects the exact main model and the configured agent. Each agent has an explicit model role. No FORCE variable is set: `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` would replace the Sol reviewer with the worker model. The explicit project `Explore` definition directs built-in discovery to Luna. [Subagent model precedence](https://code.claude.com/docs/en/sub-agents)

The launcher reserves model, agent, effort, permission, tool, settings, MCP and worktree flags. It rejects overrides, including bypass-permission flags. Change the reviewed project configuration for a role change. At the operator's explicit request it passes `--permission-mode bypassPermissions`; it does not claim that an instruction file is a security boundary. Reviewer tools are Read/Grep/Glob plus the judge-only MCP tool. Editing workers have edit/test tools only in their assigned task scope; enforce write scope in the task/integration process.

No hooks are configured by this project and no Jev gate is installed. Bundled skills remain enabled; the launcher no longer forces the blanket nonessential-traffic switch or disables browser integration. Cloud/account features still require their own supported authentication. Same-machine peer messaging is available in this CLI version through ListAgents/SendMessage and a shared local inbox mechanism. Messages do not grant consent or run slash commands. Only coordinate this project's named sessions and assigned tasks. [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)

## Private credentials

The launcher reads `A6API_API_KEY` from its inherited environment, otherwise parses an owned mode-0600 dotenv file as data. It never evaluates shell commands or sources dotenv content. File selection order: `PHPRETRO_A6API_ENV`, repository `secrets/a6api.env`, then the existing Ubuntu `~/phpretro-dashboard/secrets/a6api.env`.

It supplies `ANTHROPIC_BASE_URL=https://api.a6api.com` and a bearer `ANTHROPIC_AUTH_TOKEN` to the native process. Claude Code appends `/v1/messages`; including `/v1` in this base URL would duplicate the path. No key is put in checked-in settings, process arguments or launcher logs. An alternate auth format/endpoint must be verified before changing the route. [Connection reference](https://code.claude.com/docs/en/llm-gateway-connect)

The direct route places the A6API credential in the CLI's process environment. This launcher does not isolate that credential from same-user processes or shell tools, and a read-deny rule alone cannot provide that isolation. The autonomous production runner should use a private authenticated loopback passthrough that retains the real key server-side, issues scoped session tokens and enforces model/token ceilings. Do not describe the bootstrap direct launcher as a complete credential sandbox.

OpenRouter is a separate credential for Jev. The A6API launcher removes inherited provider-key variables other than its own ephemeral auth variable; it does not read or forward the OpenRouter key. The project judge-only MCP shim privately loads `PHPRETRO_OPENROUTER_ENV` or the existing OpenRouter file. It filters out the previously installed server's gate. Only `mcp__jev__jev_judge` is exposed; no Jev approval gate is enabled. Actual native lead/worker capability checks observed judge-only inventory and successful judge calls. If `.mcp.json` is present, the launcher loads that project configuration exclusively; otherwise it starts with no external MCP servers.

## Doctor and usage

In an interactive session, run `/doctor` for setup health and context-cost suggestions, or `/doctor prompt-audit` for conflicting/outdated instructions. The operator has authorized routine project Doctor fixes without further questions; the lead has default tools and explicit permission bypass. A read-only reviewer cannot apply fixes. `.claude/project-scope.md` is appended at launch to keep this work within the active checkout and its builder worktrees; it excludes failed projects and global-history scans and is an instruction, not an OS sandbox. Standalone read-only installation diagnostics are available with the raw binary's `doctor` subcommand. [Doctor reference](https://code.claude.com/docs/en/skills)

Use `/usage` for session tokens and cache behavior. Many CLI processes do not automatically become one token audit, and short handoffs do not guarantee total savings. Connect metadata-only OpenTelemetry to one local collector and correlate task, role, run and session IDs; retain input/output/cache counters separately. Provider-reported model aliases describe the endpoint response, not verified underlying weights. Cost figures require confirmed A6API billing rates. [Monitoring](https://code.claude.com/docs/en/monitoring-usage)

The launcher starts one process; it does not create an unbounded swarm, enforce a daily cash budget, or enforce the requested 10% account quota reserve. The supervisor/quota monitor must enforce those limits before admitting autonomous tasks. Begin with two or three disjoint workers, terminate finished sessions, and compare matched tasks before claiming savings. [Agent team costs](https://code.claude.com/docs/en/costs)

## Validation before sustained autonomy

See `setup-status.md` for observed checks and unresolved items. Run a bounded fixture for every role after changing CLI, models, agents or endpoint. Confirm the selected subagent model from actual transcript/usage evidence, rather than the definition alone. Confirm worker branches and acceptance checks before integration. Do not restart or clear the old runtime's STOP to make the new project appear active.

## Saved project sessions and scheduled continuation

Every native builder shares the ignored `.builder/claude` history. The launcher rejects `--no-session-persistence`, including print-mode runs. Coordinator, worker, reviewer and nested subagent histories remain in their native JSONL format. A worker defaults to its unique worktree name for peer discovery; an explicit session name remains supported. Session saving does not automatically inject every history into every prompt.

Use `/port-context` or `python3 scripts/project-sessions.py list` for the scoped index. `read ID --offset 0 --limit 16000` returns private content with a next character offset; follow it to read complete records. `search 'literal query' --limit 10` searches only indexed sessions and paginates hits. The index includes the current checkout and explicitly registered launcher worktrees, nested subagents, and an optional dated snapshot of the current Codex migration dialogue (operator and public assistant messages, excluding tool/system/developer records). It excludes global Claude/Codex histories and failed projects. Imported context is a snapshot, not a live connector. Never commit raw sessions or publicize private contents.

The local Codex automation **Continue active PHPRetro port** runs every30minutes against this workspace through SSH. Its entrypoint `python3 scripts/continue-project.py --run` checks private readiness, skips when busy, and holds a project lock through one externally bounded15-minute native coordinator segment and its atomic checkpoint. `--check` inspects readiness without inference. It resumes claimed work, uses Luna implementation/Sol review, and saves a current handoff. The Codex computer/app must be available; no legacy Ubuntu timer or prior project's STOP is changed. Native turn limits remain advisory and provider spending rates/cash budgets remain unresolved.
