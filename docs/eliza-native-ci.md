# Eliza native text-host CI qualification (G05-native-ci-build)

This document describes the bounded, push-only CI job that turns the reviewed
source-only startup/registry/adversarial/contributor test patches into compiled
qualification evidence. It is **not** runtime, browser, model or native-admission
evidence.

## Status

**No qualification result is claimed.** Hosted run `38717087713` on `a54f491`
was refused at the wrapper's former off-peak check before compilation or tests;
its receipt records no child status. This revision removes that provider-free
scheduling dependency and retains the caller deadline, resource floors,
supervised cleanup and receipt checks. The runtime guard remains false and main
is unchanged.

## Files

| File | Role |
| --- | --- |
| `.github/workflows/native-text-host.yml` | The push-only hosted job (checkout, wrapper, artifact). |
| `scripts/native-host-ci.py` | The bounded wrapper that drives the accepted `native-host.py` APIs. |
| `tests/ops/test_native_host_ci.py` | Fixture tests for the wrapper's pure logic and orchestration. |
| `docs/eliza-native-ci.md` | This document. |

## Workflow contract

- **Triggers:** `push` to `build/eliza-deepseek-themes` only, path-scoped to the
  owned native paths. There is **no** `pull_request` and **no**
  `pull_request_target`, so a worker-branch push runs only the standard `Verify`
  workflow.
- `runs-on: ubuntu-24.04`, `timeout-minutes: 90`, `permissions: contents: read`.
- `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` with
  `persist-credentials: false` and `submodules: false`.
- `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` (v4) with
  `retention-days: 3`, uploading only the wrapper's allowlisted artifact
  directory. No `actions/cache`, so no credential or cache data is uploaded.

## Wrapper contract

1. **Identity first.** GitHub repo/ref/sha/run/attempt are captured from the
   environment *before* it is cleared. A non-commit `GITHUB_SHA` is refused.
2. **Credential exclusion (applied, not computed).** The child environment is an
   allowlist (`PATH`, `LANG`, `LC_ALL`, `TZ`, `NO_COLOR`, `TERM`) with a private
   `HOME`. `os.environ` is **cleared and replaced** with that allowlist for the
   whole run and restored in `finally`, so the accepted helpers that build
   subprocesses without an explicit env (`_git`, `_cargo_version`,
   `ensure_source`, `prepare_build`, `ensure_toolchain`, ...) all see the
   filtered environment. Every token, provider key and `GITHUB_*` value is
   dropped; merely computing a filtered dict would not isolate a helper.
3. **Mechanical prerequisite.** The accepted `native-host.py` must expose
   `operation_deadline`, `record_build_attempt` and a `run_supervised` that
   accepts `outcome`. Absence is a refusal *before* any heavy setup; no
   preflight or build runs.
4. **One immutable provider-free deadline.** A single deadline is captured from
   the caller limit (default 3600 seconds, accepted range 6–7200) and passed to
   every stage. The qualification wrapper does not consult the DeepSeek off-peak
   schedule or replace the gate function. Its stage/resource bounds, parent-loss
   cleanup and native-admission guard remain in force.
5. **Resource preflight.** The accepted 3 GiB RAM / 12 GiB disk floors are
   checked before heavy work; an unknown reading fails closed. No broad disk
   cleanup is performed.
6. **Production build first.** The unchanged pinned source is prepared with the
   reviewed production patch and the locked release-local version repair and the
   accepted fixed profile. The production patch, lock, binary and profile are
   verified. Test patches only follow a successful production build.
7. **Ordered test patches.** Only the four reviewed public test patches
   (`qualification-tests`, `qualification-registry`,
   `qualification-adversarial`, `qualification-contributors`) are applied, in
   that exact order, each with its reviewed patch digest and the expected
   patched target digests, and a **test-file-only** scope.
8. **Bounded compile.** A locked online `cargo test --release --locked --jobs 1
   --no-run -p codex-core --lib` preparation, then the offline qualification
   command `cargo test --release --locked --offline --jobs 1 -p codex-core --lib
   native_text_host_qualification -- --test-threads=1`. Every heavy command runs
   under one `run_supervised` cutoff, the RAM floor and the filtered release
   environment.
9. **Exact results.** Exactly the nineteen selected module-qualified test names
   must complete: 19 passed and zero failed/ignored/skipped, and the run summary
   must agree (verdict `ok`, `0 failed`, `0 ignored`, `19 passed`). Missing,
   duplicate, unexpected qualification-prefixed, empty, truncated or
   contradictory (`0 passed`) results fail qualification even on exit 0. Extra
   unrelated results are ignored.
10. **Post-test preservation.** After the test compile/run the production patch
    bytes, patched target bytes, lockfile, provenance, build profile and the
    production binary are all rechecked against the exact digests captured
    before the tests — not the binary alone.
11. **Truthful failure receipts.** The native command/build stage records the
    build failure in the accepted compile scope (`supervise`), carrying the real
    `compiler_child_status`/`compiler_cleanup`. Every other stage persists the
    *actual* curated supervisor outcome (real stop reason and measured RAM) under
    its own wrapper stage name, so the helper reports the compiler scope as
    `not_applicable` rather than inventing a child-cleanup result or a resource
    cause. Each stage boundary clears the outcome so a completed build's state
    can never leak into a later stage. An `OSError` yields a truthfully staged
    receipt with no exception text.
    The failing supervised stage also contributes at most one bounded
    **diagnostic** excerpt (`failure.diagnostic`), because the failure artifact is
    otherwise receipt-only and a hosted `cargo` failure (observed as a bare
    `exit 101` in `prepare-tests`) leaves no compiler text anywhere else:
    - the excerpt is **stage-scoped**: every stage boundary assigns the log
      reference it owns (`build`, `prepare-tests`, `offline-tests`) or `null`
      (`preflight`, `source`, `toolchain`, `prepare`, `test-patches`), so a
      `test-patches` failure can never present the completed build's log as its
      own evidence, and a stage with no log of its own always yields `null`;
    - only diagnostic-shaped log lines are kept — `error`, `error[E…]`, `-->`
      and `warning: unused` — so cargo boilerplate, source snippets and progress
      lines are dropped;
    - absolute paths, URLs, the literal `$HOME` and `KEY=value` environment
      values are redacted (`<path>`, `<url>`, `<home>`, `<redacted>`); relative
      paths such as `core/src/tools/registry.rs` are kept;
    - at most the **last 256 KiB** of the stage log is read, and the excerpt is
      hard-capped to **40 lines / 4000 characters** with a `truncated` flag;
    - a missing, unreadable or diagnostic-free log yields `null` — nothing is
      fabricated. No full log, environment dump or secret is published, and the
      artifact on failure remains the receipt alone.
12. **Fresh atomic artifact.** The upload directory is published by an atomic
    replace of a dedicated staging directory that contains only the receipt (on
    failure) or the binary, receipt, `LICENSE` and `NOTICE` (on success). A
    success must publish all four mandatory files or it fails. Unexpected entries
    and symlinks are refused, a failure replaces any earlier upload so no stale
    or partial binary remains uploadable, and nothing outside the wrapper's own
    artifact directory is ever removed.

## The nineteen selected module-qualified tests

Each test module is declared as `#[cfg(test)] #[path = "..._tests.rs"] mod tests;`
inside its parent module, so `cargo test` prints names relative to the crate root
as `<parent module path>::tests::<fn>`. Enumerated by applying `text-only` then
`qualification-tests`, `-registry`, `-adversarial` and `-contributors` in order to
the pinned tree (`c1382380`) and reading the real `#[test]`/`#[tokio::test]`
functions: nine startup/resume, eight spec-plan, two registry.

`thread_manager::tests` (startup/resume):

- `native_text_host_qualification_new_thread_without_init_captures_empty_ceiling`
- `native_text_host_qualification_overwrites_unrestricted_caller_init`
- `native_text_host_qualification_overwrites_conflicting_caller_init`
- `native_text_host_qualification_legacy_history_mode_is_covered`
- `native_text_host_qualification_legacy_fork_is_covered`
- `native_text_host_qualification_internal_guardian_source_is_covered`
- `native_text_host_qualification_cold_resume_captures_empty_ceiling`
- `native_text_host_qualification_warm_resume_keeps_live_session_and_ceiling`
- `native_text_host_qualification_cold_resume_constructor_matrix_captures_ceiling`

`tools::spec_plan::tests` (registry-empty + contributor/hosted controls):

- `native_text_host_qualification_manager_started_registry_is_empty`
- `native_text_host_qualification_manager_started_registry_rejects_dynamic_tools`
- `native_text_host_qualification_manager_started_code_mode_only_registry_is_empty`
- `native_text_host_qualification_manager_started_code_mode_registry_is_empty`
- `native_text_host_qualification_extension_contributor_positive_control_registers_tools`
- `native_text_host_qualification_extension_contributor_constrained_session_denied`
- `native_text_host_qualification_hosted_web_search_positive_control_is_visible`
- `native_text_host_qualification_hosted_web_search_constrained_session_denied`

`tools::registry::tests` (registry dispatch):

- `native_text_host_qualification_captured_policy_rejects_registration`
- `native_text_host_qualification_captured_policy_denies_dispatch`

## Patch and target digests

| Patch | SHA-256 | Patched target digest |
| --- | --- | --- |
| `text-only.patch` | `09b3ef5a…66c7f` | `thread_manager.rs` = `d65c1697…88bf8` |
| `qualification-tests.patch` | `c2c2f0bb…3fc57` | `thread_manager_tests.rs` = `4e0ec767…28d3e0` |
| `qualification-registry.patch` | `87cb1a63…ab754ad` | `spec_plan_tests.rs` = `d45fc796…75579`; `registry_tests.rs` = `6d1621dc…8c2b7` |
| `qualification-adversarial.patch` | `3c668046…3d27648` | `thread_manager_tests.rs` = `13b3e918…618aa`; `spec_plan_tests.rs` = `688cec9b…c9481`; `registry_tests.rs` = `ffb81085…08570857` |
| `qualification-contributors.patch` | `3caae90b…a23c749` | `spec_plan_tests.rs` = `73a843f2…b225a4` |

The full digests are embedded in `scripts/native-host-ci.py`; the reviewed docs
give the leading/trailing segments shown here.

## Artifact allowlist

On success the artifact contains only the compiled `codex` binary, the sanitized
JSON receipt, and the upstream `LICENSE`/`NOTICE`. On failure it contains only
the truthful receipt: no qualified binary, no partial binary, no home data and
no full build or test logs — and any earlier upload is replaced so a stale binary
can never be downloaded. The receipt records the exact build inputs (source pin,
patch, lock, toolchain, profile, binary digest), the test-patch digests and
observed result names, the GitHub run/ref/SHA identity, timing and resources,
the curated failure stage/scope/stop reason (no fabricated cleanup or resource
cause), the bounded redacted `diagnostic` excerpt of the failing stage (≤ 40
lines / ≤ 4000 chars; `null` when there is none), and always
`native_admission: false` with the guard unchanged.

## Remaining gaps

- The heavy job is **unrun**: no compiled qualification, resource fit or artifact
  integrity is claimed until a fresh quota window permits a real run.
- Import of the resulting binary remains a separate, independently reviewed
  task. This task neither imports nor admits anything.
- Full MCP tool injection and extension-executor *invocation* are documented
  seams, not covered here; the nineteen tests are a partial qualification.
