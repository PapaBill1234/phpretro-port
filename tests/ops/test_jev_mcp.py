import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/jev-judge-mcp.py"
spec = importlib.util.spec_from_file_location("jev_judge_mcp", SCRIPT)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

FAKE_SERVER = r'''
import json, os, sys
from pathlib import Path
held = None
def send(message):
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    with Path(os.environ["TMPDIR"]).joinpath("upstream.jsonl").open("a") as log:
        log.write(json.dumps(message) + "\n")
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "tools/list":
        send({"jsonrpc":"2.0", "id":999999, "result":{"untracked":True}})
        result = {"tools":[{"name":"jev_judge", "inputSchema":{"type":"object"}},
                           {"name":"jev_gate"}, {"name":"other_tool"}]}
    elif method == "initialize":
        result = {"protocolVersion":"2025-03-26", "capabilities":{"tools":{}, "resources":{}},
                  "serverInfo":{"name":"fake-upstream", "version":"1"}}
    elif method == "ping":
        if message.get("params", {}).get("hold"):
            held = message
            continue
        result = {"pong":True}
        if held:
            send({"jsonrpc":"2.0", "id":message["id"], "result":result})
            send({"jsonrpc":"2.0", "id":held["id"], "result":{"held":True}})
            held = None
            continue
    else:
        if message.get("params", {}).get("arguments", {}).get("error"):
            send({"jsonrpc":"2.0", "id":message["id"],
                  "error":{"code":-32603, "message":os.environ["OPENROUTER_API_KEY"]}})
            continue
        result = {"content":[{"type":"text", "text":"fixture verdict"}]}
    send({"jsonrpc":"2.0", "id":message["id"], "result":result})
'''


class Client:
    def __init__(self, root):
        self.root = root
        fake = root / "upstream.py"
        fake.write_text(FAKE_SERVER)
        env = dict(os.environ, PHPRETRO_JEV_NODE=sys.executable, PHPRETRO_JEV_CLI=str(fake),
                   OPENROUTER_API_KEY="fixture-only-private-key", TMPDIR=str(root))
        env.pop("PHPRETRO_JEV_CONFIG", None)
        env.pop("PHPRETRO_OPENROUTER_ENV", None)
        self.process = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self.collect, daemon=True)
        self.reader.start()

    def collect(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def read(self):
        return self.messages.get(timeout=5)

    def forwarded(self):
        path = self.root / "upstream.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        stderr = self.process.stderr.read().decode()
        self.process.stderr.close()
        if self.process.returncode != 0:
            raise AssertionError("Proxy exited unexpectedly: " + stderr)
        if "fixture-only-private-key" in stderr:
            raise AssertionError("Credential leaked in stderr")


def request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Client(Path(self.temp.name))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_initialization_and_tool_inventory_are_judge_only(self):
        self.client.send(request("init", "initialize", {}))
        initialized = self.client.read()
        self.assertEqual(initialized["id"], "init")
        self.assertEqual(set(initialized["result"]["capabilities"]), {"tools"})
        self.client.send(request(2, "tools/list"))
        inventory = self.client.read()
        self.assertEqual(inventory["id"], 2)
        self.assertEqual([tool["name"] for tool in inventory["result"]["tools"]], ["jev_judge"])

    def test_gate_and_other_tool_calls_are_never_forwarded(self):
        for name in ("jev_gate", "other_tool", "JEV_JUDGE", "jev_judge "):
            self.client.send(request(name, "tools/call", {"name": name, "arguments": {}}))
            self.assertEqual(self.client.read()["error"]["code"], -32601)
        self.client.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "jev_gate"}})
        self.client.send(request("barrier", "ping"))
        self.client.read()
        self.assertFalse(any(message["method"] == "tools/call" for message in self.client.forwarded()))

    def test_allowed_judge_call_retains_result_and_client_id(self):
        args = {"state": "Two fixture checks passed.", "questions": [{"type": "noul", "question": "Did both checks pass?"}]}
        self.client.send(request("judge-1", "tools/call", {"name": "jev_judge", "arguments": args}))
        response = self.client.read()
        self.assertEqual(response["id"], "judge-1")
        self.assertEqual(response["result"]["content"][0]["text"], "fixture verdict")
        forwarded = self.client.forwarded()[0]
        self.assertEqual(forwarded["params"]["arguments"], args)
        self.assertNotEqual(forwarded["id"], "judge-1")

    def test_out_of_order_ids_and_cancellation_are_tracked(self):
        self.client.send(request("held", "ping", {"hold": True}))
        self.client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "held"}})
        self.client.send(request(7, "ping"))
        self.assertEqual(self.client.read()["id"], 7)
        self.assertEqual(self.client.read()["id"], "held")
        forwarded = self.client.forwarded()
        self.assertEqual(forwarded[1]["params"]["requestId"], forwarded[0]["id"])

    def test_duplicate_active_client_id_fails_without_forwarding(self):
        self.client.send(request("same", "ping", {"hold": True}))
        self.client.send(request("same", "ping"))
        self.assertIn("error", self.client.read())
        self.client.send(request("release", "ping"))
        self.client.read(); self.client.read()
        self.assertEqual(len(self.client.forwarded()), 2)

    def test_upstream_errors_are_redacted_and_known_keys_are_rejected(self):
        self.client.send(request(1, "tools/call", {"name": "jev_judge", "arguments": {"error": True}}))
        self.assertNotIn("fixture-only-private-key", json.dumps(self.client.read()))
        self.client.send(request(2, "tools/call", {"name": "jev_judge", "arguments": {"state": "fixture-only-private-key"}}))
        self.assertEqual(self.client.read()["error"]["code"], -32602)
        self.client.send(request(3, "ping")); self.client.read()
        self.assertEqual(len(self.client.forwarded()), 2)

    def test_unexposed_methods_fail_without_forwarding(self):
        self.client.send(request(1, "resources/list"))
        self.assertEqual(self.client.read()["error"]["code"], -32601)
        self.client.send(request(2, "ping")); self.client.read()
        self.assertEqual(len(self.client.forwarded()), 1)


class PrivateConfigurationTests(unittest.TestCase):
    def test_owned_dotenv_is_data_not_shell_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.env"
            path.write_text("export OPENROUTER_API_KEY='fixture-only-key'\nOPENROUTER_BASE_URL=https://openrouter.ai\nOTHER=$(touch not-executed)\n")
            path.chmod(0o600)
            self.assertEqual(proxy.dotenv(path)["OPENROUTER_API_KEY"], "fixture-only-key")
            self.assertFalse((Path(directory) / "not-executed").exists())
            path.chmod(0o644)
            with self.assertRaises(proxy.SetupError):
                proxy.dotenv(path)

    def test_duplicate_and_symlink_credential_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.env"
            path.write_text("OPENROUTER_API_KEY=one\nOPENROUTER_API_KEY=two\n")
            path.chmod(0o600)
            with self.assertRaises(proxy.SetupError):
                proxy.dotenv(path)
            link = Path(directory) / "link.env"
            link.symlink_to(path)
            with self.assertRaises(proxy.SetupError):
                proxy.private_text(link)

    def test_private_file_injects_only_openrouter_fields_to_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "private.env"
            path.write_text("OPENROUTER_API_KEY=fixture-only-key\nOPENROUTER_BASE_URL=https://openrouter.ai\nANTHROPIC_AUTH_TOKEN=other-private-key\n")
            path.chmod(0o600)
            cli = root / "cli.js"; cli.write_text("fixture")
            env = {"PHPRETRO_OPENROUTER_ENV": str(path), "PHPRETRO_JEV_NODE": sys.executable,
                   "PHPRETRO_JEV_CLI": str(cli), "ANTHROPIC_AUTH_TOKEN": "other-private-key"}
            with patch.dict(os.environ, env, clear=True):
                command, child, secrets = proxy.upstream_config()
            self.assertEqual(command[-3:], ["serve", "--backend", "openrouter"])
            self.assertEqual(child["OPENROUTER_API_KEY"], "fixture-only-key")
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child)
            self.assertIn("other-private-key", secrets)


if __name__ == "__main__":
    unittest.main()
