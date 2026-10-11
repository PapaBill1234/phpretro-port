import importlib.util
import json
import multiprocessing
from pathlib import Path
import os
import stat
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "builder_history", Path(__file__).resolve().parents[2] / "scripts/builder-history.py"
)
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def register_worker(root, name):
    history.register_worktree(Path(root), name)


class BuilderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_registry_and_idempotence(self):
        self.assertEqual(history.registered_worktrees(self.root), [])
        history.register_worktree(self.root, "worker-a")
        history.register_worktree(self.root, "worker-a")
        self.assertEqual(history.registered_worktrees(self.root), ["worker-a"])

    def test_concurrent_process_registrations_preserve_all_names(self):
        names = [f"worker-{index}" for index in range(12)]
        context = multiprocessing.get_context("fork")
        processes = [context.Process(target=register_worker, args=(str(self.root), name)) for name in names]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(history.registered_worktrees(self.root), sorted(names))

    def test_invalid_name_is_rejected(self):
        for name in ("", "-worker", "worker name", "x" * 65, 3):
            with self.subTest(name=name), self.assertRaises(ValueError):
                history.register_worktree(self.root, name)

    def test_malformed_registry_is_rejected_without_overwriting_bytes(self):
        history.register_worktree(self.root, "valid")
        registry = self.root / ".builder/doctor/worktrees.json"
        original = b'{"version": 1, "worktrees": [broken]\n'
        registry.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "Invalid worktree registry"):
            history.register_worktree(self.root, "other")
        self.assertEqual(registry.read_bytes(), original)

    def test_symlink_registry_and_lock_are_rejected(self):
        doctor = self.root / ".builder/doctor"
        doctor.mkdir(parents=True)
        target = self.root / "target"
        target.write_text("target")
        (doctor / "worktrees.json").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Invalid worktree registry"):
            history.registered_worktrees(self.root)

        (doctor / "worktrees.json").unlink()
        (doctor / "worktrees.lock").unlink()
        (doctor / "worktrees.lock").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Invalid worktree registry lock"):
            history.registered_worktrees(self.root)

    def test_registry_and_lock_permissions_are_restrictive(self):
        history.register_worktree(self.root, "worker")
        for path in (self.root / ".builder", self.root / ".builder/doctor"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.root / ".builder/doctor/worktrees.json", self.root / ".builder/doctor/worktrees.lock"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
