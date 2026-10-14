"""Exercise the generic native-Sol generation transport at its real process
boundary. The app-server is a deterministic fake: no login, no model call and
no real Codex process is used, but the stdio/RPC/serialization/validation
contracts are the real ones."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = _load("codex_generate", "scripts/codex-generate.py")

FAKE = r'''
import json,sys,time,subprocess
scenario,record=sys.argv[1:]
def send(row):print(json.dumps(row),flush=True)
for line in sys.stdin:
 row=json.loads(line);method=row.get('method');ident=row.get('id');p=row.get('params',{})
 with open(record,'a') as f:f.write(json.dumps({'method':method,'params':p})+'\n')
 if method=='initialize':value={}
 elif method=='initialized':continue
 elif method=='account/read':value={'account':{'type':'apiKey' if scenario=='apikey' else 'chatgpt','email':'private@example.invalid'}}
 elif method=='account/rateLimits/read':
  value={'ordinaryUsageAllowed':True,'accountId':'account-one','rateLimitsByLimitId':{'codex':{'spendControlReached':False,'rateLimitReachedType':None,'primary':{'usedPercent':90 if scenario=='reserve' else 5,'windowDurationMins':300,'resetsAt':time.time()+3600},'secondary':{'usedPercent':1,'windowDurationMins':10080,'resetsAt':time.time()+9000}}}}
  if scenario=='unknown':value['ordinaryUsageAllowed']=None
 elif method=='config/read':
  value={'config':{'model_provider':'openai','mcp_servers':{},'chatgpt_base_url':'https://chatgpt.com/backend-api/'}}
  if scenario=='connectors':value['config']['mcp_servers']={'inherited':{'enabled':True}}
 elif method=='model/list':value={'data':[{'id':'sol','model':'gpt-6.1-sol'}],'nextCursor':None}
 elif method=='thread/start':
  value={'thread':{'id':'thread-one'},'model':'wrong-model' if scenario=='model-mismatch' else 'gpt-6.1-sol','modelProvider':'openai'}
 elif method=='turn/start':
  send({'id':ident,'result':{'turn':{'id':'turn-one'}}})
  if scenario=='eof':sys.exit(0)
  if scenario=='malformed':print('private-bad-json',flush=True);continue
  if scenario=='oversized':print('x'*1048577,flush=True);continue
  if scenario=='timeout':continue
  if scenario=='unexpected':
   send({'method':'item/completed','params':{'threadId':'thread-one','turnId':'turn-one','item':{'type':'commandExecution','id':'c1'}}});continue
  if scenario=='event':
   send({'method':'turn/unknownEvent','params':{'threadId':'thread-one','turnId':'turn-one'}});continue
  if scenario=='fakedelta':
   send({'method':'item/agentMessage/monkeydelta','params':{'threadId':'thread-one','turnId':'turn-one','text':'x'}});continue
  if scenario=='offturn':
   send({'method':'item/agentMessage/delta','params':{'threadId':'thread-one','turnId':'other-turn','delta':'leak'}});sys.exit(0)
  send({'method':'item/agentMessage/delta','params':{'threadId':'thread-one','turnId':'turn-one','delta':'Hel'}})
  send({'method':'item/agentMessage/delta','params':{'threadId':'thread-one','turnId':'turn-one','delta':'lo'}})
  if scenario=='wrongturn':
   send({'method':'turn/completed','params':{'threadId':'thread-one','turn':{'id':'other-turn','status':'completed'}}});sys.exit(0)
  item={'type':'agentMessage','id':'m1','text':'Hello','phase':'final_answer'}
  if scenario=='toolcall':item['toolCalls']=[{'id':'t','name':'exec','arguments':{}}]
  if scenario=='json':item['text']='{"a":1}'
  if scenario=='badjson':item['text']='not json'
  send({'method':'item/completed','params':{'threadId':'thread-one','turnId':'turn-one','item':item}})
  send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-one','turnId':'turn-one','tokenUsage':{'last':{'inputTokens':10,'outputTokens':5,'private':'never-copy'}}}})
  send({'method':'turn/completed','params':{'threadId':'thread-one','turn':{'id':'turn-one','status':scenario if scenario in ('failed','interrupted') else 'completed'}}})
  continue
 elif method=='turn/interrupt':
  send({'id':ident,'result':{}});sys.exit(0)
 elif 'error' in row:continue
 else:value={}
 if ident is not None:send({'id':ident,'result':value})
'''


class _Stdin:
    """Minimal stdin shim exposing the ``.buffer`` attribute execute() reads."""

    def __init__(self, data):
        self.buffer = io.BytesIO(data)


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE)
        self.record = self.root / "calls.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def request(self, **overrides):
        payload = {"messages": [{"role": "user", "content": "Say hello"}]}
        payload.update(overrides)
        return payload

    def run_generate(self, scenario="completed", request=None, timeout=2, qualify=None):
        events = []
        args = generate.parser().parse_args(["--state-dir", str(self.state), "--timeout", str(timeout)])
        command = [sys.executable, str(self.fake), scenario, str(self.record)]
        with patch.object(sys, "stdin", _Stdin(json.dumps(request or self.request()).encode())):
            result = generate.execute(args, command=command, emit=events.append, qualify=qualify)
        return result, events

    def rows(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def methods(self):
        return [row.get("method") for row in self.rows()]

    def test_happy_path_streams_provisional_deltas_then_validated_final(self):
        result, events = self.run_generate()
        self.assertTrue(result["ok"])
        self.assertEqual([e["type"] for e in events], ["delta", "delta", "final"])
        final = events[-1]
        self.assertEqual(final["text"], "Hello")
        self.assertEqual(final["usage"], {"last": {"inputTokens": 10, "outputTokens": 5}})
        self.assertEqual(final["finishReason"], "stop")

    def test_thread_start_serializes_message_roles_and_omits_unsupported_fields(self):
        self.run_generate(request={"system": "be terse", "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]})
        thread = next(row["params"] for row in self.rows() if row.get("method") == "thread/start")
        # These isolation fields are absent from the installed schema; they are
        # not sent and are not relied upon.
        for absent in generate.REQUIRED_THREAD_FIELDS:
            self.assertNotIn(absent, thread)
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["developerInstructions"], generate.GENERATION_DEVELOPER_INSTRUCTIONS)
        self.assertNotIn("source review", thread["developerInstructions"].lower())
        turn = next(row["params"] for row in self.rows() if row.get("method") == "turn/start")
        texts = [item["text"] for item in turn["input"]]
        self.assertEqual(texts[0], "[system]\nbe terse")
        self.assertTrue(texts[1].startswith("[system]\nsys"))
        self.assertTrue(texts[2].startswith("[user]\nu1"))
        self.assertTrue(texts[3].startswith("[assistant]\na1"))
        self.assertNotIn("thread/resume", self.methods())

    def test_private_account_data_never_reaches_output(self):
        _, events = self.run_generate()
        blob = json.dumps(events)
        self.assertNotIn("private@example", blob)
        self.assertNotIn("account-one", blob)

    def test_model_provider_mismatch_blocks_success(self):
        _, events = self.run_generate("model-mismatch")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "model_mismatch")

    def test_failed_interrupted_eof_malformed_and_oversized_are_not_success(self):
        for scenario, code in [
            ("failed", "turn_failed"),
            ("interrupted", "turn_interrupted"),
            ("eof", "rpc_eof"),
            ("malformed", "rpc_malformed"),
            ("oversized", "rpc_oversized"),
        ]:
            with self.subTest(scenario=scenario):
                _, events = self.run_generate(scenario)
                self.assertEqual(events[-1]["type"], "error")
                self.assertEqual(events[-1]["code"], code)
                self.assertNotIn("private-bad", json.dumps(events))

    def test_non_matching_turn_cannot_settle(self):
        _, events = self.run_generate("wrongturn")
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotEqual(events[-1].get("code"), "final")

    def test_unexpected_tool_item_and_unknown_event_abort(self):
        for scenario, code in [("unexpected", "unexpected_tool"), ("event", "unexpected_event")]:
            with self.subTest(scenario=scenario):
                _, events = self.run_generate(scenario)
                self.assertEqual(events[-1], {"type": "error", "code": code, "message": events[-1]["message"]})
                self.assertNotEqual(events[-1]["type"], "final")

    def test_requested_schema_validates_structured_output(self):
        _, good = self.run_generate("json", request=self.request(responseSchema={"type": "object"}))
        self.assertEqual(good[-1]["type"], "final")
        _, bad = self.run_generate("badjson", request=self.request(responseSchema={"type": "object"}))
        self.assertEqual(bad[-1]["code"], "schema_invalid")

    def test_unrequested_tool_call_fails_validation(self):
        _, events = self.run_generate("toolcall")
        self.assertEqual(events[-1]["code"], "unexpected_tool")

    def test_unknown_and_reserve_quota_block_before_any_turn(self):
        for scenario in ("apikey", "unknown", "reserve"):
            self.record.unlink(missing_ok=True)
            _, events = self.run_generate(scenario)
            self.assertEqual(events[-1]["type"], "error")
            self.assertNotIn("turn/start", self.methods())

    def test_off_peak_pause_blocks_generation(self):
        def blocked():
            raise ValueError("off-peak closed")
        with patch.object(generate.window_policy, "require_allowed", blocked):
            _, events = self.run_generate()
        self.assertEqual(events[-1]["code"], "off_peak_paused")
        self.assertFalse(self.record.exists())

    def test_deadline_interrupts_and_cleans_the_group(self):
        _, events = self.run_generate("timeout", timeout=0.3)
        self.assertEqual(events[-1]["code"], "timeout")
        self.assertIn("turn/interrupt", self.methods())

    def test_each_call_opens_a_unique_stateless_thread(self):
        self.run_generate()
        self.run_generate(request=self.request(messages=[{"role": "user", "content": "second"}]))
        self.assertEqual(self.methods().count("thread/start"), 2)
        self.assertNotIn("thread/resume", self.methods())

    def test_request_scope_and_bounds_are_enforced(self):
        for bad in ({"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "x"}]},
                    {"cwd": "/etc", "messages": [{"role": "user", "content": "x"}]},
                    {"messages": [{"role": "root", "content": "x"}]},
                    {"messages": []}):
            with self.subTest(payload=bad):
                _, events = self.run_generate(request=bad)
                self.assertEqual(events[-1]["type"], "error")
        with self.assertRaises(generate.bridge.BridgeError):
            generate.parse_request(b"x" * (generate.MAX_REQUEST + 1))
        with self.assertRaises(generate.bridge.BridgeError):
            generate.parse_request(b"not-json")

    def test_child_environment_never_forwards_credentials(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "private",
            "ACCESS_TOKEN": "private",
            "ANTHROPIC_AUTH_TOKEN": "private",
            "CODEX_HOME": "/selected/codex-home",
        }):
            env = generate.bridge.child_environment()
            self.assertEqual(env["CODEX_HOME"], "/selected/codex-home")
            self.assertFalse(any(key.endswith(("API_KEY", "TOKEN")) for key in env))

    def test_installed_codex_version_is_verified_before_dispatch(self):
        good = self.root / "codex-ok"
        good.write_text("#!/bin/sh\nprintf 'codex-cli 0.162.0\\n'\n")
        good.chmod(0o755)
        self.assertEqual(generate.verify_version(str(good)), generate.CODEX_VERSION)
        bad = self.root / "codex-bad"
        bad.write_text("#!/bin/sh\nprintf 'codex-cli 0.100.0\\n'\n")
        bad.chmod(0o755)
        with self.assertRaises(generate.bridge.BridgeError) as caught:
            generate.verify_version(str(bad))
        self.assertEqual(caught.exception.code, "codex_version")

    def test_only_the_exact_delta_method_matching_the_turn_is_read(self):
        _, good = self.run_generate()
        self.assertEqual([e["type"] for e in good if e["type"] == "delta"], ["delta", "delta"])
        _, fake = self.run_generate("fakedelta")
        self.assertEqual(fake[-1]["code"], "unexpected_event")
        _, offturn = self.run_generate("offturn")
        self.assertNotIn("delta", [e["type"] for e in offturn])

    def _feature_map(self, sticky=()):
        """Baseline with every relevant feature active plus benign/tombstone entries."""
        features = {name: {"stage": "stable", "state": True} for name in generate.RELEVANT_FEATURES}
        features["apply_patch_freeform"] = {"stage": "removed", "state": False}
        features["api_key_model_discovery"] = {"stage": "stable", "state": True}
        features["skill_mcp_dependency_install"] = {"stage": "stable", "state": True}
        return features, set(sticky)

    def _probe_factory(self, sticky=()):
        sticky = set(sticky)

        def probe(binary, overrides=()):
            features, _ = self._feature_map()
            for key, value in overrides:
                name = key.split(".", 1)[1]
                if name in sticky:
                    continue  # override has no effect (sticky capability)
                features[name] = {"stage": "stable", "state": False if value == "false" else True}
            return features

        return probe

    def test_command_uses_recognized_feature_keys_not_unknown_tools_namespace(self):
        command = generate.generation_codex_command(binary=sys.executable)
        joined = " ".join(command)
        for unknown in ("tools.unified_exec", "tools.apply_patch", "tools.web_search"):
            self.assertNotIn(unknown, joined)
        for override in (
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.apply_patch_freeform=false",
            "features.view_image=false",
            "features.code_mode_host=false",
            "features.workspace_dependencies=false",
            'web_search="disabled"',
            "mcp_servers={}",
            "project_doc_max_bytes=0",
            'sandbox_mode="read-only"',
            'approval_policy="never"',
        ):
            self.assertIn(override, joined)
        keys = [key for key, _ in generate.GENERATION_CONFIG_OVERRIDES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_qualify_passes_only_when_candidate_overrides_disable_every_relevant_feature(self):
        result = generate.qualify_native_contract(
            "x", probe=self._probe_factory(), thread_probe=lambda _b: True
        )
        self.assertTrue(result["qualified"])
        self.assertEqual(result["reasons"], ())

    def test_qualify_blocks_when_a_override_does_not_take_effect(self):
        # unified_exec stays true even with the candidate override (the real bug).
        result = generate.qualify_native_contract(
            "x", probe=self._probe_factory(sticky=["unified_exec"]), thread_probe=lambda _b: True
        )
        self.assertFalse(result["qualified"])
        self.assertIn("active:unified_exec", result["reasons"])

    def test_qualify_blocks_unsupported_thread_fields_and_unknown_active_feature(self):
        threads_off = generate.qualify_native_contract(
            "x", probe=self._probe_factory(), thread_probe=lambda _b: False
        )
        self.assertFalse(threads_off["qualified"])
        self.assertIn("thread_params_unsupported", threads_off["reasons"])

        def probe(binary, overrides=()):
            features = self._probe_factory()(binary, overrides)
            features["shell_tool_v2"] = {"stage": "stable", "state": True}
            return features

        unknown = generate.qualify_native_contract("x", probe=probe, thread_probe=lambda _b: True)
        self.assertFalse(unknown["qualified"])
        self.assertIn("unknown_active:shell_tool_v2", unknown["reasons"])

    def test_probe_applies_overrides_and_parses_stdout_only(self):
        # A fake CLI that records argv and separates stdout/stderr.
        fake = self.root / "fake-codex"
        record = self.root / "feat-args.txt"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > " + str(record) + "\n"
            "echo 'shell_tool  stable  true' >&2\n"
            "printf 'not_shell_tool  stable  false\\nviews  stable  true\\n'\n"
        )
        fake.chmod(0o755)
        parsed = generate.probe_feature_list(str(fake), overrides=[("features.unified_exec", "false")])
        self.assertEqual(
            parsed,
            {"not_shell_tool": {"stage": "stable", "state": False}, "views": {"stage": "stable", "state": True}},
        )
        argv = record.read_text().split()
        self.assertIn("-c", argv)
        self.assertIn("features.unified_exec=false", argv)
        self.assertIn("features", argv)  # subcommand preserved after the overrides

    def test_execute_blocks_before_starting_rpc_on_unqualified_contract(self):
        result, events = self.run_generate(qualify=lambda: {"qualified": False, "reasons": ("active:unified_exec",)})
        self.assertFalse(result["ok"])
        self.assertEqual(events[-1]["code"], "unsupported_contract")
        self.assertFalse(self.record.exists())  # the app-server never started

    def test_effective_config_with_retained_mcp_connector_is_refused(self):
        _, events = self.run_generate("connectors")
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("turn/start", self.methods())

    def test_real_installed_contract_is_not_qualified_offline(self):
        binary = (
            "/home/ubuntu/phpretro-codex/cli/node_modules/@openai/codex-linux-x64/"
            "vendor/x86_64-unknown-linux-musl/bin/codex"
        )
        if not Path(binary).is_file():
            self.skipTest("installed Codex binary not present")
        baseline = generate.probe_feature_list(binary)
        self.assertGreater(len(baseline), 50)
        self.assertIn("shell_tool", baseline)
        result = generate.qualify_native_contract(binary)
        self.assertFalse(result["qualified"])
        self.assertIn("active:unified_exec", result["reasons"])
        self.assertIn("thread_params_unsupported", result["reasons"])


if __name__ == "__main__":
    unittest.main()
