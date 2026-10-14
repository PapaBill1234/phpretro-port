"""Tests for the bounded, push-only native text-host CI wrapper.

The heavy Rust build and the real network are never exercised.  A locked fake
host records every API call so the wrapper's orchestration (single deadline,
filtered environment, ordered test patches, exact result checking, artifact
allowlist and truthful failure receipt) is verified with fixtures only.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CI_PATH = ROOT / "scripts" / "native-host-ci.py"
HOST_PATH = ROOT / "scripts" / "native-host.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "native-text-host.yml"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _load("native_host_ci", CI_PATH)
real_host = _load("native_host_real_for_ci", HOST_PATH)


def future_iso(seconds=3600):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def passing_output(names):
    lines = ["test %s ... ok" % name for name in sorted(names)]
    lines.append("test unrelated_helper ... ok")
    lines.append("test result: ok. %d passed; 0 failed; 0 ignored; 0 measured; "
                 "1 filtered out; finished in 12.50s" % len(names))
    return "\n".join(lines) + "\n"


class FakeSetupError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class FakeHost:
    """A locked stand-in that records calls and fabricates no real work."""

    ROOT = Path("/tmp/native-ci-fake-root")
    CODEX_PIN = "c1382380de69521303b416720a52f42d51af6248"
    BIN = "codex"
    JOBS = 1
    RUST_VERSION = "1.95.0"
    RAM_MIN_AVAILABLE_BYTES = 3 * 1024 ** 3
    LOCK_BUILD_SHA256 = "1f770c0e411d51770e6dd48e9f3f4eab847bbabc7949f82f3619414697d68b8d"
    SetupError = FakeSetupError

    def __init__(self, tmp, prereq=True, build_code=0, prep_code=0, test_code=0,
                 test_output=None, patch_dir=None, fail_apply=False, fail_source=None,
                 mutate_binary=False, build_log_text="", prep_log_text=""):
        self.tmp = Path(tmp)
        self.calls = []
        self.applied = []
        self._deadlines = []
        self._ram_floors = []
        self._environments = []
        self._helper_envs = []
        self._attempts = []
        self._prereq = prereq
        self._build_code = build_code
        self._prep_code = prep_code
        self._test_code = test_code
        self._test_output = test_output
        self._patch_dir = Path(patch_dir) if patch_dir else None
        self._apply_contents = {}
        self._fail_apply = fail_apply
        self._fail_source = fail_source
        self._mutate_binary = mutate_binary
        self._build_log_text = build_log_text
        self._prep_log_text = prep_log_text
        self._gate_calls = 0
        self._gate_cutoff_calls = 0

        self.ROOT = self.tmp / "root"
        self.ROOT.mkdir(parents=True, exist_ok=True)
        self._logs = self.tmp / "logs"
        self._target = self.tmp / "target"
        self._private = self.tmp / "private"
        self._private.mkdir(parents=True, exist_ok=True)
        self._home = self.tmp / "home"
        self._build = self.tmp / "build"
        self._build.mkdir(parents=True, exist_ok=True)
        self._cargo = self.tmp / "cargo"
        self._binary = self._target / "release" / self.BIN
        self._binary.parent.mkdir(parents=True, exist_ok=True)
        self._binary.write_bytes(b"fake-binary")
        self._provenance = self._private / "provenance.json"
        self._provenance.write_text("{\"pin\": \"%s\"}\n" % self.CODEX_PIN)

    # --- environment snapshots (fix: isolation is applied, not only computed) ---
    def _snapshot_env(self):
        self._helper_envs.append(dict(os.environ))

    def provenance_path(self, root):
        return self._provenance

    # --- attributes the wrapper uses ---
    def gate_cutoff(self):
        self._gate_cutoff_calls += 1
        return future_iso(7200)

    def private_root(self, root):
        return self._private

    def home_dir(self, root):
        return self._home

    def logs_dir(self, root):
        self._logs.mkdir(parents=True, exist_ok=True)
        return self._logs

    def target_dir(self, root):
        return self._target

    def build_profile_contract(self):
        return {"opt-level": "0", "lto": "false", "codegen-units": "256",
                "debug": "0", "incremental": "false"}

    def time_budget(self, deadline, cap):
        return cap

    def preflight_resources(self, path):
        self.calls.append(("preflight_resources", str(path)))
        return {"free_disk_bytes": 20 * 1024 ** 3, "available_ram_bytes": 8 * 1024 ** 3}

    def ensure_source(self, root, deadline=None):
        self.calls.append(("ensure_source", deadline))
        if self._fail_source is not None:
            raise self._fail_source
        self._snapshot_env()

    def ensure_toolchain(self, root, deadline=None):
        self.calls.append(("ensure_toolchain", deadline))
        self._snapshot_env()
        return self._cargo

    def prepare_build(self, root, deadline=None):
        self.calls.append(("prepare_build", deadline))
        self._snapshot_env()
        return self._build, self.tmp / "text-only.patch", "prod-patch-digest"

    def build_env(self, root):
        return {"CARGO_HOME": "x", "HOME": str(self._home)}

    def build_argv(self, cargo):
        return ["nice", "-n", "19", str(cargo), "build", "--release", "--locked",
                "-p", "codex-cli", "--bin", self.BIN, "--jobs", "1"]

    def _git(self, args, cwd=None, timeout=None):
        name = Path(args[-1]).name
        self.applied.append(name)
        self._snapshot_env()

        class Result:
            returncode = 0 if not self._fail_apply else 1
            stderr = ""
        if self._fail_apply:
            return Result()
        for rel, content in self._apply_contents.get(name, {}).items():
            path = Path(cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return Result()

    def run_supervised(self, argv, cwd, env, log_path, stop_at, max_log_bytes=None,
                       ram_floor_bytes=None, ram_probe=None, ram_interval=None, outcome=None):
        stage = (outcome or {}).get("stage")
        self.calls.append(("run_supervised", stage, stop_at, ram_floor_bytes))
        self._deadlines.append(stop_at)
        self._ram_floors.append(ram_floor_bytes)
        self._environments.append(dict(env))
        self._snapshot_env()
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        if stage == "offline-tests":
            Path(log_path).write_text(self._test_output or "")
            if self._mutate_binary:
                self._binary.write_bytes(b"mutated-after-tests")
            return self._test_code
        if stage == "prepare-tests":
            Path(log_path).write_text(self._prep_log_text)
            return self._prep_code
        Path(log_path).write_text(self._build_log_text)
        return self._build_code

    # --- stop-receipt prerequisite ---
    def record_build_attempt(self, root, deadline, outcome, code):
        self._attempts.append((root, deadline, dict(outcome), code))
        return True


def _fake_host_without_prereq(tmp):
    host = FakeHost(tmp)
    host.record_build_attempt = None
    host.run_supervised = None
    return host


def make_synthetic_patches(tmp):
    """Write production + test patches and a fully-patched build tree.

    Returns (patch_dir, prod_manifest, test_manifest, build_dir, lock_digest)
    with synthetic but self-consistent digests so the real verification logic is
    exercised.
    """
    patch_dir = Path(tmp) / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    build = Path(tmp) / "build"
    build.mkdir(parents=True, exist_ok=True)

    prod_rel = "codex-rs/core/src/thread_manager.rs"
    prod_content = b"// production\n"
    (build / prod_rel).parent.mkdir(parents=True, exist_ok=True)
    (build / prod_rel).write_bytes(prod_content)
    prod_patch_text = "--- a/%s\n+++ b/%s\n@@\n-x\n+y\n" % (prod_rel, prod_rel)
    (patch_dir / "text-only.patch").write_text(prod_patch_text)
    prod_manifest = {"file": "text-only.patch", "sha256": _sha(patch_dir / "text-only.patch"),
                     "targets": {prod_rel: _sha(build / prod_rel)}}

    test_specs = [
        ("qualification-tests.patch", {"codex-rs/core/src/thread_manager_tests.rs": b"// startup\n"}),
        ("qualification-registry.patch", {
            "codex-rs/core/src/tools/spec_plan_tests.rs": b"// registry-spec\n",
            "codex-rs/core/src/tools/registry_tests.rs": b"// registry-tools\n"}),
        ("qualification-adversarial.patch", {
            "codex-rs/core/src/thread_manager_tests.rs": b"// startup+adversarial\n",
            "codex-rs/core/src/tools/spec_plan_tests.rs": b"// registry-spec+adversarial\n",
            "codex-rs/core/src/tools/registry_tests.rs": b"// registry-tools+adversarial\n"}),
        ("qualification-contributors.patch", {
            "codex-rs/core/src/tools/spec_plan_tests.rs": b"// registry-spec+adversarial+contrib\n"}),
    ]
    test_manifest = []
    for name, targets in test_specs:
        header = "".join("--- a/%s\n+++ b/%s\n@@\n-x\n+y\n" % (rel, rel) for rel in targets)
        (patch_dir / name).write_text(header)
        entry = {"file": name, "sha256": _sha(patch_dir / name),
                 "targets": {rel: _sha_bytes(text) for rel, text in targets.items()}}
        test_manifest.append(entry)

    (patch_dir / "LICENSE").write_text("Apache-2.0\n")
    (patch_dir / "NOTICE").write_text("NOTICE\n")
    lock = build / "codex-rs" / "Cargo.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("# synthetic repaired lock\n")
    return patch_dir, prod_manifest, test_manifest, build, _sha(lock)


def _sha(path):
    return ci.sha256_file(path)


def _sha_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class EnvFilteringTests(unittest.TestCase):
    def test_identity_captures_only_nonsensitive_fields(self):
        environ = {
            "GITHUB_REPOSITORY": "o/r", "GITHUB_REF": "refs/heads/build/x",
            "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_TOKEN": "ghs-secret", "ACTIONS_RUNTIME_TOKEN": "rt",
        }
        identity = ci.capture_github_identity(environ)
        self.assertEqual(set(identity), {"repository", "ref", "sha", "run_id", "run_attempt"})
        self.assertNotIn("ghs-secret", json.dumps(identity))
        self.assertNotIn("rt", json.dumps(identity))

    def test_identity_rejects_non_commit_sha(self):
        with self.assertRaises(ci.Refusal):
            ci.capture_github_identity({"GITHUB_SHA": "not-a-sha"})

    def test_filtered_environment_drops_credentials_and_sets_private_home(self):
        environ = {
            "PATH": "/usr/bin", "LANG": "C.UTF-8", "GITHUB_TOKEN": "ghs",
            "OPENAI_API_KEY": "sk", "DEEPSEEK_API_KEY": "d", "AWS_SECRET_ACCESS_KEY": "a",
            "HOME": "/home/operator", "SOME_RANDOM": "x",
        }
        env = ci.filtered_environment(environ, "/private/home")
        self.assertEqual(env["HOME"], "/private/home")
        self.assertEqual(env["PATH"], "/usr/bin")
        for leaked in ("GITHUB_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                       "AWS_SECRET_ACCESS_KEY", "SOME_RANDOM"):
            self.assertNotIn(leaked, env)
        self.assertNotIn("/home/operator", json.dumps(env))

    def test_child_environment_clears_os_environ_and_restores_it(self):
        """The filtered env is *applied*, not merely computed, and put back."""
        original = dict(os.environ)
        os.environ["NATIVE_CI_OPERATOR_TOKEN"] = "ghs-operator"
        os.environ["HOME"] = "/home/operator"
        try:
            with ci.child_environment(os.environ, "/private/home") as child:
                self.assertEqual(os.environ.get("HOME"), "/private/home")
                self.assertNotIn("NATIVE_CI_OPERATOR_TOKEN", os.environ)
                self.assertEqual(dict(os.environ), child)
            # restored after the block
            self.assertEqual(os.environ.get("NATIVE_CI_OPERATOR_TOKEN"), "ghs-operator")
            self.assertEqual(os.environ.get("HOME"), "/home/operator")
        finally:
            os.environ.clear()
            os.environ.update(original)


class DeadlineTests(unittest.TestCase):
    def test_deadline_is_min_of_canonical_and_caller(self):
        now = dt.datetime.now(dt.timezone.utc)
        canonical = (now + dt.timedelta(seconds=1800)).isoformat()
        deadline = ci.capture_deadline(canonical, 3600, now=now)
        epoch = ci.parse_iso_epoch(deadline)
        self.assertAlmostEqual(epoch, now.timestamp() + 1800, delta=2)

    def test_caller_limit_wins_when_shorter(self):
        now = dt.datetime.now(dt.timezone.utc)
        canonical = (now + dt.timedelta(seconds=9000)).isoformat()
        deadline = ci.capture_deadline(canonical, 3600, now=now)
        self.assertAlmostEqual(ci.parse_iso_epoch(deadline), now.timestamp() + 3600, delta=2)

    def test_expired_deadline_refused(self):
        now = dt.datetime.now(dt.timezone.utc)
        canonical = (now + dt.timedelta(seconds=10)).isoformat()
        with self.assertRaises(ci.Refusal):
            ci.capture_deadline(canonical, 1, now=now)

    def test_naive_deadline_refused(self):
        with self.assertRaises(ci.Refusal):
            ci.capture_deadline("2026-10-14T00:00:00", 3600)

    def test_scoped_gate_cutoff_restores_on_exception(self):
        class H:
            def gate_cutoff(self):
                return "original"
        host = H()
        with self.assertRaises(RuntimeError):
            with ci.scoped_gate_cutoff(host, "forced"):
                self.assertEqual(host.gate_cutoff(), "forced")
                raise RuntimeError("boom")
        self.assertEqual(host.gate_cutoff(), "original")


class PrerequisiteTests(unittest.TestCase):
    def test_real_host_reports_matching_prerequisite_state(self):
        missing = ci.check_prerequisite(real_host)
        has_api = callable(getattr(real_host, "record_build_attempt", None))
        if has_api:
            self.assertEqual(missing, [])
        else:
            self.assertIn("record_build_attempt", missing)

    def test_fake_host_with_api_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ci.check_prerequisite(FakeHost(tmp)), [])

    def test_refusal_when_prerequisite_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = _fake_host_without_prereq(tmp)
            qualified, receipt, reason = ci.run_qualification(host)
            self.assertFalse(qualified)
            self.assertIn("prerequisite", reason)
            self.assertFalse(receipt["native_admission"])
            self.assertEqual(host.calls, [])  # no heavy setup attempted
            self.assertEqual(host._gate_cutoff_calls, 0)


class PatchScopeTests(unittest.TestCase):
    def test_real_test_patches_are_test_file_only(self):
        for entry in ci.TEST_PATCH_MANIFEST:
            text = (ci.PATCH_DIR / entry["file"]).read_text()
            self.assertTrue(ci.verify_test_file_scope(text, entry["file"]))

    def test_production_patch_is_not_test_file_only(self):
        text = (ci.PATCH_DIR / "text-only.patch").read_text()
        with self.assertRaises(ci.Refusal):
            ci.verify_test_file_scope(text, "text-only.patch")

    def test_real_patch_digests_match_docs(self):
        for entry in ci.TEST_PATCH_MANIFEST:
            ci.verify_patch_digest(ci.PATCH_DIR / entry["file"], entry["sha256"], entry["file"])
        ci.verify_patch_digest(ci.PATCH_DIR / ci.PRODUCTION_PATCH["file"],
                               ci.PRODUCTION_PATCH["sha256"], "text-only.patch")

    def test_qualification_test_patches_do_not_clone_turn_context(self):
        """The pinned `TurnContext` derives only `Debug` (no `Clone`).

        A patched test that mutates a turn created by
        `Session::new_default_turn` must therefore reach through the fresh,
        unshared `Arc` with `Arc::get_mut`. `Arc::make_mut(&mut turn)` demands
        `TurnContext: Clone` (E0277 `CloneToUninit`) and exited the hosted
        runner with 101 before any test ran, so no test patch may reintroduce
        it. `Arc::make_mut(&mut turn.config)` targets the inner `Arc<Config>`
        and is unaffected by this exact-string check.
        """
        for entry in ci.TEST_PATCH_MANIFEST:
            text = (ci.PATCH_DIR / entry["file"]).read_text()
            self.assertNotIn(
                "Arc::make_mut(&mut turn)",
                text,
                "%s must not require TurnContext: Clone" % entry["file"],
            )
        for name in ("qualification-registry.patch", "qualification-adversarial.patch"):
            text = (ci.PATCH_DIR / name).read_text()
            self.assertIn("Arc::get_mut(&mut turn)", text, name)

    def test_tampered_patch_digest_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "p.patch"
            fake.write_text("tampered")
            with self.assertRaises(ci.Refusal):
                ci.verify_patch_digest(fake, "0" * 64, "p.patch")

    def test_expected_target_digest_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-rs/core/src/thread_manager.rs"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content")
            with self.assertRaises(ci.Refusal):
                ci.verify_target_digests(tmp, {"codex-rs/core/src/thread_manager.rs": "0" * 64})


class ResultEvaluationTests(unittest.TestCase):
    def test_exactly_nineteen_qualified_names_accepted(self):
        parsed = ci.parse_cargo_test_output(passing_output(ci.EXPECTED_TESTS))
        self.assertEqual(len(ci.EXPECTED_TESTS), 19)
        self.assertTrue(ci.evaluate_qualification(parsed)["ok"])

    def test_expected_names_are_module_qualified(self):
        # Every reviewed test is declared inside its parent module's #[path] tests
        # module, so cargo prints `<parent>::tests::<fn>`; a bare name is wrong.
        for name in ci.EXPECTED_TESTS:
            self.assertRegex(name, r"^(thread_manager|tools::spec_plan|tools::registry)::tests::")
        self.assertIn("thread_manager::tests::"
                      "native_text_host_qualification_new_thread_without_init_captures_empty_ceiling",
                      ci.EXPECTED_TESTS)
        self.assertIn("tools::spec_plan::tests::"
                      "native_text_host_qualification_extension_contributor_constrained_session_denied",
                      ci.EXPECTED_TESTS)
        self.assertIn("tools::registry::tests::"
                      "native_text_host_qualification_captured_policy_denies_dispatch",
                      ci.EXPECTED_TESTS)

    def test_bare_names_are_rejected_as_missing(self):
        # The old bare-name fixture must now fail: nothing bare is expected.
        bare = sorted(n.rsplit("::", 1)[1] for n in ci.EXPECTED_TESTS)
        parsed = ci.parse_cargo_test_output(passing_output(bare))
        self.assertFalse(ci.evaluate_qualification(parsed)["ok"])

    def test_missing_result_rejected(self):
        names = sorted(ci.EXPECTED_TESTS)[:-1]
        parsed = ci.parse_cargo_test_output(passing_output(names))
        self.assertFalse(ci.evaluate_qualification(parsed)["ok"])

    def test_failed_result_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "thread_manager::tests::native_text_host_qualification_legacy_fork_is_covered ... ok",
            "thread_manager::tests::native_text_host_qualification_legacy_fork_is_covered ... FAILED")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_ignored_summary_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "0 ignored", "1 ignored")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_ignored_individual_result_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "thread_manager::tests::native_text_host_qualification_legacy_fork_is_covered ... ok",
            "thread_manager::tests::native_text_host_qualification_legacy_fork_is_covered ... ignored")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_extra_qualification_prefixed_test_rejected(self):
        # An unexpected test that carries the qualification prefix must fail even
        # when the nineteen expected ones all pass.
        output = passing_output(ci.EXPECTED_TESTS)
        output += "test tools::spec_plan::tests::native_text_host_qualification_bogus ... ok\n"
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_contradictory_zero_passed_summary_rejected(self):
        # Every result line says ok yet the summary claims 0 passed: contradictory.
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "%d passed" % len(ci.EXPECTED_TESTS), "0 passed")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_wrong_passed_count_summary_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "%d passed" % len(ci.EXPECTED_TESTS), "%d passed" % (len(ci.EXPECTED_TESTS) - 1))
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_bad_verdict_summary_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "test result: ok.", "test result: FAILED.")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_duplicate_result_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS)
        output += ("test thread_manager::tests::"
                   "native_text_host_qualification_legacy_fork_is_covered ... ok\n")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_empty_results_rejected(self):
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(""))["ok"])

    def test_truncated_results_rejected_despite_exit_zero(self):
        output = "\n".join("test %s ... ok" % n for n in sorted(ci.EXPECTED_TESTS)) + "\n"
        parsed = ci.parse_cargo_test_output(output)
        self.assertTrue(parsed["truncated"])
        self.assertFalse(ci.evaluate_qualification(parsed)["ok"])

    def test_extra_unrelated_results_ignored(self):
        output = passing_output(ci.EXPECTED_TESTS) + "test another_module::tests::some_other ... ok\n"
        self.assertTrue(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])


COMPILER_LOG_SAMPLE = """\
   Compiling codex-core v0.1.0 (/home/runner/work/repo/repo/build/codex-rs/core)
warning: unused import: `std::fmt::Write`
  --> /home/runner/work/repo/repo/build/codex-rs/core/src/tools/registry.rs:12:5
   |
12 | use std::fmt::Write;
   |     ^^^^^^^^^^^^^^^
error[E0308]: mismatched types
  --> /home/runner/work/repo/repo/build/codex-rs/core/src/tools/spec_plan_tests.rs:88:9
   |
88 |     let n: u8 = "twelve";
   |            ^^ expected `u8`, found `&str`
error: could not compile `codex-core` (lib test) due to 1 previous error
Finished `test` profile [release] in 402.1s
"""


class FailureDiagnosticTests(unittest.TestCase):
    """The bounded, sanitized compiler-diagnostic excerpt for a failure receipt."""

    def test_only_diagnostic_shaped_lines_are_kept(self):
        excerpt = ci.sanitize_compiler_excerpt(COMPILER_LOG_SAMPLE)
        self.assertEqual(excerpt["lines"], [
            "warning: unused import: `std::fmt::Write`",
            "--> <path>:12:5",
            "error[E0308]: mismatched types",
            "--> <path>:88:9",
            "error: could not compile `codex-core` (lib test) due to 1 previous error",
        ])
        self.assertFalse(excerpt["truncated"])

    def test_absolute_paths_home_and_env_values_are_redacted(self):
        blob = json.dumps(ci.sanitize_compiler_excerpt(COMPILER_LOG_SAMPLE))
        self.assertNotIn("/home/runner", blob)
        self.assertNotIn("repo/build", blob)
        env_line = ci.sanitize_compiler_excerpt(
            "error: could not read CARGO_HOME=/home/runner/.cargo\n"
            "error: HOME is $HOME/hidden\n")
        self.assertEqual(env_line["lines"], [
            "error: could not read CARGO_HOME=<redacted>",
            "error: HOME is <home><path>",
        ])
        self.assertNotIn("runner", json.dumps(env_line))
        url_line = ci.sanitize_compiler_excerpt(
            "error: failed to fetch https://crates.io/api/v1/crates\n")
        self.assertEqual(url_line["lines"], ["error: failed to fetch <url>"])

    def test_relative_paths_are_preserved(self):
        excerpt = ci.sanitize_compiler_excerpt("--> core/src/tools/registry.rs:12:5\n")
        self.assertEqual(excerpt["lines"], ["--> core/src/tools/registry.rs:12:5"])

    def test_line_cap_is_hard(self):
        text = "".join("error: e%d\n" % i for i in range(100))
        excerpt = ci.sanitize_compiler_excerpt(text)
        self.assertEqual(len(excerpt["lines"]), ci.DIAGNOSTIC_MAX_LINES)
        self.assertTrue(excerpt["truncated"])
        self.assertEqual(excerpt["lines"][0], "error: e0")
        self.assertEqual(excerpt["lines"][-1], "error: e39")

    def test_character_cap_is_hard(self):
        text = "".join("error: %s\n" % ("x" * 200) for _ in range(40))
        excerpt = ci.sanitize_compiler_excerpt(text)
        self.assertTrue(excerpt["truncated"])
        self.assertLessEqual(sum(len(line) + 1 for line in excerpt["lines"]),
                             ci.DIAGNOSTIC_MAX_CHARS)
        self.assertGreaterEqual(len(excerpt["lines"]), 1)

    def test_single_overlong_line_is_hard_truncated(self):
        excerpt = ci.sanitize_compiler_excerpt("error: %s\n" % ("y" * 9000))
        self.assertEqual(len(excerpt["lines"]), 1)
        self.assertEqual(len(excerpt["lines"][0]), ci.DIAGNOSTIC_MAX_CHARS)
        self.assertTrue(excerpt["truncated"])

    def test_non_diagnostic_log_yields_none(self):
        self.assertIsNone(ci.sanitize_compiler_excerpt(""))
        self.assertIsNone(ci.sanitize_compiler_excerpt(
            "   Compiling codex-core v0.1.0\nFinished `test` profile in 1.2s\n"))

    def test_stage_diagnostic_is_none_for_missing_or_absent_log(self):
        self.assertIsNone(ci._stage_diagnostic(None))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ci._stage_diagnostic(Path(tmp) / "does-not-exist.log"))

    def test_stage_diagnostic_reads_only_a_bounded_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prep.log"
            filler = "note: boilerplate line\n" * \
                (ci.DIAGNOSTIC_TAIL_BYTES // 22 + 64)
            path.write_text("error: first-line-outside-the-tail\n" + filler +
                            "error: last prior error\n")
            excerpt = ci._stage_diagnostic(path)
            self.assertIsNotNone(excerpt)
            self.assertEqual(excerpt["lines"], ["error: last prior error"])
            self.assertNotIn("first-line-outside-the-tail", json.dumps(excerpt))


class ArtifactAllowlistTests(unittest.TestCase):
    def _artifact_dir(self, tmp):
        patch_dir = Path(tmp) / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        (patch_dir / "LICENSE").write_text("Apache-2.0\n")
        (patch_dir / "NOTICE").write_text("NOTICE\n")
        return patch_dir, Path(tmp) / "artifact"

    def _publish(self, tmp, qualified, receipt, host):
        patch_dir, artifact = self._artifact_dir(tmp)
        with patch.object(ci, "PATCH_DIR", patch_dir), \
             patch.object(ci, "ARTIFACT_DIR", artifact):
            return ci.publish_artifact(receipt, qualified, host), artifact, patch_dir

    def test_success_publishes_all_four_mandatory_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            entries, artifact, _ = self._publish(tmp, True, {"qualified": True}, host)
            self.assertEqual(sorted(p.name for p in artifact.iterdir()),
                             sorted(["codex", "LICENSE", "NOTICE",
                                     "qualification-receipt.json"]))
            self.assertEqual(len(entries), 4)

    def test_failure_ships_only_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            entries, artifact, _ = self._publish(tmp, False, {"qualified": False}, host)
            self.assertEqual([p.name for p in artifact.iterdir()],
                             ["qualification-receipt.json"])

    def test_failure_after_success_clears_the_stale_binary(self):
        # Seed a prior successful upload, then publish a failure: the binary must
        # not remain uploadable.
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            _, artifact, _ = self._publish(tmp, True, {"qualified": True}, host)
            self.assertIn("codex", [p.name for p in artifact.iterdir()])
            self._publish(tmp, False, {"qualified": False}, host)
            self.assertEqual([p.name for p in artifact.iterdir()],
                             ["qualification-receipt.json"])

    def test_success_missing_license_fails_and_clears_any_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            patch_dir, artifact = self._artifact_dir(tmp)
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(ci, "ARTIFACT_DIR", artifact):
                ci.publish_artifact({"qualified": True}, True, host)
                self.assertIn("codex", [p.name for p in artifact.iterdir()])
                (patch_dir / "LICENSE").unlink()
                with self.assertRaises(ci.Refusal):
                    ci.publish_artifact({"qualified": True}, True, host)
            # No stale or partial binary may remain uploadable after the failure.
            self.assertNotIn("codex", [p.name for p in artifact.iterdir()])
            self.assertIn("qualification-receipt.json",
                          [p.name for p in artifact.iterdir()])

    def test_unexpected_entry_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            patch_dir, artifact = self._artifact_dir(tmp)
            artifact.mkdir(parents=True)
            (artifact / "surprise.txt").write_text("x")
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(ci, "ARTIFACT_DIR", artifact):
                with self.assertRaises(ci.Refusal):
                    ci.publish_artifact({"qualified": False}, False, host)

    def test_symlink_entry_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp)
            _, artifact = self._artifact_dir(tmp)
            artifact.mkdir(parents=True)
            target = Path(tmp) / "outside.txt"
            target.write_text("x")
            (artifact / "NOTICE").symlink_to(target)
            patch_dir, _ = self._artifact_dir(tmp)
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(ci, "ARTIFACT_DIR", artifact):
                with self.assertRaises(ci.Refusal):
                    ci.publish_artifact({"qualified": False}, False, host)


class OrchestrationFixtureTests(unittest.TestCase):
    def _run(self, host, tmp, patch_dir, prod_manifest, test_manifest, lock_digest):
        host.LOCK_BUILD_SHA256 = lock_digest
        with patch.object(ci, "PATCH_DIR", patch_dir), \
             patch.object(ci, "PRODUCTION_PATCH", prod_manifest), \
             patch.object(ci, "TEST_PATCH_MANIFEST", tuple(test_manifest)), \
             patch.object(ci, "ARTIFACT_DIR", Path(tmp) / "artifact"):
            host._apply_contents = {
                entry["file"]: {rel: _content_for(rel, entry["file"])
                                for rel in entry["targets"]}
                for entry in test_manifest}
            return ci.run_qualification(host)

    def test_qualified_run_uses_one_deadline_and_records_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            # Non-empty build/prepare logs must never surface on a qualified run.
            host = FakeHost(tmp, patch_dir=patch_dir,
                            build_log_text=COMPILER_LOG_SAMPLE,
                            prep_log_text=COMPILER_LOG_SAMPLE)
            # Production target already present; test patches rewrite targets.
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS)
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertTrue(qualified, reason)
            self.assertIsNone(receipt["failure"])
            # One single deadline across every supervised stage.  The build stage is
            # the accepted compile scope ("supervise"); the test stages keep their own
            # truthful wrapper stage names.
            stages = [c for c in host.calls if c[0] == "run_supervised"]
            self.assertEqual([s[1] for s in stages], ["supervise", "prepare-tests", "offline-tests"])
            self.assertEqual(len({s[2] for s in stages}), 1)
            self.assertTrue(all(s[3] == host.RAM_MIN_AVAILABLE_BYTES for s in stages))
            # Patches applied strictly in the reviewed order, four of them.
            self.assertEqual(host.applied, [e["file"] for e in test_manifest])
            self.assertEqual(len(test_manifest), 4)
            # Receipt is sanitized, truthful and non-admitting.
            self.assertFalse(receipt["native_admission"])
            self.assertTrue(receipt["guard_unchanged"])
            self.assertEqual(set(receipt["test_stage"]["observed_passed"]),
                             set(ci.EXPECTED_TESTS))
            self.assertEqual(receipt["build_inputs"]["source_pin"], host.CODEX_PIN)
            self.assertEqual(receipt["build_inputs"]["binary"]["sha256"],
                             ci.sha256_file(host._binary))

    def test_absent_private_root_is_created_before_the_resource_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, patch_dir=patch_dir)
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS)
            # A fresh checkout has no private build root.  Mirror shutil.disk_usage,
            # which raises FileNotFoundError on an absent directory: if the wrapper's
            # mkdir is removed, the run fails with "os error during preflight" rather
            # than creating the root first.
            absent = Path(tmp) / "fresh" / "native-host"
            self.assertFalse(absent.exists())
            host.private_root = lambda root: absent

            def strict_preflight(path):
                if not Path(path).is_dir():
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                host.calls.append(("preflight_resources", str(path)))
                return {"free_disk_bytes": 20 * 1024 ** 3,
                        "available_ram_bytes": 8 * 1024 ** 3}

            host.preflight_resources = strict_preflight
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertTrue(qualified, reason)
            self.assertTrue(absent.is_dir())

    def test_build_failure_refuses_and_ships_no_qualified_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, build_code=1, patch_dir=patch_dir)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            self.assertFalse(receipt["native_admission"])
            self.assertIsNotNone(receipt["failure"])
            # The native command/build stage records the build failure in the
            # accepted compile scope with its real curated outcome.
            self.assertEqual(receipt["failure"]["stage"], "build")
            self.assertEqual(receipt["failure"]["scope"], "supervise")
            self.assertEqual(host._attempts[-1][2]["stage"], "supervise")

    def test_wrong_test_name_refused_even_on_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, test_code=0, patch_dir=patch_dir)
            host._build = build
            names = sorted(ci.EXPECTED_TESTS)[:-1] + [
                "tools::spec_plan::tests::native_text_host_qualification_bogus"]
            host._test_output = passing_output(names)
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            self.assertIn("missing", reason)

    def test_extra_qualification_test_refused_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, patch_dir=patch_dir)
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS) + \
                "test tools::registry::tests::native_text_host_qualification_extra ... ok\n"
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            self.assertIn("unexpected", reason)

    def test_gate_cutoff_restored_after_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, build_code=1, patch_dir=patch_dir)
            host._build = build
            self._run(host, tmp, patch_dir, prod, test_manifest, lock_digest)
            self.assertEqual(host._gate_cutoff_calls, 1)  # restored, not re-queried

    def test_prepare_tests_failure_records_truthful_non_compile_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, prep_code=1, patch_dir=patch_dir)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            failure = receipt["failure"]
            self.assertEqual(failure["stage"], "prepare-tests")
            self.assertEqual(failure["scope"], "prepare-tests")
            # No fabricated child cleanup and no invented resource cause.
            self.assertIsNone(failure["compiler_cleanup"])
            self.assertIsNone(failure["compiler_child_status"])
            self.assertIsNone(failure["stop_reason"])
            self.assertEqual(host._attempts[-1][2]["stage"], "prepare-tests")

    def test_prepare_tests_failure_carries_bounded_sanitized_diagnostic(self):
        # The failing stage is otherwise unrecoverable (the artifact is
        # receipt-only), so the receipt carries one bounded, redacted excerpt.
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, prep_code=1, patch_dir=patch_dir,
                            prep_log_text=COMPILER_LOG_SAMPLE)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            failure = receipt["failure"]
            self.assertEqual(failure["stage"], "prepare-tests")
            # No fabricated supervisor fields, and the diagnostic is a bounded,
            # redacted, diagnostic-only object.
            self.assertIsNone(failure["compiler_cleanup"])
            self.assertIsNone(failure["compiler_child_status"])
            self.assertEqual(set(failure["diagnostic"]), {"lines", "truncated"})
            self.assertIn("error[E0308]: mismatched types", failure["diagnostic"]["lines"])
            self.assertLessEqual(len(failure["diagnostic"]["lines"]), ci.DIAGNOSTIC_MAX_LINES)
            blob = json.dumps(receipt)
            self.assertNotIn("/home/runner", blob)
            self.assertNotIn("repo/build", blob)
            self.assertFalse(receipt["native_admission"])
            self.assertTrue(receipt["guard_unchanged"])

    def test_build_failure_carries_bounded_sanitized_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, build_code=1, patch_dir=patch_dir,
                            build_log_text=COMPILER_LOG_SAMPLE)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            failure = receipt["failure"]
            self.assertEqual(failure["stage"], "build")
            self.assertIn("error[E0308]: mismatched types", failure["diagnostic"]["lines"])
            self.assertNotIn("/home/runner", json.dumps(receipt))

    def test_test_patch_failure_never_inherits_the_build_log_diagnostic(self):
        # The build succeeded and its log holds decoy diagnostics; a later
        # test-patches failure has no log of its own, so its receipt must not
        # present the build's excerpt as its own evidence.
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, build_code=0, fail_apply=True, patch_dir=patch_dir,
                            build_log_text=COMPILER_LOG_SAMPLE)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            failure = receipt["failure"]
            self.assertEqual(failure["stage"], "test-patches")
            self.assertIsNone(failure["diagnostic"])
            blob = json.dumps(receipt)
            self.assertNotIn("error[E0308]", blob)
            self.assertNotIn("warning: unused", blob)
            self.assertNotIn("could not compile", blob)

    def test_test_patch_failure_is_stage_scoped_without_stale_compiler_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, fail_apply=True, patch_dir=patch_dir)
            host._build = build
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            failure = receipt["failure"]
            self.assertEqual(failure["stage"], "test-patches")
            self.assertEqual(failure["scope"], "test-patches")
            # The completed build's cleanup must not leak into a later stage.
            self.assertIsNone(failure["compiler_cleanup"])

    def test_os_error_failure_leaks_no_exception_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = FakeHost(tmp, fail_source=OSError("secret /etc/shadow token=abc"))
            qualified, receipt, reason = self._run(
                host, tmp, *make_synthetic_patches(tmp)[:1], {}, [], "0" * 64)
            self.assertFalse(qualified)
            self.assertEqual(reason, "os error during source")
            blob = json.dumps(receipt)
            self.assertNotIn("secret", blob)
            self.assertNotIn("/etc/shadow", blob)
            self.assertNotIn("abc", blob)

    def test_test_stage_mutating_the_binary_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, mutate_binary=True, patch_dir=patch_dir)
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS)
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            self.assertIn("binary changed", reason)

    def test_helpers_run_isolated_and_os_environ_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, patch_dir=patch_dir)
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS)
            os.environ["NATIVE_CI_LEAK_TOKEN"] = "ghs-leak"
            os.environ["GITHUB_TOKEN"] = "ghs-run-token"
            os.environ["GITHUB_SHA"] = "b" * 40
            try:
                qualified, receipt, reason = self._run(host, tmp, patch_dir, prod,
                                                       test_manifest, lock_digest)
                restored = "NATIVE_CI_LEAK_TOKEN" in os.environ
            finally:
                for key in ("NATIVE_CI_LEAK_TOKEN", "GITHUB_TOKEN", "GITHUB_SHA"):
                    os.environ.pop(key, None)
            self.assertTrue(qualified, reason)
            self.assertTrue(host._helper_envs)
            # Every bootstrap/preparation/stage helper saw the filtered allowlist
            # with the private HOME, never an inherited credential or GITHUB_* var.
            for env in host._helper_envs:
                self.assertEqual(env.get("HOME"), str(host._home))
                for leaked in ("NATIVE_CI_LEAK_TOKEN", "GITHUB_TOKEN"):
                    self.assertNotIn(leaked, env)
            # The identity was captured *before* the clear, and put back afterwards.
            self.assertEqual(receipt["github"]["sha"], "b" * 40)
            self.assertTrue(restored)


class PreservationTests(unittest.TestCase):
    def _snapshot(self, tmp):
        patch_dir, _prod, _test_manifest, build, _lock = make_synthetic_patches(tmp)
        host = FakeHost(tmp)
        host._build = build
        binary = host._binary
        with patch.object(ci, "PATCH_DIR", patch_dir):
            before = ci._preservation_snapshot(host, build, binary)
        return host, build, binary, patch_dir, before

    def test_snapshot_covers_source_lock_provenance_profile_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, build, binary, patch_dir, before = self._snapshot(tmp)
            self.assertEqual(set(before),
                             {"patch", "target", "lock", "provenance", "profile", "binary"})
            with patch.object(ci, "PATCH_DIR", patch_dir):
                self.assertEqual(ci._verify_preservation(host, build, binary, before), before)

    def test_changed_lock_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, build, binary, patch_dir, before = self._snapshot(tmp)
            (build / "codex-rs" / "Cargo.lock").write_text("# mutated\n")
            with patch.object(ci, "PATCH_DIR", patch_dir):
                with self.assertRaises(ci.Refusal) as ctx:
                    ci._verify_preservation(host, build, binary, before)
            self.assertIn("lock changed", str(ctx.exception))

    def test_changed_production_source_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, build, binary, patch_dir, before = self._snapshot(tmp)
            (build / "codex-rs" / "core" / "src" / "thread_manager.rs").write_bytes(b"// mutated\n")
            with patch.object(ci, "PATCH_DIR", patch_dir):
                with self.assertRaises(ci.Refusal) as ctx:
                    ci._verify_preservation(host, build, binary, before)
            self.assertIn("target changed", str(ctx.exception))

    def test_changed_provenance_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, build, binary, patch_dir, before = self._snapshot(tmp)
            host._provenance.write_text("{\"mutated\": true}\n")
            with patch.object(ci, "PATCH_DIR", patch_dir):
                with self.assertRaises(ci.Refusal) as ctx:
                    ci._verify_preservation(host, build, binary, before)
            self.assertIn("provenance changed", str(ctx.exception))

    def test_changed_profile_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, build, binary, patch_dir, before = self._snapshot(tmp)
            changed = dict(host.build_profile_contract())
            changed["opt-level"] = "3"
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(host, "build_profile_contract", lambda: changed):
                with self.assertRaises(ci.Refusal) as ctx:
                    ci._verify_preservation(host, build, binary, before)
            self.assertIn("profile changed", str(ctx.exception))


def _content_for(rel, patch_file):
    # Mirrors make_synthetic_patches' post-apply contents.
    table = {
        ("codex-rs/core/src/thread_manager_tests.rs", "qualification-tests.patch"): b"// startup\n",
        ("codex-rs/core/src/tools/spec_plan_tests.rs", "qualification-registry.patch"): b"// registry-spec\n",
        ("codex-rs/core/src/tools/registry_tests.rs", "qualification-registry.patch"): b"// registry-tools\n",
        ("codex-rs/core/src/thread_manager_tests.rs", "qualification-adversarial.patch"): b"// startup+adversarial\n",
        ("codex-rs/core/src/tools/spec_plan_tests.rs", "qualification-adversarial.patch"): b"// registry-spec+adversarial\n",
        ("codex-rs/core/src/tools/registry_tests.rs", "qualification-adversarial.patch"): b"// registry-tools+adversarial\n",
        ("codex-rs/core/src/tools/spec_plan_tests.rs",
         "qualification-contributors.patch"): b"// registry-spec+adversarial+contrib\n",
    }
    return table[(rel, patch_file)]


def _top_level_event_keys(text):
    """The real ``on:`` trigger keys, ignoring comments and nested values.

    A substring search is wrong: the workflow comment names the forbidden
    triggers.  This strips comments (full-line and trailing) and returns only
    the first-level mapping keys nested under ``on:``.  No PyYAML is required.
    """
    events = set()
    in_on = False
    event_indent = None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0] if " #" in raw else raw
        line = line.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if not in_on:
            if indent == 0 and line.strip() == "on:":
                in_on = True
                event_indent = None
            continue
        if indent == 0:  # a new top-level key closes the ``on:`` block
            in_on = False
            if line.strip() == "on:":
                in_on = True
                event_indent = None
            continue
        if event_indent is None:
            event_indent = indent  # the direct-child depth of ``on:``
        if indent == event_indent:
            key = line.strip().split(":", 1)[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                events.add(key)
    return events


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text()

    def test_event_parser_ignores_comments(self):
        sample = ("on:\n"
                  "  push:\n"
                  "    branches: [main]\n"
                  "# pull_request is deliberately absent here\n"
                  "jobs:\n"
                  "  x:\n"
                  "    runs-on: ubuntu-24.04\n")
        self.assertEqual(_top_level_event_keys(sample), {"push"})

    def test_push_only_no_pull_request(self):
        events = _top_level_event_keys(self.text)
        self.assertEqual(events, {"push"})
        self.assertNotIn("pull_request", events)
        self.assertNotIn("pull_request_target", events)

    def test_restricted_branch_and_paths(self):
        self.assertIn("build/eliza-deepseek-themes", self.text)
        for path in ("scripts/native-host-ci.py", "scripts/native-host.py",
                     "tests/ops/test_native_host_ci.py", "docs/eliza-native-ci.md",
                     "builder/eliza/native-host/**"):
            self.assertIn(path, self.text)

    def test_runner_permissions_and_timeout(self):
        self.assertIn("ubuntu-24.04", self.text)
        self.assertIn("timeout-minutes: 90", self.text)
        self.assertIn("contents: read", self.text)

    def test_pinned_actions_and_checkout_isolation(self):
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertIn("submodules: false", self.text)
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", self.text)
        self.assertIn("retention-days: 3", self.text)

    def test_no_cache_step(self):
        self.assertNotIn("actions/cache", self.text)


if __name__ == "__main__":
    unittest.main()
