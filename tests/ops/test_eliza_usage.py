import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/eliza-usage.py"
spec = importlib.util.spec_from_file_location("eliza_usage", SCRIPT)
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)

SECRET = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"


def write_attempt(root: Path, name: str, text: str, meta=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.private.json").write_text(text)
    (directory / "task.json").write_text(json.dumps(meta or {}))
    return directory


def claude_result(**usage_fields):
    value = {"type": "result", "subtype": "success", "duration_ms": 2500, "num_turns": 3,
             "usage": usage_fields}
    return json.dumps(value)


class ElizaUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_startup_stream_secrets_stay_private(self):
        startup_config = json.dumps({"apiKey": SECRET, "authorization": "Bearer " + SECRET,
                                     "type": "startup", "note": json.dumps({"type": "result"})})
        stream = ("claude startup: loading configuration\n"
                  + startup_config + "\n"
                  + "warning: retrying request\n"
                  + 'diagnostic mentions {"type":"result","subtype":"fake","usage":{"input_tokens":9999}}\n'
                  + "done\n"
                  + claude_result(input_tokens=100, cache_read_input_tokens=40,
                                  cache_creation_input_tokens=5, output_tokens=7))
        write_attempt(self.root, "a1", stream, {"attempt_id": "a1", "provider": "deepseek",
                                                "model": "deepseek-flash[1m]", "task": "G19-eliza-usage"})
        report = usage.build_report(self.root)
        blob = json.dumps(report)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("apiKey", blob)
        attempt = report["attempts"][0]
        self.assertEqual(attempt["input_tokens"], 100)
        self.assertEqual(attempt["output_tokens"], 7)
        self.assertEqual(attempt["model"], "deepseek-flash[1m]")
        self.assertEqual(attempt["task_id"], "G19-eliza-usage")
        self.assertEqual(report["providers"][0]["totals"]["input_tokens"], 100)

    def test_last_top_level_result_wins(self):
        stream = claude_result(input_tokens=1, output_tokens=1) + "\n" + claude_result(input_tokens=9, output_tokens=9)
        write_attempt(self.root, "a1", stream, {"attempt_id": "a1"})
        report = usage.build_report(self.root)
        self.assertEqual(report["attempts"][0]["input_tokens"], 9)

    def test_missing_and_bool_counts_unknown_not_zero(self):
        stream = claude_result(input_tokens=True, output_tokens=None)
        write_attempt(self.root, "a1", stream, {"attempt_id": "a1"})
        report = usage.build_report(self.root)
        attempt = report["attempts"][0]
        self.assertIsNone(attempt["input_tokens"])
        self.assertIsNone(attempt["cache_read_tokens"])
        self.assertIsNone(attempt["output_tokens"])
        self.assertIsNone(report["providers"][0]["totals"]["input_tokens"])

    def test_unsupported_format_is_unknown(self):
        write_attempt(self.root, "a1", json.dumps({"foo": 1, "bar": SECRET}), {"attempt_id": "a1", "provider": "deepseek"})
        report = usage.build_report(self.root)
        attempt = report["attempts"][0]
        self.assertEqual(attempt["format"], "unsupported")
        self.assertIsNone(attempt["input_tokens"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_huge_input_is_skipped(self):
        write_attempt(self.root, "a1", claude_result(input_tokens=1, output_tokens=1).ljust(50000), {"attempt_id": "a1"})
        report = usage.build_report(self.root, max_file_bytes=64)
        self.assertEqual(report["counts"]["attempts"], 0)
        self.assertEqual(report["skipped"], [{"reason": "input_too_large"}])

    def test_symlink_input_is_unsafe(self):
        real = self.root / "real.json"
        real.write_text(claude_result(input_tokens=5, output_tokens=5))
        directory = self.root / "a1"
        directory.mkdir()
        os.symlink(real, directory / "result.private.json")
        (directory / "task.json").write_text("{}")
        report = usage.build_report(self.root)
        self.assertEqual(report["counts"]["attempts"], 0)
        self.assertEqual([s["reason"] for s in report["skipped"]], ["input_unreadable"])

    def test_duplicate_attempts_collapse_but_resumptions_count(self):
        content = claude_result(input_tokens=10, output_tokens=2)
        write_attempt(self.root, "copy1", content, {"attempt_id": "same"})
        write_attempt(self.root, "copy2", content, {"attempt_id": "same"})
        write_attempt(self.root, "run2", claude_result(input_tokens=11, output_tokens=3), {"attempt_id": "second", "session_id": "sid-shared"})
        report = usage.build_report(self.root)
        self.assertEqual(report["counts"]["attempts"], 2)
        self.assertEqual(report["counts"]["deduplicated"], 1)

    def test_codex_bridge_usage_not_double_counted(self):
        bridge = {"ok": True, "status": "completed",
                  "usage": {"last": {"inputTokens": 5, "outputTokens": 1},
                            "cumulative": {"inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 30}}}
        write_attempt(self.root, "c1", json.dumps(bridge), {"attempt_id": "c1", "provider": "codex", "model": "gpt-sol"})
        report = usage.build_report(self.root)
        attempt = report["attempts"][0]
        self.assertEqual(attempt["input_tokens"], 100)
        self.assertEqual(attempt["output_tokens"], 30)
        self.assertEqual(attempt["cache_read_tokens"], 20)
        self.assertIsNone(attempt["cache_write_tokens"])

    def test_price_table_requires_units_currency_context(self):
        write_attempt(self.root, "a1", claude_result(input_tokens=1000000, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0, output_tokens=0),
                      {"attempt_id": "a1", "provider": "deepseek", "model": "deepseek-flash"})
        incomplete = self.root / "prices.json"
        incomplete.write_text(json.dumps({"entries": [{"provider": "deepseek", "model": "deepseek-flash",
                                                       "prices": {"input": 1, "cache_read": 0, "cache_write": 0, "output": 0}}]}))
        table = usage.load_price_table(incomplete)
        report = usage.build_report(self.root, price_table=table)
        self.assertIsNone(report["attempts"][0]["cost"])
        self.assertFalse(report["costs"]["estimated"])

        complete = self.root / "prices2.json"
        complete.write_text(json.dumps({"entries": [{"provider": "deepseek", "model": "deepseek-flash",
                                                     "currency": "USD", "unit": "per_million_tokens",
                                                     "context_validity": "0-128k",
                                                     "prices": {"input": 1.0, "cache_read": 0.1, "cache_write": 0.0, "output": 2.0}}]}))
        report = usage.build_report(self.root, price_table=usage.load_price_table(complete))
        self.assertEqual(report["attempts"][0]["cost"], 1.0)
        self.assertEqual(report["attempts"][0]["currency"], "USD")
        self.assertTrue(report["costs"]["estimated"])

    def test_outputs_are_private_mode_0600(self):
        write_attempt(self.root, "a1", claude_result(input_tokens=1, output_tokens=1), {"attempt_id": "a1"})
        out = self.root / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            code = usage.main(["--workers-dir", str(self.root), "--output-dir", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for name in ("summary.json", "summary.md"):
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o600)

    def test_cli_subprocess_report_has_no_secret(self):
        stream = ("cli startup banner\n"
                  + json.dumps({"credentials": {"apiKey": SECRET}, "model": "deepseek-flash[1m]"}) + "\n"
                  + "diagnostic noise line\n"
                  + claude_result(input_tokens=42, cache_read_input_tokens=8, output_tokens=3))
        write_attempt(self.root, "a1", stream, {"attempt_id": "a1", "provider": "deepseek",
                                                "model": "deepseek-flash[1m]", "task": "G19-eliza-usage"})
        out = self.root / "cli-out"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--workers-dir", str(self.root), "--output-dir", str(out), "--print-summary"],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["schema"], usage.SCHEMA)
        self.assertEqual(summary["counts"]["attempts"], 1)
        attempt = summary["attempts"][0]
        self.assertEqual(attempt["model"], "deepseek-flash[1m]")
        self.assertEqual(attempt["task_id"], "G19-eliza-usage")
        self.assertEqual(attempt["input_tokens"], 42)
        self.assertEqual(attempt["output_tokens"], 3)
        self.assertNotIn(SECRET, (out / "summary.md").read_text())

    def test_skipped_entries_carry_no_paths(self):
        self.root.joinpath("not-a-dir").write_text("x")
        write_attempt(self.root, "a1", json.dumps({"foo": 1}), {"attempt_id": "a1"})
        report = usage.build_report(self.root)
        for entry in report["skipped"]:
            self.assertEqual(set(entry), {"reason"})


if __name__ == "__main__":
    unittest.main()
