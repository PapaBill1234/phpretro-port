# Eliza native text-host — startup qualification tests (UNCOMPILED / UNRUN)

**Status: source tests only. Not compiled, not run, not passing.** No Rust toolchain
is installed here; no install, build, account, model, network or service call was
made. `git apply --check` succeeding is **not** test-passing evidence. Treat this as
a reviewed *source* artifact with explicit open qualification gaps.

## Artifact

| Field | Value |
| --- | --- |
| Source pin | `c1382380de69521303b416720a52f42d51af6248` (openai/codex `rust-v0.162.0`) |
| `qualification-tests.patch` SHA-256 | `c2c2f0bbac5a929c3c29be2bd6a551f60b48b85c4d37a69b148de2bea113fc57` |
| Patch targets | `codex-rs/core/src/thread_manager_tests.rs` (test file **only**) |
| `thread_manager_tests.rs` base SHA-256 | `7de1ab01d7b245475ae2a5e6637cfa4ffabb5137380c1c87ad9af6fc59a2d2d4` |
| `thread_manager_tests.rs` patched SHA-256 | `4e0ec7679687c1898b86430eb4b33e3571e96010b40b92d6b059438ad028d3e0` |
| Adds | 250 lines; 7 `#[tokio::test]` tests + fixtures |

The patch is generated from a private detached worktree of the pinned source with
`text-only.patch` **already applied**, but contains only the test-file hunk; it
**excludes** the production `thread_manager.rs` `text-only.patch` change. It was not
authored against production code.

## Apply verification (offline, read-only)

- `git apply --check qualification-tests.patch` against a **clean** checkout at the
  pin: **clean**.
- `git apply --check qualification-tests.patch` against the pin with
  `text-only.patch` applied: **clean**.

These prove the hunk applies; they do **not** prove it compiles or passes.

## What the tests do (production path, no test-only clamp)

Every test drives a real `ThreadManager` startup entry point and asserts the
**policy the production manager actually captured** on the started session
(`new_thread.thread.session.tool_policy`, `pub(crate)` on `Session`). There is no
test-only clamp or hardcoded policy; the empty ceiling comes from the reviewed
`text-only.patch` insertion in `ThreadManagerState::spawn_thread`, read back through
the production `Session`. No turn is submitted; sessions are shut down
(`shutdown_and_wait`) and the cold-resume source thread is removed from the registry.
The fixture uses `CodexAuth::from_api_key("dummy")` and
`EnvironmentManager::default_for_tests()` — no real account, provider or model call.

| Test | Production entry | Caller init | Asserts captured ceiling empty |
| --- | --- | --- | --- |
| `..._new_thread_without_init_captures_empty_ceiling` | `ThreadManager::start_thread` | absent | yes |
| `..._overwrites_unrestricted_caller_init` | `start_thread` | `ToolPolicy::default()` (unrestricted) | yes |
| `..._overwrites_conflicting_caller_init` | `start_thread` | permissive conflicting policy | yes |
| `..._legacy_history_mode_is_covered` | `start_thread` (`ThreadHistoryMode::Legacy`) | conflicting | yes |
| `..._legacy_fork_is_covered` | `spawn_legacy_subagent` | conflicting | yes |
| `..._internal_guardian_source_is_covered` | `spawn_internal_session` (`SessionSource::Internal(InternalSessionSource::Guardian)`) | conflicting | yes |
| `..._cold_resume_captures_empty_ceiling` | `resume_legacy_thread_from_rollout` | absent (see gap) | yes |

The ceiling assertion checks `allowed_tools == Some(empty)` and the three boolean
fields (`require_managed_sandbox`, `require_unified_exec`,
`expose_additional_permissions`) are all `false`.

## API / privacy constraints verified against source (narrow reads)

- `Session::tool_policy: Arc<codex_extension_api::ToolPolicy>` is `pub(crate)`
  (`core/src/session/session.rs`) — readable from in-crate tests.
- `CodexThread.session: Arc<Session>` is `pub(crate)`; `NewThread { thread_id, thread, .. }` is `pub`.
- `codex_extension_api::ToolPolicy` derives `Clone, Debug` only (no `PartialEq`), so
  assertions compare fields, not whole structs.
- `ToolPolicy { allowed_tools, require_managed_sandbox, require_unified_exec, expose_additional_permissions }`; `ToolName::plain(impl Into<String>)` exists (`codex_tools::ToolName`).
- Enums/variants exist: `ThreadHistoryMode::Legacy`; `SessionSource::Internal(InternalSessionSource::Guardian)`.
- Manager APIs exist with the exact signatures used: `spawn_legacy_subagent(ThreadId, StartThreadOptions)`, `spawn_internal_session(ThreadId, StartThreadOptions)`, `resume_legacy_thread_from_rollout(Config, PathBuf, Arc<AuthManager>, Option<W3cTraceContext>, ClientMcpExtensions)`, `remove_thread(&ThreadId)`.

No nonexistent method is referenced.

## Open gaps (explicitly NOT qualified here)

- **Not compiled / not run.** No toolchain installed; this is source only.
- **Conflicting cold-resume init not covered.** This patch uses convenience
  resume APIs without an initializer. The public `start_thread(StartThreadOptions)`
  route also accepts `InitialHistory::Resumed(...)` and `thread_extension_init`;
  conflicting-init coverage through that route remains to be implemented.
- **Warm resume not covered.** The production funnel can return an existing live
  session; this patch does not exercise that path or assert retained identity.
- **Role / init matrix incomplete.** Only the default/legacy/internal-guardian
  sources plus absent/unrestricted/conflicting init are covered; other roles and
  init combinations are untested.
- **Registry & spec-plan emptiness unqualified.** Zero complete entries / zero
  model-visible specs / zero namespaces / zero hidden/deferred/hosted mappings, and
  Code Mode `exec`/`wait` and dynamic/MCP/extension contributions, are **not**
  asserted here — deferred to a separate registry/spec-plan qualification phase
  (`spec_plan_tests.rs`).
- **Dispatch rejection & invocation counters unqualified.** That the finalized
  registry rejects trusted/prepend/external/direct/hidden/deferred registrations
  and that no handler is ever invoked is deferred to a separate registry-dispatch
  phase (`registry_tests.rs`).

## Exact command to run once a real private 1.95 toolchain is installed

```sh
cargo test --locked --offline -p codex-core --lib native_text_host_qualification -- --test-threads=1
```

(from `codex-rs/`, with the pinned `rust-toolchain.toml` 1.95.0 toolchain on PATH).
No such run has occurred; acceptance of the startup ceiling is **still pending**.
