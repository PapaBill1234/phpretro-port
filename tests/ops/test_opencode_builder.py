import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("opencode_builder", ROOT / "scripts/opencode-builder.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class OpenCodeBuilderTests(unittest.TestCase):
    def test_profiles_are_isolated_and_environment_keeps_only_oauth_desktop_context(self):
        with tempfile.TemporaryDirectory() as temp:
            inherited = {
                "PATH": "/usr/bin", "HOME": "/home/example",
                "OPENAI_API_KEY": "private", "DEEPSEEK_API_KEY": "private",
                "ANTHROPIC_AUTH_TOKEN": "private", "CODEX_HOME": "/home/example/.codex",
                "XDG_DATA_HOME": "/home/example/.local/share", "DISPLAY": ":0",
                "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/run/user/1000/xauth",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            }
            first = builder.profile_environment("primary", inherited, Path(temp))
            second = builder.profile_environment("secondary", inherited, Path(temp))
            for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                self.assertNotEqual(first[name], second[name])
            for env in (first, second):
                for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_HOME"):
                    self.assertNotIn(name, env)
                for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
                    self.assertEqual(env[name], inherited[name])

    def test_current_v2_pin_is_native_and_never_silently_changes_accounts_or_models(self):
        self.assertEqual(builder.VERSION, "2.0.29")
        self.assertEqual(builder.PACKAGE_URL,
                         "https://registry.npmjs.org/@opencode/cli-linux-x64/-/cli-linux-x64-2.0.29.tgz")
        self.assertTrue(builder.PACKAGE_SRI.startswith("sha512-"))
        self.assertEqual(builder.PROFILES, ("primary", "secondary"))

    def test_profile_validation_and_runtime_receipt_mismatch_fail_closed(self):
        with self.assertRaises(builder.BootstrapError):
            builder.profile_root("deepseek")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / f"v{builder.VERSION}"
            directory.mkdir()
            binary = directory / "opencode"
            binary.write_text("not an executable")
            with self.assertRaisesRegex(builder.BootstrapError, "not installed"):
                builder.verify_binary(root)


if __name__ == "__main__":
    unittest.main()
