# Eliza native text-host — extension/hosted contributor slice (UNCOMPILED / UNRUN)

**Status: source tests only. Not compiled, not run, not passing.** No Rust toolchain
is installed here; no install, build, account, model, network or external MCP process
was used. `git apply --check` succeeding is **not** test-passing evidence.

## Artifact (incremental)

| Field | Value |
| --- | --- |
| Source pin | `c1382380de69521303b416720a52f42d51af6248` (openai/codex `rust-v0.162.0`) |
| `qualification-contributors.patch` SHA-256 | `3caae90b957f958aba5410fb1f9a27cae53d7a4a77b13ce494afdeeeda23c749` |
| Patch targets | `codex-rs/core/src/tools/spec_plan_tests.rs` (test module only; +286; 2 hunks) |
| `spec_plan_tests.rs` patched SHA-256 | `73a843f296b982f7f5030beea185248f4c0e0e866a209732809d8eb225b225a4` |
| Baseline (private) | pin `c1382380` + `text-only` + `qualification-tests` + `qualification-registry` + `qualification-adversarial` (committed; excludes the old candidate) |

## Required ordered apply (verified on a clean pin)

1. `text-only.patch` → 2. `qualification-tests.patch` → 3. `qualification-registry.patch`
→ 4. `qualification-adversarial.patch` → 5. `qualification-contributors.patch` — all
apply clean. The contributors patch alone on the pin fails (order-dependent). The
incremental diff was regenerated against the committed production+startup+registry+
adversarial baseline, then independently replayed in a second standalone `git init`
repository (toplevel = scratch dir, empty prefix); the replay applies cleanly, changes
**only** `spec_plan_tests.rs`, and its changed bytes match the staged target
(sha256 `73a843f2…`). Exit status alone was not treated as proof — changed bytes were
compared (`cmp`).

## Positive controls vs constrained paths

Positive controls use an **explicit unrestricted unit fixture**
(`make_session_and_context` + `ToolPolicy::default()`), proving each fixture **really
contributes**. They are **not** startup proof. Constrained paths use the
**manager-captured empty ceiling** (real `ThreadManager` start) and must deny the same
fixtures.

| Test | Path | Asserts |
| --- | --- | --- |
| `…_extension_contributor_positive_control_registers_tools` | unit, unrestricted | contributor evaluated; `qualification_ext_echo` (direct) **and** `qualification_ext_deferred` (deferred) registered; executor handle count `0` (planning never executes) |
| `…_extension_contributor_constrained_session_denied` | manager-started, isolated | finalized plan empty (`nr_assert_empty_tool_router`); contributor still evaluated; executor handle count `0` |
| `…_hosted_web_search_positive_control_is_visible` | unit, unrestricted | hosted `web_search` spec present in `model_visible_specs` |
| `…_hosted_web_search_constrained_session_denied` | manager-captured | plan empty; no `web_search` spec |

The extension registry is attached to the constrained session the same way the
production host does it: the fixture calls `nr_start_qualification_thread` with a
closure that inserts `SessionIsolation::Isolated` and
`IsolatedSessionExtensions(registry)` into `thread_extension_init` **before** the real
`ThreadManager::start_thread` runs. The manager selects that supplied registry on its
isolated branch (`thread_manager.rs`, isolated branch reads
`IsolatedSessionExtensions<Config>`), so `session.services.extensions` is wired at
startup — there is no post-startup mutation and no `Session` clone. The empty
`ToolPolicy` ceiling is installed unconditionally by the reviewed production patch and
captured into `session.tool_policy`, so the ceiling is unmodified. No turn is submitted.
`SessionIsolation` + `IsolatedSessionExtensions` are the actual public exports of
`codex-rs/ext/extension-api/src/session_isolation.rs`. The hosted constrained fixture
enables the hosted capability with `Arc::get_mut(&mut turn)` (upstream
`session/tests.rs` pattern) **before** the turn is shared: `TurnContext` is not `Clone`
(only `Debug`), so `Arc::make_mut` would require an impossible clone; the remaining
`Arc::make_mut(&mut settings.model_info)` is valid because `ModelInfo` is `Clone`.
`codex-rs/ext/extension-api/src/session_isolation.rs`.

## Precise seam — real MCP ToolInfo injection is BLOCKED (not covered)

Production `build_tool_router` reads MCP tools from `McpBinding::tools()`
(`mcp_tool_exposure.rs::append_mcp_tools`). `McpBinding` exposes only
`pub fn empty(config)`; the constructor that carries a tool catalog
(`McpBinding::new(connections, clients, config, plugins_available, tools, calls)`) is
**`pub(crate)` to the `codex-mcp` crate** (`codex-rs/codex-mcp/src/binding.rs:54`).
There is no public or `#[cfg(test)]` path for `codex-core` to supply real cached
`ToolInfo`, and adding one would require a production/`codex-mcp` test-only guarantee
(disallowed here). Per the phase rule, this is documented as a seam rather than
faked: **no MCP tool is injected and no MCP coverage is claimed.** A prior-phase
MCP-shaped *name-only* dispatch is explicitly not MCP coverage.

## Remaining gaps

- **Not compiled / not run** — no toolchain; source only.
- **Real MCP ToolInfo injection** — blocked by the seam above.
- **Extension executor *invocation* on dispatch** — the slice proves evaluation and
  registration/denial with a zero handle counter; dispatching the adapted extension
  executor is not exercised (deferred).
- **Deferred nested/namespace mappings** beyond the direct/deferred extension tools are
  not asserted here.

## Command (once a real private 1.95 toolchain is installed)

```sh
cargo test --locked --offline -p codex-core --lib native_text_host_qualification -- --test-threads=1
```

No such run has occurred; acceptance is **still pending**.
