# Eliza native text-host — adversarial qualification matrix (UNCOMPILED / UNRUN)

**Status: source tests only. Not compiled, not run, not passing.** No Rust toolchain
is installed here; no install, build, account, model, network or service call was
made. `git apply --check` succeeding is **not** test-passing evidence.

## Artifact (incremental)

| Field | Value |
| --- | --- |
| Source pin | `c1382380de69521303b416720a52f42d51af6248` (openai/codex `rust-v0.162.0`) |
| `qualification-adversarial.patch` SHA-256 | `952355fb2a0fda2bf10f0479c53bb607e14f70c9d6fdb805f89f45d7fccb5edc` |
| Patch targets | `thread_manager_tests.rs` (+102), `tools/spec_plan_tests.rs` (+85), `tools/registry_tests.rs` (+59) — 246 lines, test modules **only** |
| `thread_manager_tests.rs` SHA-256 | `13b3e918…618aa` |
| `tools/spec_plan_tests.rs` SHA-256 | `569d21b9…d3dc4` |
| `tools/registry_tests.rs` SHA-256 | `ffb81085ada72230327724ca40c5c990184ab2f7e35853825833e68a98570857` |

This is the **incremental** diff on top of the baseline that already carries
`text-only.patch` + the startup tests + the registry tests. It contains **only** the
five new tests below; it excludes all production and prior-phase hunks.

## Required apply order (verified on a clean pin)

1. `text-only.patch` — OK
2. `qualification-tests.patch` — OK
3. `qualification-registry.patch` — OK
4. `qualification-adversarial.patch` — OK (check + apply)

Applying the adversarial patch **alone** on the clean pin **fails** (`patch does not
apply`), so the ordered sequence above is required.

## New tests (all drive production/manger-authored paths; no fake constructors)

**Warm resume (closes the earlier "unreachable" gap).**
`native_text_host_qualification_warm_resume_keeps_live_session_and_ceiling` —
`start_thread` with `InitialHistory::Resumed` for an **already live** conversation
takes `spawn_thread`'s live-thread reuse branch (`thread_manager.rs`, the
`if let InitialHistory::Resumed(resumed) … if thread.is_running()` block) and returns
the **same** `Arc<CodexThread>`/`Arc<Session>` — proven with
`Arc::ptr_eq(&resumed.thread.session, &started.thread.session)`. A conflicting caller
`thread_extension_init` is supplied and the live session's captured ceiling is
unchanged.

**Cold resume constructor matrix.**
`native_text_host_qualification_cold_resume_constructor_matrix_captures_ceiling` —
the public `start_thread` accepts `InitialHistory::Resumed` (history read from the
persisted rollout via `RolloutRecorder::get_rollout_history`) **together with** a
caller `thread_extension_init`. For both `unrestricted` and `conflicting` init the
thread id is preserved and the captured ceiling is empty. (Absent init cold resume is
already covered by the startup phase.)

**Code Mode variants (production finalization).**
`…_manager_started_code_mode_only_registry_is_empty` and
`…_manager_started_code_mode_registry_is_empty` — a manager-started session/turn is
driven into `CodeModeOnly` / `CodeMode`, the exact mode is asserted
(`router.tool_mode()`), and the finalized plan built by `build_tool_router` has zero
complete entries, model-visible specs, namespace inventory, code-mode nested names,
and no exposure of `exec`/`wait`.

**Dispatch denial (production dispatch path).**
`…_captured_policy_denies_dispatch` — a registry built from the captured
`session.tool_policy` rejects a counting handler at registration (`entries().count()
== 0`), then dispatches representative builtin / dynamic / MCP-namespaced / external
names through the production `dispatch_any_with_state`. Each returns
`Err(FunctionCallError::RespondToModel(_))` (unsupported-tool), and the handler's
invocation counter stays `0`.

## Open gaps (explicitly NOT qualified here)

- **Not compiled / not run** — no toolchain; source only.
- **MCP and extension *contributor* injection not exercised.** No MCP server cache
  and no registered extension tool contributor are wired in; those sources would be
  denied by the empty ceiling, but that is unproven. The MCP-namespaced dispatch case
  uses only a name, not a real MCP tool.
- **Hosted mappings** remain asserted indirectly via empty `model_visible_specs`.
- **Cold-resume matrix** covers unrestricted/conflicting only; absent is in the
  startup phase.
- **Code Mode availability** is forced with `turn.code_mode_available = true` and the
  model's `tool_mode` cleared; the mode is asserted so the fixture cannot silently
  fall back to Direct.

## Exact command (once a real private 1.95 toolchain is installed)

```sh
cargo test --locked --offline -p codex-core --lib native_text_host_qualification -- --test-threads=1
```

No such run has occurred; registry/dispatch/resume acceptance is **still pending**.
