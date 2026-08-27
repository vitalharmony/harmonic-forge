#!/usr/bin/env python3
"""Unit tests for deny_cross_session_contact.py (harmonic-forge#399).

Run: python3 tools/hooks/test_deny_cross_session_contact.py
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deny_cross_session_contact as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestSendMessageDeniedRegardlessOfLane(unittest.TestCase):
    def setUp(self):
        self._prior_lane = os.environ.get("LANE")

    def tearDown(self):
        if self._prior_lane is None:
            os.environ.pop("LANE", None)
        else:
            os.environ["LANE"] = self._prior_lane

    def test_denied_when_lane_unset(self):
        os.environ.pop("LANE", None)
        self.assertTrue(_is_denied(m.decision("SendMessage")))

    def test_denied_for_lane1(self):
        os.environ["LANE"] = "1"
        self.assertTrue(_is_denied(m.decision("SendMessage")))

    def test_denied_for_lane2(self):
        os.environ["LANE"] = "2"
        self.assertTrue(_is_denied(m.decision("SendMessage")))

    def test_denied_for_lane3(self):
        os.environ["LANE"] = "3"
        self.assertTrue(_is_denied(m.decision("SendMessage")))


class TestContentBlind(unittest.TestCase):
    """harmonic-forge#399 AC3 -- the mechanism never inspects message
    content, so decision() takes only the tool name, never a payload."""

    def test_denial_is_identical_regardless_of_hypothetical_content(self):
        # decision() has no content parameter at all -- there is no path
        # by which message text could change the outcome. Prove the two
        # calls a caller might imagine "informational" vs "instructional"
        # collapse to one identical, content-blind call.
        informational_call = m.decision("SendMessage")
        instructional_call = m.decision("SendMessage")
        self.assertEqual(informational_call, instructional_call)
        self.assertTrue(_is_denied(informational_call))
        self.assertTrue(_is_denied(instructional_call))


class TestListAgentsUnaffected(unittest.TestCase):
    def test_list_agents_not_denied(self):
        self.assertFalse(_is_denied(m.decision("ListAgents")))

    def test_unrelated_tool_not_denied(self):
        self.assertFalse(_is_denied(m.decision("Bash")))

    def test_none_tool_name_not_denied(self):
        self.assertFalse(_is_denied(m.decision(None)))


class TestMainPayloadHandling(unittest.TestCase):
    def test_decision_denial_names_the_alternative(self):
        result = m.decision("SendMessage")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("GitHub issue", reason)


if __name__ == "__main__":
    unittest.main()
