# Eliza native text-host — source patch (UNBUILT / UNQUALIFIED)

**Status: source patch only. Not built, not qualified, not runtime-ready.**
Nothing in this directory proves runtime isolation. No binary was produced, no
Rust test was compiled or run, and no account, model, network or service call
was made. Treat this as a reviewed *source* artifact with an explicit open
qualification gap.

## Contents

| File | Purpose |
| --- | --- |
| `text-only.patch` | The single-hunk source patch against the pinned upstream commit. |
| `provenance.json` | Pin, toolchain, license, digests and change description. |
| `LICENSE` | Apache-2.0, copied verbatim from the pin. |
| `README.md` | This file. |

## What the patch does

Applies one insertion to the central spawn funnel in
`codex-rs/core/src/thread_manager.rs` — immediately after the existing
`thread_extension_init.insert(isolation)` line in
`ThreadManagerState::spawn_thread`:

```rust
thread_extension_init.insert(codex_extension_api::ToolPolicy {
    allowed_tools: Some(Vec::new()),
    require_managed_sandbox: false,
    require_unified_exec: false,
    expose_additional_permissions: false,
});
```

Because `insert` overwrites, this sets an unconditional empty tool ceiling for
every thread and overwrites any caller-supplied `ToolPolicy` — including the
Guardian reviewer init — with no role exception and no new
manager/config/RPC flag that could relax it. `Session::new` captures the policy
once, before extension initialization.

### Why this insertion point

Production session creation is a single funnel:
`ThreadManager::spawn_thread` → `Session::spawn` → `Session::spawn_internal` →
`Session::new`. `Session::new` has exactly one production caller
(`Session::spawn_internal`); the other `Session::new` references are tests. The
guardian reviewer is started as a thread through the thread manager
(`StartThreadOptions`), so it is covered by the same central insertion. There is
no second production entry that reaches `Session::new` directly.

## How the ceiling is expected to work (source-level)

`Session::new` reads `thread_extension_init.get::<ToolPolicy>()` and, when
present, uses it instead of the guardian fallback
(`codex-rs/core/src/session/session.rs`). The policy is threaded into the tool
registry (`core/src/tools/registry.rs`, `with_tool_policy`) and the model-visible
spec plan (`core/src/tools/spec_plan.rs`). With `allowed_tools: Some(empty)`,
`ToolPolicy::allows` returns false for every name, so central registration and
hosted-spec retain reject every tool.

This is the *designed* behavior at the pin. It is **not** qualified here.

## Planned qualification (NOT YET IMPLEMENTED)

The following tests are **planned and unimplemented**; they are not compiled,
not run, and not passing. They must drive production startup/router paths rather
than duplicate test-only clamp logic.

- Start through `ThreadManager::start_thread` (and `spawn_internal_session`)
  across: new, cold-resume, fork, internal, legacy, and guardian reviewer
  sessions, each with **absent**, **unrestricted**, and **conflicting** caller
  init, asserting the effective policy is the empty ceiling in every case.
- A **warm** existing session must stay constrained on resume (the ceiling is
  re-supplied, not relaxed).
- After startup, the **finalized registry** rejects trusted, external, dynamic,
  MCP, direct, hidden, deferred, Code Mode and hosted tools.
- The completed registry exposes **zero** complete tool entries, zero model-visible
  specs, and zero mappings; the dispatch executor is **never** invoked.
- Reuse narrow existing in-tree Rust fixtures; use **only** a controlled local
  mock server — no paid model, no real account, no network.

## Validation performed in this phase

- `git apply --check` of `text-only.patch` against a clean checkout pinned at
  `c1382380de69521303b416720a52f42d51af6248`: clean apply (see handoff).
- Base and patched `thread_manager.rs` SHA-256 recorded in `provenance.json`.

No installation, build, account, model, or service state was changed.
