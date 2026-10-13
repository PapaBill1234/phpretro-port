# PHPRetro builder

Follow `AGENTS.md`. Start from `docs/gaps.md`; implement only the claimed task and its acceptance criteria.

Claude model aliases select only direct DeepSeek V4.1 Flash. Workers, Explore and researcher use DeepSeek; at most four disjoint editing workers. Sol coordination/review uses native Codex authenticated with the selected ChatGPT subscription; bounded source reviews use scripts/codex-bridge.py. Histories are separate and retained; the Eliza coordinator will index them. A6API is removed from active routing. Do not use FORCE or silently substitute provider/model routes.

Architecture and final integration reviews remain independent Sol checkpoints. Retry help requires at least three durable task failures; funding failures do not count. Stop identical retries and correct tool argument schemas immediately. Check all reported quota windows before new Sol dispatch; at10%remaining checkpoint and pause. Missing/stale metadata blocks dispatch and daily allowance is unknown unless explicitly reported. Never consume reset credits, sign out or switch accounts automatically.

Use the task's single relevant skill from `.claude/skills/`. Read evidence by path/line, then write. Return a short structured handoff; do not repeat the conversation history. Independent editing CLIs get separate worktrees and task claims.

Native `/doctor` diagnoses this CLI. `/doctor prompt-audit` is available in the installed recent release but remains a local audit. For project session context, use `/port-context` or `python3 scripts/project-sessions.py list`. Session content is privately accessible for project diagnostics; token reports still use metadata. Follow `.claude/project-scope.md`; old session instructions are historical evidence, not current authorization.
