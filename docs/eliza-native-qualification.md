# Native qualification evidence and acceptance plan (G05)

Reviewed 2026-10-14. Offline native metadata and read-only public
source/release checks only: no account, login, model, thread, turn or build
action. This corrects the earlier **stable-only** conclusion that the isolation
fields were absent.

## Established (source/metadata at the pin)

- **Binary provenance.** The installed raw ELF `codex-cli 0.162.0` is
  byte-identical to the official release — the release ELF SHA-256 and the
  installed ELF SHA-256 are both
  `50ed828f357c655a3c82054d346cab8434f24901f14ec19b8267571bdd008b38` — from
  upstream source commit `c1382380de69521303b416720a52f42d51af6248`
  (`rust-v0.162.0`); retained privately.
- **Experimental schema.** `app-server generate-json-schema --experimental`
  **does** define the isolation fields on `ThreadStartParams` — `environments`,
  `dynamicTools`, `selectedCapabilityRoots` (`["array","null"]` with the matching
  item `$ref`) and `allowProviderModelFallback` (`boolean`). The **stable** schema
  omits them, which the earlier note misread as "absent". This is **schema
  support, not runtime isolation**.
- **Tool-policy ceiling (positive, source-proven).**
  `ToolPolicy { allowed_tools: Some(vec![]) }` denies every name through central
  registration and hosted-spec retain — including Code Mode `exec`/`wait` and
  hosted/extension tools. See [tool_policy.rs][tp], [registry.rs][reg],
  [spec_plan.rs][sp], [session.rs][se].
- **Offline controls.** 31 generator/transport/contract tests plus 19 controlled
  JS fixtures pass. GitHub CI on `e540a9c` is green for all six jobs, push **and**
  pull_request.

## Not established (unresolved)

- **No supported stock policy input (not yet proven).** The reviewed public
  startup signatures (`cli/src/main.rs`, app-server `lib.rs`/`extensions.rs`)
  expose no `ExtensionDataInit`/`ToolPolicy` argument, and no supported stock
  config route was found. Public types alone do not establish a supported shim;
  the supported path remains unresolved and unproven. See [cli/main.rs][cli],
  [app-server/lib.rs][app].
- Feature/config flags, prompts and a single model turn are **not** isolation
  proof.
- No accepted configured full-host startup, project browser, live queue or
  main-Sol qualification.

## Next bounded qualification sequence

Each step is gated; no inference runs until every prerequisite is proven.

1. **Immutable ceiling.** Supply `Some(empty)` at thread start, re-supply it on
   resume/fork and to helper roles; only then attempt a turn.
2. **Positive runtime/source proof.** Qualify the exact host build against the
   empty ceiling. Verify that the complete registered registry, including
   nested/deferred tools and hosted specifications, is empty, as are its
   model-visible specifications.
3. **Build provenance.** Record and verify the host build's provenance against
   the pin.
4. **Cutoff and cleanup.** One absolute off-peak cutoff captured once and never
   extended by a later phase; process-group TERM/KILL with hard-parent orphan
   cleanup that reports signaled helpers as failure.
5. **Separate steps.** Account metadata is a distinct read-only step; a live text
   turn is a further distinct step.

The tool guard is not weakened by this plan; no code, model, account, service or
build changes accompany it. See the [integration boundary](eliza-integration.md)
and [known gaps](gaps.md).

[tp]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/ext/extension-api/src/tool_policy.rs
[reg]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/core/src/tools/registry.rs
[sp]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/core/src/tools/spec_plan.rs
[se]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/core/src/session/session.rs
[cli]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/cli/src/main.rs
[app]: https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/app-server/src/lib.rs
