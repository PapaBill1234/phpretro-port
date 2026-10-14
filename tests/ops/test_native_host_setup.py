import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "native_host", Path(__file__).resolve().parents[2] / "scripts/native-host.py")
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def future_deadline(seconds=600):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def expired_deadline(seconds=1):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


def pid_running(pid):
    """True while pid is a live (non-zombie) process; fail proof otherwise."""
    try:
        data = Path("/proc/%d/stat" % pid).read_bytes()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AssertionError("cannot prove pid %d gone: %s" % (pid, error))
    close = data.rfind(b")")
    if close < 0:
        raise AssertionError("malformed stat for pid %d" % pid)
    fields = data[close + 2:].split()
    if not fields:
        raise AssertionError("malformed stat for pid %d" % pid)
    return fields[0] != b"Z"


def make_repo(path, files=None):
    path.mkdir(parents=True, exist_ok=True)
    git("-c", "init.defaultBranch=main", "init", "-q", cwd=path)
    for name, body in (files or {}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    git("add", "-A", cwd=path)
    git("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def write_exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_tar(path, entries):
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
    return path


def write_manifest(root, patch_body=b"diff --git a/x b/x\n", digest=None, entry=None,
                   bind=True, target=None):
    directory = Path(root) / host.PATCH_DIR_REL
    directory.mkdir(parents=True, exist_ok=True)
    patch = directory / "fix.patch"
    patch.write_bytes(patch_body)
    digest = digest or hashlib.sha256(patch_body).hexdigest()
    if entry is None:
        document = {"patch": {"file": "fix.patch", "sha256": digest,
                              "target_file": target or {"path": "a/file.rs",
                                                        "base_sha256": "a" * 64,
                                                        "patched_sha256": "b" * 64}}}
        if bind:
            document["upstream"] = {"commit": host.CODEX_PIN}
            document["patch"]["applies_to_commit"] = host.CODEX_PIN
    else:
        document = entry
    (directory / host.PATCH_MANIFEST).write_text(json.dumps(document))
    return patch, digest


class PrivateLayout(unittest.TestCase):
    def test_paths_stay_private_and_outside_home(self):
        root = Path("/tmp/example-root")
        for getter in (host.private_root, host.source_dir, host.build_dir, host.toolchain_dir,
                       host.cargo_home, host.target_dir, host.cache_dir, host.logs_dir, host.home_dir):
            self.assertTrue(str(getter(root)).startswith(str(root) + os.sep))
            self.assertFalse(str(getter(root)).startswith(str(Path.home())))
        self.assertTrue(str(host.source_dir(root)).startswith(str(host.private_root(root))))
        self.assertTrue(str(host.toolchain_cargo(root)).endswith("toolchain/bin/cargo"))


class SourcePin(unittest.TestCase):
    def test_reuse_when_pinned_and_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(host.source_dir(root), {"rust-toolchain.toml": '[toolchain]\nchannel = "1.95.0"\n'})
            original = host.CODEX_PIN
            try:
                host.CODEX_PIN = head
                self.assertEqual(host.ensure_source(root, future_deadline()), host.source_dir(root))
                self.assertTrue(host.source_state(root)["ok"])
            finally:
                host.CODEX_PIN = original

    def test_wrong_pin_and_modification_refused_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(host.source_dir(root), {"rust-toolchain.toml": '[toolchain]\nchannel = "1.95.0"\n'})
            original = host.CODEX_PIN
            try:
                host.CODEX_PIN = "0" * 40
                self.assertEqual(host.source_state(root)["reason"], "wrong_pin")
                with self.assertRaises(host.SetupError):
                    host.ensure_source(root, future_deadline())
                host.CODEX_PIN = head
                (host.source_dir(root) / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "9.9.9"\n')
                self.assertEqual(host.source_state(root)["reason"], "tracked_modifications")
                with self.assertRaises(host.SetupError):
                    host.ensure_source(root, future_deadline())
                # The dirty tree was refused, not repaired.
                self.assertIn("9.9.9", (host.source_dir(root) / "rust-toolchain.toml").read_text())
            finally:
                host.CODEX_PIN = original

    def test_missing_and_unsafe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(host.source_state(root)["reason"], "missing")
            host.source_dir(root).parent.mkdir(parents=True, exist_ok=True)
            host.source_dir(root).symlink_to(Path(directory))
            self.assertEqual(host.source_state(root)["reason"], "unsafe_path")

    def test_declared_rust_channel_read_from_codex_rs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(host.source_dir(root), {"codex-rs/rust-toolchain.toml": '[toolchain]\nchannel = "1.95.0"\n'})
            original = host.CODEX_PIN
            try:
                host.CODEX_PIN = head
                self.assertEqual(host.declared_rust(host.source_dir(root)), "1.95.0")
                self.assertEqual(host.ensure_source(root, future_deadline()), host.source_dir(root))
            finally:
                host.CODEX_PIN = original

    def test_source_toolchain_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(host.source_dir(root), {"rust-toolchain.toml": '[toolchain]\nchannel = "1.94.0"\n'})
            original = host.CODEX_PIN
            try:
                host.CODEX_PIN = head
                self.assertEqual(host.declared_rust(host.source_dir(root)).split("-", 1)[0], "1.94.0")
                with self.assertRaises(host.SetupError):
                    host.ensure_source(root, future_deadline())
            finally:
                host.CODEX_PIN = original


class ReviewedPatch(unittest.TestCase):
    def test_verified_digest_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch, digest = write_manifest(root)
            resolved, returned, manifest = host.reviewed_patch(root)
            self.assertEqual(resolved, patch)
            self.assertEqual(returned, digest)
            self.assertEqual(manifest["upstream"]["commit"], host.CODEX_PIN)

    def test_unbound_manifest_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(root, bind=False)
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(root)

    def test_unknown_digest_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(root, digest="z" * 64)
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(root)

    def test_mismatch_and_missing_manifest_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(root, digest="0" * 64)
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(Path(directory))

    def test_escaping_and_symlinked_patch_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = Path(directory) / "outside.patch"
            outside.write_bytes(b"x")
            directory_path = root / host.PATCH_DIR_REL
            directory_path.mkdir(parents=True)
            (directory_path / host.PATCH_MANIFEST).write_text(json.dumps(
                {"upstream": {"commit": host.CODEX_PIN},
                 "patch": {"file": "../outside.patch", "sha256": hashlib.sha256(b"x").hexdigest(),
                           "applies_to_commit": host.CODEX_PIN}}))
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(root)
        with tempfile.TemporaryDirectory() as directory:
            write_manifest(Path(directory))
            directory_path = Path(directory) / host.PATCH_DIR_REL
            (directory_path / "fix.patch").unlink()
            (directory_path / "fix.patch").symlink_to(directory_path / host.PATCH_MANIFEST)
            with self.assertRaises(host.SetupError):
                host.reviewed_patch(Path(directory))


class ToolchainManifest(unittest.TestCase):
    MANIFEST = """
[pkg.rust]
version = "1.95.0"

[pkg.rust.target.x86_64-unknown-linux-gnu]
available = true
url = "https://static.rust-lang.org/dist/2026-01-01/rust-1.95.0-x86_64-unknown-linux-gnu.tar.xz"
hash = "sha256-%s"
"""

    def test_hash_forms_and_rejections(self):
        good = "a" * 64
        self.assertEqual(host.parse_hash("sha256-" + good), good)
        self.assertEqual(host.parse_hash("sha256:" + good), good)
        self.assertEqual(host.parse_hash(good), good)
        for bad in ("", "sha256-xyz", "a" * 63, "g" * 64, None):
            with self.assertRaises(host.SetupError):
                host.parse_hash(bad)

    def test_manifest_entry_parsed(self):
        expected = "b" * 64
        url, digest = host.parse_channel_manifest(self.MANIFEST % expected)
        self.assertEqual(digest, expected)
        self.assertTrue(url.startswith("https://static.rust-lang.org/"))

    def test_missing_target_and_bad_host_refused(self):
        with self.assertRaises(host.SetupError):
            host.parse_channel_manifest(self.MANIFEST % ("c" * 64), target="aarch64-unknown-linux-gnu")
        offhost = self.MANIFEST % ("e" * 64)
        offhost = offhost.replace("https://static.rust-lang.org", "https://evil.example.com")
        with self.assertRaises(host.SetupError):
            host.parse_channel_manifest(offhost)


class ArchivesAndPaths(unittest.TestCase):
    def test_unsafe_members_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, entries in {
                "traversal": [("../evil.txt", "file", None)],
                "absolute": [("/etc/evil.txt", "file", None)],
                "symlink_escape": [("pkg/link", "sym", "../../etc/passwd")],
                "absolute_link": [("pkg/link", "sym", "/etc/passwd")],
            }.items():
                archive = make_tar(base / (name + ".tgz"), entries)
                with self.assertRaises(host.SetupError):
                    host.safe_extract(archive, base / "out")

    def test_safe_member_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_tar(base / "ok.tgz", [("pkg", "dir", None), ("pkg/file.txt", "file", None)])
            host.safe_extract(archive, base / "out")
            self.assertTrue((base / "out" / "pkg" / "file.txt").is_file())

    def test_private_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(host.SetupError):
                host.assert_private(root, Path(directory) / "outside", "delete")
            host.assert_private(root, host.source_dir(root) / "nested", "ok")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (Path(directory) / ".builder").symlink_to(Path(directory))
            with self.assertRaises(host.SetupError):
                host.assert_anchor(root, "delete")


class StrictCutoff(unittest.TestCase):
    def test_invalid_cutoffs_refused(self):
        for bad in (None, "", "not-a-time", "2026-01-01T00:00:00",            # naive / malformed
                    expired_deadline(), 42, "  "):
            with self.assertRaises(host.SetupError):
                host.time_budget(bad, 60)
        self.assertLessEqual(host.time_budget(future_deadline(600), 60), 60)
        self.assertGreater(host.time_budget(future_deadline(600), 60), 0)

    def test_cutoff_is_immutable_not_refreshed_per_stage(self):
        deadline = future_deadline(30)
        first = host.time_budget(deadline, 1000)
        time.sleep(0.2)
        second = host.time_budget(deadline, 1000)
        self.assertLessEqual(first, 30)
        self.assertLess(second, first)  # recomputed from the same immutable cutoff

    def test_missing_malformed_naive_never_launch(self):
        for bad in (None, "", "not-a-time", "2026-01-01T00:00:00"):
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(host.subprocess, "Popen") as popen:
                    with self.assertRaises(host.SetupError):
                        host.run_supervised([sys.executable, "-c", "pass"], cwd=directory,
                                            env={"PATH": "/usr/bin:/bin"},
                                            log_path=Path(directory) / "l.log", stop_at=bad)
                    self.assertFalse(popen.called, "launched with cutoff %r" % bad)

    def test_expired_cutoff_never_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(host.subprocess, "Popen") as popen:
                with self.assertRaises(host.SetupError):
                    host.run_supervised(["true"], cwd=directory, env={},
                                        log_path=Path(directory) / "l.log", stop_at=expired_deadline())
                self.assertFalse(popen.called)
            with patch.object(host.subprocess, "Popen") as popen:
                with self.assertRaises(host.SetupError):
                    host.ensure_source(Path(directory), None)
                with self.assertRaises(host.SetupError):
                    host.ensure_toolchain(Path(directory), None)
                self.assertFalse(popen.called)

    def test_archive_refuses_bad_cutoff_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(host.SetupError):
                host._ensure_archive("https://static.rust-lang.org/x.txz",
                                     Path(directory) / "x.txz", deadline=expired_deadline())

    def test_off_host_url_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(host.SetupError):
                host.fetch("https://evil.example.com/rust.txz", Path(directory) / "x",
                           deadline=future_deadline())

    def test_status_read_only_needs_no_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            state = host.host_state(Path(directory))  # no cutoff, still returns
            self.assertFalse(state["ready"])
            self.assertFalse(state["qualified"])


class ProcessBounds(unittest.TestCase):
    GRANDCHILD = ("import subprocess,sys,time,os,signal\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
                  "open(sys.argv[1],'w').write('%d %d'%(os.getpid(),p.pid))\n"
                  "time.sleep(120)\n")

    def _pids(self, path):
        deadline = time.time() + 5
        while time.time() < deadline and not path.exists():
            time.sleep(0.05)
        self.assertTrue(path.exists(), "child never recorded its pids")
        return [int(x) for x in path.read_text().split()]

    def _assert_gone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline and pid_running(pid):
            time.sleep(0.05)
        self.assertFalse(pid_running(pid), "pid %d still running" % pid)

    def test_timeout_cancel_cleans_resistant_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            pidfile = d / "pids"
            code = host.run_supervised([sys.executable, "-c", self.GRANDCHILD, str(pidfile)],
                                       cwd=d, env={"PATH": "/usr/bin:/bin"},
                                       log_path=d / "run.log", stop_at=future_deadline(8))
            self.assertEqual(code, 75)
            for pid in self._pids(pidfile):
                self._assert_gone(pid)

    def test_normal_early_exit_still_cleans_resistant_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            pidfile = d / "pids"
            # Leader exits immediately after spawning a TERM-ignoring grandchild.
            code = ("import subprocess,sys,os,signal\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
                    "open(sys.argv[1],'w').write('%d %d'%(os.getpid(),p.pid))\n")
            rc = host.run_supervised([sys.executable, "-c", code, str(pidfile)],
                                     cwd=d, env={"PATH": "/usr/bin:/bin"},
                                     log_path=d / "run.log", stop_at=future_deadline(8))
            self.assertIn(rc, (0, 1))
            for pid in self._pids(pidfile):
                self._assert_gone(pid)

    def test_log_bounded_while_child_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            code = "import sys; sys.stdout.write('x'*200000); sys.stdout.flush()"
            rc = host.run_supervised([sys.executable, "-c", code], cwd=d,
                                     env={"PATH": "/usr/bin:/bin"}, log_path=d / "big.log",
                                     stop_at=future_deadline(30), max_log_bytes=4096)
            self.assertEqual(rc, 0)
            text = (d / "big.log").read_text()
            self.assertLessEqual(len(text), 4096 + 128)
            self.assertIn("truncated at limit", text)

    def _resource_stop(self, later):
        """Run a real leader+TERM-resistant descendant, then feed a bad reading."""
        readings = iter([host.RAM_MIN_AVAILABLE_BYTES + 1, *later])
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            pidfile = d / "pids"
            started = time.monotonic()
            code = host.run_supervised(
                [sys.executable, "-c", self.GRANDCHILD, str(pidfile)],
                cwd=d, env={"PATH": "/usr/bin:/bin"}, log_path=d / "run.log",
                stop_at=future_deadline(20), ram_floor_bytes=host.RAM_MIN_AVAILABLE_BYTES,
                ram_probe=lambda: next(readings), ram_interval=0.2)
            self.assertEqual(code, host.RESOURCE_STOP_EXIT)  # failure, never the child's 0
            pids = self._pids(pidfile)  # real children recorded their pids first
            self.assertTrue(pids)
            for pid in pids:
                self._assert_gone(pid)
            self.assertLess(time.monotonic() - started, 20)  # stopped before cutoff
            return pids

    def test_resource_stop_cleans_group_on_low_ram(self):
        self._resource_stop([1])  # below the floor after launch

    def test_resource_stop_cleans_group_on_unknown_ram(self):
        self._resource_stop([None])  # unusable reading after launch

    def test_resource_stop_pre_launch_fails_closed(self):
        # A bad reading before launch refuses without starting any child.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(host.SetupError):
                host.run_supervised([sys.executable, "-c", "pass"], cwd=directory,
                                    env={"PATH": "/usr/bin:/bin"},
                                    log_path=Path(directory) / "run.log",
                                    stop_at=future_deadline(20),
                                    ram_floor_bytes=host.RAM_MIN_AVAILABLE_BYTES,
                                    ram_probe=lambda: None)

    def test_no_ram_floor_leaves_other_callers_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            code = host.run_supervised([sys.executable, "-c", "pass"], cwd=directory,
                                       env={"PATH": "/usr/bin:/bin"},
                                       log_path=Path(directory) / "run.log",
                                       stop_at=future_deadline(20))
            self.assertEqual(code, 0)


class ProcStatStrictness(unittest.TestCase):
    def _proc(self, base, entries):
        for pid, content in entries.items():
            (base / str(pid)).mkdir()
            (base / str(pid) / "stat").write_bytes(content)

    def test_live_and_zombie_tracked_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._proc(base, {11: b"1 (a) S 1 100 100", 12: b"1 (a) Z 1 100 100"})
            self.assertTrue(host._group_alive(100, proc=str(base)))
            self.assertFalse(host._group_alive(999, proc=str(base)))

    def test_malformed_stat_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self._proc(base, {7: b"no-parens"})
            with self.assertRaises(host.SetupError):
                host._group_alive(1, proc=str(base))

    def test_io_error_refuses_instead_of_assuming_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "8").mkdir()
            (base / "8" / "stat").mkdir()  # reading a directory is an OSError
            with self.assertRaises(host.SetupError):
                host._group_alive(1, proc=str(base))


class Containment(unittest.TestCase):
    def test_dotdot_and_absolute_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(host.SetupError):
                host.assert_private(root, host.private_root(root) / ".." / ".." / "evil", "delete")
            with self.assertRaises(host.SetupError):
                host.assert_private(root, Path("/etc/passwd"), "delete")

    def test_symlinked_private_root_refused_before_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".builder").mkdir()
            host.private_root(root).symlink_to(root / "elsewhere")
            with self.assertRaises(host.SetupError):
                with host.setup_lock(root):
                    pass
            self.assertFalse((root / "elsewhere").exists())

    def test_symlinked_lockfile_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host.private_root(root).mkdir(parents=True)
            (host.private_root(root) / "native-host.lock").symlink_to(root / "elsewhere")
            with self.assertRaises(host.SetupError):
                with host.setup_lock(root):
                    pass

    def test_named_private_subtree_symlink_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host.private_root(root).mkdir(parents=True)
            (host.private_root(root) / "home").symlink_to(root)
            with self.assertRaises(host.SetupError):
                host.assert_private(root, host.home_dir(root), "home")


class ArchiveBounds(unittest.TestCase):
    def test_duplicate_member_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_tar(base / "dup.tgz", [("a.txt", "file", None), ("a.txt", "file", None)])
            with self.assertRaises(host.SetupError):
                host.safe_extract(archive, base / "out")

    def test_hardlink_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with tarfile.open(base / "h.tgz", "w:gz") as tar:
                info = tarfile.TarInfo("link")
                info.type = tarfile.LNKTYPE
                info.linkname = "target"
                tar.addfile(info)
            with self.assertRaises(host.SetupError):
                host.safe_extract(base / "h.tgz", base / "out")

    def test_member_under_symlink_dir_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_tar(base / "s.tgz", [("d", "sym", "sub"), ("d/x", "file", None)])
            with self.assertRaises(host.SetupError):
                host.safe_extract(archive, base / "out")

    def test_member_over_size_bound_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.object(host, "ARCHIVE_MAX_MEMBER_BYTES", 1):
                with tarfile.open(base / "big.tgz", "w:gz") as tar:
                    data = b"xx"
                    info = tarfile.TarInfo("big")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                with self.assertRaises(host.SetupError):
                    host.safe_extract(base / "big.tgz", base / "out")

    def test_extract_requires_secure_filter(self):
        class FakeTar:
            def extract(self, member, path=None, filter=None):
                raise TypeError("filter unsupported")

        with self.assertRaises(host.SetupError):
            host._extract_member(FakeTar(), None, "/tmp")


class ManifestIntegrity(unittest.TestCase):
    def test_redirect_handler_refuses(self):
        with self.assertRaises(host.SetupError):
            host._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example.com/x")

    def test_oversize_download_refused(self):
        chunks = [b"x" * 4096, b"y" * 4096, b""]

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self):
                return "https://static.rust-lang.org/x"

            def read(self, _n):
                return chunks.pop(0) if chunks else b""

        class Opener:
            def open(self, request, timeout=None):
                return Resp()

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(host, "_opener", lambda: Opener()):
                with self.assertRaises(host.SetupError):
                    host.fetch("https://static.rust-lang.org/x", Path(directory) / "x",
                               deadline=future_deadline(), max_bytes=5000)

    def _fake_fetch(self, good, manifest_body):
        def fetch(url, target, deadline=None, cap=300, max_bytes=None):
            target = Path(target)
            if url.endswith(".sha256"):
                target.write_text("%s  channel-rust-1.95.0.toml\n" % hashlib.sha256(good).hexdigest())
            else:
                target.write_bytes(manifest_body)
            return target
        return fetch

    def test_tampered_cache_discarded_and_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dest = host.cache_dir(root) / "channel-rust-1.95.0.toml"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"tampered")
            good = b"version = '1.95.0'\n"
            with patch.object(host, "fetch", self._fake_fetch(good, good)):
                _, digest = host._ensure_manifest(root, host.RUST_MANIFEST_URL, dest, future_deadline())
            self.assertEqual(digest, hashlib.sha256(good).hexdigest())
            self.assertEqual(dest.read_bytes(), good)

    def test_unverifiable_manifest_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dest = host.cache_dir(root) / "channel-rust-1.95.0.toml"
            dest.parent.mkdir(parents=True)
            with patch.object(host, "fetch", self._fake_fetch(b"good", b"bad")):
                with self.assertRaises(host.SetupError):
                    host._ensure_manifest(root, host.RUST_MANIFEST_URL, dest, future_deadline())


class RamParsing(unittest.TestCase):
    def _read(self, text):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text(text)
            return host.available_ram_bytes(meminfo)

    def test_canonical_kb_value_parsed(self):
        self.assertEqual(self._read("MemTotal: 1 kB\nMemAvailable: 3072 kB\n"), 3072 * 1024)

    def test_unknown_unit_fails_closed(self):
        self.assertIsNone(self._read("MemAvailable: 3072 MB\n"))

    def test_missing_unit_fails_closed(self):
        self.assertIsNone(self._read("MemAvailable: 3072\n"))

    def test_negative_value_fails_closed(self):
        self.assertIsNone(self._read("MemAvailable: -1 kB\n"))

    def test_non_integer_value_fails_closed(self):
        self.assertIsNone(self._read("MemAvailable: many kB\n"))

    def test_missing_line_fails_closed(self):
        self.assertIsNone(self._read("MemTotal: 4096 kB\n"))

    def test_unreadable_file_fails_closed(self):
        self.assertIsNone(host.available_ram_bytes(Path("/nonexistent/meminfo")))

    def test_require_available_ram_refuses_none_and_low(self):
        with self.assertRaises(host.SetupError):
            host.require_available_ram(None, host.RAM_MIN_AVAILABLE_BYTES)
        with self.assertRaises(host.SetupError):
            host.require_available_ram(host.RAM_MIN_AVAILABLE_BYTES - 1,
                                       host.RAM_MIN_AVAILABLE_BYTES)
        host.require_available_ram(host.RAM_MIN_AVAILABLE_BYTES, host.RAM_MIN_AVAILABLE_BYTES)


class ResourcePreflight(unittest.TestCase):
    def test_low_disk_refused(self):
        low = SimpleNamespace(free=host.DISK_MIN_FREE_BYTES - 1)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(host.SetupError):
                host.preflight_resources(directory, disk_usage=lambda _: low)

    def test_low_ram_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemTotal: 1000000 kB\nMemAvailable: %d kB\n"
                               % (host.RAM_MIN_AVAILABLE_BYTES // 1024 - 1))
            plenty = SimpleNamespace(free=host.DISK_MIN_FREE_BYTES + 1)
            with self.assertRaises(host.SetupError):
                host.preflight_resources(directory, disk_usage=lambda _: plenty, meminfo=meminfo)

    def test_sufficient_resources_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemAvailable: %d kB\n" % (host.RAM_MIN_AVAILABLE_BYTES // 1024 + 1024))
            plenty = SimpleNamespace(free=host.DISK_MIN_FREE_BYTES + 1)
            report = host.preflight_resources(directory, disk_usage=lambda _: plenty, meminfo=meminfo)
            self.assertGreaterEqual(report["free_disk_bytes"], host.DISK_MIN_FREE_BYTES)


class ReleaseLockRepair(unittest.TestCase):
    BASE = b'''version = 4

[[package]]
name = "local"
version = "0.0.0"
dependencies = ["external"]

[[package]]
name = "external"
version = "0.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "unchanged"
'''
    OUTPUT = BASE.replace(b'version = "0.0.0"', b'version = "0.162.0"', 1)

    def test_only_local_versions_change_and_repeat_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            lock = build / "codex-rs/Cargo.lock"
            lock.parent.mkdir()
            lock.write_bytes(self.BASE)
            with patch.object(host, "LOCK_BASE_SHA256", hashlib.sha256(self.BASE).hexdigest()), \
                    patch.object(host, "LOCK_BUILD_SHA256", hashlib.sha256(self.OUTPUT).hexdigest()), \
                    patch.object(host, "LOCK_LOCAL_PACKAGES", 1):
                host.repair_release_lock(build)
                self.assertEqual(lock.read_bytes(), self.OUTPUT)
                with self.assertRaises(host.SetupError):
                    host.repair_release_lock(build)
                self.assertEqual(lock.read_bytes(), self.OUTPUT)

    def test_unknown_input_and_wrong_output_never_write(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            lock = build / "codex-rs/Cargo.lock"
            lock.parent.mkdir()
            lock.write_bytes(self.BASE)
            with self.assertRaises(host.SetupError):
                host.repair_release_lock(build)
            with patch.object(host, "LOCK_BASE_SHA256", hashlib.sha256(self.BASE).hexdigest()), \
                    patch.object(host, "LOCK_LOCAL_PACKAGES", 1):
                with self.assertRaises(host.SetupError):
                    host.repair_release_lock(build)
            self.assertEqual(lock.read_bytes(), self.BASE)

    def test_symlink_cannot_mutate_target(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            target = build / "trusted.lock"
            target.write_bytes(self.BASE)
            lock = build / "codex-rs/Cargo.lock"
            lock.parent.mkdir()
            lock.symlink_to(target)
            with self.assertRaises(host.SetupError):
                host.repair_release_lock(build)
            self.assertEqual(target.read_bytes(), self.BASE)


class BuildContract(unittest.TestCase):
    def test_build_uses_nested_cargo_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = host.build_dir(root)
            workspace = build / "codex-rs"
            workspace.mkdir(parents=True)
            (workspace / "Cargo.toml").write_text('[workspace]\n')
            (workspace / "Cargo.lock").write_bytes(b"fixture lock")
            compiler = root / "cargo-fixture"
            compiler.write_text(
                '#!' + sys.executable + '\n'
                'import json,os,sys\nfrom pathlib import Path\n'
                'assert Path("Cargo.toml").is_file(), "missing nested workspace"\n'
                'obs=Path(os.environ["CARGO_TARGET_DIR"])\n'
                'obs.mkdir(parents=True,exist_ok=True)\n'
                '(obs/"fixture-observation.json").write_text(\n'
                '    json.dumps({"argv":sys.argv[1:],"env":dict(os.environ)}))\n'
                'target=obs/"release"/"codex"\n'
                'target.parent.mkdir(parents=True,exist_ok=True)\n'
                'target.write_bytes(b"controlled-build-result")\n'
            )
            compiler.chmod(0o700)
            patch_path = root / "reviewed.patch"
            patch_path.write_text("controlled patch")
            deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()
            with patch.object(host, "gate_cutoff", return_value=deadline), \
                    patch.object(host, "ensure_toolchain", return_value=compiler), \
                    patch.object(host, "prepare_build", return_value=(build, patch_path, "a" * 64)), \
                    patch.object(host, "preflight_resources", return_value={"fixture": True}), \
                    patch.object(host, "LOCK_BUILD_SHA256", hashlib.sha256(b"fixture lock").hexdigest()):
                self.assertEqual(host.command_build(root), 0)
                # The real subprocess proves the actual argv and profile env it
                # received, not just the helpers we called.
                observed = json.loads((host.target_dir(root) / "fixture-observation.json").read_text())
                self.assertEqual(observed["argv"],
                                 ["build", "--release", "--locked", "-p", "codex-cli",
                                  "--bin", "codex", "--jobs", "1"])
                env = observed["env"]
                self.assertEqual(env["CARGO_PROFILE_RELEASE_OPT_LEVEL"], "0")
                self.assertEqual(env["CARGO_PROFILE_RELEASE_LTO"], "false")
                self.assertEqual(env["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"], "256")
                self.assertEqual(env["CARGO_PROFILE_RELEASE_DEBUG"], "0")
                self.assertEqual(env["CARGO_PROFILE_RELEASE_INCREMENTAL"], "false")
                self.assertEqual(env["CARGO_INCREMENTAL"], "0")
                recorded = host.load_provenance(root)
                self.assertEqual(recorded["build"]["inputs"]["profile"],
                                 host.build_profile_contract())
                compiler.write_text(compiler.read_text() + 'Path("Cargo.lock").write_bytes(b"tampered")\n')
                with self.assertRaisesRegex(host.SetupError, "lockfile changed"):
                    host.command_build(root)
                self.assertEqual(host.load_provenance(root), recorded)
            self.assertEqual((host.target_dir(root) / "release" / "codex").read_bytes(),
                             b"controlled-build-result")
            self.assertFalse(host.load_provenance(root)["checks"]["qualified"])

    def test_build_argv_is_one_low_priority_job(self):
        argv = host.build_argv("/tc/bin/cargo")
        self.assertEqual(argv[:2], ["nice", "-n"])
        self.assertIn("19", argv)
        self.assertIn("--release", argv)
        self.assertIn("--locked", argv)
        self.assertEqual(argv[argv.index("--jobs") + 1], "1")
        self.assertIn("codex-cli", argv)
        self.assertIn("codex", argv)

    def test_build_env_is_filtered_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = host.build_env(root)
            self.assertEqual(env["CARGO_HOME"], str(host.cargo_home(root)))
            self.assertEqual(env["CARGO_TARGET_DIR"], str(host.target_dir(root)))
            self.assertEqual(env["HOME"], str(host.home_dir(root)))
            for banned in ("OPENAI_API_KEY", "CODEX_HOME", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
                self.assertNotIn(banned, env)

    def test_build_env_carries_the_fixed_profile_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = host.build_env(root)
            # The env and the recorded contract come from one definition.
            expected = {"CARGO_PROFILE_RELEASE_OPT_LEVEL": "0",
                        "CARGO_PROFILE_RELEASE_LTO": "false",
                        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "256",
                        "CARGO_PROFILE_RELEASE_DEBUG": "0",
                        "CARGO_PROFILE_RELEASE_INCREMENTAL": "false"}
            for name, value in expected.items():
                self.assertEqual(env[name], value, name)
            self.assertEqual(env["CARGO_INCREMENTAL"], "0")
            self.assertEqual(host.build_profile_contract(),
                             {"opt-level": "0", "lto": "false", "codegen-units": "256",
                              "debug": "0", "incremental": "false"})

    def test_profile_contract_is_a_single_source_of_truth(self):
        # Env and provenance contract cannot drift: both derive from RELEASE_PROFILE.
        self.assertEqual(set(host.build_profile_env()),
                         {"CARGO_PROFILE_RELEASE_OPT_LEVEL", "CARGO_PROFILE_RELEASE_LTO",
                          "CARGO_PROFILE_RELEASE_CODEGEN_UNITS", "CARGO_PROFILE_RELEASE_DEBUG",
                          "CARGO_PROFILE_RELEASE_INCREMENTAL"})
        for key, value in host.RELEASE_PROFILE:
            self.assertEqual(host.build_profile_contract()[key], value)

    def test_resource_stop_during_build_writes_no_success_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = host.build_dir(root)
            workspace = build / "codex-rs"
            workspace.mkdir(parents=True)
            (workspace / "Cargo.toml").write_text('[workspace]\n')
            (workspace / "Cargo.lock").write_bytes(b"fixture lock")
            compiler = root / "cargo-fixture"
            # A TERM-resistant long runner, so RAM is sampled while it is alive.
            compiler.write_text(
                '#!' + sys.executable + '\n'
                'import signal,time\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                'time.sleep(120)\n')
            compiler.chmod(0o700)
            patch_path = root / "reviewed.patch"
            patch_path.write_text("controlled patch")
            deadline = future_deadline(30)

            def readings():
                yield host.RAM_MIN_AVAILABLE_BYTES + 1  # pre-launch passes
                while True:
                    yield 1  # then drops below the floor

            seq = readings()
            with patch.object(host, "gate_cutoff", return_value=deadline), \
                    patch.object(host, "ensure_toolchain", return_value=compiler), \
                    patch.object(host, "prepare_build", return_value=(build, patch_path, "a" * 64)), \
                    patch.object(host, "preflight_resources", return_value={"fixture": True}), \
                    patch.object(host, "available_ram_bytes", lambda *a, **k: next(seq)), \
                    patch.object(host, "LOCK_BUILD_SHA256", hashlib.sha256(b"fixture lock").hexdigest()):
                with self.assertRaises(host.SetupError):
                    host.command_build(root)
            self.assertFalse(host.provenance_path(root).exists())  # no success provenance

    def test_state_never_claims_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            state = host.host_state(Path(directory))
            self.assertFalse(state["ready"])
            self.assertFalse(state["qualified"])
            self.assertEqual(state["toolchain"]["expected"], host.RUST_VERSION)


class BuildBinding(unittest.TestCase):
    def _write(self, root, package=None, target=None, patch=None, cargo=None, rustc=None):
        _, digest = write_manifest(root)
        binary = host.target_dir(root) / "release" / "codex"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"elf")
        cargo_hash = hashlib.sha256(b"cargo").hexdigest()
        rustc_hash = hashlib.sha256(b"rustc").hexdigest()
        host.save_provenance(root, {
            "toolchain": {"version": host.RUST_VERSION,
                          "binaries": {"cargo": {"sha256": cargo_hash}, "rustc": {"sha256": rustc_hash}}},
            "build": {"package": package or host.PACKAGE, "target": target or host.RUST_TRIPLE,
                      "binary": {"path": str(binary), "sha256": hashlib.sha256(b"elf").hexdigest()},
                      "inputs": {"source": host.CODEX_PIN, "patch": patch or digest,
                                 "lock_sha256": host.LOCK_BUILD_SHA256,
                                 "toolchain": host.RUST_VERSION, "cargo_sha256": cargo or cargo_hash,
                                 "rustc_sha256": rustc or rustc_hash,
                                 "profile": host.build_profile_contract()}},
        })
        return digest

    def test_fresh_build_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root)
            self.assertTrue(host.build_state(root)["ok"])

    def test_wrong_package_target_patch_or_toolchain_rejected(self):
        for kwargs in ({"package": "other-cli"}, {"target": "aarch64-unknown-linux-gnu"},
                       {"patch": "0" * 64}, {"cargo": "0" * 64}, {"rustc": "0" * 64}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write(root, **kwargs)
                state = host.build_state(root)
                self.assertFalse(state["ok"], kwargs)
                self.assertTrue(state["stale"], kwargs)


    def test_missing_or_wrong_release_lock_provenance_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root)
            for value in (None, "0" * 64):
                data = host.load_provenance(root)
                data["build"]["inputs"]["lock_sha256"] = value
                host.save_provenance(root, data)
                self.assertFalse(host.build_state(root)["ok"])

    def test_each_profile_field_mutation_or_removal_is_stale(self):
        # Every field is bound: altering one value, dropping the key or the whole
        # profile, or recording another profile must invalidate provenance even
        # though the binary hash still matches.
        base = host.build_profile_contract()
        for field in list(base):
            for mutated in ({**base, field: base[field] + "x"}, {**base, field: "MUTATED"},
                            {k: v for k, v in base.items() if k != field}):
                with self.subTest(field=field, mutated=mutated):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        self._write(root)
                        data = host.load_provenance(root)
                        data["build"]["inputs"]["profile"] = mutated
                        host.save_provenance(root, data)
                        state = host.build_state(root)
                        self.assertFalse(state["ok"])
                        self.assertTrue(state["stale"])

    def test_missing_or_other_profile_is_stale(self):
        for mutated in (None, {"release": True}, {}, {"opt-level": "0"}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write(root)
                data = host.load_provenance(root)
                if mutated is None:
                    del data["build"]["inputs"]["profile"]
                else:
                    data["build"]["inputs"]["profile"] = mutated
                host.save_provenance(root, data)
                self.assertFalse(host.build_state(root)["ok"], mutated)

    def test_unaltered_profile_stays_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root)
            self.assertTrue(host.build_state(root)["ok"])
            self.assertFalse(host.build_state(root)["stale"])


class LockAndProvenance(unittest.TestCase):
    def test_lock_is_owned_regular_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with host.setup_lock(root):
                info = (host.private_root(root) / "native-host.lock").stat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())

    def test_provenance_write_refuses_symlinked_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host.private_root(root).mkdir(parents=True)
            (host.private_root(root) / "provenance.json").symlink_to(root / "escape.json")
            with self.assertRaises(host.SetupError):
                host.save_provenance(root, {"x": 1})


class PreflightOrdering(unittest.TestCase):
    def test_low_resource_refuses_before_download_or_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(host, "preflight_resources", side_effect=host.SetupError("low")), \
                    patch.object(host, "fetch") as fetch_mock, \
                    patch.object(host.subprocess, "Popen") as popen:
                with self.assertRaises(host.SetupError):
                    host.ensure_toolchain(root, future_deadline())
                self.assertFalse(fetch_mock.called)
                self.assertFalse(popen.called)

    def test_unknown_ram_refuses_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(host, "available_ram_bytes", lambda meminfo=None: None), \
                    patch.object(host, "fetch") as fetch_mock, \
                    patch.object(host.subprocess, "Popen") as popen:
                with self.assertRaises(host.SetupError):
                    host.ensure_toolchain(root, future_deadline())
                self.assertFalse(fetch_mock.called)
                self.assertFalse(popen.called)


class PublicManifestProbe(unittest.TestCase):
    def test_repo_root_manifest_accepted_offline(self):
        root = Path(__file__).resolve().parents[2]
        manifest = host.host_manifest_path(root)
        if not manifest.is_file():
            self.skipTest("public native-host manifest absent in this checkout")
        patch, digest, data = host.reviewed_patch(root)
        self.assertTrue(patch.is_file())
        self.assertEqual(data["upstream"]["commit"], host.CODEX_PIN)
        self.assertEqual(data["patch"]["applies_to_commit"], host.CODEX_PIN)
        self.assertEqual(digest, data["patch"]["sha256"])
        files = host.target_files(data)
        self.assertTrue(files)
        self.assertEqual(files[0][0], data["patch"]["target_file"]["path"])


class SignalOrderingAfterReap(unittest.TestCase):
    def _spy(self):
        calls = []
        real = host._signal_group_raw

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real(pgid, sig)

        return calls, spy

    def test_clean_child_signals_group_once_only(self):
        calls, spy = self._spy()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(host, "_signal_group_raw", spy):
                code = host.run_supervised([sys.executable, "-c", "pass"], cwd=directory,
                                           env={"PATH": "/usr/bin:/bin"},
                                           log_path=Path(directory) / "l.log", stop_at=future_deadline(30))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)  # one TERM+KILL set; finally never re-kills a reaped group

    def test_finally_cleans_once_when_main_path_raises(self):
        calls, spy = self._spy()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(host, "_signal_group_raw", spy), \
                    patch.object(host, "_leader_exited", side_effect=host.SetupError("boom")):
                with self.assertRaises(host.SetupError):
                    host.run_supervised([sys.executable, "-c", "import time; time.sleep(60)"],
                                        cwd=directory, env={"PATH": "/usr/bin:/bin"},
                                        log_path=Path(directory) / "l.log", stop_at=future_deadline(30))
        self.assertEqual(len(calls), 2)  # cleanup ran exactly once, never twice


class ProbeDeadline(unittest.TestCase):
    def test_mutating_probe_clamped_readonly_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = make_repo(host.source_dir(root),
                             {"rust-toolchain.toml": '[toolchain]\nchannel = "1.95.0"\n'})
            original = host.CODEX_PIN
            try:
                host.CODEX_PIN = head
                self.assertTrue(host.source_state(root)["ok"])  # read-only fixed cap
                with self.assertRaises(host.SetupError):
                    host.source_state(root, deadline=expired_deadline())  # clamped, refuses
            finally:
                host.CODEX_PIN = original


class ExtractionDeadline(unittest.TestCase):
    def test_expired_cutoff_refuses_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_tar(base / "a.tgz", [("pkg", "dir", None), ("pkg/f", "file", None)])
            with self.assertRaises(host.SetupError):
                host.safe_extract(archive, base / "out", deadline=expired_deadline())
            self.assertFalse((base / "out").exists())  # no publication past the window

    def test_blocked_archive_scan_is_interrupted_at_original_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_tar(base / "a.tgz", [("pkg", "dir", None)])
            scan_finished = base / "scan-finished"

            def blocked_scan(*args, **kwargs):
                time.sleep(3)
                scan_finished.write_text("too late")
                return []

            cutoff = future_deadline(1.15)
            started = time.monotonic()
            with patch.object(tarfile.TarFile, "getmembers", side_effect=blocked_scan) as scan:
                with self.assertRaises(host.SetupError) as caught:
                    host.safe_extract(archive, base / "out", deadline=cutoff)
            self.assertEqual(caught.exception.code, 75)
            scan.assert_called_once()
            self.assertLess(time.monotonic() - started, 1.75)
            self.assertFalse(scan_finished.exists())
            self.assertFalse((base / "out").exists())

    def test_blocked_member_extraction_cannot_install_or_record_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "channel.toml"
            manifest.write_text(
                '[pkg.rust.target.x86_64-unknown-linux-gnu]\n'
                'available = true\n'
                'url = "https://static.rust-lang.org/dist/rust-1.95.0-x86_64-unknown-linux-gnu.tar.gz"\n'
                'hash = "%s"\n' % ("a" * 64))
            archive = make_tar(root / "a.tgz", [("pkg", "dir", None), ("pkg/f", "file", None)])
            extraction_finished = root / "extraction-finished"

            def blocked_extract(*args, **kwargs):
                time.sleep(3)
                extraction_finished.write_text("too late")

            cutoff = future_deadline(1.15)
            started = time.monotonic()
            with patch.object(host, "preflight_resources", return_value={}), \
                    patch.object(host, "_ensure_manifest", return_value=(manifest, "b" * 64)), \
                    patch.object(host, "_ensure_archive", return_value=archive), \
                    patch.object(tarfile.TarFile, "extract", side_effect=blocked_extract) as extract, \
                    patch.object(host, "run_supervised") as run, \
                    patch.object(host, "save_provenance") as record, \
                    patch.object(host, "fetch") as download:
                with self.assertRaises(host.SetupError) as caught:
                    host.ensure_toolchain(root, deadline=cutoff)
            self.assertEqual(caught.exception.code, 75)
            extract.assert_called_once()
            self.assertLess(time.monotonic() - started, 1.75)
            run.assert_not_called()
            record.assert_not_called()
            download.assert_not_called()
            self.assertFalse(extraction_finished.exists())
            self.assertFalse(host.provenance_path(root).exists())
            self.assertFalse(host.toolchain_dir(root).exists())
            self.assertEqual(list(host.private_root(root).glob("rust-*")), [])

    def test_nested_work_deadline_restores_outer_alarm_without_extending_it(self):
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()

        def prior_alarm(*args):
            raise AssertionError("prior alarm unexpectedly fired")

        try:
            signal.signal(signal.SIGALRM, prior_alarm)
            signal.setitimer(signal.ITIMER_REAL, 2.4)
            with host.absolute_work_deadline(future_deadline(1.8), "outer"):
                outer_handler = signal.getsignal(signal.SIGALRM)
                outer_remaining = signal.getitimer(signal.ITIMER_REAL)[0]
                with host.absolute_work_deadline(future_deadline(1.4), "inner"):
                    self.assertLess(signal.getitimer(signal.ITIMER_REAL)[0], outer_remaining)
                    time.sleep(0.02)
                self.assertIs(signal.getsignal(signal.SIGALRM), outer_handler)
                restored = signal.getitimer(signal.ITIMER_REAL)[0]
                self.assertLess(restored, outer_remaining)
                self.assertGreater(restored, outer_remaining - 0.2)
            self.assertIs(signal.getsignal(signal.SIGALRM), prior_alarm)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 2.0)
            self.assertLess(signal.getitimer(signal.ITIMER_REAL)[0], 2.4)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL,
                                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                                previous_timer[1])

    def test_unknown_ram_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo = Path(directory) / "meminfo"
            meminfo.write_text("MemTotal: 4096 kB\n")  # MemAvailable absent
            plenty = SimpleNamespace(free=host.DISK_MIN_FREE_BYTES + 1)
            with self.assertRaises(host.SetupError):
                host.preflight_resources(directory, disk_usage=lambda _: plenty, meminfo=meminfo)


if __name__ == "__main__":
    unittest.main()
