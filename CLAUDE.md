# PHPRetro builder

Follow `AGENTS.md`. Start from `docs/gaps.md`; implement only the claimed task and its acceptance criteria.

Role aliases are provider mappings verified by runtime probes, not claims about native Anthropic model identity. Default lead and architect/reviewer: Sol 6.1; implementation workers: Luna. DeepSeek is an optional explicit selection. Use explicit agent model roles. Do not set `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`: it would override the expensive reviewer's model too.

Before locking an architectural plan, invoke the reviewer. Invoke it after the same test/compiler failure twice and before accepting a diff. These are orchestration instructions; an instruction file alone does not enforce a checkpoint. Durable task states and the integration process must enforce them.

Use the task's single relevant skill from `.claude/skills/`. Read evidence by path/line, then write. Return a short structured handoff; do not repeat the conversation history. Independent editing CLIs get separate worktrees and task claims.

Native `/doctor` diagnoses this CLI. `/doctor prompt-audit` is available in the installed recent release but remains a local audit. For project session context, use `/port-context` or `python3 scripts/project-sessions.py list`. Session content is privately accessible for project diagnostics; token reports still use metadata. Follow `.claude/project-scope.md`; old session instructions are historical evidence, not current authorization.
