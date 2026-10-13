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
            original_pin = upstream.UPSTREAM_PIN
            original_gate = upstream.gate_decision
            os.environ["OPENAI_API_KEY"] = "leak"
            try:
                upstream.UPSTREAM_PIN = head
                upstream.gate_decision = lambda now=None: {"allowed": True, "stop_at": None}
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
                upstream.gate_decision = lambda now=None: {"allowed": True, "stop_at": None}
                self.assertEqual(upstream.main(["cli", "--", "--help"], root=root), 1)
            finally:
                upstream.gate_decision = original_gate


if __name__ == "__main__":
    unittest.main()
