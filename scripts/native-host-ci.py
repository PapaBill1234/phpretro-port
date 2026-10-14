#!/usr/bin/env python3
"""Bounded push-only CI wrapper for the Eliza native text-host qualification.

This wrapper turns the source-only startup/registry/adversarial test patches into
a single gated CI job.  It does **not** re-implement any native-host logic: it
loads the accepted, reviewed `scripts/native-host.py` and drives its public
functions under one captured deadline.  At authoring time the heavy job is
**not launched**: the stop-receipt prerequisite is a separate, accepted-but-not-
yet-integrated fix, so the wrapper refuses before any heavy setup until the
reviewed `record_build_attempt` API is present in the accepted source.

Contract enforced here
----------------------
* Push-only, no `pull_request`/`pull_request_target`; the hosted workflow is the
  only caller and the root is the fixed checkout (no arbitrary CLI paths).
* GitHub identity (repo/ref/sha/run/attempt only) is captured from the
  environment *before* the parent environment is cleared to an allowlist with a
  private HOME.  Tokens, provider keys and `GITHUB_TOKEN` are never forwarded.
* The stop-receipt prerequisite (`record_build_attempt`, and `run_supervised`
  accepting an `outcome`) is checked mechanically before heavy setup; absence is
  a refusal.
* One deadline = min(canonical off-peak cutoff, caller limit) is captured once
  and passed to every stage.  Any gate override is restored in `finally`.
* Production build first (unchanged source, locked repair, accepted profile),
  then **only** the three public test patches in order, with patch digests and
  expected patched target digests from the reviewed docs and a test-file-only
  scope; production patch/lock/binary/provenance digests are rechecked.
* Bounded locked preparation (`--no-run`) then the offline qualification command,
  both under the same cutoff, RAM floor and filtered release environment.
* Exactly the fifteen selected test names must complete: 15 passed and zero
  failed/ignored/skipped.  Missing, duplicate, unexpected, empty or truncated
  results fail qualification even on exit 0.  Extra unrelated results are
  ignored.
* The artifact is an allowlist: compiled binary + sanitized receipt +
  LICENSE/NOTICE only when every check completed.  A failure ships a truthful
  receipt and no qualified binary.
"""
import argparse
import datetime as dt
import hashlib
import inspect
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the fixed checkout root; never a CLI argument
HOST_PATH = HERE / "native-host.py"
PATCH_DIR = ROOT / "builder" / "eliza" / "native-host"

CALLER_LIMIT_SECONDS = 3600  # CI caller budget; job timeout is 90 minutes
MIN_FUTURE_MARGIN = 5  # seconds of slack required on the captured deadline
ARTIFACT_DIR = ROOT / ".builder" / "native-host-ci" / "artifact"
RECEIPT_NAME = "qualification-receipt.json"

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
                  "APIKEY", "PRIVATE_KEY", "COOKIE", "SESSION", "AUTH", "OPENAI",
                  "ANTHROPIC", "DEEPSEEK", "GITHUB", "AWS", "GH_")

# --- production and public-test patch manifests -----------------------------
# Digests and expected patched-target digests are taken from the reviewed docs
# (provenance.json, qualification-tests.md, qualification-registry.md,
# qualification-adversarial.md); truncated doc prefixes were reconciled against
# the pinned source so every comparison is exact.
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
        "sha256": "21a127fac8bad33aa71dddea6def0b58196303c6b7aba5586e9f73843c8d0408",
        "targets": {
            "codex-rs/core/src/tools/spec_plan_tests.rs":
                "cef566b0c85bc19b909e6ee14997b9f5d24f117a7e023beaf48ff59fc28be09c",
            "codex-rs/core/src/tools/registry_tests.rs":
                "6d1621dcdc8fe1a11003c5c3a99bf1619c032c60ffca443afbace0de5d28c2b7",
        },
    },
    {
        "file": "qualification-adversarial.patch",
        "sha256": "952355fb2a0fda2bf10f0479c53bb607e14f70c9d6fdb805f89f45d7fccb5edc",
        "targets": {
            "codex-rs/core/src/thread_manager_tests.rs":
                "13b3e91819a20cdca3ac33b3b69d01bf8554de51c835d90984ebc1d8f52618aa",
            "codex-rs/core/src/tools/spec_plan_tests.rs":
                "569d21b9380c23384a056190157971d41bfef6c060db1e4231d3a0de4bbd3dc4",
            "codex-rs/core/src/tools/registry_tests.rs":
                "ffb81085ada72230327724ca40c5c990184ab2f7e35853825833e68a98570857",
        },
    },
)

# --- the fifteen selected test names (gathered from the three patches) -------
EXPECTED_TESTS = frozenset({
    "native_text_host_qualification_captured_policy_denies_dispatch",
    "native_text_host_qualification_captured_policy_rejects_registration",
    "native_text_host_qualification_cold_resume_captures_empty_ceiling",
    "native_text_host_qualification_cold_resume_constructor_matrix_captures_ceiling",
    "native_text_host_qualification_internal_guardian_source_is_covered",
    "native_text_host_qualification_legacy_fork_is_covered",
    "native_text_host_qualification_legacy_history_mode_is_covered",
    "native_text_host_qualification_manager_started_code_mode_only_registry_is_empty",
    "native_text_host_qualification_manager_started_code_mode_registry_is_empty",
    "native_text_host_qualification_manager_started_registry_is_empty",
    "native_text_host_qualification_manager_started_registry_rejects_dynamic_tools",
    "native_text_host_qualification_new_thread_without_init_captures_empty_ceiling",
    "native_text_host_qualification_overwrites_conflicting_caller_init",
    "native_text_host_qualification_overwrites_unrestricted_caller_init",
    "native_text_host_qualification_warm_resume_keeps_live_session_and_ceiling",
})
TEST_FILTER = "native_text_host_qualification"

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
    can never leak into a supervised child process.
    """
    env = {}
    for key in ALLOWED_ENV_KEYS:
        value = environ.get(key)
        if value is not None and not _is_secret(key):
            env[key] = value
    env["HOME"] = str(private_home)
    return env


def parse_iso_epoch(value):
    """Strict, timezone-aware, finite ISO cutoff epoch; else refuse."""
    if not isinstance(value, str) or not value.strip():
        raise Refusal("missing canonical cutoff; refusing")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise Refusal("malformed canonical cutoff; refusing")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Refusal("naive canonical cutoff; refusing")
    return parsed.timestamp()


def capture_deadline(canonical, caller_limit_seconds=CALLER_LIMIT_SECONDS, now=None):
    """The single immutable deadline: min(canonical cutoff, now + caller limit)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    canonical_epoch = parse_iso_epoch(canonical)
    caller_epoch = now.timestamp() + caller_limit_seconds
    deadline = min(canonical_epoch, caller_epoch)
    if deadline - now.timestamp() <= MIN_FUTURE_MARGIN:
        raise Refusal("captured deadline is not in the future; refusing", code=75)
    return dt.datetime.fromtimestamp(deadline, dt.timezone.utc).isoformat()


def check_prerequisite(host):
    """Mechanical, read-only check that the accepted stop-receipt API exists."""
    missing = []
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
    """Require all expected names to pass exactly once, with no failure/ignored.

    Missing, duplicate, unexpected-outcome, empty or truncated results fail even
    when the process exited 0.  Extra unrelated results are ignored.
    """
    errors = []
    if parsed["truncated"]:
        errors.append("truncated output: no 'test result:' summary line")
    if not parsed["results"]:
        errors.append("empty results: no test outcome lines parsed")
    for name in sorted(set(parsed["duplicates"])):
        errors.append("duplicate result for %s" % name)
    for name in sorted(expected):
        outcome = parsed["results"].get(name)
        if outcome is None:
            errors.append("missing result for %s" % name)
        elif outcome != "ok":
            errors.append("unexpected outcome for %s: %s" % (name, outcome))
    summary = parsed["summary"]
    if summary is not None and summary["failed"]:
        errors.append("summary reports %d failed" % summary["failed"])
    if summary is not None and summary["ignored"]:
        errors.append("summary reports %d ignored/skipped" % summary["ignored"])
    observed = sorted(name for name, outcome in parsed["results"].items()
                      if name in expected and outcome == "ok")
    return {"ok": not errors, "errors": errors, "observed_passed": observed}


def selection_artifact_entries(qualified, binary, receipt, license_file, notice_file):
    """Allowlist of uploadable files; a failure ships only the truthful receipt."""
    if not qualified:
        return [receipt]
    wanted = [binary, receipt, license_file, notice_file]
    return [path for path in wanted if Path(path).is_file()]


def build_receipt(identity, deadline, timing, resources, inputs, test_stage,
                  qualified, failure=None):
    """A sanitized receipt: no argv, no environment, no secrets, no full logs."""
    return {
        "task": "G05-native-ci-build",
        "schema": 1,
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


# --- gate override that is always restored ----------------------------------
class scoped_gate_cutoff:
    """Force one captured deadline onto the host gate, restoring it in finally."""

    def __init__(self, host, deadline):
        self.host = host
        self.deadline = deadline
        self.original = None

    def __enter__(self):
        self.original = self.host.gate_cutoff
        self.host.gate_cutoff = lambda: self.deadline
        return self.deadline

    def __exit__(self, *exc):
        if self.original is not None:
            self.host.gate_cutoff = self.original
        return False


# --- heavy orchestration (never run at authoring time) ----------------------
def _load_host(path=HOST_PATH):
    spec = importlib.util.spec_from_file_location("native_host_ci_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _supervised(host, argv, cwd, env, log_path, deadline, outcome, stage):
    outcome["stage"] = stage
    return host.run_supervised(argv, cwd=cwd, env=env, log_path=log_path,
                               stop_at=deadline, ram_floor_bytes=host.RAM_MIN_AVAILABLE_BYTES,
                               outcome=outcome)


def run_qualification(host, caller_limit_seconds=CALLER_LIMIT_SECONDS, environ=None):
    """Drive the bounded qualification and return (qualified, receipt_dict).

    Deterministically refuses before heavy setup when the stop-receipt
    prerequisite is absent.  On refusal or failure, no qualified binary is
    reported and the receipt is truthful.
    """
    environ = os.environ if environ is None else environ
    identity = capture_github_identity(environ)
    started = dt.datetime.now(dt.timezone.utc)
    deadline = None

    missing = check_prerequisite(host)
    if missing:
        refusal = ("stop-receipt prerequisite missing from accepted source: %s"
                   % ", ".join(missing))
        receipt = build_receipt(identity, None, {"seconds": 0.0}, None,
                                {"source_pin": getattr(host, "CODEX_PIN", None)}, None,
                                False, refusal)
        return False, receipt, refusal

    outcome = {"stage": "gate", "cleanup": "not_started"}
    receipt = None
    try:
        canonical = host.gate_cutoff()
        deadline = capture_deadline(canonical, caller_limit_seconds)
        private_root = host.private_root(host.ROOT)
        home = host.home_dir(host.ROOT)
        env = filtered_environment(environ, home)
        log_dir = host.logs_dir(host.ROOT)
        build_log = log_dir / "build.log"
        prep_log = log_dir / "test-prep.log"
        test_log = log_dir / "qualification-test.log"
        with scoped_gate_cutoff(host, deadline):
            resources = host.preflight_resources(private_root)
            host.ensure_source(host.ROOT, deadline=deadline)
            cargo = host.ensure_toolchain(host.ROOT, deadline=deadline)
            build, patch, digest = host.prepare_build(host.ROOT, deadline=deadline)
            verify_patch_digest(PATCH_DIR / PRODUCTION_PATCH["file"],
                                PRODUCTION_PATCH["sha256"], "text-only.patch")
            verify_target_digests(build, PRODUCTION_PATCH["targets"])
            build_env = host.build_env(host.ROOT)
            code = _supervised(host, host.build_argv(cargo), build / "codex-rs", build_env,
                               build_log, deadline, outcome, "build")
            if code != 0:
                raise Refusal("production build failed (exit %d)" % code)
            lock = build / "codex-rs" / "Cargo.lock"
            if sha256_file(lock) != host.LOCK_BUILD_SHA256:
                raise Refusal("production lockfile digest changed; refusing")
            binary = host.target_dir(host.ROOT) / "release" / host.BIN
            if not binary.is_file():
                raise Refusal("production build produced no %s binary" % host.BIN)
            binary_digest = sha256_file(binary)

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
            # Production digests must be preserved by a test-file-only change.
            verify_patch_digest(PATCH_DIR / PRODUCTION_PATCH["file"],
                                PRODUCTION_PATCH["sha256"], "text-only.patch")
            verify_target_digests(build, PRODUCTION_PATCH["targets"])
            if sha256_file(lock) != host.LOCK_BUILD_SHA256:
                raise Refusal("lockfile digest changed after test patches; refusing")

            job = host.JOBS if isinstance(getattr(host, "JOBS", None), int) else 1
            cargo_bin = str(cargo)
            prep_argv = ["nice", "-n", "19", cargo_bin, "test", "--release", "--locked",
                         "--jobs", str(job), "--no-run", "-p", "codex-core", "--lib"]
            test_argv = ["nice", "-n", "19", cargo_bin, "test", "--release", "--locked",
                         "--offline", "--jobs", str(job), "-p", "codex-core", "--lib",
                         TEST_FILTER, "--", "--test-threads=1"]
            code = _supervised(host, prep_argv, build / "codex-rs", build_env,
                               prep_log, deadline, outcome, "prepare-tests")
            if code != 0:
                raise Refusal("locked online test preparation failed (exit %d)" % code)
            code = _supervised(host, test_argv, build / "codex-rs", build_env,
                               test_log, deadline, outcome, "offline-tests")
            parsed = parse_cargo_test_output(test_log.read_text(errors="replace"))
            evaluated = evaluate_qualification(parsed)
            if not evaluated["ok"]:
                raise Refusal("qualification results rejected: %s"
                              % "; ".join(evaluated["errors"]))
            if code != 0:
                raise Refusal("offline qualification exited %d with passing results" % code)
            if sha256_file(binary) != binary_digest:
                raise Refusal("binary digest changed across the test stage; refusing")

            elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            inputs = {"source_pin": host.CODEX_PIN, "patch": digest,
                      "lock_sha256": host.LOCK_BUILD_SHA256,
                      "toolchain": host.RUST_VERSION,
                      "profile": host.build_profile_contract(),
                      "binary": {"path": str(binary), "sha256": binary_digest}}
            test_stage = {"patches": applied,
                          "expected": sorted(EXPECTED_TESTS),
                          "observed_passed": evaluated["observed_passed"],
                          "results": parsed["results"], "summary": parsed["summary"]}
            receipt = build_receipt(identity, deadline, {"seconds": elapsed}, resources,
                                    inputs, test_stage, True, None)
            return True, receipt, None
    except (Refusal, host.SetupError) as error:
        reason = str(error)
        elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        receipt = build_receipt(identity, deadline, {"seconds": elapsed}, None,
                                {"source_pin": getattr(host, "CODEX_PIN", None)}, None,
                                False, {"stage": outcome.get("stage"), "reason": reason})
        return False, receipt, reason


def write_artifact(receipt, qualified, host):
    """Write the sanitized receipt and copy the allowlisted files into it.

    The upload directory contains exactly the allowlist: the compiled binary,
    the receipt and LICENSE/NOTICE when every check completed, or only the
    truthful receipt on failure.  No home data and no full logs are copied.
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = ARTIFACT_DIR / RECEIPT_NAME
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    binary = host.target_dir(host.ROOT) / "release" / host.BIN
    selected = selection_artifact_entries(qualified, binary, receipt_path,
                                          PATCH_DIR / "LICENSE", PATCH_DIR / "NOTICE")
    import shutil
    for source in selected:
        source = Path(source)
        if source == receipt_path or not source.is_file():
            continue
        shutil.copy2(source, ARTIFACT_DIR / source.name)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--caller-limit-seconds", type=int, default=CALLER_LIMIT_SECONDS,
                        choices=range(1, 7201), metavar="[1-7200]")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        host = _load_host()
    except Exception as error:  # the accepted source must load before anything else
        print("native-host-ci: cannot load accepted native-host.py: %s" % error,
              file=sys.stderr)
        return 1
    qualified, receipt, reason = run_qualification(host, args.caller_limit_seconds)
    write_artifact(receipt, qualified, host)
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
