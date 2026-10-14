"""Behavior boundaries for the isolated Eliza acceptance host adapter.

Owned path: tests/ops/test_eliza_acceptance_host.py.  Covers the immutable
profile, regular-copy overlay provenance (real temp git pin + patch), the
verified plugin-entry layout, the allowlist child environment and the gated
no-cutoff/no-launch boundary.  No install, launch, browser or credential call.
"""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "acceptance_host", Path(__file__).resolve().parents[2] / "scripts/eliza-acceptance-host.py")
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)

REPO = Path(__file__).resolve().parents[2]


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ReadOnlyCheckTest(unittest.TestCase):
    def test_check_never_creates_private_state_lock_or_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(host, "validate_overlay_source", return_value={"readiness": True}), \
                    mock.patch.object(host.dash, "require_window", return_value={"allowed": True}), \
                    mock.patch.object(host.dash, "port_free"), \
                    mock.patch.object(host, "_dropin_ready", return_value=True), \
                    mock.patch("builtins.print"):
                self.assertEqual(host.cmd_check(root), 0)
            self.assertEqual(list(root.iterdir()), [])


class ProfileTest(unittest.TestCase):
    def test_profile_ports_origins_and_immutability(self):
        profile = host.acceptance_profile(Path("/tmp/root"))
        self.assertEqual((profile.api_port, profile.ui_port), (18831, 18832))
        self.assertEqual(profile.api_host, "127.0.0.1")
        self.assertEqual(profile.allowed_origins,
                         "http://127.0.0.1:18832,http://127.0.0.1:18831")
        self.assertNotIn("*", profile.allowed_origins)
        self.assertFalse(profile.compat)
        self.assertFalse(profile.autonomy)
        with self.assertRaises(AttributeError):
            profile.api_port = 1

    def test_no_arbitrary_root_or_path_cli(self):
        parser_args = host.main.__doc__ or ""
        self.assertNotIn("--root", parser_args)
        # main() rejects unknown flags; only fixed commands exist
        with self.assertRaises(SystemExit):
            host.main(["check", "--root", "/etc"])


class EnvAllowlistTest(unittest.TestCase):
    def test_acceptance_env_is_allowlisted_with_private_home(self):
        root = Path("/tmp/root")
        profile = host.acceptance_profile(root)
        secrets = {"profile": host.state_dir(root) / "runtime"}
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://secret",
                                          "OPENAI_API_KEY": "sk-secret"}, clear=False):
            api_env, ui_env = host.dash.build_env(profile.source, secrets, profile)
        for leaked in ("DATABASE_URL", "POSTGRES_URL", "OPENAI_API_KEY"):
            self.assertNotIn(leaked, api_env)
            self.assertNotIn(leaked, ui_env)
        self.assertEqual(api_env["HOME"], str(host.state_dir(root) / "home"))
        self.assertEqual(api_env["ELIZA_API_PORT"], "18831")
        self.assertEqual(api_env["ELIZA_UI_PORT"], "18832")
        self.assertEqual(api_env["PGLITE_DATA_DIR"], str(secrets["profile"] / "db"))
        self.assertEqual(api_env["ELIZA_WORKSPACE_DIR"], str(secrets["profile"] / "workspace"))
        self.assertEqual(host.plugins_custom_dir(root), secrets["profile"] / "plugins" / "custom")
        self.assertNotIn("ELIZA_API_TOKEN", ui_env)  # UI receives no secret


class PluginConfigTest(unittest.TestCase):
    def test_plugin_entries_match_verified_schema(self):
        cfg = host.acceptance_config(Path("/tmp/root"))
        entries = cfg["plugins"]["entries"]
        self.assertEqual(set(entries), {"@phpretro/plugin-project-companion",
                                        "@phpretro/eliza-native-model"})
        for value in entries.values():
            self.assertEqual(value, {"enabled": True})  # only schema-allowed keys


class OverlayTest(unittest.TestCase):
    PATCHED = "packages/ui/src/components/shell/ChatOverlay.tsx"

    def _fixture(self, tmp):
        root = Path(tmp)
        source = host.canonical_source(root)
        (source / "packages/ui/src/components/shell").mkdir(parents=True)
        target = source / self.PATCHED
        target.write_text("old\n")
        (source / "node_modules/@elizaos").mkdir(parents=True)
        (source / "node_modules/@elizaos/ui").symlink_to("../../packages/ui")
        (source / "node_modules/react").mkdir()
        vite = source / "node_modules/vite/bin"
        vite.mkdir(parents=True)
        (vite / "vite.js").write_text("// vite\n")
        (source / ".gitignore").write_text("node_modules/\n")  # untracked, as upstream
        for pkg, spec in host.DROPINS.items():  # canonical drop-in entries
            entry = root / spec["canonical"] / spec["entry"]
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("export const canonical = true;\n")
        _git(source, "init")
        _git(source, "config", "user.email", "t@example.com")
        _git(source, "config", "user.name", "t")
        _git(source, "add", "-A")
        _git(source, "commit", "-m", "pin")
        # write the reviewed patch for this fixture and pin its digest
        patch = root / host.PATCH_REL
        patch.parent.mkdir(parents=True)
        patch.write_text(
            "diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-old\n+new\n"
            % (self.PATCHED, self.PATCHED, self.PATCHED, self.PATCHED)
            + "".join("diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1 @@\n+added\n"
                      % (name, name, name) for name in sorted(host.EXPECTED_ADDED)))
        return target, patch

    def _build(self, tmp):
        target, patch = self._fixture(tmp)
        pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=host.canonical_source(Path(tmp)), text=True).strip()
        with mock.patch.object(host.upstream, "UPSTREAM_PIN", pin), \
                mock.patch.object(host, "EXPECTED_CHANGED", frozenset({self.PATCHED})), \
                mock.patch.object(host, "PATCH_SHA256", host.sha256_file(patch)):
            host.build_overlay(Path(tmp), deadline=None)
        return target

    def test_pinned_internal_directory_alias_is_preserved_without_shared_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, patch = self._fixture(tmp)
            source = host.canonical_source(root)
            (source / "ui-alias").symlink_to("packages/ui", target_is_directory=True)
            _git(source, "add", "ui-alias")
            _git(source, "commit", "-m", "internal alias")
            pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            with mock.patch.object(host.upstream, "UPSTREAM_PIN", pin), \
                    mock.patch.object(host, "PATCH_SHA256", host.sha256_file(patch)):
                host.build_overlay(root)
                self.assertTrue(host.verify_overlay(root))
            alias = host.overlay_dir(root) / "ui-alias"
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(), (host.overlay_dir(root) / "packages/ui").resolve())
            self.assertNotEqual(target.stat().st_ino, (host.overlay_dir(root) / self.PATCHED).stat().st_ino)

    def test_package_dependencies_resolve_overlay_workspaces_and_reject_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, patch = self._fixture(tmp)
            source = host.canonical_source(root)
            modules = source / "packages/core/node_modules"
            (modules / "markdown-it").mkdir(parents=True)
            (modules / "@elizaos").mkdir()
            (modules / "@elizaos/ui").symlink_to(source / "packages/ui")
            pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            with mock.patch.object(host.upstream, "UPSTREAM_PIN", pin), \
                    mock.patch.object(host, "PATCH_SHA256", host.sha256_file(patch)):
                host.build_overlay(root)
                self.assertTrue(host.verify_overlay(root))
                location = host.overlay_dir(root) / "packages/core/node_modules"
                self.assertEqual((location / "@elizaos/ui").resolve(),
                                 (host.overlay_dir(root) / "packages/ui").resolve())
                self.assertEqual((location / "markdown-it").resolve(), (modules / "markdown-it").resolve())
                (location / "markdown-it").unlink()
                (location / "markdown-it").symlink_to(root)
                with self.assertRaises(host.AcceptanceError):
                    host.verify_overlay(root)
                with self.assertRaises(host.AcceptanceError):
                    host.link_dependencies(root, host.overlay_dir(root))

    def test_legacy_scope_migration_rejects_unknown_scope_and_empty_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, patch = self._fixture(tmp)
            source = host.canonical_source(root)
            scope = source / "node_modules/@types"
            (scope / "known").mkdir(parents=True)
            pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            with mock.patch.object(host.upstream, "UPSTREAM_PIN", pin), \
                    mock.patch.object(host, "PATCH_SHA256", host.sha256_file(patch)):
                host.build_overlay(root)
                overlay = host.overlay_dir(root)
                location = overlay / "node_modules/@types"
                (location / "known").unlink()
                location.rmdir()
                location.symlink_to(scope)
                host.link_dependencies(root, overlay)
                self.assertFalse(location.is_symlink())
                self.assertTrue(host.verify_overlay(root))
                (overlay / "node_modules/@unknown-empty").mkdir()
                with self.assertRaises(host.AcceptanceError):
                    host.verify_overlay(root)
                (location / "known").unlink()
                location.rmdir()
                location.symlink_to(root)
                with self.assertRaises(host.AcceptanceError):
                    host.link_dependencies(root, overlay)

    def test_regular_copy_preserves_canonical_and_overlay_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._build(tmp)
            root = Path(tmp)
            built = host.overlay_dir(root) / self.PATCHED
            self.assertEqual(built.read_text(), "new\n")
            self.assertEqual(target.read_text(), "old\n")          # canonical intact
            self.assertNotEqual(built.stat().st_ino, target.stat().st_ino)  # no shared inode
            ui = host.overlay_dir(root) / "node_modules/@elizaos/ui"
            self.assertEqual(ui.resolve(), (host.overlay_dir(root) / "packages/ui").resolve())
            for rel in host.EXPECTED_ADDED:
                self.assertEqual((host.overlay_dir(root) / rel).read_text(), "added\n")

    def test_parent_repository_does_not_silently_skip_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _git(tmp, "init")
            self._build(tmp)
            self.assertEqual((host.overlay_dir(Path(tmp)) / self.PATCHED).read_text(), "new\n")

    def test_known_modified_and_added_files_are_byte_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build(tmp)
            root = Path(tmp)
            with mock.patch.object(host, "PATCH_SHA256", host.sha256_file(root / host.PATCH_REL)):
                self.assertTrue(host.verify_overlay(root))
                for rel in [self.PATCHED, *sorted(host.EXPECTED_ADDED)]:
                    path = host.overlay_dir(root) / rel
                    original = path.read_bytes()
                    path.write_bytes(b"tampered\n")
                    with self.assertRaises(host.AcceptanceError):
                        host.verify_overlay(root)
                    path.write_bytes(original)

    def test_unknown_overlay_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build(tmp)
            root = Path(tmp)
            (host.overlay_dir(root) / "node_modules/react").mkdir(exist_ok=True)  # not tracked
            # tamper with a tracked, non-expected file
            stray = host.overlay_dir(root) / "node_modules/vite/bin/vite.js"
            stray.unlink()
            _git(host.canonical_source(root), "ls-files")  # sanity
            with mock.patch.object(host, "EXPECTED_CHANGED", frozenset({self.PATCHED})):
                # verify_overlay compares tracked files: tampering breaks verification
                (host.overlay_dir(root) / "packages/ui/src/components/shell/x.ts").write_text("x")
                self.assertFalse(host._overlay_ok(root))


class DropInTest(unittest.TestCase):
    def test_dropins_are_real_dirs_importing_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            OverlayTest()._build(tmp)
            root = Path(tmp)
            created = host.install_dropins(root)
            self.assertEqual(len(created), 2)
            for package in host.DROPINS:
                target = host.dropin_dir(root, package)
                self.assertTrue(target.is_dir())
                self.assertFalse(target.is_symlink())  # discovery skips symlink dirs
                pkg = json.loads((target / "package.json").read_text())
                self.assertEqual(pkg["name"], package)   # original package id
                self.assertEqual(pkg["exports"]["."]["import"], "./index.mjs")
                url = host.canonical_entry(root, package).resolve().as_uri()
                self.assertIn(url, (target / "index.mjs").read_text())
                self.assertTrue(str(target).startswith(str(host.state_dir(root))))
            self.assertTrue(host._dropin_ready(root))


    def test_dropin_tampering_and_missing_provenance_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            OverlayTest()._build(tmp)
            root = Path(tmp)
            host.install_dropins(root)
            package = next(iter(host.DROPINS))
            entry = host.dropin_dir(root, package) / "index.mjs"
            original = entry.read_text()
            entry.write_text(original + "// unknown appended code\n")
            self.assertFalse(host._dropin_ready(root))
            entry.write_text(original)
            self.assertTrue(host._dropin_ready(root))
            provenance = host.provenance_path(root)
            data = json.loads(provenance.read_text())
            del data["dropins"][package]["entry_sha256"]
            provenance.write_text(json.dumps(data))
            self.assertFalse(host._dropin_ready(root))


class DeadlinePropagationTest(unittest.TestCase):
    def test_cli_short_deadline_prevents_remaining_setup_and_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(host, "ROOT", root), \
                    mock.patch.object(host.dash, "require_window", return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch.object(host, "build_overlay"), \
                    mock.patch.object(host.time, "monotonic", side_effect=[0, 2]), \
                    mock.patch("builtins.print") as output:
                self.assertEqual(host.main(["setup", "--yes", "--deadline", "1"]), 2)
            self.assertFalse(host.overlay_dir(root).exists())
            self.assertFalse(host.state_dir(root).exists())
            self.assertNotIn("ready", " ".join(str(call) for call in output.call_args_list))

    def test_start_validator_uses_captured_cutoff_before_source_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = host.cutoff_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"stop_at": "2000-01-01T00:00:00+00:00"}))
            with mock.patch.object(host, "_verify_canonical_source") as verify:
                with self.assertRaises(host.AcceptanceError):
                    host.acceptance_profile(root).source["validate"](root)
                verify.assert_not_called()

    def test_expired_deadline_prevents_patch_hash_and_dropin_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expired = time.monotonic() - 1
            with self.assertRaises(host.AcceptanceError):
                host.sha256_file(root / "missing", expired)
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(host.AcceptanceError):
                    host._apply_patch(root, root / "patch", expired)
                run.assert_not_called()
            with self.assertRaises(host.AcceptanceError):
                host.install_dropins(root, expired)
            self.assertFalse(host.plugins_custom_dir(root).exists())


class CapturedDeadlineTest(unittest.TestCase):
    """The effective caller/canonical cutoff must bound launch validation."""

    def _run_start(self, root, namespace, canonical, saved, window):
        cutoff = root / namespace / "cutoff.json"
        cutoff.parent.mkdir(parents=True)
        cutoff.write_text(json.dumps({"stop_at": saved}) + "\n")
        before = cutoff.read_bytes()
        seen = {}

        def fake_validate(_root, deadline=None):
            seen["deadline"] = deadline
            seen["remaining"] = None if deadline is None else deadline - time.monotonic()
            host._remaining(time.monotonic() - 1, "slow source validation")

        with host._Namespace(namespace), \
                mock.patch.object(host.dash, "require_window", return_value={"stop_at": canonical}), \
                mock.patch.object(host.dash, "port_free"), \
                mock.patch.object(host, "_reserve_ports"), \
                mock.patch.object(host, "validate_overlay_source", side_effect=fake_validate), \
                mock.patch.object(host.dash.subprocess, "Popen") as popen, \
                mock.patch("builtins.print"):
            code = host.cmd_start(root, SimpleNamespace(deadline=window))
        return code, before, cutoff, seen, popen

    def test_start_threads_caller_window_into_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ns = host.RUNS_REL / "short"
            now = datetime.now(timezone.utc)
            later = (now + timedelta(hours=1)).isoformat()
            code, before, cutoff, seen, popen = self._run_start(root, ns, later, later, 30)
            self.assertEqual(code, 2)
            popen.assert_not_called()                             # no child launch
            self.assertEqual(cutoff.read_bytes(), before)         # saved bytes unchanged
            self.assertIsNotNone(seen["deadline"])
            self.assertLessEqual(seen["remaining"], 30)           # bounded by caller window
            self.assertGreater(seen["remaining"], 0)
            with host._Namespace(ns):                             # admission released
                released = host.require_admission(root)
                released.__enter__()
                released.__exit__(None, None, None)

    def test_shorter_canonical_reaches_validator_over_later_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ns = host.RUNS_REL / "canon"
            now = datetime.now(timezone.utc)
            code, before, cutoff, seen, popen = self._run_start(
                root, ns, (now + timedelta(seconds=20)).isoformat(),
                (now + timedelta(hours=1)).isoformat(), 300)
            self.assertEqual(code, 2)
            popen.assert_not_called()
            self.assertEqual(cutoff.read_bytes(), before)
            self.assertIsNotNone(seen["deadline"])
            self.assertLessEqual(seen["remaining"], 20)           # bounded by canonical
            self.assertGreater(seen["remaining"], 0)


class PrivateBoundaryTest(unittest.TestCase):
    def test_symlinked_provenance_package_and_overlay_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            OverlayTest()._build(tmp)
            host.install_dropins(root)
            paths = [host.provenance_path(root),
                     host.dropin_dir(root, next(iter(host.DROPINS))) / "package.json",
                     host.overlay_dir(root) / ".gitignore"]
            for index, path in enumerate(paths):
                saved = path.read_bytes()
                foreign = root / ("foreign-%d" % index)
                foreign.write_bytes(saved)
                path.unlink()
                path.symlink_to(foreign)
                with self.assertRaises(host.AcceptanceError):
                    if index == 0:
                        host._overlay_provenance_matches(root)
                    elif index == 1:
                        host._dropin_ready(root)
                    else:
                        host.verify_overlay(root)
                path.unlink()
                path.write_bytes(saved)
            for rel in ("foreign-link", "packages/ui/src/node_modules"):
                link = host.overlay_dir(root) / rel
                link.symlink_to(root, target_is_directory=True)
                with self.assertRaises(host.AcceptanceError):
                    host.verify_overlay(root)
                link.unlink()

    def test_parent_and_leaf_symlinks_do_not_modify_foreign_tree(self):
        for location in (".builder", ".builder/eliza-acceptance", ".builder/eliza-acceptance/state/runtime/plugins"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "root"
                root.mkdir()
                foreign = Path(tmp) / "foreign"
                foreign.mkdir()
                link = root / location
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(foreign, target_is_directory=True)
                with self.assertRaises(host.AcceptanceError):
                    host.install_dropins(root)
                self.assertEqual(list(foreign.iterdir()), [])


class GatedTest(unittest.TestCase):
    def test_setup_closed_gate_never_creates_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(host.dash, "require_window", side_effect=host.DashboardError("closed")), \
                    mock.patch.object(host, "build_overlay") as build:
                with self.assertRaises(host.DashboardError):
                    host.cmd_setup(root)
            build.assert_not_called()
            self.assertFalse(host.acceptance_root(root).exists())

    def test_gated_commands_require_yes(self):
        self.assertEqual(host.main(["setup"]), 1)
        self.assertEqual(host.main(["start"]), 1)

    def test_acceptance_gate_refuses_after_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = host.cutoff_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"stop_at": "2000-01-01T00:00:00+00:00"}))
            with mock.patch.object(host.dash, "require_window", return_value={"stop_at": "2099-01-01T00:00:00+00:00"}):
                with self.assertRaises(host.AcceptanceError):
                    host.acceptance_gate(root)

    def test_closed_canonical_gate_never_creates_allowed_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(host.dash, "require_window", side_effect=host.DashboardError("closed")):
                with self.assertRaises(host.DashboardError):
                    host.acceptance_gate(root)
            self.assertFalse(host.cutoff_path(root).exists())

    def test_canonical_cutoff_clamps_and_never_extends_saved_cutoff(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            with mock.patch.object(host.dash, "require_window", return_value={"stop_at": first}):
                self.assertEqual(host.acceptance_gate(root)["stop_at"], first)
            with mock.patch.object(host.dash, "require_window", return_value={"stop_at": later}):
                self.assertEqual(host.acceptance_gate(root)["stop_at"], first)


class RunNamespaceTest(unittest.TestCase):
    def test_invalid_run_ids_are_refused_before_dispatch(self):
        root = Path(tempfile.mkdtemp())
        stub = mock.Mock(return_value=0)
        with mock.patch.object(host, "ROOT", root), \
                mock.patch.dict(host.COMMANDS, {"check": stub}), \
                mock.patch("builtins.print"):
            for bad in ("", "-lead", "Upper", "a/b", "a b", "a.b", ".x",
                        "x" * 49, "run_id", "naïve", "a" * 48 + "-"):
                self.assertEqual(host.main(["check", "--run-id=" + bad]), 2)
        stub.assert_not_called()
        self.assertEqual(list(root.iterdir()), [])  # no filesystem access

    def test_run_namespace_is_fixed_and_default_is_preserved(self):
        self.assertEqual(host.run_namespace(None), host.ACCEPTANCE_REL)
        self.assertEqual(host.run_namespace("keep-me"), host.RUNS_REL / "keep-me")
        self.assertEqual(host.RUNS_REL, Path(".builder") / "eliza-acceptance-runs")
        with self.assertRaises(host.AcceptanceError):
            host.run_namespace("Bad/Id")


class ContainmentTest(unittest.TestCase):
    def test_full_prefix_containment_rejects_sibling_and_prefix_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.ACCEPTANCE_REL):
                host.assert_private_path(root, root / ".builder/eliza-acceptance/state")
                for escaped in (".builder/eliza-acceptance-runs/x/cutoff.json",
                                ".builder/eliza-acceptance-evil/f",
                                ".builder/elsewhere/f",
                                ".builder/eliza-acceptance/../escape"):
                    with self.assertRaises(host.AcceptanceError):
                        host.assert_private_path(root, root / escaped)
            with host._Namespace(host.RUNS_REL / "run-a"):
                host.assert_private_path(root, root / ".builder/eliza-acceptance-runs/run-a/cutoff.json")
                for escaped in (".builder/eliza-acceptance-runs/run-b/cutoff.json",
                                ".builder/eliza-acceptance/cutoff.json"):
                    with self.assertRaises(host.AcceptanceError):
                        host.assert_private_path(root, root / escaped)

    def test_symlink_ancestors_are_rejected_for_every_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            foreign = Path(tmp) / "foreign"
            foreign.mkdir()
            (root / ".builder").symlink_to(foreign, target_is_directory=True)
            with host._Namespace(host.RUNS_REL / "run-a"), \
                    mock.patch.object(host.dash, "require_window",
                                      return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch("builtins.print"):
                self.assertNotEqual(host.cmd_check(root), 0)
                for command in (host.cmd_setup, host.cmd_start, host.cmd_stop, host.cmd_status):
                    with self.assertRaises(host.AcceptanceError):
                        command(root)
            self.assertEqual(list(foreign.iterdir()), [])

    def test_fixed_admission_path_is_contained_and_symlink_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            foreign = Path(tmp) / "foreign"
            foreign.mkdir()
            (root / ".builder").symlink_to(foreign, target_is_directory=True)
            with host._Namespace(host.RUNS_REL / "run-a"):
                with self.assertRaises(host.AcceptanceError):
                    host.require_admission(root).__enter__()
            self.assertEqual(list(foreign.iterdir()), [])


class ContextRestorationTest(unittest.TestCase):
    def test_selected_namespace_is_restored_after_success_and_failure(self):
        before = host._current_rel()
        with host._Namespace(host.RUNS_REL / "ok"):
            self.assertEqual(host._current_rel(), host.RUNS_REL / "ok")
        self.assertEqual(host._current_rel(), before)
        with self.assertRaises(host.AcceptanceError):
            with host._Namespace(host.RUNS_REL / "boom"):
                raise host.AcceptanceError("boom")
        self.assertEqual(host._current_rel(), before)

    def test_main_restores_namespace_when_a_command_raises(self):
        before = host._current_rel()

        def boom(_root, _args):
            raise host.AcceptanceError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(host, "ROOT", Path(tmp)), \
                    mock.patch.dict(host.COMMANDS, {"check": boom}), \
                    mock.patch("builtins.print"):
                self.assertEqual(host.main(["check", "--run-id", "run-a"]), 1)
        self.assertEqual(host._current_rel(), before)


class IsolationTest(unittest.TestCase):
    def test_run_namespaces_are_distinct_and_default_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / host.ACCEPTANCE_REL / "operator.json"
            marker.parent.mkdir(parents=True)
            marker.write_text("operator\n")
            before = marker.read_bytes()
            with host._Namespace(host.RUNS_REL / "run-a"):
                a_root, a_state = host.acceptance_root(root), host.state_dir(root)
            with host._Namespace(host.RUNS_REL / "run-b"):
                b_root, b_state = host.acceptance_root(root), host.state_dir(root)
            with host._Namespace(host.ACCEPTANCE_REL):
                default_root = host.acceptance_root(root)
            self.assertEqual(a_root, root / ".builder/eliza-acceptance-runs/run-a")
            self.assertEqual(default_root, root / ".builder/eliza-acceptance")
            self.assertNotEqual(a_root, b_root)
            self.assertNotEqual(a_state, b_state)
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual([p.name for p in (root / host.ACCEPTANCE_REL).iterdir()],
                             ["operator.json"])


class CallbackBindingTest(unittest.TestCase):
    def test_profile_callbacks_are_bound_to_the_captured_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "bind"):
                profile = host.acceptance_profile(root)
                bound_rel = profile.state_relative
                gate = profile.gate
                validate = profile.source["validate"]
            # ambient namespace is restored; the bound callbacks still target it
            self.assertEqual(host._current_rel(), host.ACCEPTANCE_REL)
            self.assertEqual(bound_rel, host.RUNS_REL / "bind")
            with mock.patch.object(host.dash, "require_window",
                                   return_value={"stop_at": "2099-01-01T00:00:00+00:00"}):
                gate(root)
            self.assertTrue((root / host.RUNS_REL / "bind" / "cutoff.json").exists())
            self.assertFalse((root / host.ACCEPTANCE_REL).exists())
            self.assertTrue(callable(validate))


class CutoffTest(unittest.TestCase):
    def test_cutoff_is_created_once_exclusively_and_never_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "once"):
                first = datetime.now(timezone.utc) + timedelta(seconds=60)
                later = datetime.now(timezone.utc) + timedelta(seconds=600)
                host._ensure_cutoff(root, first)
                after_first = host.cutoff_path(root).read_bytes()
                host._ensure_cutoff(root, later)  # must not overwrite
                self.assertEqual(host.cutoff_path(root).read_bytes(), after_first)
                self.assertEqual(json.loads(after_first)["stop_at"], first.isoformat())

    def test_canonical_and_caller_clamps_are_memory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "clamp"):
                first = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                with mock.patch.object(host.dash, "require_window", return_value={"stop_at": first}):
                    self.assertEqual(host.acceptance_gate(root)["stop_at"], first)
                saved = host.cutoff_path(root).read_bytes()
                with mock.patch.object(host.dash, "require_window", return_value={"stop_at": later}):
                    self.assertEqual(host.acceptance_gate(root)["stop_at"], first)
                # a smaller caller window can only tighten, never rewrite the file
                with mock.patch.object(host.dash, "require_window", return_value={"stop_at": later}):
                    self.assertLess(host.effective_cutoff(root, 5),
                                    datetime.now(timezone.utc) + timedelta(seconds=10))
                self.assertEqual(host.cutoff_path(root).read_bytes(), saved)

    def test_serialized_capture_under_admission_is_single_and_immutable(self):
        host_path = str(REPO / "scripts/eliza-acceptance-host.py")
        canonical = (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            winner = (
                "import importlib.util, time\n"
                "spec = importlib.util.spec_from_file_location('h', %r)\n"
                "h = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(h)\n"
                "h.dash.require_window = lambda r: {'stop_at': %r}\n"
                "with h._Namespace(h.RUNS_REL / 'race'):\n"
                "    adm = h.require_admission(%r)\n"
                "    adm.__enter__()\n"
                "    try:\n"
                "        res = h.acceptance_gate(%r)\n"
                "        print('captured', res['stop_at'], flush=True)\n"
                "        time.sleep(2.5)\n"
                "    finally:\n"
                "        adm.__exit__(None, None, None)\n"
            ) % (host_path, canonical, str(root), str(root))
            holder = subprocess.Popen([sys.executable, "-c", winner],
                                      stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.wait)
            first = holder.stdout.readline().strip()
            self.assertTrue(first.startswith("captured "), first)
            self.assertEqual(first.split(" ", 1)[1], canonical)
            # a same-id process cannot capture concurrently (real file lock)
            challenger = (
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('h', %r)\n"
                "h = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(h)\n"
                "with h._Namespace(h.RUNS_REL / 'race'):\n"
                "    try:\n"
                "        h.require_admission(%r).__enter__()\n"
                "    except h._AdmissionBusy:\n"
                "        print('busy', flush=True); sys.exit(0)\n"
                "print('acquired', flush=True)\n"
            ) % (host_path, str(root))
            busy = subprocess.run([sys.executable, "-c", challenger],
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(busy.stdout.strip(), "busy", busy.stderr)
            holder.wait(timeout=10)
            # same-id recheck under admission returns the saved value and the
            # saved cutoff file is byte-identical (created once, never rewritten)
            cutoff = root / host.RUNS_REL / "race" / "cutoff.json"
            saved = cutoff.read_bytes()
            with host._Namespace(host.RUNS_REL / "race"), \
                    mock.patch.object(host.dash, "require_window",
                                      return_value={"stop_at": later}):
                admission = host.require_admission(root)
                admission.__enter__()
                try:
                    self.assertEqual(host.acceptance_gate(root)["stop_at"], canonical)
                finally:
                    admission.__exit__(None, None, None)
            self.assertEqual(cutoff.read_bytes(), saved)

    def test_expired_run_refuses_before_any_write_and_leaves_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "expired"):
                path = host.cutoff_path(root)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"stop_at": "2000-01-01T00:00:00+00:00"}) + "\n")
                before = path.read_bytes()
                with mock.patch.object(host.dash, "require_window",
                                       return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                        mock.patch.object(host, "build_overlay") as build, \
                        mock.patch("builtins.print"):
                    for command in (host.cmd_setup, host.cmd_start):
                        with self.assertRaises(host.AcceptanceError):
                            command(root)
                build.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(host.operation_lock_path(root).exists())
                self.assertFalse(host.state_dir(root).exists())


class ActiveNamespaceTest(unittest.TestCase):
    def _runtime(self, root, payload):
        path = host.acceptance_root(root) / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        os.chmod(path, 0o600)

    def test_active_namespace_refuses_start_and_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "live"):
                self._runtime(root, {"apiPid": 4242, "apiStart": "55"})
                with mock.patch.object(host.dash, "require_window",
                                       return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                        mock.patch.object(host.dash, "pid_identity", return_value="55"), \
                        mock.patch.object(host.dash, "cmd_start") as dash_start, \
                        mock.patch("builtins.print") as out:
                    self.assertEqual(host.cmd_start(root), 1)
                    dash_start.assert_not_called()
                    self.assertEqual(host.cmd_setup(root), 1)
                self.assertIn("active", " ".join(str(c) for c in out.call_args_list))

    def test_unknown_pid_identity_is_not_treated_as_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "stale"):
                self._runtime(root, {"apiPid": 4242})  # no recorded start tick
                with mock.patch.object(host.dash, "pid_identity", return_value=None):
                    self.assertFalse(host._namespace_active(root))
                with mock.patch.object(host.dash, "pid_identity", return_value="99"):
                    self.assertTrue(host._namespace_active(root))


class AdmissionTest(unittest.TestCase):
    def test_same_run_contention_is_busy_not_ebadf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "shared"):
                first = host.require_admission(root)
                first.__enter__()
                try:
                    second = host.require_admission(root)
                    with self.assertRaises(host._AdmissionBusy):
                        second.__enter__()
                finally:
                    first.__exit__(None, None, None)
                third = host.require_admission(root)
                third.__enter__()
                third.__exit__(None, None, None)

    def test_setup_and_start_content_on_the_same_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "shared"), \
                    mock.patch.object(host.dash, "require_window",
                                      return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch.object(host, "build_overlay") as build, \
                    mock.patch("builtins.print"):
                held = host.require_admission(root)
                held.__enter__()
                try:
                    self.assertEqual(host.cmd_start(root), 1)  # busy, no delegation
                    self.assertEqual(host.cmd_setup(root), 1)
                finally:
                    held.__exit__(None, None, None)
                build.assert_not_called()

    def test_fixed_admission_lock_is_shared_across_namespaces(self):
        host_path = str(REPO / "scripts/eliza-acceptance-host.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = (
                "import importlib.util, sys, time\n"
                "spec = importlib.util.spec_from_file_location('h', %r)\n"
                "h = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(h)\n"
                "adm = h.require_admission(%r)\n"
                "adm.__enter__()\n"
                "print('held', flush=True)\n"
                "time.sleep(3.0)\n"
                "adm.__exit__(None, None, None)\n"
            ) % (host_path, str(root))
            holder = subprocess.Popen([sys.executable, "-c", child],
                                      stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.wait)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            # a different namespace must still contend on the fixed shared lock
            with host._Namespace(host.RUNS_REL / "other"):
                with self.assertRaises(host._AdmissionBusy):
                    host.require_admission(root).__enter__()
            holder.wait(timeout=10)


class StartOverlapTest(unittest.TestCase):
    def test_busy_port_refuses_start_under_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocker = socket.socket()
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind((host.API_HOST, 0))
            blocked_port = blocker.getsockname()[1]
            blocker.listen(1)
            self.addCleanup(blocker.close)
            probe = socket.socket()
            probe.bind((host.API_HOST, 0))
            free_port = probe.getsockname()[1]
            probe.close()
            with host._Namespace(host.RUNS_REL / "ports"), \
                    mock.patch.object(host, "API_PORT", blocked_port), \
                    mock.patch.object(host, "UI_PORT", free_port), \
                    mock.patch.object(host.dash, "require_window",
                                      return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch.object(host.dash, "cmd_start") as dash_start, \
                    mock.patch("builtins.print"):
                with self.assertRaises(host.AcceptanceError):
                    host.cmd_start(root)
                dash_start.assert_not_called()

    def test_delayed_binding_start_refuses_overlap_until_released(self):
        host_path = str(REPO / "scripts/eliza-acceptance-host.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "foreground.ready"
            child = (
                "import importlib.util, time\n"
                "spec = importlib.util.spec_from_file_location('h', %r)\n"
                "h = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(h)\n"
                "h.dash.require_window = lambda r: {'stop_at': '2099-01-01T00:00:00+00:00'}\n"
                "h._reserve_ports = lambda: None\n"
                "def foreground(root, source, profile):\n"
                "    open(%r, 'w').write('ready')\n"
                "    time.sleep(2.5)\n"
                "    return 0\n"
                "h.dash.cmd_start = foreground\n"
                "with h._Namespace(h.RUNS_REL / 'run-x'):\n"
                "    print('rc', h.cmd_start(%r), flush=True)\n"
            ) % (host_path, str(marker), str(root))
            holder = subprocess.Popen([sys.executable, "-c", child],
                                      stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.wait)
            deadline = time.time() + 5
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            # a different namespace cannot start while admission is held
            with host._Namespace(host.RUNS_REL / "run-y"), \
                    mock.patch.object(host.dash, "require_window",
                                      return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch.object(host.dash, "cmd_start") as dash_start, \
                    mock.patch("builtins.print"):
                self.assertEqual(host.cmd_start(root), 1)
                dash_start.assert_not_called()
            self.assertEqual(holder.stdout.readline().strip(), "rc 0")
            holder.wait(timeout=10)
            # admission is available again once the first start released
            available = host.require_admission(root)
            available.__enter__()
            available.__exit__(None, None, None)


class ExactCommandTest(unittest.TestCase):
    def test_stop_and_status_target_the_exact_selected_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}

            def fake_status(_root, profile):
                seen["status"] = profile.state_relative
                return 0

            def fake_stop(_root, profile):
                seen["stop"] = profile.state_relative
                return 0

            with host._Namespace(host.RUNS_REL / "exact"), \
                    mock.patch.object(host.dash, "cmd_status", side_effect=fake_status), \
                    mock.patch.object(host.dash, "cmd_stop", side_effect=fake_stop):
                host.cmd_status(root)
                host.cmd_stop(root)
            self.assertEqual(seen["status"], host.RUNS_REL / "exact")
            self.assertEqual(seen["stop"], host.RUNS_REL / "exact")

    def test_stop_and_status_create_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with host._Namespace(host.RUNS_REL / "readonly"), mock.patch("builtins.print"):
                self.assertEqual(host.cmd_status(root), 0)
                self.assertEqual(host.cmd_stop(root), 0)
            self.assertFalse((root / host.RUNS_REL / "readonly").exists())


if __name__ == "__main__":
    unittest.main()
