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
                 test_output=None, patch_dir=None):
        self.tmp = Path(tmp)
        self.calls = []
        self.applied = []
        self._deadlines = []
        self._ram_floors = []
        self._environments = []
        self._prereq = prereq
        self._build_code = build_code
        self._prep_code = prep_code
        self._test_code = test_code
        self._test_output = test_output
        self._patch_dir = Path(patch_dir) if patch_dir else None
        self._apply_contents = {}
        self._gate_calls = 0
        self._gate_cutoff_calls = 0

        self.ROOT = self.tmp / "root"
        self.ROOT.mkdir(parents=True, exist_ok=True)
        self._logs = self.tmp / "logs"
        self._target = self.tmp / "target"
        self._private = self.tmp / "private"
        self._home = self.tmp / "home"
        self._build = self.tmp / "build"
        self._build.mkdir(parents=True, exist_ok=True)
        self._cargo = self.tmp / "cargo"
        self._binary = self._target / "release" / self.BIN
        self._binary.parent.mkdir(parents=True, exist_ok=True)
        self._binary.write_bytes(b"fake-binary")

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

    def ensure_toolchain(self, root, deadline=None):
        self.calls.append(("ensure_toolchain", deadline))
        return self._cargo

    def prepare_build(self, root, deadline=None):
        self.calls.append(("prepare_build", deadline))
        return self._build, self.tmp / "text-only.patch", "prod-patch-digest"

    def build_env(self, root):
        return {"CARGO_HOME": "x", "HOME": str(self._home)}

    def build_argv(self, cargo):
        return ["nice", "-n", "19", str(cargo), "build", "--release", "--locked",
                "-p", "codex-cli", "--bin", self.BIN, "--jobs", "1"]

    def _git(self, args, cwd=None, timeout=None):
        name = Path(args[-1]).name
        self.applied.append(name)
        for rel, content in self._apply_contents.get(name, {}).items():
            path = Path(cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        class Result:
            returncode = 0
            stderr = ""
        return Result()

    def run_supervised(self, argv, cwd, env, log_path, stop_at, max_log_bytes=None,
                       ram_floor_bytes=None, ram_probe=None, ram_interval=None, outcome=None):
        stage = (outcome or {}).get("stage")
        self.calls.append(("run_supervised", stage, stop_at, ram_floor_bytes))
        self._deadlines.append(stop_at)
        self._ram_floors.append(ram_floor_bytes)
        self._environments.append(dict(env))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        if stage == "offline-tests":
            Path(log_path).write_text(self._test_output or "")
            return self._test_code
        if stage == "prepare-tests":
            Path(log_path).write_text("")
            return self._prep_code
        Path(log_path).write_text("")
        return self._build_code

    # --- stop-receipt prerequisite ---
    def record_build_attempt(self, *a, **k):
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
    def test_exactly_fifteen_passing_accepted(self):
        parsed = ci.parse_cargo_test_output(passing_output(ci.EXPECTED_TESTS))
        self.assertEqual(len(ci.EXPECTED_TESTS), 15)
        self.assertTrue(ci.evaluate_qualification(parsed)["ok"])

    def test_missing_result_rejected(self):
        names = sorted(ci.EXPECTED_TESTS)[:-1]
        parsed = ci.parse_cargo_test_output(passing_output(names))
        self.assertFalse(ci.evaluate_qualification(parsed)["ok"])

    def test_failed_result_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "native_text_host_qualification_legacy_fork_is_covered ... ok",
            "native_text_host_qualification_legacy_fork_is_covered ... FAILED")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_ignored_summary_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS).replace(
            "0 ignored", "1 ignored")
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_duplicate_result_rejected(self):
        output = passing_output(ci.EXPECTED_TESTS)
        output += "test native_text_host_qualification_legacy_fork_is_covered ... ok\n"
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])

    def test_empty_results_rejected(self):
        self.assertFalse(ci.evaluate_qualification(ci.parse_cargo_test_output(""))["ok"])

    def test_truncated_results_rejected_despite_exit_zero(self):
        output = "\n".join("test %s ... ok" % n for n in sorted(ci.EXPECTED_TESTS)) + "\n"
        parsed = ci.parse_cargo_test_output(output)
        self.assertTrue(parsed["truncated"])
        self.assertFalse(ci.evaluate_qualification(parsed)["ok"])

    def test_extra_unrelated_results_ignored(self):
        output = passing_output(ci.EXPECTED_TESTS) + "test another_module ... ok\n"
        self.assertTrue(ci.evaluate_qualification(ci.parse_cargo_test_output(output))["ok"])


class ArtifactAllowlistTests(unittest.TestCase):
    def test_failure_ships_only_receipt(self):
        entries = ci.selection_artifact_entries(False, "/bin/codex", "/r.json",
                                                "/LICENSE", "/NOTICE")
        self.assertEqual(entries, ["/r.json"])

    def test_success_allowlists_only_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "r.json"; receipt.write_text("{}")
            license_file = Path(tmp) / "LICENSE"; license_file.write_text("l")
            notice = Path(tmp) / "NOTICE"; notice.write_text("n")
            missing_binary = Path(tmp) / "codex"
            entries = ci.selection_artifact_entries(True, missing_binary, receipt,
                                                    license_file, notice)
            self.assertNotIn(str(missing_binary), [str(e) for e in entries])
            self.assertIn(str(receipt), [str(e) for e in entries])

    def _artifact_dir(self, tmp):
        patch_dir = Path(tmp) / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        (patch_dir / "LICENSE").write_text("Apache-2.0\n")
        (patch_dir / "NOTICE").write_text("NOTICE\n")
        return patch_dir, Path(tmp) / "artifact"

    def test_write_artifact_success_copies_only_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, artifact = self._artifact_dir(tmp)
            host = FakeHost(tmp)
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(ci, "ARTIFACT_DIR", artifact):
                ci.write_artifact({"ok": True}, True, host)
            self.assertEqual(sorted(p.name for p in artifact.iterdir()),
                             sorted(["codex", "LICENSE", "NOTICE",
                                     "qualification-receipt.json"]))

    def test_write_artifact_failure_ships_only_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, artifact = self._artifact_dir(tmp)
            host = FakeHost(tmp)
            with patch.object(ci, "PATCH_DIR", patch_dir), \
                 patch.object(ci, "ARTIFACT_DIR", artifact):
                ci.write_artifact({"qualified": False}, False, host)
            self.assertEqual([p.name for p in artifact.iterdir()],
                             ["qualification-receipt.json"])


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
            host = FakeHost(tmp, patch_dir=patch_dir)
            # Production target already present; test patches rewrite targets.
            host._build = build
            host._test_output = passing_output(ci.EXPECTED_TESTS)
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertTrue(qualified, reason)
            # One single deadline across every supervised stage.
            stages = [c for c in host.calls if c[0] == "run_supervised"]
            self.assertEqual([s[1] for s in stages], ["build", "prepare-tests", "offline-tests"])
            self.assertEqual(len({s[2] for s in stages}), 1)
            self.assertTrue(all(s[3] == host.RAM_MIN_AVAILABLE_BYTES for s in stages))
            # Patches applied strictly in the reviewed order.
            self.assertEqual(host.applied, [e["file"] for e in test_manifest])
            # Receipt is sanitized, truthful and non-admitting.
            self.assertFalse(receipt["native_admission"])
            self.assertTrue(receipt["guard_unchanged"])
            self.assertEqual(set(receipt["test_stage"]["observed_passed"]),
                             set(ci.EXPECTED_TESTS))
            self.assertEqual(receipt["build_inputs"]["source_pin"], host.CODEX_PIN)
            self.assertEqual(receipt["build_inputs"]["binary"]["sha256"],
                             ci.sha256_file(host._binary))

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

    def test_wrong_test_name_refused_even_on_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, test_code=0, patch_dir=patch_dir)
            host._build = build
            names = sorted(ci.EXPECTED_TESTS)[:-1] + ["native_text_host_qualification_bogus"]
            host._test_output = passing_output(names)
            qualified, receipt, reason = self._run(host, tmp, patch_dir, prod, test_manifest,
                                                   lock_digest)
            self.assertFalse(qualified)
            self.assertIn("missing", reason)

    def test_gate_cutoff_restored_after_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir, prod, test_manifest, build, lock_digest = make_synthetic_patches(tmp)
            host = FakeHost(tmp, build_code=1, patch_dir=patch_dir)
            host._build = build
            self._run(host, tmp, patch_dir, prod, test_manifest, lock_digest)
            self.assertEqual(host._gate_cutoff_calls, 1)  # restored, not re-queried


def _content_for(rel, patch_file):
    # Mirrors make_synthetic_patches' post-apply contents.
    table = {
        ("codex-rs/core/src/thread_manager_tests.rs", "qualification-tests.patch"): b"// startup\n",
        ("codex-rs/core/src/tools/spec_plan_tests.rs", "qualification-registry.patch"): b"// registry-spec\n",
        ("codex-rs/core/src/tools/registry_tests.rs", "qualification-registry.patch"): b"// registry-tools\n",
        ("codex-rs/core/src/thread_manager_tests.rs", "qualification-adversarial.patch"): b"// startup+adversarial\n",
        ("codex-rs/core/src/tools/spec_plan_tests.rs", "qualification-adversarial.patch"): b"// registry-spec+adversarial\n",
        ("codex-rs/core/src/tools/registry_tests.rs", "qualification-adversarial.patch"): b"// registry-tools+adversarial\n",
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
