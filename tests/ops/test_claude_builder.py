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
        (self.root / "scripts").mkdir()
        (self.root / "scripts/deepseek-slots.py").write_text("# integration-owned fixture")
        shutil.copy2(REPO / "scripts/codex-bridge.py", self.root / "scripts/codex-bridge.py")
        shutil.copy2(REPO / "config/models.json", self.root / "config/models.json")
        shutil.copy2(REPO / ".claude/settings.json", self.root / ".claude/settings.json")
        shutil.copy2(REPO / ".claude/project-scope.md", self.root / ".claude/project-scope.md")

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
        path = self.dotenv(f'DEEPSEEK_API_KEY="{literal}"\n')
        self.assertEqual(builder.private_key(path, "DEEPSEEK_API_KEY"), literal)
        self.assertFalse(marker.exists())

    def test_private_dotenv_rejects_world_readable_duplicate_and_symlink(self):
        path = self.dotenv("DEEPSEEK_API_KEY=fixture-secret\n", 0o644)
        with self.assertRaises(builder.SetupError):
            builder.private_key(path, "DEEPSEEK_API_KEY")
        path.chmod(0o600)
        path.write_text("DEEPSEEK_API_KEY=one\nDEEPSEEK_API_KEY=two\n")
        with self.assertRaises(builder.SetupError):
            builder.private_key(path, "DEEPSEEK_API_KEY")
        link = self.root / "link.env"
        link.symlink_to(path)
        with self.assertRaises(builder.SetupError):
            builder.private_key(link, "DEEPSEEK_API_KEY")

    def test_malformed_dotenv_error_does_not_echo_contents(self):
        marker = "private-synthetic-marker"
        path = self.dotenv(f'DEEPSEEK_API_KEY="{marker}\n')
        with self.assertRaises(builder.SetupError) as caught:
            builder.private_key(path, "DEEPSEEK_API_KEY")
        self.assertNotIn(marker, str(caught.exception))

    def test_user_cannot_override_permissions_model_tools_or_settings(self):
        for flag in ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions",
                     "--model=other", "--tools=Bash", "--settings=untrusted.json",
                     "--mcp-config=untrusted.json", "--setting-sources=user", "--bare", "--no-session-persistence"):
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

    def test_worker_direct_route_is_private_and_preserves_history(self):
        observed = {}
        def capture(binary, command, environment):
            observed.update(command=command, environment=environment)
        inherited = {"PATH": "/usr/bin:/bin", "DEEPSEEK_API_KEY": "synthetic", "A6API_API_KEY": "old", "OPENAI_API_KEY": "do-not-forward", "ANTHROPIC_BASE_URL": "https://wrong.invalid"}
        with patch.object(builder, "ROOT", self.root), patch.object(builder, "native_binary", return_value=Path("/bin/true")), patch.object(builder, "funding_ready"), patch.object(builder, "git_head", return_value="fixture"), patch.object(builder, "worker_path", return_value=self.root/"worker"), patch.object(builder.history_registry, "register_worktree"), patch.dict(os.environ, inherited, clear=True), patch("sys.argv", ["builder", "--role", "worker"]), patch.object(builder.os, "execve", side_effect=capture), patch.object(builder.os, "chdir"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main(), 0)
        env = observed["environment"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "synthetic")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.root/".builder/claude"))
        for key in inherited:
            if key not in ("PATH", "ANTHROPIC_BASE_URL"): self.assertNotIn(key, env)
        command = observed["command"]
        self.assertEqual(command[command.index("--model")+1], "deepseek-flash[1m]")
        self.assertEqual(command[command.index("--permission-mode")+1], "bypassPermissions")
        self.assertNotIn("gpt-6.1-sol", command)
        self.assertEqual(command[1:4], [str(self.root/"scripts/deepseek-slots.py"), "run", "--"])

    def test_worker_path_uses_actual_common_repository_for_linked_checkout(self):
        import subprocess
        original, linked = self.root / "original", self.root / "active-auth"
        subprocess.run(["git", "init", "-q", str(original)], check=True)
        subprocess.run(["git", "-C", str(original), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(original), "worktree", "add", "-q", "-b", "linked-fixture", str(linked)], check=True)
        with patch.object(builder, "ROOT", linked):
            self.assertEqual(builder.worker_path("future-worker"), original / ".claude/worktrees/future-worker")

    def test_codex_roles_use_subscription_and_reviewer_is_read_only(self):
        for role in ("lead", "backup", "reviewer"):
            observed = {}
            def capture(binary, command, environment): observed.update(command=command, environment=environment)
            with patch.object(builder, "ROOT", self.root), patch.object(builder, "codex_readiness"), patch.dict(os.environ, {"PATH":"/usr/bin", "OPENAI_API_KEY":"private", "A6API_API_KEY":"old"}, clear=True), patch("sys.argv", ["builder", "--role", role]), patch.object(builder.os, "execvpe", side_effect=capture):
                self.assertEqual(builder.main(), 0)
            self.assertEqual(observed["command"][2], "gpt-6.1-sol")
            self.assertIn("read-only" if role=="reviewer" else "workspace-write", observed["command"])
            self.assertNotIn("OPENAI_API_KEY", observed["environment"])
            self.assertNotIn("A6API_API_KEY", observed["environment"])

    def test_offline_check_never_queries_provider_and_sol_needs_no_deepseek_key(self):
        for role in ("lead","worker"):
            with patch.object(builder,"ROOT",self.root), patch.object(builder,"native_binary",return_value=Path("/bin/true")), patch.dict(os.environ,{"DEEPSEEK_API_KEY":"fixture"},clear=True), patch("sys.argv",["builder","--role",role,"--check"]), patch.object(builder,"funding_ready") as funding, patch.object(builder,"codex_readiness") as readiness, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(builder.main(),0)
                funding.assert_not_called();readiness.assert_not_called()

    def test_offline_prepare_creates_only_private_keyless_settings(self):
        output=io.StringIO()
        with patch.object(builder,"ROOT",self.root),patch.object(builder,"native_binary",return_value=Path("/bin/true")),patch.dict(os.environ,{"DEEPSEEK_API_KEY":"synthetic-not-for-settings"},clear=True),patch("sys.argv",["builder","--role","worker","--prepare"]),patch.object(builder,"funding_ready") as funding,contextlib.redirect_stdout(output):
            self.assertEqual(builder.main(),0);funding.assert_not_called()
        metadata=json.loads(output.getvalue());settings=Path(metadata["settings_path"])
        self.assertEqual(settings.stat().st_mode&0o777,0o600)
        self.assertNotIn("synthetic-not-for-settings",settings.read_text()+output.getvalue())
        self.assertTrue(json.loads(settings.read_text())["disableAllHooks"])

    def test_zero_unknown_or_unavailable_funding_pauses_dispatch(self):
        from unittest.mock import MagicMock
        for available,amount in ((True,"0.00"),(False,"10"),(True,"NaN"),(True,"invalid")):
            response=MagicMock();response.read.return_value=json.dumps({"is_available":available,"balance_infos":[{"currency":"USD","total_balance":amount}]}).encode()
            response.__enter__.return_value=response
            opener=MagicMock();opener.open.return_value=response
            with patch.object(builder.urllib.request,"build_opener",return_value=opener), self.assertRaises(builder.SetupError):builder.funding_ready("synthetic")
        response.read.return_value=b'{"is_available":true,"balance_infos":[{"currency":"USD","total_balance":"1"}]}'
        with patch.object(builder.urllib.request,"build_opener",return_value=opener):builder.funding_ready("synthetic")


if __name__ == "__main__":
    unittest.main()
