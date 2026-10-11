# Judge-only Jev connection

Project `.mcp.json` names the server `jev` and starts `python3 scripts/jev-judge-mcp.py` from the repository/worktree root. The launcher passes this project configuration with `--strict-mcp-config`; global MCP settings are unchanged. The proxy invokes installed `jev-use ... serve --backend openrouter` with newline JSON-RPC over stdio.

Only `jev_judge` appears in `tools/list`. Every other `tools/call`, including `jev_gate`, is denied locally and never forwarded. Unrelated MCP methods are also denied. The proxy remaps/tracks request IDs, handles out-of-order responses and cancellation, caps message/pending-request size, discards upstream stderr and replaces upstream error text with fixed diagnostics. Known inherited credentials are rejected in requests and redacted in responses. This is a transport restriction; ordinary Claude/Codex permissions still apply. No PreToolUse gate is installed.

Dependencies: Python 3.9+ on Linux/macOS, an executable Node runtime, and the installed `jev-use 0.8.0` CLI with its Node dependencies. Default CLI discovery is `~/.codex/jev/jev-use-0.8.0/dist/cli.js`; Node is resolved from `PATH`. Overrides are `PHPRETRO_JEV_NODE`, `PHPRETRO_JEV_CLI`, and optional private `PHPRETRO_JEV_CONFIG` (default `.builder/jev.json`). Private JSON accepts only `node`, `cli`, and `env_file` string fields. It must be an owned regular file, mode 0600, <=64 KiB; do not store keys in it.

The proxy accepts inherited `OPENROUTER_API_KEY`, or reads a private dotenv selected by `PHPRETRO_OPENROUTER_ENV`, private config `env_file`, then `~/phpretro-dashboard/secrets/openrouter.env`. The dotenv has the same ownership/mode/size restrictions. It is parsed as data; no shell sourcing, interpolation or command execution occurs. Only `OPENROUTER_API_KEY`, optional `OPENROUTER_BASE_URL`, and optional `JEV_MODEL` are loaded. Unrelated provider credentials do not reach the Jev subprocess. The Claude launcher intentionally strips inherited provider keys and preserves the private dotenv path; set an absolute path so isolated worktrees share the same private source.

OpenRouter base URL should normally be `https://openrouter.ai`, without `/api/v1`; the installed backend appends `/api/alpha/decisions`, not the chat-completions route. HTTPS is required, with no embedded username/password, query or fragment. This alpha endpoint is an external dependency; availability/model/usage behavior must be verified separately from local transport readiness. No key or credential-bearing configuration is committed.

Run the offline protocol/configuration tests:

```bash
python3 -m unittest discover -s tests/ops -p test_jev_mcp.py -v
```

Ten tests verify inventory filtering, forbidden calls never forwarded, allowed calls, out-of-order IDs/cancellation, duplicate active IDs, error redaction, credential rejection and private dotenv handling against a fake upstream. A separate installed-CLI initialization/list smoke establishes actual local MCP compatibility without a judgment request or inference spend. It does not establish provider inference availability or savings.

Keep judgment states small and factual; batch related questions, verify important verdicts against source and handle `escalate`/unavailable locally. No permission decisions or unchanged retries. Record Jev calls/usage/latency and unknown cost explicitly. Savings need matched same-task/same-main-model trials; a working connection alone proves none.
