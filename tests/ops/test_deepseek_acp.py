"""Behavioral gate-order tests for Eliza's direct DeepSeek ACP launcher.

No provider request or worker process is launched; provider, adapter lookup and
exec are controlled collaborators.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deepseek-acp.py"
SPEC = importlib.util.spec_from_file_location("deepseek_acp", SCRIPT)
acp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acp)


class Loader:
    def __init__(self, provider):
        self.provider = provider

    def exec_module(self, module):
        module.native_binary = self.provider.native_binary
        module.credential = self.provider.credential
        module.funding_ready = self.provider.funding_ready


class DeepSeekAcpgateTest(unittest.TestCase):
    def prepare(self, root):
        (root / "config").mkdir()
        (root / "config/models.json").write_text(json.dumps({"native_claude_version": "fixture"}))
        settings = root / ".builder/provider-settings/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{}")
        settings.chmod(0o600)

    def run_launcher(self, *, gate_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root)
            events = []
            provider = types.SimpleNamespace(
                native_binary=lambda version: Path("/fixture/claude"),
                credential=lambda: ("synthetic-key", "fixture"),
                funding_ready=lambda key: events.append(("funding", key)),
            )
            spec = types.SimpleNamespace(loader=Loader(provider))
            adapter_command = mock.Mock(return_value="/fixture/adapter.js")
            execve = mock.Mock(side_effect=lambda *args: events.append(("exec", args)))
            with (mock.patch.object(acp, "ROOT", root),
                  mock.patch.object(acp.importlib.util, "spec_from_file_location", return_value=spec),
                  mock.patch.object(acp.importlib.util, "module_from_spec", return_value=provider),
                  mock.patch.object(acp.window_policy, "require_allowed",
                                    side_effect=gate_error if gate_error else lambda: events.append(("window",))),
                  mock.patch.object(acp.subprocess, "check_output", adapter_command),
                  mock.patch.object(acp.os, "execve", execve)):
                if gate_error:
                    with self.assertRaisesRegex(ValueError, "closed"):
                        acp.main()
                else:
                    acp.main()
            return events, adapter_command, execve

    def test_closed_window_refuses_before_funding_or_adapter_launch(self):
        events, adapter_command, execve = self.run_launcher(gate_error=ValueError("closed window"))
        self.assertEqual(events, [])
        adapter_command.assert_not_called()
        execve.assert_not_called()

    def test_open_window_checks_before_funding_and_keeps_slots_supervisor(self):
        events, adapter_command, execve = self.run_launcher()
        self.assertEqual([event[0] for event in events], ["window", "funding", "exec"])
        self.assertEqual(events[1], ("funding", "synthetic-key"))
        adapter_command.assert_called_once()
        argv = execve.call_args.args[1]
        self.assertEqual(Path(argv[1]).name, "deepseek-slots.py")
        self.assertEqual(argv[2:4], ["run", "--"])
        self.assertEqual(argv[-2:], ["node", "/fixture/adapter.js"])


if __name__ == "__main__":
    unittest.main()
