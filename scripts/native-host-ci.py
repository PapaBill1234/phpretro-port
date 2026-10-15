#!/usr/bin/env python3
"""Bounded push-only CI wrapper for the Eliza native text-host qualification.

This wrapper turns the source-only startup/registry/adversarial test patches into
a bounded hosted job. It does **not** re-implement any native-host logic: it
loads the accepted, reviewed `scripts/native-host.py` and drives its public
functions under one captured caller deadline. Qualification remains unverified
until the hosted compile and exact test-result checks succeed.

Contract enforced here
----------------------
* Push-only, no `pull_request`/`pull_request_target`; the hosted workflow is the
  only caller and the root is the fixed checkout (no arbitrary CLI paths).
* GitHub identity (repo/ref/sha/run/attempt only) is captured from the
  environment *before* the parent environment is cleared.  For **all** bootstrap,
  preparation and supervised stages `os.environ` is replaced by an allowlist with
  a private HOME (tokens, provider keys and `GITHUB_*` are dropped, and the
  accepted `_git`/`_cargo_version` helpers that inherit `os.environ` therefore
  see the filtered environment); the original is restored in `finally`.
* The stop-receipt prerequisite (`record_build_attempt`, and `run_supervised`
  accepting an `outcome`) is checked mechanically before heavy setup; absence is
  a refusal.  On failure the accepted `record_build_attempt` is called with the
  real curated supervisor outcome (the accepted `compiler_child_status` /
  `compiler_cleanup` / `stop_reason` and measured RAM), never a fabricated child
  cleanup, and an `OSError` still yields a truthful failure artifact with no
  exception text.
* One provider-free operation deadline is captured once from the caller limit
  and passed to every stage. The wrapper does not consult the DeepSeek coding
  window; subprocess, resource and cleanup bounds still apply.
* Production build first (unchanged source, locked repair, accepted profile),
  then **only** the four reviewed public test patches in order
  (qualification-tests, qualification-registry, qualification-adversarial,
  qualification-contributors), each with its patch digest and expected patched
  target digests from the reviewed docs and a test-file-only scope.  After the
  test compile/run the production patch bytes, target bytes, lockfile,
  provenance, build profile and binary are rechecked against their captured
  exact digests (not the binary alone).
* Bounded locked preparation (`--no-run`) then the offline qualification command,
  both under the same cutoff, RAM floor and filtered release environment.
* Exactly the nineteen selected module-qualified test names must complete: 19
  passed and zero failed/ignored/skipped, and the run summary must agree.
  Missing, duplicate, unexpected qualification-prefixed, empty, truncated or
  contradictory (`0 passed`) results fail qualification even on exit 0.  Extra
  unrelated results are ignored.
* Failure evidence is truthful and stage-scoped: the native command/build stage
  records the build failure; every other stage persists the *actual* curated
  supervisor outcome (its real stop reason and measured RAM) under its own
  wrapper stage name, so the accepted helper records the compiler scope as
  ``not_applicable`` outside the build stage.  No child-cleanup result and no
  resource cause is ever fabricated, and an ``OSError`` yields a receipt with no
  exception text.  The failing stage's receipt also carries one bounded,
  sanitized ``diagnostic`` excerpt of *its own* stage log (every stage boundary
  assigns the log reference, so no stage inherits another's): only
  diagnostic-shaped log lines, with
  absolute paths/``$HOME``/environment values redacted and hard caps on lines
  and characters; the artifact itself stays receipt-only and no full log,
  environment or secret is ever published.
* The artifact is published into a fresh, dedicated directory by an atomic
  replace: compiled binary + sanitized receipt + LICENSE/NOTICE only on full
  success (all four mandatory), and a truthful receipt only on failure with no
  stale or partial binary.  Pre-existing unknown entries or symlinks are refused;
  nothing outside the wrapper's own artifact directory is ever deleted.
"""
import argparse
import datetime as dt
import hashlib
import inspect
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the fixed checkout root; never a CLI argument
HOST_PATH = HERE / "native-host.py"
PATCH_DIR = ROOT / "builder" / "eliza" / "native-host"

CALLER_LIMIT_SECONDS = 3600  # CI caller budget; job timeout is 90 minutes
MIN_CALLER_LIMIT_SECONDS = 6  # five-second cleanup margin plus one second to start
MAX_CALLER_LIMIT_SECONDS = 7200
ARTIFACT_DIR = ROOT / ".builder" / "native-host-ci" / "artifact"
RECEIPT_NAME = "qualification-receipt.json"
COMPILER_SCOPE = "supervise"  # the accepted scope where the compiler child runs

# --- GitHub identity (nonsensitive only) ------------------------------------
GITHUB_ENV_MAP = {
    "repository": "GITHUB_REPOSITORY",
    "ref": "GITHUB_REF",
    "sha": "GITHUB_SHA",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# --- child environment allowlist --------------------------------------------
ALLOWED_ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "NO_COLOR", "TERM"})
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE",
                  "APIKEY", "COOKIE", "SESSION", "AUTH", "OPENAI", "ANTHROPIC",
                  "DEEPSEEK", "GITHUB", "AWS", "GH_")

# --- production and public-test patch manifests -----------------------------
# Digests and expected patched-target digests are taken from the reviewed docs
# (provenance.json, qualification-{tests,registry,adversarial,contributors}.md);
# truncated doc prefixes were reconciled against the pinned source so every
# comparison is exact.  The four test patches apply, in this order, after the
# production patch.
PRODUCTION_PATCH = {
    "file": "text-only.patch",
    "sha256": "09b3ef5a7b09914f4013e23f5abc4797d41c42b022b4b05e9532861c2d566c7f",
    "targets": {"codex-rs/core/src/thread_manager.rs":
                "d65c1697520b64acd27016f4b6c077ae50e3f4cf0650a9a1becb1337dad88bf8"},
}
TEST_PATCH_MANIFEST = (
    {
        "file": "qualification-tests.patch",
        "sha256": "c2c2f0bbac5a929c3c29be2bd6a551f60b48b85c4d37a69b148de2bea113fc57",
        "targets": {"codex-rs/core/src/thread_manager_tests.rs":
                    "4e0ec7679687c1898b86430eb4b33e3571e96010b40b92d6b059438ad028d3e0"},
    },
    {
        "file": "qualification-registry.patch",
        "sha256": "87cb1a63c68a1103b4028f88fd159ae825301822413732e743a5aabefab754ad",
        "targets": {
            "codex-rs/core/src/tools/spec_plan_tests.rs":
                "d45fc796727af4bbfc7e826248df3a073efa10bb30931e57040e99b395f75579",
            "codex-rs/core/src/tools/registry_tests.rs":
                "6d1621dcdc8fe1a11003c5c3a99bf1619c032c60ffca443afbace0de5d28c2b7",
        },
    },
    {
        "file": "qualification-adversarial.patch",
        "sha256": "3c6680469db60cd9cf4b2cbe555d10f9c97e1ee02b843b4157f40b0403d27648",
        "targets": {
            "codex-rs/core/src/thread_manager_tests.rs":
                "13b3e91819a20cdca3ac33b3b69d01bf8554de51c835d90984ebc1d8f52618aa",
            "codex-rs/core/src/tools/spec_plan_tests.rs":
                "688cec9ba6d528763d406ee3a35bdc156b242053bdf138b83a27b660e52c9481",
            "codex-rs/core/src/tools/registry_tests.rs":
                "ffb81085ada72230327724ca40c5c990184ab2f7e35853825833e68a98570857",
        },
    },
    {
        "file": "qualification-contributors.patch",
        "sha256": "3caae90b957f958aba5410fb1f9a27cae53d7a4a77b13ce494afdeeeda23c749",
        "targets": {"codex-rs/core/src/tools/spec_plan_tests.rs":
                    "73a843f296b982f7f5030beea185248f4c0e0e866a209732809d8eb225b225a4"},
    },
)

# --- the nineteen selected tests, qualified by their declared source modules ---
# Each test module is declared with `#[cfg(test)] #[path = "..."] mod tests;`
# inside its parent module, so cargo prints names relative to the crate root as
# `<parent module path>::tests::<fn>`:
#   codex-rs/core/src/thread_manager.rs    -> thread_manager::tests
#   codex-rs/core/src/tools/spec_plan.rs   -> tools::spec_plan::tests
#   codex-rs/core/src/tools/registry.rs    -> tools::registry::tests
#
# Bare function names declared per target across the four test patches.
SELECTED_FUNCTIONS = {
    # Startup / resume (qualification-tests + qualification-adversarial).
    "codex-rs/core/src/thread_manager_tests.rs": (
        "native_text_host_qualification_new_thread_without_init_captures_empty_ceiling",
        "native_text_host_qualification_overwrites_unrestricted_caller_init",
        "native_text_host_qualification_overwrites_conflicting_caller_init",
        "native_text_host_qualification_legacy_history_mode_is_covered",
        "native_text_host_qualification_legacy_fork_is_covered",
        "native_text_host_qualification_internal_guardian_source_is_covered",
        "native_text_host_qualification_cold_resume_captures_empty_ceiling",
        "native_text_host_qualification_warm_resume_keeps_live_session_and_ceiling",
        "native_text_host_qualification_cold_resume_constructor_matrix_captures_ceiling",
    ),
    # Spec-plan / registry-empty + contributor & hosted-web-search controls
    # (qualification-registry + qualification-adversarial + qualification-contributors).
    "codex-rs/core/src/tools/spec_plan_tests.rs": (
        "native_text_host_qualification_manager_started_registry_is_empty",
        "native_text_host_qualification_manager_started_registry_rejects_dynamic_tools",
        "native_text_host_qualification_manager_started_code_mode_only_registry_is_empty",
        "native_text_host_qualification_manager_started_code_mode_registry_is_empty",
        "native_text_host_qualification_extension_contributor_positive_control_registers_tools",
        "native_text_host_qualification_extension_contributor_constrained_session_denied",
        "native_text_host_qualification_hosted_web_search_positive_control_is_visible",
        "native_text_host_qualification_hosted_web_search_constrained_session_denied",
    ),
    # Registry dispatch (qualification-registry + qualification-adversarial).
    "codex-rs/core/src/tools/registry_tests.rs": (
        "native_text_host_qualification_captured_policy_rejects_registration",
        "native_text_host_qualification_captured_policy_denies_dispatch",
    ),
}
MODULE_BY_TARGET = {
    "codex-rs/core/src/thread_manager_tests.rs": "thread_manager::tests",
    "codex-rs/core/src/tools/spec_plan_tests.rs": "tools::spec_plan::tests",
    "codex-rs/core/src/tools/registry_tests.rs": "tools::registry::tests",
}
TEST_FILTER = "native_text_host_qualification"


def selected_test_names():
    """The nineteen fully module-qualified test names derived from source modules.

    Enumerated by applying ``text-only`` then ``qualification-tests``,
    ``-registry``, ``-adversarial`` and ``-contributors`` in order to the pinned
    tree and reading the real ``#[test]``/``#[tokio::test]`` functions (nine
    startup/resume, eight spec-plan, two registry).
    """
    return frozenset("%s::%s" % (MODULE_BY_TARGET[target], fn)
                     for target, fns in SELECTED_FUNCTIONS.items() for fn in fns)


EXPECTED_TESTS = selected_test_names()

_RESULT_RE = re.compile(r"^test ([A-Za-z0-9_:]+) \.\.\. (ok|FAILED|ignored|bench)$")
_SUMMARY_RE = re.compile(
    r"^test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; "
    r"(\d+) filtered out; finished in ([\d.]+)s$")


class Refusal(Exception):
    """A fail-closed CI refusal; carries the process exit code to return."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# --- pure helpers -----------------------------------------------------------
def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_github_identity(environ):
    """Nonsensitive GitHub repo/ref/sha/run/attempt, captured before env clear."""
    identity = {}
    for key, name in GITHUB_ENV_MAP.items():
        value = environ.get(name)
        identity[key] = value if isinstance(value, str) and value.strip() else None
    sha = identity.get("sha")
    if sha is not None and not _SHA_RE.match(sha):
        raise Refusal("GITHUB_SHA is not a 40-hex commit id; refusing")
    return identity


def _is_secret(name):
    upper = name.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def filtered_environment(environ, private_home):
    """An allowlisted child environment with a private HOME and no credentials.

    Only allowlisted, non-secret keys are copied.  Every token/provider/GitHub
    key is dropped even if it were added to the allowlist later, so a credential
    can never leak into a helper or a supervised child process.
    """
    env = {}
    for key in ALLOWED_ENV_KEYS:
        value = environ.get(key)
        if value is not None and not _is_secret(key):
            env[key] = value
    env["HOME"] = str(private_home)
    return env


class child_environment:
    """Replace ``os.environ`` with the allowlist for the whole run; restore after.

    The accepted `_git`/`_cargo_version` helpers build subprocesses without an
    explicit environment, so they inherit ``os.environ``.  Merely computing a
    filtered dict is not enough: the process environment itself must be cleared
    for all bootstrap/preparation/stages and put back in ``finally``.
    """

    def __init__(self, environ, private_home):
        self.child = filtered_environment(environ, private_home)
        self.saved = None

    def __enter__(self):
        self.saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.child)
        return self.child

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)
        return False


def check_prerequisite(host):
    """Check the accepted bounded-operation and stop-receipt APIs."""
    missing = []
    if not callable(getattr(host, "operation_deadline", None)):
        missing.append("operation_deadline")
    if not callable(getattr(host, "record_build_attempt", None)):
        missing.append("record_build_attempt")
    run_supervised = getattr(host, "run_supervised", None)
    if not callable(run_supervised):
        missing.append("run_supervised")
    else:
        try:
            params = inspect.signature(run_supervised).parameters
        except (TypeError, ValueError):
            params = {}
        if "outcome" not in params:
            missing.append("run_supervised(outcome=...)")
    return missing


class scoped_operation_deadline:
    """Keep nested accepted-host calls on the one captured operation deadline."""

    def __init__(self, host, deadline):
        self.host = host
        self.deadline = deadline
        self.original = None

    def __enter__(self):
        self.original = self.host.operation_deadline
        self.host.operation_deadline = lambda *_args, **_kwargs: self.deadline
        return self.deadline

    def __exit__(self, *exc):
        if self.original is not None:
            self.host.operation_deadline = self.original
        return False


def test_patch_targets(patch_text):
    """The target paths a patch touches, from its unified-diff headers."""
    targets = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            targets.add(line[4:].split("\t")[0].strip())
    return targets


def verify_test_file_scope(patch_text, name):
    """Refuse a public test patch that touches anything but ``*_tests.rs``."""
    for path in test_patch_targets(patch_text):
        normalized = path[2:] if path.startswith("b/") else path
        if not normalized.endswith("_tests.rs"):
            raise Refusal("patch %s touches non-test path %s; refusing" % (name, normalized))
    return True


def verify_patch_digest(patch_path, expected_sha, name):
    actual = sha256_file(patch_path)
    if actual != expected_sha:
        raise Refusal("patch %s digest %s != %s; refusing" % (name, actual, expected_sha))
    return actual


def verify_target_digests(build_dir, targets):
    """Verify each declared target file digest inside the private build copy."""
    for rel, expected in targets.items():
        path = Path(build_dir) / rel
        if not path.is_file():
            raise Refusal("expected target %s is missing; refusing" % rel)
        actual = sha256_file(path)
        if actual != expected:
            raise Refusal("target %s digest %s != %s; refusing" % (rel, actual, expected))
    return True


def parse_cargo_test_output(text):
    """Parse cargo test output into results + summary; detect truncation."""
    results = {}
    duplicates = []
    summary = None
    for raw in text.splitlines():
        line = raw.strip()
        match = _RESULT_RE.match(line)
        if match:
            name, outcome = match.group(1), match.group(2)
            if name in results:
                duplicates.append(name)
            else:
                results[name] = outcome
            continue
        match = _SUMMARY_RE.match(line)
        if match:
            summary = {
                "verdict": match.group(1),
                "passed": int(match.group(2)),
                "failed": int(match.group(3)),
                "ignored": int(match.group(4)),
                "measured": int(match.group(5)),
                "filtered_out": int(match.group(6)),
                "seconds": float(match.group(7)),
            }
    return {"results": results, "duplicates": duplicates, "summary": summary,
            "truncated": summary is None}


def evaluate_qualification(parsed, expected=EXPECTED_TESTS):
    """Require all nineteen selected names to pass, and the summary to agree.

    Missing, duplicate, unexpected qualification-prefixed, non-``ok``, empty,
    truncated or contradictory (``0 passed`` / summary disagreeing with the
    selected set) results all fail even when the process exited 0.  Extra
    unrelated results are ignored.
    """
    errors = []
    results = parsed["results"]
    if parsed["truncated"]:
        errors.append("truncated output: no 'test result:' summary line")
    if not results:
        errors.append("empty results: no test outcome lines parsed")
    for name in sorted(set(parsed["duplicates"])):
        errors.append("duplicate result for %s" % name)
    for name in sorted(results):
        if name not in expected and TEST_FILTER in name:
            errors.append("unexpected qualification-prefixed test: %s" % name)
    for name in sorted(expected):
        outcome = results.get(name)
        if outcome is None:
            errors.append("missing result for %s" % name)
        elif outcome != "ok":
            errors.append("unexpected outcome for %s: %s" % (name, outcome))
    observed = sorted(name for name, outcome in results.items()
                      if name in expected and outcome == "ok")
    summary = parsed["summary"]
    if summary is not None:
        if summary["verdict"] != "ok":
            errors.append("summary verdict is %s" % summary["verdict"])
        if summary["failed"]:
            errors.append("summary reports %d failed" % summary["failed"])
        if summary["ignored"]:
            errors.append("summary reports %d ignored/skipped" % summary["ignored"])
        if summary["passed"] != len(expected):
            errors.append("summary reports %d passed, expected %d"
                          % (summary["passed"], len(expected)))
    return {"ok": not errors, "errors": errors, "observed_passed": observed}


# --- bounded, sanitized failure diagnostics ---------------------------------
# On failure the artifact stays receipt-only, so the failing stage's compiler
# text is otherwise unrecoverable from CI (the hosted runs returned only
# ``exit 101``).  A strictly bounded, redacted excerpt of *diagnostic-shaped*
# lines is therefore carried in the failure receipt: only lines that look like
# rustc/cargo diagnostics are kept, absolute paths / ``$HOME`` / environment
# values are redacted, and the line and character counts are hard-capped.
DIAGNOSTIC_MAX_LINES = 40
DIAGNOSTIC_MAX_CHARS = 4000
DIAGNOSTIC_TAIL_BYTES = 256 * 1024  # read at most this much of the stage log
DIAGNOSTIC_LINE_RE = re.compile(
    r"^(?:error\b|error\[[0-9A-Za-z]+\]|warning: unused|-->)")
# Only an *absolute* path is redacted: the leading ``/`` must not follow a
# path-ish character, so a relative ``core/src/...`` path is preserved verbatim
# while ``/home/runner/...`` (after the line start or any separator) is not.
DIAGNOSTIC_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.\-])/[^\s:`'\"()\[\],]*")
DIAGNOSTIC_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*")
DIAGNOSTIC_ENV_VALUE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=\S+")
TEST_FAILURE_HEADER_RE = re.compile(r"^---- ([A-Za-z0-9_:]+) stdout ----$")
TEST_PANIC_LOCATION_RE = re.compile(
    r"^thread '([A-Za-z0-9_:]+)' panicked at (.+):(\d+):(\d+):$")


def _redact_diagnostic_line(line):
    """Redact absolute paths, URLs, ``$HOME`` and ``KEY=value`` environment values."""
    line = DIAGNOSTIC_URL_RE.sub("<url>", line)
    line = line.replace("$HOME", "<home>")
    line = DIAGNOSTIC_ABS_PATH_RE.sub("<path>", line)
    return DIAGNOSTIC_ENV_VALUE_RE.sub(r"\1=<redacted>", line)


def sanitize_compiler_excerpt(text, max_lines=DIAGNOSTIC_MAX_LINES,
                              max_chars=DIAGNOSTIC_MAX_CHARS):
    """A bounded, redacted excerpt of compiler diagnostics, else ``None``.

    Only diagnostic-shaped lines are kept (``error``, ``error[E...]``, ``-->``,
    ``warning: unused``); the kept lines are redacted and then hard-capped to
    ``max_lines`` lines and ``max_chars`` characters.  ``truncated`` records that
    a cap dropped further lines.  Nothing is invented: a log with no
    diagnostic-shaped line yields ``None``.
    """
    kept = []
    truncated = False
    for raw in text.splitlines():
        line = raw.strip()
        if not DIAGNOSTIC_LINE_RE.match(line):
            continue
        if len(kept) >= max_lines:
            truncated = True
            break
        kept.append(_redact_diagnostic_line(line))
    if not kept:
        return None
    lines = []
    used = 0
    for line in kept:
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:  # one line already over the character cap: hard-truncate it
        lines = [kept[0][:max_chars]]
        truncated = True
    return {"lines": lines, "truncated": truncated}


def sanitize_offline_test_excerpt(text, max_lines=DIAGNOSTIC_MAX_LINES,
                                  max_chars=DIAGNOSTIC_MAX_CHARS):
    """Keep bounded test-failure structure without arbitrary test stdout.

    Failure receipts need the failed test and summary, but test stdout can
    contain arbitrary fixture data. Retain only harness result/summary lines,
    failure headings, and panic source locations; drop assertion payloads.
    """
    kept = []
    truncated = False
    for raw in text.splitlines():
        line = raw.strip()
        result = _RESULT_RE.match(line)
        summary = _SUMMARY_RE.match(line)
        header = TEST_FAILURE_HEADER_RE.match(line)
        panic = TEST_PANIC_LOCATION_RE.match(line)
        if result and result.group(2) == "FAILED":
            safe_line = line
        elif summary:
            safe_line = line
        elif line == "failures:" or header:
            safe_line = line
        elif panic:
            path = panic.group(2).replace("\\", "/")
            marker = "/codex-rs/"
            relative = path[path.rfind(marker) + 1:] if marker in path else Path(path).name
            safe_line = "thread '%s' panicked at %s:%s:%s:" % (
                panic.group(1), relative, panic.group(3), panic.group(4))
        elif line == "error: test failed, to rerun pass `-p codex-core --lib`":
            safe_line = line
        else:
            continue
        if len(kept) >= max_lines:
            truncated = True
            break
        kept.append(_redact_diagnostic_line(safe_line))
    if not kept:
        return None
    lines = []
    used = 0
    for line in kept:
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        lines = [kept[0][:max_chars]]
        truncated = True
    return {"lines": lines, "truncated": truncated}


def _stage_diagnostic(log_path, stage=None):
    """Bounded sanitized diagnostics from a failing stage log, else ``None``.

    At most ``DIAGNOSTIC_TAIL_BYTES`` are read from the end of the log; a
    missing, unreadable or diagnostic-free log yields ``None`` rather than any
    fabricated text.
    """
    if log_path is None:
        return None
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - DIAGNOSTIC_TAIL_BYTES))
            tail = handle.read(DIAGNOSTIC_TAIL_BYTES)
    except OSError:
        return None
    text = tail.decode("utf-8", errors="replace")
    if stage == "offline-tests":
        return sanitize_offline_test_excerpt(text)
    return sanitize_compiler_excerpt(text)


def _sanitized_failure(wrapper_stage, outcome, reason, diagnostic=None):
    """Curated failure evidence from the real supervisor outcome; no exception text."""
    return {
        "stage": wrapper_stage,
        "scope": outcome.get("stage"),
        "reason": reason,
        "stop_reason": outcome.get("stop_reason"),
        "available_ram_bytes": outcome.get("available_ram_bytes"),
        "ram_floor_bytes": outcome.get("ram_floor_bytes"),
        "compiler_child_status": outcome.get("compiler_child_status"),
        "compiler_cleanup": outcome.get("compiler_cleanup"),
        "diagnostic": diagnostic,
    }


def build_receipt(identity, deadline, timing, resources, inputs, test_stage,
                  qualified, failure=None):
    """A sanitized receipt: no argv, no environment, no secrets, no full logs."""
    return {
        "task": "G05-native-ci-build",
        "schema": 2,
        "qualified": bool(qualified),
        "native_admission": False,
        "guard_unchanged": True,
        "github": identity,
        "deadline": deadline,
        "timing": timing,
        "resources": resources,
        "build_inputs": inputs,
        "test_stage": test_stage,
        "failure": failure,
        "note": ("compiled qualification evidence only; not runtime isolation, not "
                 "native admission and not a model/toolset qualification"),
    }


# --- artifact publication (fresh, atomic, allowlisted) ----------------------
ARTIFACT_ALLOWED = frozenset({RECEIPT_NAME, "LICENSE", "NOTICE"})


def _validate_existing_artifact(path, allowed):
    """Refuse a pre-existing artifact directory with unknown entries or symlinks."""
    if path.is_symlink():
        raise Refusal("artifact directory is a symlink; refusing")
    if not path.exists():
        return
    if not path.is_dir():
        raise Refusal("artifact path is not a directory; refusing")
    for entry in path.iterdir():
        if entry.is_symlink():
            raise Refusal("unexpected symlink in artifact directory: %s" % entry.name)
        if entry.name not in allowed:
            raise Refusal("unexpected artifact entry: %s" % entry.name)


def _staging_dir(parent):
    """A fresh, dedicated staging directory beside (never inside) the artifact."""
    staging = parent / (".artifact-staging-%d" % os.getpid())
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    return staging


def _mandatory_success_sources(host):
    """The four mandatory success files: binary, receipt, LICENSE and NOTICE."""
    return [
        (Path(host.target_dir(host.ROOT)) / "release" / host.BIN, host.BIN),
        (PATCH_DIR / "LICENSE", "LICENSE"),
        (PATCH_DIR / "NOTICE", "NOTICE"),
    ]


def _fill_staging(staging, receipt, qualified, host):
    """Write the receipt and (on success) every mandatory file into staging."""
    (staging / RECEIPT_NAME).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    published = [RECEIPT_NAME]
    if not qualified:
        return published
    for source, name in _mandatory_success_sources(host):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise Refusal("required success artifact %s is missing; refusing" % name)
        shutil.copy2(source, staging / name)
        published.append(name)
    for name in (host.BIN, RECEIPT_NAME, "LICENSE", "NOTICE"):
        target = staging / name
        if target.is_symlink() or not target.is_file():
            raise Refusal("mandatory artifact %s missing; refusing" % name)
    return published


def _replace_artifact(staging, artifact_dir):
    """Atomically replace the owned artifact directory; never touch anything else."""
    if artifact_dir.is_symlink():
        raise Refusal("artifact directory is a symlink; refusing")
    if artifact_dir.exists():
        if not artifact_dir.is_dir():
            raise Refusal("artifact path is not a directory; refusing")
        shutil.rmtree(artifact_dir)
    os.replace(staging, artifact_dir)


def _fallback_receipt(receipt, error):
    """A truthful receipt-only publication after a failed mandatory-file copy."""
    out = dict(receipt)
    out["qualified"] = False
    out["publication_failure"] = (str(error) if isinstance(error, Refusal)
                                  else "artifact publication error")
    return out


def publish_artifact(receipt, qualified, host, artifact_dir=None):
    """Publish a fresh, allowlisted artifact directory by an atomic replace.

    A failure publishes only the truthful receipt and never a stale or partial
    binary; a success must publish all four mandatory files (binary, receipt,
    LICENSE, NOTICE) or it fails.  After *any* publication failure the owned
    upload directory is replaced with a receipt-only directory so no stale or
    partial binary can ever be uploaded.  Only the wrapper's own artifact
    directory and its own staging directory are ever removed.
    """
    artifact_dir = Path(ARTIFACT_DIR if artifact_dir is None else artifact_dir)
    allowed = set(ARTIFACT_ALLOWED) | {host.BIN}
    parent = artifact_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    _validate_existing_artifact(artifact_dir, allowed)
    staging = _staging_dir(parent)
    try:
        published = _fill_staging(staging, receipt, qualified, host)
    except BaseException as error:
        shutil.rmtree(staging, ignore_errors=True)
        # The upload directory must not keep a stale binary from an earlier run.
        fallback = _staging_dir(parent)
        try:
            (fallback / RECEIPT_NAME).write_text(
                json.dumps(_fallback_receipt(receipt, error), indent=2, sort_keys=True) + "\n")
            _replace_artifact(fallback, artifact_dir)
        except BaseException:
            shutil.rmtree(fallback, ignore_errors=True)
        raise
    try:
        _replace_artifact(staging, artifact_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return [artifact_dir / name for name in published]


# --- heavy orchestration (never run at authoring time) ----------------------
def _load_host(path=HOST_PATH):
    spec = importlib.util.spec_from_file_location("native_host_ci_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enter_stage(outcome, stage, log_path=None):
    """Reset the caller-owned supervisor outcome to the truthful current stage.

    Every stage boundary clears the dict so a failure never reuses the previous
    stage's curated fields (a stale ``compiler_cleanup`` from a completed build
    must never be attributed to a later stage).  It also returns the log path
    this stage may write — ``None`` for a stage that has no log of its own — and
    every boundary assigns that return value, so a failure can never present an
    earlier stage's log as its own diagnostic evidence.
    """
    outcome.clear()
    outcome["stage"] = stage
    return log_path


def _supervised(host, argv, cwd, env, log_path, deadline, outcome):
    """Supervise one stage; the caller supplies the truthful ``outcome['stage']``.

    The accepted ``run_supervised`` populates the curated stop reason, measured
    RAM and compiler status/cleanup; the wrapper never invents any of them.
    """
    return host.run_supervised(argv, cwd=cwd, env=env, log_path=log_path,
                               stop_at=deadline, ram_floor_bytes=host.RAM_MIN_AVAILABLE_BYTES,
                               outcome=outcome)


def _profile_digest(host):
    """A stable digest of the accepted release-profile contract."""
    return hashlib.sha256(
        json.dumps(host.build_profile_contract(), sort_keys=True).encode()).hexdigest()


def _preservation_snapshot(host, build, binary):
    """Exact digests of production inputs/complements captured before the tests."""
    lock = build / "codex-rs" / "Cargo.lock"
    return {
        "patch": sha256_file(PATCH_DIR / PRODUCTION_PATCH["file"]),
        "target": sha256_file(build / "codex-rs" / "core" / "src" / "thread_manager.rs"),
        "lock": sha256_file(lock),
        "provenance": sha256_file(host.provenance_path(host.ROOT)),
        "profile": _profile_digest(host),
        "binary": sha256_file(binary),
    }


def _verify_preservation(host, build, binary, before):
    """Recheck the production source, lock, provenance, profile and binary.

    Every value is compared against the exact digest captured before the test
    compile/run; a change to any one of them (not only the binary) refuses.
    """
    checks = {
        "patch": sha256_file(PATCH_DIR / PRODUCTION_PATCH["file"]),
        "target": sha256_file(build / "codex-rs" / "core" / "src" / "thread_manager.rs"),
        "lock": sha256_file(build / "codex-rs" / "Cargo.lock"),
        "provenance": sha256_file(host.provenance_path(host.ROOT)),
        "profile": _profile_digest(host),
        "binary": sha256_file(binary),
    }
    for key, value in checks.items():
        if value != before[key]:
            raise Refusal("production %s changed across the test stage; refusing" % key)
    return checks


def run_qualification(host, caller_limit_seconds=CALLER_LIMIT_SECONDS, environ=None):
    """Drive the bounded qualification and return (qualified, receipt, reason).

    Deterministically refuses before heavy setup when the stop-receipt
    prerequisite is absent.  On refusal or failure no qualified binary is
    reported and the receipt is truthful.
    """
    environ = os.environ if environ is None else environ
    identity = capture_github_identity(environ)
    started = dt.datetime.now(dt.timezone.utc)
    deadline = None
    wrapper_stage = "deadline"
    outcome = {"stage": wrapper_stage}
    # The supervised stage currently running; its log is the only source for the
    # bounded diagnostic excerpt in the failure receipt.
    stage_log = None

    missing = check_prerequisite(host)
    if missing:
        refusal = ("stop-receipt prerequisite missing from accepted source: %s"
                   % ", ".join(missing))
        receipt = build_receipt(identity, None, {"seconds": 0.0}, None,
                                {"source_pin": getattr(host, "CODEX_PIN", None)}, None,
                                False, {"stage": "prerequisite", "reason": refusal})
        return False, receipt, refusal

    try:
        deadline = host.operation_deadline(caller_limit_seconds)
        private_root = host.private_root(host.ROOT)
        home = host.home_dir(host.ROOT)
        log_dir = host.logs_dir(host.ROOT)
        build_log = log_dir / "build.log"
        prep_log = log_dir / "test-prep.log"
        test_log = log_dir / "qualification-test.log"
        # The filtered environment is *applied* (not merely computed) for all
        # bootstrap, preparation and staged work, and restored in finally, so the
        # accepted helpers that build subprocesses without an explicit env (``_git``,
        # ``_cargo_version``, ``ensure_source``, ``prepare_build``, ...) all see the
        # allowlist with a private HOME and no inherited credentials.
        with child_environment(environ, home):
            with scoped_operation_deadline(host, deadline):
                wrapper_stage = "preflight"
                stage_log = _enter_stage(outcome, wrapper_stage)
                # A fresh checkout has no private build root yet, and the disk/
                # RAM preflight must not fail on the absent directory itself.
                # Creating the root is idempotent and precedes any heavy work.
                private_root.mkdir(parents=True, exist_ok=True)
                resources = host.preflight_resources(private_root)
                wrapper_stage = "source"
                stage_log = _enter_stage(outcome, wrapper_stage)
                host.ensure_source(host.ROOT, deadline=deadline)
                wrapper_stage = "toolchain"
                stage_log = _enter_stage(outcome, wrapper_stage)
                cargo = host.ensure_toolchain(host.ROOT, deadline=deadline)
                wrapper_stage = "prepare"
                stage_log = _enter_stage(outcome, wrapper_stage)
                build, patch, digest = host.prepare_build(host.ROOT, deadline=deadline)
                verify_patch_digest(PATCH_DIR / PRODUCTION_PATCH["file"],
                                    PRODUCTION_PATCH["sha256"], "text-only.patch")
                verify_target_digests(build, PRODUCTION_PATCH["targets"])
                build_env = host.build_env(host.ROOT)
                # The native command/build stage: the accepted compile scope is the
                # only stage whose receipt carries the compiler child status/cleanup.
                wrapper_stage = "build"
                stage_log = _enter_stage(outcome, COMPILER_SCOPE, build_log)
                code = _supervised(host, host.build_argv(cargo), build / "codex-rs",
                                   build_env, build_log, deadline, outcome)
                if code != 0:
                    raise Refusal("production build failed (exit %d)" % code)
                lock = build / "codex-rs" / "Cargo.lock"
                if sha256_file(lock) != host.LOCK_BUILD_SHA256:
                    raise Refusal("production lockfile digest changed; refusing")
                binary = host.target_dir(host.ROOT) / "release" / host.BIN
                if not binary.is_file():
                    raise Refusal("production build produced no %s binary" % host.BIN)

                wrapper_stage = "test-patches"
                stage_log = _enter_stage(outcome, wrapper_stage)
                applied = []
                for entry in TEST_PATCH_MANIFEST:
                    patch_path = PATCH_DIR / entry["file"]
                    text = patch_path.read_text()
                    verify_patch_digest(patch_path, entry["sha256"], entry["file"])
                    verify_test_file_scope(text, entry["file"])
                    apply = host._git(["apply", str(patch_path)], cwd=str(build),
                                      timeout=host.time_budget(deadline, 120))
                    if apply.returncode != 0:
                        raise Refusal("public test patch %s failed to apply" % entry["file"])
                    verify_target_digests(build, entry["targets"])
                    applied.append({"file": entry["file"], "sha256": entry["sha256"]})
                # The production patch/target must survive a test-file-only change.
                verify_patch_digest(PATCH_DIR / PRODUCTION_PATCH["file"],
                                    PRODUCTION_PATCH["sha256"], "text-only.patch")
                verify_target_digests(build, PRODUCTION_PATCH["targets"])
                # Snapshot every production input/complement the test stage must not
                # perturb: source, lock, provenance, profile and the binary.
                before = _preservation_snapshot(host, build, binary)

                job = host.JOBS if isinstance(getattr(host, "JOBS", None), int) else 1
                cargo_bin = str(cargo)
                prep_argv = ["nice", "-n", "19", cargo_bin, "test", "--release",
                             "--locked", "--jobs", str(job), "--no-run",
                             "-p", "codex-core", "--lib"]
                test_argv = ["nice", "-n", "19", cargo_bin, "test", "--release",
                             "--locked", "--offline", "--jobs", str(job),
                             "-p", "codex-core", "--lib", TEST_FILTER,
                             "--", "--test-threads=1"]
                wrapper_stage = "prepare-tests"
                stage_log = _enter_stage(outcome, wrapper_stage, prep_log)
                code = _supervised(host, prep_argv, build / "codex-rs", build_env,
                                   prep_log, deadline, outcome)
                if code != 0:
                    raise Refusal("locked online test preparation failed (exit %d)" % code)
                wrapper_stage = "offline-tests"
                stage_log = _enter_stage(outcome, wrapper_stage, test_log)
                code = _supervised(host, test_argv, build / "codex-rs", build_env,
                                   test_log, deadline, outcome)
                parsed = parse_cargo_test_output(test_log.read_text(errors="replace"))
                evaluated = evaluate_qualification(parsed)
                if not evaluated["ok"]:
                    raise Refusal("qualification results rejected: %s"
                                  % "; ".join(evaluated["errors"]))
                if code != 0:
                    raise Refusal("offline qualification exited %d with passing results" % code)
                _verify_preservation(host, build, binary, before)

                elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                inputs = {"source_pin": host.CODEX_PIN, "patch": digest,
                          "lock_sha256": host.LOCK_BUILD_SHA256,
                          "toolchain": host.RUST_VERSION,
                          "profile": host.build_profile_contract(),
                          "binary": {"path": str(binary), "sha256": before["binary"]}}
                test_stage = {"patches": applied,
                              "expected": sorted(EXPECTED_TESTS),
                              "observed_passed": evaluated["observed_passed"],
                              "results": parsed["results"], "summary": parsed["summary"]}
                receipt = build_receipt(identity, deadline, {"seconds": elapsed},
                                        resources, inputs, test_stage, True, None)
                return True, receipt, None
    except (Refusal, host.SetupError, OSError) as error:
        elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        if isinstance(error, OSError) and not isinstance(error, (Refusal, host.SetupError)):
            reason = "os error during %s" % wrapper_stage  # never the exception text
            code = 1
        else:
            reason = str(error)
            code = getattr(error, "code", 1)
        # Persist the *actual* curated supervisor outcome via the accepted API:
        # its real stage/scope, stop reason and measured RAM.  The build stage keeps
        # the compiler scope; any other stage is recorded under its own wrapper name
        # so the helper reports the compiler scope as ``not_applicable`` instead of
        # inventing a child-cleanup result.
        try:
            host.record_build_attempt(host.ROOT, deadline, outcome,
                                      outcome.get("code") or code)
        except Exception:
            pass
        failure = _sanitized_failure(wrapper_stage, outcome, reason,
                                     _stage_diagnostic(stage_log, wrapper_stage))
        receipt = build_receipt(identity, deadline, {"seconds": elapsed}, None,
                                {"source_pin": getattr(host, "CODEX_PIN", None)}, None,
                                False, failure)
        return False, receipt, reason


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--caller-limit-seconds", type=int, default=CALLER_LIMIT_SECONDS,
                        choices=range(MIN_CALLER_LIMIT_SECONDS,
                                      MAX_CALLER_LIMIT_SECONDS + 1),
                        metavar="[6-7200]")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        host = _load_host()
    except Exception as error:  # the accepted source must load before anything else
        print("native-host-ci: cannot load accepted native-host.py: %s" % error,
              file=sys.stderr)
        return 1
    qualified, receipt, reason = run_qualification(host, args.caller_limit_seconds)
    try:
        publish_artifact(receipt, qualified, host)
    except Refusal as refusal:
        print("native-host-ci: artifact publication refused: %s" % refusal, file=sys.stderr)
        return 1
    print(json.dumps({"qualified": qualified, "reason": reason}, indent=2))
    return 0 if qualified else 1


def entry():  # pragma: no cover - thin process boundary
    try:
        return main()
    except Refusal as refusal:
        print("native-host-ci: %s" % refusal, file=sys.stderr)
        return refusal.code


if __name__ == "__main__":
    sys.exit(entry())
