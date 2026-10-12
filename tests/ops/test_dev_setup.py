import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("dev_setup", Path(__file__).resolve().parents[2] / "scripts/dev-setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class DevelopmentSecrets(unittest.TestCase):
    def test_creation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup.configure(root)
            original = (root / ".env").read_bytes()
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                setup.configure(root)
            self.assertEqual((root / ".env").read_bytes(), original)
            setup.configure(root, upgrade=True)
            self.assertEqual((root / ".env").read_bytes(), original)

    def test_upgrade_preserves_initialized_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = "".join(f"{key}=unchanged-{index}\n" for index, key in enumerate(setup.KEYS[:5]))
            path = root / ".env"
            path.write_text(original)
            path.chmod(0o600)
            setup.configure(root, upgrade=True)
            self.assertTrue(path.read_text().startswith(original))
            self.assertIn("CMS_STAFF_ENCRYPTION_KEY=", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsafe_upgrade_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private"
            target.write_text("untouched")
            (root / ".env").symlink_to(target)
            with self.assertRaises(ValueError):
                setup.configure(root, upgrade=True)
            self.assertEqual(target.read_text(), "untouched")
