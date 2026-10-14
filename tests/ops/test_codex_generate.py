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


def _iso(epoch):
    dt = generate.window_policy.dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def _window(stop_at=None, seconds=3600):
    """Deterministic allowed gate: a cutoff derived from a caller-chosen offset,
    never from the real off-peak schedule."""
    if stop_at is None:
        stop_at = _iso(generate.window_policy.dt.datetime.now(
            generate.window_policy.dt.timezone.utc).timestamp() + seconds)
    return {"allowed": True, "timezone": "UTC", "reason": "off_peak_buffered", "stop_at": stop_at}

FAKE = r'''
import json,sys,time,subprocess
scenario,record=sys.argv[1:]
def send(row):print(json.dumps(row),flush=True)
for line in sys.stdin:
 row=json.loads(line);method=row.get('method');ident=row.get('id');p=row.get('params',{})
 with open(record,'a') as f:f.write(json.dumps({'method':method,'params':p})+'\n')
 if method=='initialize':value={}
 elif method=='initialized':continue
 elif method=='account/read':
  if scenario=='account-stall':time.sleep(5);continue
  value={'account':{'type':'apiKey' if scenario=='apikey' else 'chatgpt','email':'private@example.invalid'}}
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


class _RecordingClient:
    """Minimal RpcClient stand-in for controlled-clock run_turn deadline tests."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.active = None
        self.pending = []

    def request(self, method, params):
        self.calls.append(method)
        return self.responses[method]

    def next(self):
        return None


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

    def run_generate(self, scenario="completed", request=None, timeout=2, qualify=None,
                     window=None, gate=None):
        events = []
        args = generate.parser().parse_args(["--state-dir", str(self.state), "--timeout", str(timeout)])
        command = [sys.executable, str(self.fake), scenario, str(self.record)]
        if gate is None:
            chosen = window if window is not None else _window()

            def gate():
                return chosen
        with patch.object(generate.window_policy, "require_allowed", gate), patch.object(
            sys, "stdin", _Stdin(json.dumps(request or self.request()).encode())
        ):
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

    def test_thread_start_serializes_message_roles_and_omits_unconfirmed_fields(self):
        self.run_generate(request={"system": "be terse", "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]})
        thread = next(row["params"] for row in self.rows() if row.get("method") == "thread/start")
        # With no offline schema confirmation (caller-emulated host), the
        # isolation fields are NOT sent — they must never be assumed.
        for absent in generate.REQUIRED_THREAD_FIELDS:
            self.assertNotIn(absent, thread)
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["developerInstructions"], generate.GENERATION_DEVELOPER_INSTRUCTIONS)
        self.assertNotIn("source review", thread["developerInstructions"].lower())
        turn = next(row["params"] for row in self.rows() if row.get("method") == "turn/start")
        self.assertNotIn("environments", turn)
        texts = [item["text"] for item in turn["input"]]
        self.assertEqual(texts[0], "[system]\nbe terse")
        self.assertTrue(texts[1].startswith("[system]\nsys"))
        self.assertTrue(texts[2].startswith("[user]\nu1"))
        self.assertTrue(texts[3].startswith("[assistant]\na1"))
        self.assertNotIn("thread/resume", self.methods())

    def test_thread_and_turn_pin_isolation_fields_only_when_schema_confirmed(self):
        # The experimental schema is offline-confirmed -> the isolation fields are
        # sent as explicit empty/false values on the thread and turn.
        self.run_generate(qualify=lambda: {"qualified": True, "schema_supported": True})
        thread = next(row["params"] for row in self.rows() if row.get("method") == "thread/start")
        self.assertEqual(thread["environments"], [])
        self.assertEqual(thread["dynamicTools"], [])
        self.assertEqual(thread["selectedCapabilityRoots"], [])
        self.assertIs(thread["allowProviderModelFallback"], False)
        turn = next(row["params"] for row in self.rows() if row.get("method") == "turn/start")
        self.assertEqual(turn["environments"], [])

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
        _, events = self.run_generate(gate=blocked)
        self.assertEqual(events[-1]["code"], "off_peak_paused")
        self.assertFalse(self.record.exists())

    def test_deadline_interrupts_and_cleans_the_group(self):
        _, events = self.run_generate("timeout", timeout=0.3)
        self.assertEqual(events[-1]["code"], "timeout")
        self.assertIn("turn/interrupt", self.methods())

    def test_capture_work_deadline_rejects_missing_invalid_expired_and_reserved(self):
        now = 1000.0
        for bad in (None, {}, {"stop_at": None}, {"stop_at": ""}, {"stop_at": "not-a-time"},
                    {"stop_at": "2030-01-01T00:00:00"},  # naive timestamp (no timezone)
                    {"stop_at": _iso(now - 1)}, {"stop_at": _iso(now + 4)}):
            with self.subTest(bad=bad):
                with self.assertRaises(generate.bridge.BridgeError) as caught:
                    generate.capture_work_deadline(bad, 30, now_monotonic=now, now_utc=now)
                self.assertEqual(caught.exception.code, "off_peak_paused")
        self.assertAlmostEqual(
            generate.capture_work_deadline({"stop_at": _iso(now + 3600)}, 30, now_monotonic=now, now_utc=now),
            now + 30,
        )

    def test_capture_work_deadline_uses_earlier_of_caller_timeout_and_gate(self):
        now = 500.0
        # A far gate: the short caller timeout binds and the reserve is never taken from it.
        self.assertAlmostEqual(
            generate.capture_work_deadline({"stop_at": _iso(now + 3600)}, 0.3, now_monotonic=now, now_utc=now),
            now + 0.3,
        )
        # A near gate: only the GATE budget loses the five-second cleanup reserve.
        self.assertAlmostEqual(
            generate.capture_work_deadline({"stop_at": _iso(now + 6)}, 300, now_monotonic=now, now_utc=now),
            now + 1.0,
        )

    def test_missing_or_expired_cutoff_blocks_before_app_server(self):
        for window in ({"allowed": True}, {"allowed": True, "stop_at": _iso(
                generate.window_policy.dt.datetime.now(generate.window_policy.dt.timezone.utc).timestamp() - 1)}):
            with self.subTest(window=window):
                self.record.unlink(missing_ok=True)
                result, events = self.run_generate(window=window)
                self.assertFalse(result["ok"])
                self.assertEqual(events[-1]["code"], "off_peak_paused")
                self.assertFalse(self.record.exists())

    def test_metadata_exhausting_budget_blocks_before_rpc(self):
        self.record.unlink(missing_ok=True)

        def qualify():
            # Metadata consumed the whole captured budget.
            generate._DEADLINE.set(generate.time.monotonic() - 1)
            return {"qualified": True, "schema_supported": True}

        result, events = self.run_generate(qualify=qualify)
        self.assertFalse(result["ok"])
        self.assertEqual(events[-1]["code"], "timeout")
        self.assertFalse(self.record.exists())  # the app-server never started

    def test_window_is_captured_once_and_cannot_be_extended(self):
        calls = []
        windows = [_window(seconds=3600), _window(seconds=99999)]

        def gate():
            calls.append(1)
            return windows[min(len(calls) - 1, 1)]

        result, events = self.run_generate(gate=gate)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)  # no later window can extend the deadline

    def test_short_caller_timeout_still_completes_with_gate_only_reserve(self):
        result, events = self.run_generate(timeout=0.5)
        self.assertTrue(result["ok"])
        self.assertEqual(events[-1]["type"], "final")

    def test_gate_cutoff_binds_rpc_deadline_not_caller_timeout(self):
        now = generate.window_policy.dt.datetime.now(generate.window_policy.dt.timezone.utc).timestamp()
        start = time.monotonic()
        _, events = self.run_generate("timeout", timeout=30, window={"allowed": True, "stop_at": _iso(now + 8)})
        elapsed = time.monotonic() - start
        self.assertEqual(events[-1]["code"], "timeout")
        self.assertLess(elapsed, 6)  # bounded by gate - reserve (~3s), not the 30s caller timeout
        self.assertIn("turn/interrupt", self.methods())

    def test_account_stall_times_out_before_thread(self):
        self.record.unlink(missing_ok=True)
        _, events = self.run_generate("account-stall", timeout=0.5)
        self.assertEqual(events[-1]["code"], "timeout")
        self.assertNotIn("thread/start", self.methods())

    def test_run_turn_refuses_thread_start_once_budget_is_spent(self):
        client = _RecordingClient({})
        request = generate.parse_request(json.dumps(self.request()).encode())
        generate._DEADLINE.set(generate.time.monotonic() - 1)
        try:
            with self.assertRaises(generate.bridge.BridgeError) as caught:
                generate.run_turn(client, request, lambda _e: None, self.root)
        finally:
            generate._DEADLINE.set(None)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(client.calls, [])

    def test_run_turn_refuses_turn_start_when_thread_used_the_budget(self):
        client = _RecordingClient({
            "thread/start": {"thread": {"id": "t"}, "model": generate.MODEL, "modelProvider": generate.PROVIDER},
        })
        request = generate.parse_request(json.dumps(self.request()).encode())
        generate._DEADLINE.set(100.0)
        try:
            with patch.object(generate.time, "monotonic", side_effect=[99.0, 101.0]):
                with self.assertRaises(generate.bridge.BridgeError) as caught:
                    generate.run_turn(client, request, lambda _e: None, self.root)
        finally:
            generate._DEADLINE.set(None)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(client.calls, ["thread/start"])

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

    def test_guardian_command_is_host_fixed_and_preserves_native_argv(self):
        native = ["host-codex", "app-server", "--listen", "stdio://"]
        deadline = 123456.5
        command = generate.guardian_command(native, deadline)
        self.assertEqual(command[:5], [sys.executable, generate.GUARDIAN,
                                       "--parent-pid", str(os.getpid()), "--deadline"])
        self.assertEqual(command[5], repr(float(deadline)))
        self.assertEqual(command[6], "--")
        self.assertEqual(command[7:], native)  # native argv verbatim after --
        self.assertTrue(Path(generate.GUARDIAN).is_file())
        # A caller cannot choose the wrapper: only the host passes pid/deadline.
        self.assertEqual(generate.guardian_command(native, deadline, parent_pid=7)[3], "7")

    def test_launched_native_command_is_wrapped_in_the_group_guardian(self):
        captured = {}

        class _Recorder:
            def __init__(self, command, timeout):
                captured["command"] = list(command)
                self.deadline = timeout
                self.active = None
                self.pending = []
                self.process = None

            def request(self, method, params):
                raise generate.bridge.BridgeError("rpc_error", "recorder stops here")

            def close(self, interrupt=False):
                captured["interrupt"] = interrupt

        native = ["host-codex", "app-server", "--listen", "stdio://"]
        fixed_deadline = time.monotonic() + 20.0
        args = generate.parser().parse_args(["--state-dir", str(self.state), "--timeout", "30"])
        with patch.object(generate, "capture_work_deadline", return_value=fixed_deadline), \
                patch.object(generate, "generation_codex_command", return_value=list(native)), \
                patch.object(generate.bridge, "resolve_codex_binary", return_value="host-codex"), \
                patch.object(generate.bridge, "RpcClient", _Recorder), \
                patch.object(generate.window_policy, "require_allowed", lambda: _window()), \
                patch.object(sys, "stdin", _Stdin(json.dumps(self.request()).encode())):
            result = generate.execute(args, command=None, emit=lambda _e: None,
                                      qualify=lambda: {"qualified": True, "schema_supported": False})
        self.assertFalse(result["ok"])
        command = captured["command"]
        self.assertEqual(command[1], generate.GUARDIAN)
        self.assertEqual(command[2:4], ["--parent-pid", str(os.getpid())])
        # The guardian receives the ORIGINAL captured monotonic work deadline...
        self.assertEqual(command[5], repr(float(fixed_deadline)))
        # ...and the host-fixed native argv verbatim.
        self.assertEqual(command[7:], native)

    def test_qualify_passes_only_when_candidate_overrides_disable_every_relevant_feature(self):
        result = generate.qualify_native_contract(
            "x",
            probe=self._probe_factory(),
            thread_probe=lambda _b: True,
            runtime_probe=lambda _b: True,
        )
        self.assertTrue(result["qualified"])
        self.assertTrue(result["schema_supported"])
        self.assertEqual(result["reasons"], ())

    def test_qualify_blocks_when_a_override_does_not_take_effect(self):
        # unified_exec stays true even with the candidate override (the real bug).
        result = generate.qualify_native_contract(
            "x",
            probe=self._probe_factory(sticky=["unified_exec"]),
            thread_probe=lambda _b: True,
            runtime_probe=lambda _b: True,
        )
        self.assertFalse(result["qualified"])
        self.assertIn("active:unified_exec", result["reasons"])

    def test_qualify_blocks_unsupported_thread_fields_and_unknown_active_feature(self):
        threads_off = generate.qualify_native_contract(
            "x",
            probe=self._probe_factory(),
            thread_probe=lambda _b: False,
            runtime_probe=lambda _b: True,
        )
        self.assertFalse(threads_off["qualified"])
        self.assertFalse(threads_off["schema_supported"])
        self.assertIn("thread_params_unsupported", threads_off["reasons"])

        def probe(binary, overrides=()):
            features = self._probe_factory()(binary, overrides)
            features["shell_tool_v2"] = {"stage": "stable", "state": True}
            return features

        unknown = generate.qualify_native_contract(
            "x", probe=probe, thread_probe=lambda _b: True, runtime_probe=lambda _b: True
        )
        self.assertFalse(unknown["qualified"])
        self.assertIn("unknown_active:shell_tool_v2", unknown["reasons"])

    def test_qualify_blocks_on_unverified_runtime_toolset_even_when_schema_supported(self):
        # Metadata support is reported honestly (schema_supported True) but MUST
        # NOT relax generation: the exact builtin runtime toolset is unverified.
        result = generate.qualify_native_contract(
            "x",
            probe=self._probe_factory(),
            thread_probe=lambda _b: True,
            runtime_probe=generate.runtime_toolset_verified,
        )
        self.assertTrue(result["schema_supported"])
        self.assertFalse(result["qualified"])
        self.assertIn("unverified_runtime_toolset", result["reasons"])

    def test_probe_applies_overrides_and_parses_stdout_only(self):
        response = generate.subprocess.CompletedProcess([], 0,
            b"not_shell_tool  stable  false\nviews  stable  true\n",
            b"shell_tool stable true\n")
        with patch.object(generate.subprocess, "run", return_value=response) as run:
            parsed = generate.probe_feature_list(sys.executable,
                overrides=[("features.unified_exec", "false")])
        argv = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotEqual(environment["CODEX_HOME"], os.environ.get("CODEX_HOME"))
        self.assertEqual(
            parsed,
            {"not_shell_tool": {"stage": "stable", "state": False}, "views": {"stage": "stable", "state": True}},
        )
        self.assertIn("-c", argv)
        self.assertIn("features.unified_exec=false", argv)
        self.assertIn("features", argv)  # subcommand preserved after the overrides

    def test_profile_rewriting_wrapper_is_refused_before_execution(self):
        wrapper = self.root / "wrapper"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        with patch.object(generate.subprocess, "run") as run:
            with self.assertRaises(generate.bridge.BridgeError) as error:
                generate.probe_feature_list(str(wrapper))
            self.assertEqual(error.exception.code, "unsupported_executable")
            run.assert_not_called()

    def test_execute_blocks_before_starting_rpc_on_unqualified_contract(self):
        for reason in ("active:unified_exec", "thread_params_unsupported", "unverified_runtime_toolset"):
            with self.subTest(reason=reason):
                self.record.unlink(missing_ok=True)
                result, events = self.run_generate(qualify=lambda: {"qualified": False, "reasons": (reason,)})
                self.assertFalse(result["ok"])
                self.assertEqual(events[-1]["code"], "unsupported_contract")
                self.assertFalse(self.record.exists())  # the app-server never started

    def test_schema_probe_reads_experimental_bundle_within_bounds(self):
        binary = self.root / "codex-elf"
        binary.write_bytes(b"\x7fELF" + b"\x00" * 8)

        def fake_run(command, **kwargs):
            # Offline only: no account/model/thread/turn. The probe must use an
            # empty HOME/CODEX_HOME and the --experimental schema subcommand.
            env = kwargs["env"]
            self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
            self.assertNotEqual(env["CODEX_HOME"], os.environ.get("CODEX_HOME"))
            self.assertIn("generate-json-schema", command)
            self.assertIn("--experimental", command)
            out = Path(command[command.index("--out") + 1])
            (out / "v2").mkdir(parents=True)
            (out / "v2" / "ThreadStartParams.json").write_text(json.dumps({
                "title": "ThreadStartParams",
                "properties": {
                    "environments": {"type": ["array", "null"], "items": {"$ref": "#/definitions/TurnEnvironmentParams"}},
                    "dynamicTools": {"type": ["array", "null"], "items": {"$ref": "#/definitions/DynamicToolSpec"}},
                    "selectedCapabilityRoots": {"type": ["array", "null"], "items": {"$ref": "#/definitions/SelectedCapabilityRoot"}},
                    "allowProviderModelFallback": {"type": "boolean"},
                },
            }))
            return generate.subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(generate.subprocess, "run", side_effect=fake_run):
            self.assertTrue(generate.thread_params_supported(str(binary)))

    def test_schema_probe_rejects_unknown_or_mistyped_schema(self):
        binary = self.root / "codex-elf"
        binary.write_bytes(b"\x7fELF" + b"\x00" * 8)

        def bundle(title="ThreadStartParams", mutate=lambda props: None):
            props = {
                "environments": {"type": ["array", "null"], "items": {"$ref": "#/definitions/TurnEnvironmentParams"}},
                "dynamicTools": {"type": ["array", "null"], "items": {"$ref": "#/definitions/DynamicToolSpec"}},
                "selectedCapabilityRoots": {"type": ["array", "null"], "items": {"$ref": "#/definitions/SelectedCapabilityRoot"}},
                "allowProviderModelFallback": {"type": "boolean"},
            }
            mutate(props)
            return {"title": title, "properties": props}

        cases = {
            "wrong-title": bundle(title="SomethingElse"),
            "missing-field": bundle(mutate=lambda p: p.pop("dynamicTools")),
            "renamed-field": bundle(mutate=lambda p: p.update({"env": p.pop("environments")})),
            "wrong-type": bundle(mutate=lambda p: p.__setitem__("allowProviderModelFallback", {"type": "string"})),
            "wrong-item-ref": bundle(mutate=lambda p: p.__setitem__("environments", {"type": ["array", "null"], "items": {"$ref": "#/definitions/Other"}})),
        }
        for label, schema in cases.items():
            with self.subTest(case=label):

                def fake_run(command, **kwargs):
                    out = Path(command[command.index("--out") + 1])
                    (out / "v2").mkdir(parents=True)
                    (out / "v2" / "ThreadStartParams.json").write_text(json.dumps(schema))
                    return generate.subprocess.CompletedProcess(command, 0, b"", b"")

                with patch.object(generate.subprocess, "run", side_effect=fake_run):
                    self.assertFalse(generate.thread_params_supported(str(binary)))

    def test_schema_probe_refuses_wrapper_before_execution(self):
        wrapper = self.root / "wrapper"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        with patch.object(generate.subprocess, "run") as run:
            with self.assertRaises(generate.bridge.BridgeError) as error:
                generate.thread_params_supported(str(wrapper))
            self.assertEqual(error.exception.code, "unsupported_executable")
            run.assert_not_called()

    def _valid_thread_bundle(self):
        """A valid experimental bundle built from the declared isolation shapes."""
        properties = {}
        for name, expected in generate.THREAD_FIELD_SHAPES.items():
            types = sorted(expected["types"])
            prop = {"type": types[0] if len(types) == 1 else types}
            if expected["items"] is not None:
                prop["items"] = {"$ref": "#/definitions/" + expected["items"]}
            properties[name] = prop
        return {"title": generate.THREAD_SCHEMA_TITLE, "properties": properties}

    def _run_metadata_helper(self, name, deadline, now):
        """Run one offline metadata helper at its real subprocess boundary under a
        captured work deadline and a controlled monotonic clock. Every helper's
        process call is replaced by a valid stand-in, so no native binary, account,
        model or network is touched; the returned mock records what it was called
        with. ``deadline`` of ``None`` leaves the work budget unset (plain cap)."""
        binary = sys.executable
        if name in ("probe_feature_list", "thread_params_supported"):
            elf = self.root / ("codex-elf-" + name)
            elf.write_bytes(b"\x7fELF" + b"\x00" * 8)
            binary = str(elf)

        def fake_run(command, **kwargs):
            if name == "verify_version":
                return generate.subprocess.CompletedProcess(
                    command, 0, b"codex-cli " + generate.CODEX_VERSION.encode() + b"\n", b"")
            if name == "probe_feature_list":
                return generate.subprocess.CompletedProcess(
                    command, 0, b"shell_tool  stable  true\n", b"")
            out = Path(command[command.index("--out") + 1])
            (out / "v2").mkdir(parents=True)
            (out / "v2" / "ThreadStartParams.json").write_text(json.dumps(self._valid_thread_bundle()))
            return generate.subprocess.CompletedProcess(command, 0, b"", b"")

        helper = getattr(generate, name)
        with patch.object(generate.subprocess, "run", side_effect=fake_run) as run, patch.object(
                generate.time, "monotonic", return_value=now):
            token = generate._DEADLINE.set(deadline)
            try:
                try:
                    result = helper(binary)
                except generate.bridge.BridgeError as error:
                    result = error.code
            finally:
                generate._DEADLINE.reset(token)
        return run, result

    def _expected_metadata_result(self, name):
        if name == "verify_version":
            return generate.CODEX_VERSION
        if name == "probe_feature_list":
            return {"shell_tool": {"stage": "stable", "state": True}}
        return True  # thread_params_supported

    def test_metadata_probe_timeout_is_positive_and_capped_by_work_deadline(self):
        now = 1000.0
        for name in ("verify_version", "probe_feature_list", "thread_params_supported"):
            with self.subTest(helper=name):
                expected = self._expected_metadata_result(name)
                # Unset budget: the helper's own cap is the timeout it passes on.
                plain, result = self._run_metadata_helper(name, None, now)
                self.assertEqual(plain.call_count, 1)  # the process ran exactly once
                self.assertEqual(result, expected)
                cap = plain.call_args.kwargs["timeout"]
                self.assertGreater(cap, 0)
                # A generous captured deadline must NOT extend the helper's own cap.
                roomy, result = self._run_metadata_helper(name, now + 10000.0, now)
                self.assertEqual(roomy.call_count, 1)
                self.assertEqual(result, expected)
                timeout = roomy.call_args.kwargs["timeout"]
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 10000.0)
                self.assertEqual(timeout, cap)
                # A tighter captured deadline shortens the timeout to what remains.
                remaining = 3.0
                tight, result = self._run_metadata_helper(name, now + remaining, now)
                self.assertEqual(tight.call_count, 1)
                self.assertEqual(result, expected)  # a setup/parse failure cannot pass
                timeout = tight.call_args.kwargs["timeout"]
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, remaining)
                self.assertAlmostEqual(timeout, remaining)

    def test_expired_work_deadline_blocks_metadata_before_subprocess(self):
        now = 1000.0
        for name in ("verify_version", "probe_feature_list", "thread_params_supported"):
            with self.subTest(helper=name):
                run, result = self._run_metadata_helper(name, now - 1.0, now)
                run.assert_not_called()
                self.assertEqual(result, "timeout")

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
        # The installed build's EXPERIMENTAL schema DOES define the isolation
        # fields (the earlier stable-only "absence" conclusion was wrong), but
        # generation must STAY blocked on the separate unverified runtime toolset.
        self.assertTrue(generate.thread_params_supported(binary))
        result = generate.qualify_native_contract(binary)
        self.assertTrue(result["schema_supported"])
        self.assertFalse(result["qualified"])
        self.assertIn("active:unified_exec", result["reasons"])
        self.assertIn("unverified_runtime_toolset", result["reasons"])
        self.assertNotIn("thread_params_unsupported", result["reasons"])


if __name__ == "__main__":
    unittest.main()
