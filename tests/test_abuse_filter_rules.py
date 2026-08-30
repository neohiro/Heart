"""
test_abuse_filter.py — tests for Brain/src/abuse_filter.py
Run: python -m pytest Heart/tests/test_abuse_filter.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure Brain/src is on path
_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root / "Brain" / "src"))

from Brain.src import abuse_filter as af
from Brain.src.abuse_filter import (
    AbuseSignal,
    evaluate,
    _compare,
    RULES_PATH,
    _load_rules,
    Verdict,
)

# _worst_severity lives in Heart/tools/abuse_bridge.py
from abuse_bridge import _worst_severity


class TestCompare(unittest.TestCase):
    def test_eq(self):
        self.assertTrue(_compare("foo", "eq", "foo"))
        self.assertFalse(_compare("foo", "eq", "bar"))

    def test_ne(self):
        self.assertTrue(_compare("foo", "ne", "bar"))
        self.assertFalse(_compare("foo", "ne", "foo"))

    def test_gt_gte_lt_lte(self):
        self.assertTrue(_compare(5, "gt", 3))
        self.assertFalse(_compare(5, "gt", 5))
        self.assertTrue(_compare(5, "gte", 5))
        self.assertTrue(_compare(3, "lt", 5))
        self.assertFalse(_compare(5, "lt", 5))
        self.assertTrue(_compare(5, "lte", 5))

    def test_contains(self):
        self.assertTrue(_compare("hello world", "contains", "world"))
        self.assertFalse(_compare("hello", "contains", "x"))

    def test_regex(self):
        self.assertTrue(_compare("abc123", "regex", r"^\w+\d+"))
        self.assertFalse(_compare("abc", "regex", r"^\d+"))

    def test_in(self):
        self.assertTrue(_compare(3, "in", [1, 2, 3]))
        self.assertFalse(_compare(4, "in", [1, 2, 3]))

    def test_exists_missing(self):
        self.assertTrue(_compare("present", "exists", True))
        self.assertTrue(_compare(None, "exists", True))


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        # Use default rules (no YAML file needed)
        af._rules_cache = None

    def test_tor_exit_triggers_flag(self):
        signal = AbuseSignal(
            source="heartbeat_osint",
            entity_id="ip-hash:abc123",
            signal_type="ip_observed",
            raw={"is_tor": True, "ip": "198.51.100.5"},
        )
        verdicts = evaluate(signal)
        self.assertTrue(len(verdicts) > 0)
        self.assertEqual(verdicts[0].severity, "FLAG")
        self.assertEqual(verdicts[0].delta_trust, -25)
        self.assertIn("tor_exit", verdicts[0].tags_added)

    def test_vpn_detected_triggers_watch(self):
        signal = AbuseSignal(
            source="heartbeat_osint",
            entity_id="ip-hash:abc456",
            signal_type="ip_observed",
            raw={"is_vpn": True, "is_tor": False, "ip": "203.0.113.1"},
        )
        verdicts = evaluate(signal)
        self.assertTrue(len(verdicts) > 0)
        self.assertEqual(verdicts[0].severity, "WATCH")
        self.assertIn("vpn", verdicts[0].tags_added)

    def test_rapid_auth_fail_triggers_flag(self):
        signal = AbuseSignal(
            source="auth_failure",
            entity_id="phone:+32470123456",
            signal_type="auth_fail",
            raw={"count": 6, "window_minutes": 30},
        )
        verdicts = evaluate(signal)
        self.assertTrue(len(verdicts) > 0)
        self.assertEqual(verdicts[0].severity, "FLAG")
        self.assertEqual(verdicts[0].delta_trust, -20)

    def test_under_threshold_returns_empty(self):
        signal = AbuseSignal(
            source="auth_failure",
            entity_id="phone:+32470123456",
            signal_type="auth_fail",
            raw={"count": 3, "window_minutes": 30},
        )
        verdicts = evaluate(signal)
        self.assertEqual(len(verdicts), 0, "3 auth fails should not trigger under default threshold of 5")

    def test_mass_issue_triggers_escalate(self):
        signal = AbuseSignal(
            source="github_event",
            entity_id="ip-hash:xyz",
            signal_type="mass_issue_open",
            raw={"count": 15, "window_hours": 12},
        )
        verdicts = evaluate(signal)
        self.assertTrue(len(verdicts) > 0)
        self.assertEqual(verdicts[0].severity, "ESCALATE")
        self.assertEqual(verdicts[0].delta_trust, -30)
        self.assertIn("spam_behavior", verdicts[0].tags_added)

    def test_unknown_signal_returns_empty(self):
        signal = AbuseSignal(
            source="heartbeat_osint",
            entity_id="ip-hash:unknown",
            signal_type="unknown_signal_type",
            raw={},
        )
        verdicts = evaluate(signal)
        self.assertEqual(len(verdicts), 0)


class TestResilience(unittest.TestCase):
    """Regression: malformed rules must not crash evaluate()."""

    def setUp(self):
        af._rules_cache = None

    def test_rule_missing_id_skipped_not_crashed(self):
        original = af._load_rules
        def fake():
            return [
                {
                    "id": "good_rule",
                    "signal_type": "ip_observed",
                    "source": "heartbeat_osint",
                    "checks": [{"field": "is_tor", "op": "eq", "value": True}],
                    "verdict": "FLAG", "delta_trust": -25, "tags_add": ["tor_exit"],
                    "reason": "Tor exit",
                },
                {
                    # no "id" — must be skipped, not crash
                    "signal_type": "ip_observed",
                    "source": "heartbeat_osint",
                    "checks": [{"field": "is_vpn", "op": "eq", "value": True}],
                    "verdict": "WATCH", "delta_trust": -10, "tags_add": ["vpn"],
                    "reason": "VPN",
                },
            ]
        af._load_rules = fake
        try:
            sig = AbuseSignal(
                source="heartbeat_osint",
                entity_id="user_test",
                signal_type="ip_observed",
                raw={"is_tor": True, "is_vpn": True},
            )
            verdicts = evaluate(sig)
            ids = {v.rule_id for v in verdicts}
            self.assertIn("good_rule", ids)
            self.assertEqual(len(verdicts), 1)
        finally:
            af._load_rules = original

    def test_check_missing_field_does_not_crash(self):
        original = af._load_rules
        def fake():
            return [
                {
                    "id": "broken_check",
                    "signal_type": "ip_observed",
                    "source": "heartbeat_osint",
                    "checks": [{"op": "eq", "value": True}],  # no "field"
                    "verdict": "WATCH", "delta_trust": -5, "tags_add": [],
                    "reason": "broken check",
                },
            ]
        af._load_rules = fake
        try:
            sig = AbuseSignal(
                source="heartbeat_osint",
                entity_id="user_test",
                signal_type="ip_observed",
                raw={},
            )
            # The point: must not raise KeyError or TypeError.
            # Whether the rule matches or not is secondary — both are
            # acceptable, as long as evaluate() returns gracefully.
            verdicts = evaluate(sig)
            self.assertIsInstance(verdicts, list)
        finally:
            af._load_rules = original


class TestWorstSeverity(unittest.TestCase):
    def test_order(self):
        self.assertEqual(_worst_severity(["ALLOW", "WATCH"]), "WATCH")
        self.assertEqual(_worst_severity(["FLAG", "ESCALATE", "ALLOW"]), "ESCALATE")
        self.assertEqual(_worst_severity(["WATCH", "FLAG", "SUSPEND"]), "SUSPEND")
        self.assertEqual(_worst_severity(["ALLOW"]), "ALLOW")


if __name__ == "__main__":
    unittest.main()
