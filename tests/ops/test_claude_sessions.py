"""Handoff coordination tests; no tmux server or inference is started."""
import importlib.util
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("claude_sessions", Path(__file__).resolve().parents[2] / "scripts/claude-sessions.py")
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)


class HandoffTests(unittest.TestCase):
    def test_interactive_command_quotes_paths_and_add_dir_is_last(self):
        root = Path("/tmp/a project's files")
        with patch.object(sessions, "ROOT", root):
            command = sessions.command("worker", "luna-discovery", "Luna's discovery")
        argv = shlex.split(command)
        self.assertEqual(argv[0], "exec")
        self.assertIn(str(root / "scripts/claude-builder.py"), argv)
        self.assertIn("Luna's discovery", argv)
        self.assertEqual(argv[-2:], ["--add-dir", str(root / ".builder/handoff")])
        self.assertNotIn("--print", argv)
        self.assertNotIn("-p", argv)

    def test_unrelated_session_is_refused(self):
        responses = [subprocess.CompletedProcess([], 0, "/unrelated\n"), subprocess.CompletedProcess([], 0, "1\n")]
        with patch.object(sessions, "tmux", side_effect=responses):
            with self.assertRaises(sessions.SetupError):
                sessions.owned()

    def test_complete_owned_session_is_not_restarted(self):
        existing = [name for name, _, _ in sessions.WINDOWS]
        with patch.object(sessions, "exists", return_value=True), patch.object(sessions, "owned"), \
                patch.object(sessions, "names", return_value=existing), patch.object(sessions, "status", return_value={"running": True}), \
                patch.object(sessions, "clean_checkpoint") as checkpoint, patch.object(sessions, "configure_window") as configure:
            self.assertEqual(sessions.start(), {"running": True})
            checkpoint.assert_not_called()
            configure.assert_not_called()

    def test_dirty_checkpoint_prevents_new_windows(self):
        results = [subprocess.CompletedProcess([], 0, "fixture-head\n"), subprocess.CompletedProcess([], 0, " M source.go\n")]
        with patch.object(sessions.subprocess, "run", side_effect=results):
            with self.assertRaises(sessions.SetupError):
                sessions.clean_checkpoint()


if __name__ == "__main__":
    unittest.main()
