"""Operational security checks for the project launcher, without inference."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("claude_builder", REPO / "scripts/claude-builder.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / ".claude").mkdir()
        shutil.copy2(REPO / "config/models.json", self.root / "config/models.json")
        shutil.copy2(REPO / ".claude/settings.json", self.root / ".claude/settings.json")

    def tearDown(self):
        self.temp.cleanup()

    def dotenv(self, text, mode=0o600):
        path = self.root / "credentials.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_dotenv_command_substitution_is_data_and_never_executes(self):
        marker = self.root / "should-not-exist"
        literal = f"$(touch {marker})"
        path = self.dotenv(f'A6API_API_KEY="{literal}"\n')
        self.assertEqual(builder.private_key(path, "A6API_API_KEY"), literal)
        self.assertFalse(marker.exists())

    def test_private_dotenv_rejects_world_readable_duplicate_and_symlink(self):
        path = self.dotenv("A6API_API_KEY=fixture-secret\n", 0o644)
        with self.assertRaises(builder.SetupError):
            builder.private_key(path, "A6API_API_KEY")
        path.chmod(0o600)
        path.write_text("A6API_API_KEY=one\nA6API_API_KEY=two\n")
        with self.assertRaises(builder.SetupError):
            builder.private_key(path, "A6API_API_KEY")
        link = self.root / "link.env"
        link.symlink_to(path)
        with self.assertRaises(builder.SetupError):
            builder.private_key(link, "A6API_API_KEY")

    def test_malformed_dotenv_error_does_not_echo_contents(self):
        marker = "private-synthetic-marker"
        path = self.dotenv(f'A6API_API_KEY="{marker}\n')
        with self.assertRaises(builder.SetupError) as caught:
            builder.private_key(path, "A6API_API_KEY")
        self.assertNotIn(marker, str(caught.exception))

    def test_user_cannot_override_permissions_model_tools_or_settings(self):
        for flag in ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions",
                     "--model=other", "--tools=Bash", "--settings=untrusted.json",
                     "--mcp-config=untrusted.json", "--setting-sources=user", "--bare"):
            with self.subTest(flag=flag), patch("sys.argv", ["builder", flag]):
                with self.assertRaises(builder.SetupError):
                    builder.main()

    def test_force_override_and_committed_key_are_rejected(self):
        path = self.root / ".claude/settings.json"
        for key, value in (("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "1"),
                           ("ANTHROPIC_AUTH_TOKEN", "fixture-secret")):
            settings = json.loads((REPO / ".claude/settings.json").read_text())
            settings["env"][key] = value
            path.write_text(json.dumps(settings))
            with self.subTest(key=key), patch.object(builder, "ROOT", self.root):
                with self.assertRaises(builder.SetupError):
                    builder.configuration()

    def test_native_wrapper_is_rejected(self):
        wrapper = self.root / "claude"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(0o700)
        with patch.dict(os.environ, {"PHPRETRO_CLAUDE_BIN": str(wrapper)}, clear=True):
            with self.assertRaises(builder.SetupError):
                builder.native_binary("2.1.287")

    def test_reviewer_exec_is_read_only_and_strips_other_provider_credentials(self):
        synthetic = "private-launch-fixture-key"
        inherited = {
            "PATH": "/usr/bin:/bin", "A6API_API_KEY": synthetic,
            "OPENROUTER_API_KEY": "do-not-forward-openrouter",
            "DEEPSEEK_API_KEY": "do-not-forward-deepseek",
            "ANTHROPIC_API_KEY": "do-not-forward-anthropic",
            "ANTHROPIC_BEDROCK_BASE_URL": "https://wrong.invalid",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        }
        observed = {}

        def capture(binary, command, environment):
            observed.update(binary=binary, command=command, environment=environment)

        stderr = io.StringIO()
        with patch.object(builder, "ROOT", self.root), patch.object(builder, "native_binary", return_value=Path("/bin/true")), \
                patch.dict(os.environ, inherited, clear=True), patch("sys.argv", ["builder", "--role", "reviewer", "--", "-p", "fixture"]), \
                patch.object(builder.os, "execve", side_effect=capture), patch.object(builder.os, "chdir") as change_directory, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(builder.main(), 0)
        env = observed["environment"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], synthetic)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.a6api.com")
        for key in inherited:
            if key != "PATH":
                self.assertNotIn(key, env)
        command = observed["command"]
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--permission-mode") + 1], "manual")
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
        self.assertNotIn(synthetic, stderr.getvalue())
        self.assertNotIn(synthetic, " ".join(command))
        self.assertEqual(Path(env["CLAUDE_CONFIG_DIR"]), self.root / ".builder/claude")
        change_directory.assert_called_once_with(self.root)


if __name__ == "__main__":
    unittest.main()
