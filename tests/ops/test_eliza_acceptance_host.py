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
import subprocess
import tempfile
import time
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
                    mock.patch.object(host, "acceptance_gate", return_value={"stop_at": "2099-01-01T00:00:00+00:00"}), \
                    mock.patch.object(host, "build_overlay"), \
                    mock.patch.object(host.time, "monotonic", side_effect=[0, 2]), \
                    mock.patch("builtins.print") as output:
                self.assertEqual(host.main(["setup", "--yes", "--deadline", "1"]), 2)
            self.assertFalse(host.acceptance_root(root).exists())
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


if __name__ == "__main__":
    unittest.main()
