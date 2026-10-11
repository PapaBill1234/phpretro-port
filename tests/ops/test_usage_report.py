import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("usage_report", Path(__file__).resolve().parents[2] / "scripts/usage-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class UsageTests(unittest.TestCase):
    def test_missing_cost_is_not_reported_as_zero(self):
        group = report.summarize([{"role": "worker", "model": "luna", "input_tokens": 100, "estimated_usd": 0.01},
                                  {"role": "worker", "model": "luna", "input_tokens": 40}])[0]
        self.assertEqual(group["totals"]["input_tokens"], 140)
        self.assertIsNone(group["totals"]["estimated_usd"])
        self.assertEqual(group["unknown_rows"]["estimated_usd"], 1)

    def test_roles_and_cache_counters_stay_separate(self):
        groups = report.summarize([{"role": "worker", "model": "luna", "input_tokens": 10, "cached_input_tokens": 90},
                                   {"role": "reviewer", "model": "sol", "input_tokens": 20, "cached_input_tokens": 80}])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["totals"]["input_tokens"], 10)
        self.assertEqual(groups[0]["totals"]["cached_input_tokens"], 90)

    def test_invalid_accounting_is_rejected(self):
        with self.assertRaises(ValueError):
            report.summarize([{"output_tokens": -1}])


if __name__ == "__main__":
    unittest.main()
