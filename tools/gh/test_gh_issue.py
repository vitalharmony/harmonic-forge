#!/usr/bin/env python3
"""Unit tests for gh_issue.py (harmonic-forge#203) -- all subprocess calls
mocked, no live gh/API calls. Run: python3 tools/gh/test_gh_issue.py"""

import json
import os
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
    {"name": "Tier", "id": "F_tier", "options": [
        {"name": "fast", "id": "OPT_fast"},
        {"name": "standard", "id": "OPT_standard"},
        {"name": "deep", "id": "OPT_deep"},
    ]},
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
        GraphQL cost per add_to_board() call with a tier set."""
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
            ok = gh_issue.add_to_board("https://github.com/o/r/issues/1", "owner", "3", tier="standard")

        self.assertTrue(ok)
        self.assertEqual(len(view_calls), 1, "project view should be called exactly once, not once per field")
        self.assertEqual(len(fieldlist_calls), 1, "field-list should be called exactly once, not once per field")


class TestStandaloneSetTier(unittest.TestCase):
    def test_set_tier_standalone_still_works_for_backfill_use(self):
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
            ok = gh_issue.set_tier("ITEM1", "owner", "3", "deep")
        self.assertTrue(ok)
        edit_cmd = next(c for c in calls if c[:3] == ["gh", "project", "item-edit"])
        self.assertIn("F_tier", edit_cmd)
        self.assertIn("--single-select-option-id", edit_cmd)
        self.assertIn("OPT_deep", edit_cmd)
        self.assertNotIn("--number", edit_cmd, "must write the Tier select, not the numeric field")


ITEM_LIST_JSON = json.dumps({"items": [
    {"id": "PVTI_other", "content": {"url": "https://github.com/o/r/issues/1"}},
    {"id": "PVTI_target", "content": {"url": "https://github.com/o/r/issues/42"}},
    {"id": "PVTI_no_content"},
]})


class TestMissingTierFieldFailsLoudly(unittest.TestCase):
    """harmonic-forge#257: the legacy Estimate write path is gone. A board
    without a Tier field must fail, not silently write somewhere else."""

    def test_no_tier_field_returns_false(self):
        fields = [{"name": "Status", "id": "F_status",
                   "options": [{"name": "Todo", "id": "O_todo"}]}]
        with patch("gh_issue._run") as run:
            self.assertFalse(gh_issue._set_tier("IT_1", "PVT_1", fields, "fast"))
        run.assert_not_called()

    def test_no_estimate_write_path_remains(self):
        self.assertNotIn("--number", gh_issue._set_tier.__doc__ or "")


class TestAlreadyOnBoardRecovery(unittest.TestCase):
    """hrse#883: a repo auto-add can win the race, GitHub answers the second
    add with 'Content already exists', and bailing there left the item on
    the board with Tier unset — the silent-unset outcome harmonic-forge#263
    exists to prevent."""

    URL = "https://github.com/o/r/issues/42"

    def _responses(self):
        # item-add fails, then item-list, project view, field-list, and the
        # two field mutations succeed.
        return [
            _completed("", returncode=1),   # item-add
            _completed(ITEM_LIST_JSON),     # item-list recovery
            _completed(VIEW_JSON),          # project view
            _completed(FIELDS_JSON),        # field-list
            _completed("{}"),               # set Status
            _completed("{}"),               # set Tier
        ]

    def test_tier_is_still_set_when_the_item_already_exists(self):
        with patch("gh_issue._run", side_effect=self._responses()) as m:
            ok = gh_issue.add_to_board(self.URL, "o", "1", "fast")
        self.assertTrue(ok, "add_to_board must succeed via the recovery path")
        edits = [" ".join(c[0][0]) for c in m.call_args_list
                 if "item-edit" in c[0][0]]
        self.assertTrue(
            any("OPT_fast" in cmd for cmd in edits),
            f"Tier was never written on the recovery path; edits={edits}",
        )

    def test_recovered_item_id_is_the_matching_one(self):
        with patch("gh_issue._run", side_effect=self._responses()) as m:
            gh_issue.add_to_board(self.URL, "o", "1", "fast")
        joined = " ".join(" ".join(c[0][0]) for c in m.call_args_list)
        self.assertIn("PVTI_target", joined)
        self.assertNotIn("PVTI_other", joined)

    def test_still_fails_when_the_item_genuinely_is_not_there(self):
        """A real add failure must not be laundered into success."""
        responses = [
            _completed("", returncode=1),  # item-add
            _completed(json.dumps({"items": []})),  # nothing to recover
        ]
        with patch("gh_issue._run", side_effect=responses):
            self.assertFalse(gh_issue.add_to_board(self.URL, "o", "1", "fast"))

    def test_item_list_uses_an_explicit_limit(self):
        """The default is 30 and these boards are larger; a freshly created
        item is not reliably in the first page."""
        with patch("gh_issue._run", side_effect=self._responses()) as m:
            gh_issue.add_to_board(self.URL, "o", "1", "fast")
        list_cmd = m.call_args_list[1][0][0]
        self.assertIn("item-list", list_cmd)
        self.assertIn("--limit", list_cmd)

    def test_recovery_is_not_attempted_on_the_happy_path(self):
        """The extra listing cost must be paid only on the rare path."""
        responses = [
            _completed(json.dumps({"id": "PVTI_fresh"})),  # item-add ok
            _completed(VIEW_JSON), _completed(FIELDS_JSON),
            _completed("{}"), _completed("{}"),
        ]
        with patch("gh_issue._run", side_effect=responses) as m:
            gh_issue.add_to_board(self.URL, "o", "1", "fast")
        self.assertFalse(
            any("item-list" in c[0][0] for c in m.call_args_list),
            "item-list must not run when item-add succeeded",
        )


class TestMilestoneRequirement(unittest.TestCase):
    """harmonic-forge#283 — the milestone field is release membership, and an
    unset one reads identically to a deliberate "in no release"."""

    def test_fetch_milestones_parses_title_to_number(self):
        with patch("gh_issue._run", return_value=_completed("2.7\t1\nLater\t5\n")):
            self.assertEqual(gh_issue.fetch_milestones("o/r"), {"2.7": 1, "Later": 5})

    def test_fetch_milestones_empty_for_a_repo_that_uses_none(self):
        with patch("gh_issue._run", return_value=_completed("")):
            self.assertEqual(gh_issue.fetch_milestones("o/r"), {})

    def test_fetch_milestones_fails_loud_on_query_error(self):
        """NC1: an auth/network failure must not be indistinguishable from
        "this repo genuinely has none" — that would silently make --milestone
        optional exactly when the check matters (harmonic-forge#263's class)."""
        with patch("gh_issue._run", return_value=_completed("", returncode=1)):
            with self.assertRaises(SystemExit) as caught:
                gh_issue.fetch_milestones("o/r")
        self.assertIn("cannot read milestones", str(caught.exception))

    def test_create_issue_passes_milestone_number_not_title(self):
        """NC3: REST's issue-create takes the milestone NUMBER."""
        with patch("gh_issue._run", return_value=_completed("url")) as m:
            gh_issue.create_issue("o/r", "t", "b", ["bug"], 7)
        cmd = m.call_args[0][0]
        self.assertIn("milestone=7", cmd)
        self.assertNotIn("milestone=2.7", cmd)

    def test_create_issue_omits_milestone_when_none(self):
        with patch("gh_issue._run", return_value=_completed("url")) as m:
            gh_issue.create_issue("o/r", "t", "b", ["bug"], None)
        self.assertFalse([a for a in m.call_args[0][0] if a.startswith("milestone=")])

    def test_existing_positional_calls_still_work(self):
        """The new parameter is last and defaulted, so callers that predate
        it are unaffected — verified rather than assumed."""
        with patch("gh_issue._run", return_value=_completed("url")):
            self.assertEqual(gh_issue.create_issue("o/r", "t", "b", ["bug"]), "url")


class TestMilestoneCliGate(unittest.TestCase):
    def _main(self, argv, milestones):
        with patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value=milestones), \
             patch("gh_issue.create_issue", return_value=None) as created:
            try:
                gh_issue.main()
            except SystemExit as exc:
                return exc, created
        return None, created

    def test_milestone_required_on_a_repo_that_has_them(self):
        exc, _ = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t"],
            {"2.7": 1, "Later": 5},
        )
        self.assertIsNotNone(exc)
        self.assertNotEqual(exc.code, 0)

    def test_milestone_not_required_on_a_repo_with_none(self):
        # AC5 / LBA3: harmonic-forge has zero milestones and is decided never
        # to carry release ones — it must never be gated.
        exc, created = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/harmonic-forge", "--title", "t"], {},
        )
        created.assert_called_once()
        self.assertIsNone(created.call_args[0][4])

    def test_unknown_milestone_title_is_rejected(self):
        exc, _ = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
             "--milestone", "9.9"],
            {"2.7": 1},
        )
        self.assertIsNotNone(exc)
        self.assertNotEqual(exc.code, 0)

    def test_valid_milestone_resolves_to_its_number(self):
        _, created = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
             "--milestone", "2.7"],
            {"2.7": 1, "Later": 5},
        )
        created.assert_called_once()
        self.assertEqual(created.call_args[0][4], 1)

    def test_later_sentinel_is_accepted(self):
        _, created = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
             "--milestone", "Later"],
            {"2.7": 1, "Later": 5},
        )
        self.assertEqual(created.call_args[0][4], 5)


class TestTierWriteFailureIsLoud(unittest.TestCase):
    """harmonic-forge#263. A tier that was asked for and not written must not
    exit 0 -- the model-tier gate reads an unset Tier as 'does not escalate',
    so the silent outcome is the unsafe one."""

    def _ctx(self):
        return {"project_id": "PVT_project", "fields": json.loads(FIELDS_JSON)["fields"]}

    def test_failed_tier_write_makes_add_to_board_fail(self):
        """AC1, at the level main() branches on."""
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_status_todo", return_value=True), \
             patch("gh_issue._set_tier", return_value=False):
            self.assertFalse(gh_issue.add_to_board("https://x/1", "o", "1", "fast"))

    def test_successful_tier_write_still_succeeds(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_status_todo", return_value=True), \
             patch("gh_issue._set_tier", return_value=True):
            self.assertTrue(gh_issue.add_to_board("https://x/1", "o", "1", "fast"))

    def test_no_tier_requested_is_unaffected(self):
        """AC4: omitting --tier must still exit 0."""
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_status_todo", return_value=True), \
             patch("gh_issue._set_tier", return_value=False) as tier:
            self.assertTrue(gh_issue.add_to_board("https://x/1", "o", "1", None))
        tier.assert_not_called()

    def test_failure_message_names_the_repair_command(self):
        """AC3. Every id is resolved by the time the write fails, so the
        failing invocation is itself the fix."""
        fields = json.loads(FIELDS_JSON)["fields"]
        with patch("gh_issue._run", return_value=_completed("", returncode=1)), \
             patch("sys.stderr") as err:
            ok = gh_issue._set_tier("IT_1", "PVT_project", fields, "fast")
        self.assertFalse(ok)
        printed = "".join(str(c) for c in err.write.call_args_list)
        self.assertIn("gh project item-edit", printed)
        self.assertIn("OPT_fast", printed)
        self.assertIn("IT_1", printed)


class TestTierRequestedWithNoBoard(unittest.TestCase):
    """The same defect in the branch the issue did not name: Tier is a board
    field, so `--tier` with no board configured cannot be written at all --
    and that path returned 0."""

    def _main(self, argv):
        # --project-owner/--project-number default to $GH_PROJECT_OWNER /
        # $GH_PROJECT_NUMBER, which are set in a real operator shell. Without
        # clearing them "no board configured" is untestable and the test makes
        # live gh calls instead.
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1"):
            return gh_issue.main()

    def test_tier_with_no_board_exits_non_zero(self):
        rc = self._main(["gh_issue.py", "--repo", "o/r", "--title", "t", "--tier", "fast"])
        self.assertEqual(rc, 1)

    def test_no_tier_with_no_board_still_exits_zero(self):
        rc = self._main(["gh_issue.py", "--repo", "o/r", "--title", "t"])
        self.assertEqual(rc, 0)

    def test_issue_is_never_rolled_back(self):
        """AC2: a filed issue with a missing field beats a lost issue."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv",
                          ["gh_issue.py", "--repo", "o/r", "--title", "t", "--tier", "fast"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created:
            gh_issue.main()
        created.assert_called_once()


if __name__ == "__main__":
    unittest.main()
