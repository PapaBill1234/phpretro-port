"""Behavior boundaries for the isolated Eliza acceptance host helper.

Owned path: tests/ops/test_eliza_acceptance_host.py.  These checks assert the
launch contract (exact argv, filtered env), patch/source provenance, read-only
`check`, and the private-tree/identity-safe boundaries.  They never install,
launch, or read credentials.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "acceptance_host", Path(__file__).resolve().parents[2] / "scripts/eliza-acceptance-host.py")
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)

REPO = Path(__file__).resolve().parents[2]


class StartContractTest(unittest.TestCase):
    def test_start_argv_is_exact(self):
        root = Path("/tmp/trusted")
        argv = host.start_argv(root, bun_bin="/rt/bun")
        self.assertEqual(argv, [
            "/rt/bun", "--no-env-file", "--no-install", "--conditions=eliza-source",
            str(root / host.ACCEPTANCE_REL / "source" / host.ENTRY_REL), "start",
        ])
        # the entry point must be the overlay copy, never the canonical source
        self.assertIn("/source/packages/app/src/entry.ts", argv[4])
        self.assertIn("--no-env-file", argv)
        self.assertNotIn("--install", argv)

    def test_env_filter_drops_db_provider_and_foreign_eliza(self):
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "DATABASE_URL": "postgres://secret",
            "POSTGRES_URL": "postgres://secret",
            "OPENAI_API_KEY": "sk-secret",
            "DEEPSEEK_API_KEY": "sk-secret",
            "ELIZAOS_CLOUD_API_KEY": "sk-secret",
            "ELIZA_STATE_DIR": "/foreign/state",
            "ELIZA_API_PORT": "9999",
        }
        env = host.build_env(Path("/tmp/trusted"), base=base)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/x")
        for leaked in ("DATABASE_URL", "POSTGRES_URL", "OPENAI_API_KEY",
                       "DEEPSEEK_API_KEY", "ELIZAOS_CLOUD_API_KEY"):
            self.assertNotIn(leaked, env)
        # fixed isolated values replace any inherited ELIZA_*
        self.assertEqual(env["ELIZA_API_BIND"], "127.0.0.1")
        self.assertEqual(env["ELIZA_API_PORT"], str(host.API_PORT))
        self.assertEqual(env["ELIZA_UI_PORT"], str(host.UI_PORT))
        self.assertEqual(env["ELIZA_ALLOWED_ORIGINS"], host.ALLOWED_ORIGINS)
        self.assertNotIn("*", env["ELIZA_ALLOWED_ORIGINS"])
        self.assertNotIn(env["ELIZA_STATE_DIR"], ("/foreign/state",))

    def test_ports_are_fixed_loopback(self):
        self.assertEqual(host.API_PORT, 18831)
        self.assertEqual(host.UI_PORT, 18832)
        self.assertEqual(host.BIND_ADDR, "127.0.0.1")


class ProvenanceTest(unittest.TestCase):
    def test_reviewed_patch_digest_matches_record(self):
        patch = REPO / host.PATCH_REL
        self.assertTrue(patch.is_file())
        self.assertEqual(host.sha256_file(patch), host.PATCH_SHA256)

    def test_verify_patch_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / host.PATCH_REL
            dest.parent.mkdir(parents=True)
            dest.write_text("not the reviewed patch\n")
            with self.assertRaises(host.AcceptanceError):
                host.verify_patch(root)

    def test_patched_ui_resolves_into_overlay_not_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            canon = Path(tmp) / "canon"
            (canon / "@elizaos").mkdir(parents=True)
            (canon / "@elizaos" / "ui").symlink_to("../../packages/ui")
            (canon / "react").mkdir()
            overlay = Path(tmp) / "overlay"
            (overlay / "packages" / "ui").mkdir(parents=True)
            host._link_node_modules(canon, overlay / "node_modules", overlay)
            ui = overlay / "node_modules" / "@elizaos" / "ui"
            self.assertTrue(ui.is_symlink())
            self.assertEqual(ui.resolve(), (overlay / "packages" / "ui").resolve())
            # the scope is materialized, not the canonical whole-tree link
            self.assertEqual((overlay / "node_modules" / "react").resolve(),
                             (canon / "react").resolve())


class OverlayBuildTest(unittest.TestCase):
    def test_hardlink_tree_excludes_node_modules_and_applies_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "canon"
            (src / "packages" / "ui").mkdir(parents=True)
            target = src / "packages" / "ui" / "x.ts"
            target.write_text("old\n")
            (src / "node_modules" / "react").mkdir(parents=True)
            overlay = tmp / "overlay"
            host._hardlink_tree(src, overlay, deadline=None)
            built = overlay / "packages" / "ui" / "x.ts"
            self.assertTrue(built.exists())
            self.assertFalse((overlay / "node_modules").exists())  # handled separately
            self.assertEqual(built.stat().st_ino, target.stat().st_ino)  # hardlink

            patch = tmp / "p.patch"
            patch.write_text("diff --git a/packages/ui/x.ts b/packages/ui/x.ts\n"
                             "--- a/packages/ui/x.ts\n+++ b/packages/ui/x.ts\n"
                             "@@ -1 +1 @@\n-old\n+new\n")
            host._apply_patch(overlay, patch)  # forward + reverse-check
            self.assertEqual(built.read_text(), "new\n")
            # canonical source is untouched
            self.assertEqual(target.read_text(), "old\n")


class BoundaryTest(unittest.TestCase):
    def test_write_target_must_stay_inside_owned_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned = host.acceptance_root(root)
            owned.mkdir(parents=True)
            with self.assertRaises(host.AcceptanceError):
                host.assert_no_symlink_ancestors(root / "outside", owned)

    def test_symlinked_ancestor_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real)
            with self.assertRaises(host.AcceptanceError):
                host.assert_no_symlink_ancestors(link / "child", root)

    def test_trusted_root_rejects_symlinked_eliza(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".builder").mkdir()
            (root / "builder").mkdir()
            (root / "builder" / "eliza").symlink_to(root / "elsewhere")
            with self.assertRaises(host.AcceptanceError):
                host.assert_trusted_root(root)


class CheckReadOnlyTest(unittest.TestCase):
    def test_check_writes_nothing_and_reports_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".builder").mkdir()
            (root / "builder" / "eliza").mkdir(parents=True)
            before = sorted(p.name for p in root.rglob("*"))
            report = host.run_check(root)
            after = sorted(p.name for p in root.rglob("*"))
            self.assertEqual(before, after)  # read-only
            self.assertFalse(report["ready"])
            self.assertTrue(report["blockers"])


class SetupStopSafetyTest(unittest.TestCase):
    def test_setup_refuses_foreign_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = host.overlay_dir(root)
            overlay.mkdir(parents=True)
            (overlay / "keep.txt").write_text("operator data")
            host.provenance_path(root).parent.mkdir(parents=True, exist_ok=True)
            host.provenance_path(root).write_text(json.dumps({"pin": "other", "patch_sha256": "x"}))
            ready = {"ready": True, "blockers": [], "details": {}, "root": str(root)}
            with mock.patch.object(host, "run_check", return_value=ready):
                with mock.patch.object(host.upstream, "verified_runtime", return_value=Path("/bun")):
                    with self.assertRaises(host.AcceptanceError):
                        host.run_setup(root)
            self.assertTrue((overlay / "keep.txt").exists())  # never wiped

    def test_stop_refuses_foreign_recorded_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host.run_dir(root).mkdir(parents=True)
            host.pid_path(root).write_text("%d\n" % os.getpid())
            with self.assertRaises(host.AcceptanceError):
                host.run_stop(root)
            self.assertFalse(host.pid_path(root).exists())

    def test_stop_reports_noop_without_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = host.run_stop(Path(tmp))
            self.assertFalse(result["stopped"])

    def test_same_host_rejects_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(host._same_host(os.getpid(), Path(tmp)))


class ConfigTest(unittest.TestCase):
    def test_config_uses_pglite_and_isolated_workspace(self):
        root = Path("/tmp/trusted")
        cfg = host.acceptance_config(root)
        self.assertEqual(cfg["database"]["provider"], "pglite")
        self.assertTrue(str(cfg["database"]["dir"]).startswith(str(host.state_dir(root))))
        self.assertTrue(str(cfg["agent"]["workspace"]).startswith(str(host.state_dir(root))))
        # no credential material is written to config
        self.assertNotIn("key", json.dumps(cfg).lower())
        self.assertNotIn("secret", json.dumps(cfg).lower())


class CliTest(unittest.TestCase):
    def test_gated_commands_require_yes(self):
        self.assertEqual(host.main(["setup"]), 1)
        self.assertEqual(host.main(["start"]), 1)


if __name__ == "__main__":
    unittest.main()
