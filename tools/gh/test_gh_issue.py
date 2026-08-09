#!/usr/bin/env python3
"""Unit tests for gh_issue.py (harmonic-forge#203) -- all subprocess calls
mocked, no live gh/API calls. Run: python3 tools/gh/test_gh_issue.py"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import gh_issue


def _completed(stdout="", returncode=0):
    class R:
        pass
    r = R()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


FIELDS_JSON = json.dumps({"fields": [
    {"name": "Status", "id": "F_status", "options": [{"name": "Todo", "id": "O_todo"}]},
    {"name": "Estimate", "id": "F_estimate"},
]})
VIEW_JSON = json.dumps({"id": "PVT_project"})


class TestCreateIssueUsesRestNotGraphQL(unittest.TestCase):
    def test_create_issue_calls_gh_api_not_gh_issue_create(self):
        with patch("gh_issue._run", return_value=_completed("https://github.com/o/r/issues/1")) as m:
            url = gh_issue.create_issue("o/r", "title", "body", ["bug"])
        self.assertEqual(url, "https://github.com/o/r/issues/1")
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[:2], ["gh", "api"])
        self.assertNotIn("create", cmd)  # i.e. not ["gh", "issue", "create"]
        self.assertIn("-X", cmd)
        self.assertIn("POST", cmd)

    def test_create_issue_passes_each_label_as_separate_field(self):
        with patch("gh_issue._run", return_value=_completed("url")) as m:
            gh_issue.create_issue("o/r", "t", "b", ["bug", "urgent"])
        cmd = m.call_args[0][0]
        self.assertIn("labels[]=bug", cmd)
        self.assertIn("labels[]=urgent", cmd)


class TestBoardContextFetchedOnce(unittest.TestCase):
    def test_add_to_board_fetches_project_context_exactly_once(self):
        """The bug this issue exists to fix: Status and Estimate used to
        each independently call `project view` + `field-list`, doubling
        GraphQL cost per add_to_board() call with an estimate set."""
        responses = {
            ("gh", "project", "item-add"): _completed(json.dumps({"id": "ITEM1"})),
            ("gh", "project", "view"): _completed(VIEW_JSON),
            ("gh", "project", "field-list"): _completed(FIELDS_JSON),
            ("gh", "project", "item-edit"): _completed(""),
        }

        def fake_run(cmd, check=False):
            for prefix, resp in responses.items():
                if tuple(cmd[:3]) == prefix:
                    return resp
            raise AssertionError(f"unexpected command: {cmd}")

        view_calls = []
        fieldlist_calls = []

        def counting_run(cmd, check=False):
            if tuple(cmd[:3]) == ("gh", "project", "view"):
                view_calls.append(cmd)
            if tuple(cmd[:3]) == ("gh", "project", "field-list"):
                fieldlist_calls.append(cmd)
            return fake_run(cmd, check)

        with patch("gh_issue._run", side_effect=counting_run):
            ok = gh_issue.add_to_board("https://github.com/o/r/issues/1", "owner", "3", estimate=5.0)

        self.assertTrue(ok)
        self.assertEqual(len(view_calls), 1, "project view should be called exactly once, not once per field")
        self.assertEqual(len(fieldlist_calls), 1, "field-list should be called exactly once, not once per field")


class TestStandaloneSetEstimate(unittest.TestCase):
    def test_set_estimate_standalone_still_works_for_backfill_use(self):
        calls = []

        def fake_run(cmd, check=False):
            calls.append(cmd)
            if cmd[:3] == ["gh", "project", "view"]:
                return _completed(VIEW_JSON)
            if cmd[:3] == ["gh", "project", "field-list"]:
                return _completed(FIELDS_JSON)
            if cmd[:3] == ["gh", "project", "item-edit"]:
                return _completed("")
            raise AssertionError(cmd)

        with patch("gh_issue._run", side_effect=fake_run):
            ok = gh_issue.set_estimate("ITEM1", "owner", "3", 8.0)
        self.assertTrue(ok)
        edit_cmd = next(c for c in calls if c[:3] == ["gh", "project", "item-edit"])
        self.assertIn("8.0", edit_cmd)


if __name__ == "__main__":
    unittest.main()
