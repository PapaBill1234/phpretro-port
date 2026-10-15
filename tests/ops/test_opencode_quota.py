import importlib.util
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "opencode-quota.py"
SPEC = importlib.util.spec_from_file_location("opencode_quota", SCRIPT)
quota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quota)
_DEFAULT_WINDOWS = object()


class QuotaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)

    def evidence(self, windows=_DEFAULT_WINDOWS, **changes):
        result = {
            "schema_version": 1,
            "profile": "primary",
            "connection_binding": "opaque-primary-1",
            "source": quota.SOURCE_MANUAL,
            "operator_attested": True,
            "observed_at": (self.now - timedelta(seconds=30)).isoformat(),
            "verified_at": (self.now - timedelta(seconds=5)).isoformat(),
            "windows": [
                {"id": "rolling-300", "percentage": 50, "basis": "remaining",
                 "duration_minutes": 300, "reset_at": None},
            ] if windows is _DEFAULT_WINDOWS else windows,
        }
        result.update(changes)
        return result

    def validate(self, evidence=None, **kwargs):
        return quota.validate_evidence(evidence or self.evidence(),
                                       expected_profile="primary",
                                       expected_binding="opaque-primary-1", now=self.now,
                                       **kwargs)

    def test_valid_all_windows_preserve_reported_durations_and_unknown_reset(self):
        result = self.validate(self.evidence([
            {"id": "window-300", "percentage": 35, "basis": "remaining",
             "duration_minutes": 300},
            {"id": "window-10080", "percentage": 70, "basis": "remaining",
             "duration_minutes": 10080, "reset_at": None},
        ]))
        self.assertEqual("allow", result["decision"])
        self.assertTrue(result["dispatch_allowed"])
        self.assertEqual([300, 10080], [w["duration_minutes"] for w in result["windows"]])
        self.assertIsNone(result["windows"][1]["reset_at"])

    def test_used_and_remaining_threshold_including_exact_boundary(self):
        for percent, basis in ((10, "remaining"), (90, "used")):
            with self.subTest(percent=percent, basis=basis):
                result = self.validate(self.evidence([
                    {"id": "quota", "percentage": percent, "basis": basis}]))
                self.assertEqual("checkpoint", result["decision"])
                self.assertFalse(result["dispatch_allowed"])

    def test_every_window_counts_and_ambiguous_basis_uses_lower_bound(self):
        result = self.validate(self.evidence([
            {"id": "five-hour", "percentage": 50, "basis": "unspecified",
             "duration_minutes": 300},
            {"id": "weekly", "percentage": 70, "basis": "unspecified",
             "duration_minutes": 10080},
        ]))
        self.assertEqual("allow", result["decision"])
        self.assertEqual([50.0, 30.0], [w["conservative_remaining_percent"]
                                        for w in result["windows"]])
        self.assertEqual(["unspecified", "unspecified"],
                         [w["reported_basis"] for w in result["windows"]])

    def test_missing_windows_blocks_and_no_window_means_no_quota(self):
        for windows in ([], None):
            result = self.validate(self.evidence(windows=windows))
            self.assertEqual("blocked", result["decision"])
            self.assertEqual("missing_windows", result["reason"])

    def test_profile_binding_source_attestation_and_extra_fields_block(self):
        variants = [
            (self.evidence(profile="secondary"), "profile_mismatch"),
            (self.evidence(connection_binding="other"), "binding_mismatch"),
            (self.evidence(source="desktop_usage"), "unsupported_source"),
            (self.evidence(operator_attested=False), "operator_attestation_required"),
            (self.evidence(password="secret"), "invalid_evidence_fields"),
        ]
        for item, reason in variants:
            with self.subTest(reason=reason):
                self.assertEqual(reason, self.validate(item)["reason"])

    def test_stale_future_naive_and_reversed_timestamps_block(self):
        cases = [
            (self.evidence(observed_at=(self.now - timedelta(seconds=301)).isoformat()),
             "stale_evidence"),
            (self.evidence(verified_at=(self.now + timedelta(seconds=1)).isoformat()),
             "future_timestamp"),
            (self.evidence(observed_at="2026-10-15T19:59:00"), "naive_timestamp"),
            (self.evidence(observed_at=(self.now - timedelta(seconds=1)).isoformat(),
                           verified_at=(self.now - timedelta(seconds=2)).isoformat()),
             "observation_after_verification"),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(reason, self.validate(item)["reason"])

    def test_invalid_numeric_values_and_boolean_are_rejected(self):
        for value in (True, float("nan"), float("inf"), -1, 101, "50"):
            with self.subTest(value=value):
                result = self.validate(self.evidence([
                    {"id": "q", "percentage": value, "basis": "remaining"}]))
                self.assertEqual("blocked", result["decision"])
                self.assertEqual("invalid_percentage", result["reason"])

    def test_reset_passing_does_not_allow_stale_evidence_or_auto_resume(self):
        item = self.evidence([
            {"id": "quota", "percentage": 0, "basis": "remaining",
             "reset_at": (self.now - timedelta(days=1)).isoformat()},
        ], observed_at=(self.now - timedelta(seconds=301)).isoformat())
        self.assertEqual("blocked", self.validate(item)["decision"])

    def test_checkpoint_is_private_atomic_and_whitelisted(self):
        item = self.evidence([{"id": "five-hour", "percentage": 10,
                               "basis": "remaining", "duration_minutes": 300}])
        decision = self.validate(item)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            root = Path(temp) / "private"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            path = quota.write_checkpoint(root, item, decision,
                                         expected_profile="primary",
                                         expected_binding="opaque-primary-1", now=self.now)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            saved = json.loads(path.read_text())
            self.assertEqual("checkpoint", saved["decision"])
            self.assertIn("Manually re-open", " ".join(saved["next_steps"]))
            self.assertNotIn("operator_attested", saved)
            self.assertNotIn("secret", path.read_text())

    def test_checkpoint_refuses_symlink_roots_and_unsafe_targets(self):
        item = self.evidence()
        decision = self.validate(item)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            parent = Path(temp)
            actual = parent / "actual"
            actual.mkdir(mode=0o700)
            os.chmod(actual, 0o700)
            link = parent / "link"
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink_path_component"):
                quota.write_checkpoint(link, item, decision, expected_profile="primary",
                                       expected_binding="opaque-primary-1", now=self.now)
            unsafe = actual / "opencode-quota-handoff.json"
            unsafe.write_text("not-private")
            os.chmod(unsafe, 0o644)
            with self.assertRaisesRegex(ValueError, "unsafe_checkpoint_target"):
                quota.write_checkpoint(actual, item, decision, expected_profile="primary",
                                       expected_binding="opaque-primary-1", now=self.now)

    def test_checkpoint_requires_fresh_matching_validated_decision(self):
        item = self.evidence()
        decision = self.validate(item)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            os.chmod(temp, 0o700)
            with self.assertRaisesRegex(ValueError, "matching_validated_decision"):
                quota.write_checkpoint(temp, item, {**decision, "profile": "secondary"},
                                       expected_profile="primary",
                                       expected_binding="opaque-primary-1", now=self.now)


if __name__ == "__main__":
    unittest.main()
