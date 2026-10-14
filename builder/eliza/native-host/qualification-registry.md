# Eliza native text-host — registry / dispatch qualification tests (UNCOMPILED / UNRUN)

**Status: source tests only. Not compiled, not run, not passing.** No Rust toolchain
is installed here; no install, build, account, model, network or service call was
made. `git apply --check` succeeding is **not** test-passing evidence.

## Artifact

| Field | Value |
| --- | --- |
| Source pin | `c1382380de69521303b416720a52f42d51af6248` (openai/codex `rust-v0.162.0`) |
| `qualification-registry.patch` SHA-256 | `87cb1a63c68a1103b4028f88fd159ae825301822413732e743a5aabefab754ad` |
| Patch targets | `codex-rs/core/src/tools/spec_plan_tests.rs`, `codex-rs/core/src/tools/registry_tests.rs` (test files **only**) |
| Adds | 298 lines; 3 `#[tokio::test]` tests + fixtures |
| `spec_plan_tests.rs` base / patched SHA-256 | `d068a2b6…d3342` / `d45fc796…75579` |
| `registry_tests.rs` base / patched SHA-256 | `91fc8a06…eb590` / `6d1621dc…8c2b7` |

Generated as an **incremental** diff from the accepted private baseline (which has
`text-only.patch` + the startup `thread_manager_tests.rs` change applied). It contains
**only** the two registry-phase test-file hunks and **excludes** the production
`text-only.patch` change and the startup test change. No production code is edited.

## Apply verification (offline, read-only)

- vs **clean** pin: **clean**.
- vs pin **+ `text-only.patch`**: **clean**.
- vs pin **+ `text-only.patch` + `qualification-tests.patch`**: **clean**.

## Tests (all drive the production manager-captured policy; no test-only clamp)

Every fixture starts a session with the real `ThreadManager`
(`with_models_provider_and_home_for_tests`, dummy auth,
`EnvironmentManager::default_for_tests`) so the production startup patch captures the
empty ceiling; tests read that captured policy back. No turn is submitted, sessions
are shut down, and no model/account/network call is made.

1. `native_text_host_qualification_manager_started_registry_is_empty`
   — manager-started session → `Session::new_default_turn` → production
   `spec_plan::build_tool_router`. Asserts the **finalized** plan has **zero**
   model-visible specs, **zero** complete registry entries
   (`registered_tool_names_for_test`), no namespace inventory, zero code-mode nested
   names, and `exposes_tool == false` for representative builtins
   (`exec_command`, `write_stdin`, `view_image`, `apply_patch`, `update_plan`,
   `web_search`).
2. `native_text_host_qualification_manager_started_registry_rejects_dynamic_tools`
   — same, with an adversarial direct dynamic function tool and a deferred namespaced
   dynamic tool injected onto the turn; asserts the same all-zero outcome and that
   neither dynamic name is exposed.
3. `native_text_host_qualification_captured_policy_rejects_registration`
   — builds a `ToolRegistry` from the **captured** `session.tool_policy` and attempts
   `register_trusted`, `prepend_trusted`, `register_external`, and registrations with
   `Hidden`, `Deferred`, `CodeModeOnly`, `DirectModelOnly` exposures. Asserts zero
   registry entries, `register_external` returns `false`, and a counting handler's
   invocation counter stays `0`.

## APIs verified against source (narrow reads; no nonexistent methods)

`Session::new_default_turn()`, `StepContext::for_test(Arc<TurnContext>)`,
`spec_plan::build_tool_router(...)` (8-arg, as in the existing `reviewer_tool_policy_*`
tests), `ToolRouter::{model_visible_specs, registered_tool_names_for_test,
tool_exposure_for_test, tool_namespaces_info, code_mode_tool_names, exposes_tool}`,
`ToolRegistry::{with_tool_policy, register_trusted, prepend_trusted, register_external,
entries}`, `TurnContext.dynamic_tools: Vec<DynamicToolSpec>` (mutable via
`Arc::make_mut`; `TurnContext: Clone`), `ToolExposure` variants
(`Direct/Deferred/DeferredModelOnly/DirectModelOnly/CodeModeOnly/Hidden`),
`ToolName::{plain,new}`. `session.tool_policy` is `pub(crate)`; `CodexThread.session`
is `pub(crate)`.

## Open gaps (explicitly NOT qualified here)

- **Not compiled / not run** — no toolchain; source only.
- **ToolMode matrix incomplete.** Only the default (`Direct`) plan is exercised.
  `CodeMode` / `CodeModeOnly` are **not** covered; forcing those modes on this
  fixture depends on config features and model capability and was not verified
  reachable, so no such coverage is claimed.
- **MCP and extension contributions not injected.** The text uses no MCP server
  cache and no registered extension tool contributor, so those adversarial sources
  are **not** exercised (they would still be rejected by the empty ceiling, but that
  is unproven here).
- **Dispatch rejection not covered.** `dispatch_any_with_state` with a constructed
  `ToolInvocation` for builtins/contributors is deferred to a separate dispatch
  phase; only *registration* rejection and a zero-counting handler are asserted.
- **Hosted mappings** are asserted only indirectly (hosted specs are retained into
  `model_visible_specs`, asserted empty); there is no separate hosted-mapping count.
- **Dynamic-tool gate unverified.** Whether the dynamic-tools feature actually admits
  the injected tools through the router is not verified; the assertion is
  zero-exposure regardless.

## Exact command to run once a real private 1.95 toolchain is installed

```sh
cargo test --locked --offline -p codex-core --lib native_text_host_qualification -- --test-threads=1
```

(from `codex-rs/`, pinned `rust-toolchain.toml` 1.95.0 on PATH). No such run has
occurred; registry/dispatch acceptance is **still pending**.
