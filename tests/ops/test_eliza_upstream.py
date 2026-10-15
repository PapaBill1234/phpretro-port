import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "eliza_upstream", Path(__file__).resolve().parents[2] / "scripts/eliza-upstream.py")
upstream = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upstream)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git("-c", "init.defaultBranch=main", "init", "-q", cwd=path)
    git("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "--allow-empty",
        "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def write_exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_archive(path, files, dirs=()):
    with tarfile.open(path, "w:gz") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, body, mode in files:
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class OffPeakGate(unittest.TestCase):
    def test_exact_boundaries(self):
        # 2026-10-13 is a Tuesday: peak 00:30-04:05 and 05:30-10:05 UTC.
        tuesday = dt.date(2026, 10, 13)
        def at(h, m, s=0):
            return dt.datetime(tuesday.year, tuesday.month, tuesday.day, h, m, s,
                               tzinfo=dt.timezone.utc)
        self.assertTrue(upstream.gate_decision(at(0, 29, 59))["allowed"])
        self.assertFalse(upstream.gate_decision(at(0, 30, 0))["allowed"])
        self.assertFalse(upstream.gate_decision(at(4, 4, 59))["allowed"])
        self.assertTrue(upstream.gate_decision(at(4, 5, 0))["allowed"])
        self.assertFalse(upstream.gate_decision(at(5, 30, 0))["allowed"])
        self.assertTrue(upstream.gate_decision(at(10, 5, 0))["allowed"])
        # Weekend is always off-peak with a verified future cutoff.
        saturday = dt.datetime(2026, 10, 17, 2, 0, tzinfo=dt.timezone.utc)
        decision = upstream.gate_decision(saturday)
        self.assertTrue(decision["allowed"])
        self.assertIsNotNone(decision["stop_at"])

    def test_enforce_raises_when_closed(self):
        now = dt.datetime(2026, 10, 13, 2, 0, tzinfo=dt.timezone.utc)
        with self.assertRaises(upstream.SetupError):
            upstream.enforce_gate(now)


class SourcePin(unittest.TestCase):
    def test_wrong_pin_and_modifications_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(upstream.source_dir(root))
            original = upstream.UPSTREAM_PIN
            try:
                upstream.UPSTREAM_PIN = head
                self.assertTrue(upstream.source_state(root)["ok"])
                upstream.UPSTREAM_PIN = "0" * 40
                self.assertEqual(upstream.source_state(root)["reason"], "wrong_pin")
                upstream.UPSTREAM_PIN = head
                (upstream.source_dir(root) / "tracked.txt").write_text("change\n")
                git("add", "tracked.txt", cwd=upstream.source_dir(root))
                git("-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "-q", "-m", "add", cwd=upstream.source_dir(root))
                head2 = git("rev-parse", "HEAD", cwd=upstream.source_dir(root)).stdout.strip()
                upstream.UPSTREAM_PIN = head2
                self.assertTrue(upstream.source_state(root)["ok"])
                (upstream.source_dir(root) / "tracked.txt").write_text("dirty\n")
                self.assertEqual(upstream.source_state(root)["reason"], "tracked_modifications")
            finally:
                upstream.UPSTREAM_PIN = original

    def test_missing_and_unsafe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(upstream.source_state(root)["reason"], "missing")
            upstream.source_dir(root).parent.mkdir(parents=True, exist_ok=True)
            upstream.source_dir(root).symlink_to(Path(directory))
            self.assertEqual(upstream.source_state(root)["reason"], "unsafe_path")


class IntegrityAndExtraction(unittest.TestCase):
    def test_hash_and_sri_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            blob = Path(directory) / "blob"
            blob.write_bytes(b"payload")
            self.assertFalse(upstream.verify_sha256(blob, "0" * 64))
            self.assertFalse(upstream.verify_sri(blob, "sha512-AAAA"))
            self.assertTrue(upstream.verify_sha256(blob, upstream.sha256_file(blob)))

    def _tar(self, path, entries):
        with tarfile.open(path, "w:gz") as tar:
            for name, kind, target in entries:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    data = b"x"
                    info.mode = 0o644
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                elif kind == "sym":
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    tar.addfile(info)
                elif kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)

    def test_unsafe_members_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            dest = base / "out"
            for name, entries in {
                "traversal": [("../evil.txt", "file", None)],
                "absolute": [("/etc/evil.txt", "file", None)],
                "symlink_escape": [("pkg/link", "sym", "../../etc/passwd")],
                "absolute_link": [("pkg/link", "sym", "/etc/passwd")],
            }.items():
                archive = base / (name + ".tgz")
                self._tar(archive, entries)
                with self.assertRaises(upstream.SetupError):
                    upstream.safe_extract(archive, dest)

    def test_safe_member_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "ok.tgz"
            self._tar(archive, [("pkg", "dir", None), ("pkg/file.txt", "file", None)])
            dest = base / "out"
            upstream.safe_extract(archive, dest)
            self.assertTrue((dest / "pkg" / "file.txt").is_file())


class NoGlobalFallback(unittest.TestCase):
    def test_off_host_url_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(upstream.SetupError):
                upstream.fetch("https://evil.example.com/node.txz", Path(directory) / "x")

    def test_private_paths_stay_under_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for getter in (upstream.private_root, upstream.runtimes_dir, upstream.node_bin,
                           upstream.bun_bin, upstream.cache_dir, upstream.state_dir):
                path = getter(root)
                self.assertTrue(str(path).startswith(str(root)))
            self.assertFalse(str(upstream.private_root(root)).startswith(str(Path.home())))


class BuildCommands(unittest.TestCase):
    def test_frozen_install_args(self):
        self.assertEqual(upstream.frozen_install_argv("/bun"),
                         ["/bun", "install", "--ignore-scripts", "--frozen-lockfile"])

    def test_cli_args(self):
        self.assertEqual(upstream.cli_argv("/bun", ["start"]),
                         ["/bun", "--no-install", "--conditions=eliza-source",
                          "packages/app/src/entry.ts", "start"])

    def test_registration_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / "plugins"
            names = []
            for index in range(12):
                name = "@elizaos/plugin-%02d" % index
                pkg = plugins / ("plugin-%02d" % index)
                pkg.mkdir(parents=True)
                (pkg / "package.json").write_text(json.dumps(
                    {"name": name, "elizaos": {"appRegister": True}, "scripts": {"build:js": "x"}}))
                names.append(name)
            skip = plugins / "plugin-skip"
            skip.mkdir()
            (skip / "package.json").write_text(json.dumps(
                {"name": "@elizaos/plugin-skip", "scripts": {"build:js": "x"}}))
            nobuild = plugins / "plugin-nobuild"
            nobuild.mkdir()
            (nobuild / "package.json").write_text(json.dumps(
                {"name": "@elizaos/plugin-nobuild", "elizaos": {"appRegister": True}}))
            selected = upstream.select_registration_packages(plugins)
            self.assertEqual([name for name, _ in selected], sorted(names))

    def test_setup_is_provider_free_and_uses_one_caller_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deadlines = []
            original = upstream.gate_decision
            try:
                upstream.gate_decision = lambda now=None: {"allowed": False}
                with patch.object(upstream, "require_platform"), \
                     patch.object(upstream, "ensure_node", side_effect=lambda *a, **k: deadlines.append(k["deadline"])), \
                     patch.object(upstream, "ensure_bun", side_effect=lambda *a, **k: deadlines.append(k["deadline"])), \
                     patch.object(upstream, "ensure_source", side_effect=lambda *a, **k: (deadlines.append(k["deadline"]) or root)), \
                     patch.object(upstream, "run_supervised", side_effect=lambda *a, **k: (deadlines.append(k["stop_at"]) or 0)), \
                     patch.object(upstream, "select_registration_packages", return_value=[]):
                    self.assertEqual(upstream.command_setup(root, timeout=120), 0)
            finally:
                upstream.gate_decision = original
            self.assertEqual(len(deadlines), 5)
            self.assertEqual(len(set(deadlines)), 1)
            self.assertGreater(dt.datetime.fromisoformat(deadlines[0]), dt.datetime.now(dt.timezone.utc))

    def test_setup_deadline_is_bounded_and_captured_once(self):
        now = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        self.assertEqual(upstream.setup_deadline(120, now=now),
                         "2026-10-15T00:02:00+00:00")
        for timeout in (True, False, 0, -1, 7201, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(upstream.SetupError):
                upstream.setup_deadline(timeout, now=now)

    def test_setup_cli_forwards_timeout_after_command_without_running_setup(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(upstream, "command_setup", return_value=0) as setup:
            root = Path(directory)
            code = upstream.main(
                ["setup", "--timeout", "23.5", "--node-path", "/trusted/node",
                 "--bun-path", "/trusted/bun"], root=root)
        self.assertEqual(code, 0)
        setup.assert_called_once_with(root, node_path="/trusted/node",
                                      bun_path="/trusted/bun", timeout=23.5)

    def test_setup_cli_rejects_invalid_timeout_before_setup(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(upstream, "command_setup") as setup, \
             patch("sys.stderr", new_callable=io.StringIO):
            code = upstream.main(["setup", "--timeout", "nan"], root=Path(directory))
        self.assertEqual(code, 2)
        setup.assert_not_called()


class RuntimeValidation(unittest.TestCase):
    def test_provided_runtime_version_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            good = write_exe(Path(directory) / "bun-good", "#!/bin/sh\necho 1.4.2\n")
            bad = write_exe(Path(directory) / "bun-bad", "#!/bin/sh\necho 1.4.0\n")
            self.assertEqual(upstream.validate_provided_runtime(good, "bun"), good)
            with self.assertRaises(upstream.SetupError):
                upstream.validate_provided_runtime(bad, "bun")
            with self.assertRaises(upstream.SetupError):
                upstream.validate_provided_runtime(Path(directory) / "missing", "node")


class ProcessCleanup(unittest.TestCase):
    def test_group_termination_leaves_no_orphan(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,time; subprocess.Popen(['sleep','30']); time.sleep(30)"],
            start_new_session=True)
        time.sleep(0.4)
        try:
            self.assertTrue(upstream._group_alive(child.pid))
            upstream.terminate_group(child.pid, time.time() + 3)
            child.wait()
            self.assertFalse(upstream._group_alive(child.pid))
        finally:
            upstream.terminate_group(child.pid, time.time() + 1)
            child.wait()

    def test_supervised_cutoff_stops_group(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            stop_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
            started = time.time()
            code = upstream.run_supervised(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=directory, env=dict(os.environ), log_path=log, stop_at=stop_at)
            self.assertEqual(code, 75)
            self.assertLess(time.time() - started, 10)

    def test_signal_death_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            code = upstream.run_supervised(
                [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
                cwd=directory, env=dict(os.environ), log_path=log)
            self.assertEqual(code, 137)

    def test_cutoff_cancels_orphan_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            log = Path(directory) / "log"
            script = ("import subprocess, sys, time; "
                      "p = subprocess.Popen(['sleep', '30']); "
                      "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(30)")
            stop_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=7)).isoformat()
            code = upstream.run_supervised([sys.executable, "-c", script, str(pid_file)],
                                           cwd=directory, env=dict(os.environ), log_path=log,
                                           stop_at=stop_at)
            self.assertEqual(code, 75)
            self.assertTrue(pid_file.is_file())
            grandchild = int(pid_file.read_text())
            deadline = time.time() + 5
            while pid_alive(grandchild) and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(pid_alive(grandchild))


class CliFixture(unittest.TestCase):
    def test_cli_uses_pinned_binary_clean_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(upstream.source_dir(root))
            argv_out = root / "argv.txt"
            env_out = root / "env.txt"
            write_exe(upstream.bun_bin(root),
                      "#!/bin/sh\nprintf '%%s\\n' \"$@\" > %s\nenv > %s\nexit 0\n"
                      % (argv_out, env_out))
            write_exe(upstream.node_bin(root), "#!/bin/sh\necho v24.15.0\n")
            upstream.record_runtime(root, "bun", upstream.bun_bin(root))
            original_pin = upstream.UPSTREAM_PIN
            original_gate = upstream.gate_decision
            os.environ["OPENAI_API_KEY"] = "leak"
            try:
                upstream.UPSTREAM_PIN = head
                upstream.gate_decision = lambda now=None: {"allowed": True, "stop_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()}
                code = upstream.main(["cli", "--", "--help"], root=root)
                self.assertEqual(code, 0)
                argv = argv_out.read_text().splitlines()
                self.assertEqual(argv, ["--no-install", "--conditions=eliza-source",
                                        "packages/app/src/entry.ts", "--help"])
                env = env_out.read_text()
                self.assertIn("ELIZA_STATE_DIR=%s" % upstream.state_dir(root), env)
                self.assertNotIn("OPENAI_API_KEY", env)
            finally:
                upstream.UPSTREAM_PIN = original_pin
                upstream.gate_decision = original_gate
                os.environ.pop("OPENAI_API_KEY", None)

    def test_cli_refuses_unpinned_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_repo(upstream.source_dir(root))
            write_exe(upstream.bun_bin(root), "#!/bin/sh\nexit 0\n")
            original_gate = upstream.gate_decision
            try:
                upstream.gate_decision = lambda now=None: {"allowed": True, "stop_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()}
                self.assertEqual(upstream.main(["cli", "--", "--help"], root=root), 1)
            finally:
                upstream.gate_decision = original_gate


class DefaultArchivePaths(unittest.TestCase):
    def test_extraction_lands_on_canonical_binary_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node_archive = root / "node.tgz"
            make_archive(node_archive,
                         files=[("node-v24.15.0-linux-x64/bin/node", "#!/bin/sh\necho v24.15.0\n", 0o755)],
                         dirs=["node-v24.15.0-linux-x64", "node-v24.15.0-linux-x64/bin"])
            bun_archive = root / "bun.tgz"
            make_archive(bun_archive,
                         files=[("package/bin/bun", "#!/bin/sh\necho 1.4.2\n", 0o755)],
                         dirs=["package", "package/bin"])
            original = upstream._ensure_archive
            upstream._ensure_archive = lambda *a, **k: (bun_archive if k.get("sri") else node_archive)
            try:
                upstream.ensure_node(root)
                upstream.ensure_bun(root)
            finally:
                upstream._ensure_archive = original
            self.assertTrue(upstream.node_bin(root).is_file())
            self.assertEqual(upstream.bun_bin(root),
                             upstream.runtimes_dir(root) / "bun" / "package" / "bin" / "bun")
            self.assertTrue(upstream.bun_bin(root).is_file())
            self.assertIsNotNone(upstream.verified_runtime(root, "node"))
            self.assertIsNotNone(upstream.verified_runtime(root, "bun"))


class TamperReexecution(unittest.TestCase):
    def test_tampered_cached_binary_is_not_trusted_or_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bun.tgz"
            make_archive(archive, files=[("package/bin/bun", "#!/bin/sh\necho 1.4.2\n", 0o755)],
                         dirs=["package", "package/bin"])
            original = upstream._ensure_archive
            upstream._ensure_archive = lambda *a, **k: archive
            try:
                upstream.ensure_bun(root)
            finally:
                upstream._ensure_archive = original
            self.assertIsNotNone(upstream.verified_runtime(root, "bun"))
            with open(upstream.bun_bin(root), "ab") as handle:
                handle.write(b"\n# tamper\n")
            self.assertIsNone(upstream.verified_runtime(root, "bun"))
            self.assertFalse(upstream.runtime_state(root)["ok"])
            head = make_repo(upstream.source_dir(root))
            original_pin = upstream.UPSTREAM_PIN
            original_gate = upstream.gate_decision
            try:
                upstream.UPSTREAM_PIN = head
                upstream.gate_decision = lambda now=None: {"allowed": True, "stop_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()}
                self.assertEqual(upstream.main(["cli", "--", "--help"], root=root), 1)
            finally:
                upstream.UPSTREAM_PIN = original_pin
                upstream.gate_decision = original_gate


class AncestorSafety(unittest.TestCase):
    def test_malicious_ancestor_preserves_external_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            (external / "marker.txt").write_text("keep")
            upstream.runtimes_dir(root).mkdir(parents=True, exist_ok=True)
            (upstream.runtimes_dir(root) / "bun").symlink_to(external)
            archive = root / "bun.tgz"
            make_archive(archive, files=[("package/bin/bun", "#!/bin/sh\necho 1.4.2\n", 0o755)],
                         dirs=["package", "package/bin"])
            original = upstream._ensure_archive
            upstream._ensure_archive = lambda *a, **k: archive
            try:
                with self.assertRaises(upstream.SetupError):
                    upstream.ensure_bun(root)
            finally:
                upstream._ensure_archive = original
            self.assertTrue((external / "marker.txt").is_file())

    def test_builder_symlink_preserves_external_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            (external / "marker.txt").write_text("keep")
            (root / ".builder").symlink_to(external)
            archive = root / "bun.tgz"
            make_archive(archive, files=[("package/bin/bun", "#!/bin/sh\necho 1.4.2\n", 0o755)],
                         dirs=["package", "package/bin"])
            original = upstream._ensure_archive
            upstream._ensure_archive = lambda *a, **k: archive
            try:
                with self.assertRaises(upstream.SetupError):
                    upstream.ensure_bun(root)
            finally:
                upstream._ensure_archive = original
            self.assertTrue((external / "marker.txt").is_file())
            self.assertFalse((external / "upstream").exists())


class ManifestTrust(unittest.TestCase):
    def test_manifest_path_must_equal_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foreign = root / "foreign.bin"
            foreign.write_bytes(b"#!/bin/sh\necho 1.4.2\n")
            foreign.chmod(0o755)
            manifest = upstream.integrity_path(root)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(
                {"bun": {"path": str(foreign), "sha256": upstream.sha256_file(foreign)}}))
            # Names a foreign path: rejected, never hashed-and-returned unchecked.
            self.assertIsNone(upstream.verified_runtime(root, "bun"))
            self.assertFalse(upstream.runtime_state(root)["ok"])

    def test_canonical_path_wrong_digest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foreign = root / "foreign.bin"
            foreign.write_bytes(b"copied-digest")
            canonical = upstream.bun_bin(root)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_bytes(b"actual-tampered-bytes")
            manifest = upstream.integrity_path(root)
            manifest.write_text(json.dumps(
                {"bun": {"path": str(canonical), "sha256": upstream.sha256_file(foreign)}}))
            self.assertIsNone(upstream.verified_runtime(root, "bun"))


class DeadlineBudget(unittest.TestCase):
    def test_late_eof_cannot_publish_download(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "archive"
            response = io.BytesIO(b"")
            deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()
            with patch.object(upstream.urllib.request, "urlopen", return_value=response), \
                    patch.object(upstream, "past_deadline", side_effect=[False, True]):
                with self.assertRaises(upstream.SetupError):
                    upstream.fetch(upstream.NODE_URL, target, deadline=deadline)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".part").exists())

    def test_blocking_read_is_interrupted_at_absolute_deadline(self):
        class BlockedResponse(io.BytesIO):
            def read(self, size):
                time.sleep(30)
                return b""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "archive"
            deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)).isoformat()
            started = time.monotonic()
            with patch.object(upstream.urllib.request, "urlopen", return_value=BlockedResponse()):
                with self.assertRaises(upstream.SetupError):
                    upstream.fetch(upstream.NODE_URL, target, deadline=deadline)
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".part").exists())

    def test_clone_consumes_remaining_and_blocks_next(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            original_git = upstream._git
            original_state = upstream.source_state
            upstream._git = lambda args, cwd=None, timeout=600: (
                calls.append(timeout) or subprocess.CompletedProcess(args, 0, "", ""))
            upstream.source_state = lambda r, deadline=None: {"ok": True, "reason": "pinned_clean"}
            try:
                deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat()
                upstream.ensure_source(root, deadline=deadline)
                self.assertEqual(len(calls), 2)  # clone then checkout
                self.assertLessEqual(calls[0], 3)
                self.assertLessEqual(calls[1], calls[0])  # fresh, decreasing budget
            finally:
                upstream._git = original_git
                upstream.source_state = original_state

    def test_existing_checkout_validation_keeps_original_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (upstream.source_dir(root) / ".git").mkdir(parents=True)
            calls = []
            original_git = upstream._git
            def metadata(args, cwd=None, timeout=600):
                calls.append((args, timeout))
                return subprocess.CompletedProcess(args, 0,
                    upstream.UPSTREAM_PIN if args[0] == "rev-parse" else "", "")
            upstream._git = metadata
            try:
                deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat()
                upstream.ensure_source(root, deadline=deadline)
                self.assertEqual([args[0] for args, _ in calls], ["rev-parse", "status"])
                self.assertLessEqual(calls[0][1], 3)
                self.assertLessEqual(calls[1][1], calls[0][1])
                calls.clear()
                expired = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
                with self.assertRaises(upstream.SetupError):
                    upstream.source_state(root, deadline=expired)
                self.assertEqual(calls, [])
            finally:
                upstream._git = original_git

    def test_expired_deadline_blocks_clone_and_provisioning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deadline = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
            self.assertTrue(upstream.past_deadline(deadline))
            with self.assertRaises(upstream.SetupError):
                upstream.ensure_source(root, deadline=deadline)
            with self.assertRaises(upstream.SetupError):
                upstream.ensure_node(root, deadline=deadline)
            with self.assertRaises(upstream.SetupError):
                upstream.time_budget(deadline, 30)
            # version phase cap can never exceed the remaining budget
            self.assertLessEqual(upstream.time_budget(None, 30), 30)


if __name__ == "__main__":
    unittest.main()
