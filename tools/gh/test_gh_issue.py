#!/usr/bin/env python3
"""Unit tests for gh_issue.py (harmonic-forge#203) -- all subprocess calls
mocked, no live gh/API calls. Run: python3 tools/gh/test_gh_issue.py"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import gh_issue

#: harmonic-forge#1032: every main()-level test predating the prior-owner
#: search stubs it to "no candidates"; the tests for it use the original.
_ORIGINAL_FIND_PRIOR_OWNERS = gh_issue.find_prior_owners
_FIND_PATCHER = patch("gh_issue.find_prior_owners", return_value=[])


def setUpModule():
    _FIND_PATCHER.start()


def tearDownModule():
    _FIND_PATCHER.stop()


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
    {"name": "Theme", "id": "F_theme", "options": [
        {"name": "Career", "id": "OPT_career"},
        {"name": "Cy", "id": "OPT_cy"},
        {"name": "Tooling", "id": "OPT_tooling"},
    ]},
    {"name": "Venture", "id": "F_venture", "options": [
        {"name": "CymaGraph", "id": "OPT_cymagraph"},
        {"name": "Platform", "id": "OPT_platform"},
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


# harmonic-forge#468: the recovery path no longer scans the board. It asks the
# issue for its own project items — ~1 GraphQL point instead of up to 5000 rows
# — so the fixture is the GraphQL response shape, and the "wrong board" row is
# what the board-number filter has to reject.
ITEM_LOOKUP_JSON = json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [
    {"id": "PVTI_other", "project": {"number": 3, "owner": {"login": "o"}}},
    {"id": "PVTI_other_owner", "project": {"number": 1, "owner": {"login": "someone-else"}}},
    {"id": "PVTI_target", "project": {"number": 1, "owner": {"login": "o"}}},
]}}}}})


class TestFindExistingItemMatchesOwner(unittest.TestCase):
    """harmonic-forge#992: board #1 under another owner must never receive this board's writes."""

    def _nodes(self, *nodes):
        return json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": list(nodes)}}}}})

    def test_the_target_owners_item_wins_in_either_order(self):
        ours = {"id": "PVTI_ours", "project": {"number": 1, "owner": {"login": "Owner"}}}
        theirs = {"id": "PVTI_theirs", "project": {"number": 1, "owner": {"login": "other"}}}
        for nodes in ((theirs, ours), (ours, theirs)):
            with self.subTest(first=nodes[0]["id"]), \
                    patch.object(gh_issue, "_run", return_value=_completed(self._nodes(*nodes))):
                self.assertEqual(gh_issue._find_existing_item(
                    "https://github.com/o/r/issues/42", "owner", "1"), "PVTI_ours")

    def test_only_another_owners_item_is_none(self):
        theirs = {"id": "PVTI_theirs", "project": {"number": 1, "owner": {"login": "other"}}}
        with patch.object(gh_issue, "_run", return_value=_completed(self._nodes(theirs))):
            self.assertIsNone(gh_issue._find_existing_item(
                "https://github.com/o/r/issues/42", "owner", "1"))


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
            _completed(ITEM_LOOKUP_JSON),    # targeted item-id lookup
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
            _completed(json.dumps({"data": {"repository": {"issue": None}}})),  # nothing to recover
        ]
        with patch("gh_issue._run", side_effect=responses):
            self.assertFalse(gh_issue.add_to_board(self.URL, "o", "1", "fast"))

    def test_recovery_never_scans_the_board(self):
        """harmonic-forge#468, and the inverse of the assertion this replaces.

        This used to require an explicit `--limit` on an `item-list` recovery,
        because the default page of 30 could miss a freshly created item on a
        900-row board. The fix removed the scan entirely rather than sizing it:
        finding one issue's item id is the "field X on issue N" shape, about one
        GraphQL point against hundreds. A regression here is a full-board scan
        on a routine filing path.
        """
        with patch("gh_issue._run", side_effect=self._responses()) as m:
            gh_issue.add_to_board(self.URL, "o", "1", "fast")
        for call in m.call_args_list:
            self.assertNotIn(
                "item-list", call[0][0],
                f"the recovery path scanned the board: {' '.join(call[0][0])}",
            )

    def test_recovery_asks_the_issue_directly(self):
        """The positive half — it must actually do the targeted lookup, not
        merely avoid the scan."""
        with patch("gh_issue._run", side_effect=self._responses()) as m:
            gh_issue.add_to_board(self.URL, "o", "1", "fast")
        lookup = m.call_args_list[1][0][0]
        self.assertEqual(lookup[:3], ["gh", "api", "graphql"])
        joined = " ".join(lookup)
        self.assertIn("projectItems", joined)
        self.assertIn("number=42", joined)

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
             patch("gh_issue._set_single_select", return_value=True), \
             patch("gh_issue._set_tier", return_value=False):
            self.assertFalse(gh_issue.add_to_board("https://x/1", "o", "1", "fast"))

    def test_successful_tier_write_still_succeeds(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", return_value=True), \
             patch("gh_issue._set_tier", return_value=True):
            self.assertTrue(gh_issue.add_to_board("https://x/1", "o", "1", "fast"))

    def test_no_tier_requested_is_unaffected(self):
        """AC4: omitting --tier must still exit 0."""
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", return_value=True), \
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


class TestUnmappedRepoFailsLoudly(unittest.TestCase):
    """harmonic-forge#107 supersedes harmonic-forge#263's no-board branch.

    #263 made `--tier` with no board configured exit non-zero, because a
    routing signal was being requested and silently dropped. With an
    exhaustive repo->board map that state is no longer reachable by
    configuration -- an unmapped repo fails before board-add is attempted.
    The invariant #263 established still holds and is asserted here: the
    issue is created first and is never rolled back.
    """

    def _main(self, argv):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.apply_project_identity"), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created:
            try:
                return gh_issue.main(), created
            except SystemExit as exc:
                return exc, created

    def test_unmapped_repo_exits_rather_than_guessing(self):
        result, _ = self._main(["gh_issue.py", "--repo", "o/r", "--title", "t"])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("No project board mapped", str(result))

    def test_issue_is_still_created_before_the_failure(self):
        """harmonic-forge#263 AC2 -- a filed issue with a missing field beats
        a lost issue, and that holds for a missing board too."""
        _, created = self._main(["gh_issue.py", "--repo", "o/r", "--title", "t",
                                 "--tier", "fast"])
        created.assert_called_once()

    def test_mapped_repo_does_not_fail(self):
        with patch("gh_issue.add_to_board", return_value=True):
            result, _ = self._main(["gh_issue.py", "--repo", "vitalharmony/hrse",
                                    "--title", "t"])
        self.assertEqual(result, 0)


class TestIdentityAppliedBeforeAnyGithubCall(unittest.TestCase):
    """harmonic-forge#804: `main` acts as the repo's registered account before the
    first `gh` call, so the issue is filed as that account."""

    def test_identity_is_applied_before_the_issue_is_created(self):
        order = unittest.mock.Mock()
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.add_to_board", return_value=True), \
             patch("gh_issue.apply_project_identity", order.identity), \
             patch("gh_issue.create_issue", order.create) as _:
            order.create.return_value = "https://x/1"
            gh_issue.main()
        names = [c[0] for c in order.mock_calls]
        self.assertLess(names.index("identity"), names.index("create"))
        order.identity.assert_called_once_with("vitalharmony/hrse")


class TestUnregisteredRepoRefusedBeforeFiling(unittest.TestCase):
    """harmonic-forge#804: a repo with no `projects.toml` entry has no registered
    account, and the tool never falls back to the global login. Unlike the
    board-mapping failure above (which happens after the issue exists), this one is
    knowable from the manifest alone, so nothing is filed."""

    def test_nothing_is_created(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", ["gh_issue.py", "--repo", "o/r", "--title", "t"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created:
            with self.assertRaises(Exception) as caught:
                gh_issue.main()
        created.assert_not_called()
        self.assertIn("refusing to fall back", str(caught.exception))


class TestHalfOverrideValidatedBeforeCreate(unittest.TestCase):
    """harmonic-forge#329: a half-override (--project-owner without
    --project-number, or vice versa) is knowable from argv alone -- it must
    never let create_issue() run first, unlike an unmapped repo (above),
    which #263 AC2 requires still create the issue."""

    def _main(self, argv):
        """argparse's parser.error() writes to stderr and exits with a bare
        code, unlike resolve_board_for_repo's SystemExit(message) above --
        capture stderr rather than asserting on str(SystemExit)."""
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created, \
             contextlib.redirect_stderr(stderr):
            try:
                return gh_issue.main(), created, stderr.getvalue()
            except SystemExit as exc:
                return exc, created, stderr.getvalue()

    def test_owner_without_number_refuses_before_create_issue(self):
        result, created, stderr = self._main([
            "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
            "--project-owner", "someone",
        ])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("must be given together", stderr)
        created.assert_not_called()

    def test_number_without_owner_refuses_before_create_issue(self):
        result, created, stderr = self._main([
            "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
            "--project-number", "9",
        ])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("must be given together", stderr)
        created.assert_not_called()

    def test_both_together_is_not_a_half_override(self):
        with patch("gh_issue.add_to_board", return_value=True):
            result, created, _ = self._main([
                "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
                "--project-owner", "someone", "--project-number", "9",
            ])
        self.assertEqual(result, 0)
        created.assert_called_once()


class TestRepoBoardMap(unittest.TestCase):
    """harmonic-forge#107: the board follows the repo, not the ambient shell."""

    def test_every_known_repo_maps(self):
        self.assertEqual(gh_issue.resolve_board_for_repo("vitalharmony/hrse"),
                         ("vitalharmony", "1"))
        self.assertEqual(gh_issue.resolve_board_for_repo("vitalharmony/harmonic-forge"),
                         ("vitalharmony", "3"))
        self.assertEqual(gh_issue.resolve_board_for_repo("vitalharmony/cymagraph-infra"),
                         ("vitalharmony", "1"))
        # Board #4, not #1, since 2026-09-10: openclaw is a prototyping ground
        # that feeds the products and ships in none of them, so it is its own
        # venture rather than CymaGraph work. A board is per venture, not per
        # repo -- which is also why cymagraph-infra above legitimately shares
        # hrse's #1.
        self.assertEqual(gh_issue.resolve_board_for_repo("vitalharmony/openclaw-projects"),
                         ("vitalharmony", "4"))

    def test_unmapped_repo_fails_loudly(self):
        """A default would reintroduce the silent misroute this issue fixes."""
        with self.assertRaises(SystemExit) as caught:
            gh_issue.resolve_board_for_repo("vitalharmony/brand-new")
        msg = str(caught.exception)
        self.assertIn("No project board mapped", msg)
        self.assertIn("vitalharmony/brand-new", msg)
        self.assertIn("REPO_BOARDS", msg)

    def test_forge_and_hrse_do_not_share_a_board(self):
        """The actual 2026-07-25 incident: forge issues landed on board #1."""
        self.assertNotEqual(
            gh_issue.resolve_board_for_repo("vitalharmony/hrse")[1],
            gh_issue.resolve_board_for_repo("vitalharmony/harmonic-forge")[1],
        )

    def test_a_mixed_case_repo_still_resolves(self):
        """harmonic-forge#800: REPO_BOARDS keys are lowercased by
        manifest.normalize_repo, but every repo onboarded before LeasePAL
        happened to be all-lowercase already, so an exact-match lookup here
        never surfaced the mismatch. `--repo` is a real GitHub owner/name,
        and GitHub repo names are case-insensitive, so the mixed-case form a
        human actually types must resolve too."""
        self.assertEqual(
            gh_issue.resolve_board_for_repo("LeasePAL-ML/LeasePAL-App-Prototype"),
            ("LeasePAL-ML", "1"))


class TestBoardResolutionIgnoresAmbientEnv(unittest.TestCase):
    """The bug: $GH_PROJECT_* from the invoking shell decided the board."""

    def _main(self, argv, env):
        with patch.dict(os.environ, env, clear=False), \
             patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1"), \
             patch("gh_issue.add_to_board", return_value=True) as add:
            gh_issue.main()
        return add

    def test_hrse_env_does_not_misroute_a_forge_issue(self):
        add = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/harmonic-forge", "--title", "t"],
            {"GH_PROJECT_OWNER": "vitalharmony", "GH_PROJECT_NUMBER": "1"},
        )
        self.assertEqual(add.call_args[0][2], "3", "must use forge's board, not the shell's")

    def test_explicit_override_still_wins(self):
        add = self._main(
            ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
             "--project-owner", "someone", "--project-number", "9"],
            {},
        )
        self.assertEqual(add.call_args[0][1:3], ("someone", "9"))

    def test_half_an_override_is_an_error(self):
        with self.assertRaises(SystemExit):
            self._main(
                ["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
                 "--project-number", "9"],
                {},
            )


class TestSetSingleSelectSharedHelper(unittest.TestCase):
    """harmonic-forge#308: _set_tier must delegate to the shared helper, not
    keep its own copy of the match/edit/error shape."""

    def test_set_tier_delegates_to_set_single_select(self):
        fields = json.loads(FIELDS_JSON)["fields"]
        with patch("gh_issue._set_single_select", return_value=True) as shared:
            ok = gh_issue._set_tier("IT_1", "PVT_1", fields, "fast")
        self.assertTrue(ok)
        shared.assert_called_once_with("IT_1", "PVT_1", fields, "Tier", "fast")

    def test_case_insensitive_match_still_works_through_the_shared_path(self):
        fields = json.loads(FIELDS_JSON)["fields"]
        with patch("gh_issue._run", return_value=_completed("")) as m:
            ok = gh_issue._set_tier("IT_1", "PVT_1", fields, "FAST")
        self.assertTrue(ok)
        self.assertIn("OPT_fast", m.call_args[0][0])

    def test_unmatched_value_lists_available_options(self):
        """New AC (harmonic-forge#308): unlike the old _set_tier, an
        unmatched value must print the real options read from the board,
        not just fail silently."""
        fields = json.loads(FIELDS_JSON)["fields"]
        with patch("gh_issue._run") as run, patch("sys.stderr") as err:
            ok = gh_issue._set_single_select("IT_1", "PVT_1", fields, "Theme", "Nonexistent")
        self.assertFalse(ok)
        run.assert_not_called()
        printed = "".join(str(c) for c in err.write.call_args_list)
        self.assertIn("Career", printed)
        self.assertIn("Cy", printed)
        self.assertIn("Tooling", printed)

    def test_missing_field_returns_false_without_calling_run(self):
        fields = [{"name": "Status", "id": "F_status", "options": []}]
        with patch("gh_issue._run") as run:
            ok = gh_issue._set_single_select("IT_1", "PVT_1", fields, "Theme", "Career")
        self.assertFalse(ok)
        run.assert_not_called()

    def test_theme_and_venture_both_write_correctly(self):
        fields = json.loads(FIELDS_JSON)["fields"]
        with patch("gh_issue._run", return_value=_completed("")) as m:
            self.assertTrue(gh_issue._set_single_select("IT_1", "PVT_1", fields, "Theme", "Cy"))
        self.assertIn("OPT_cy", m.call_args[0][0])
        with patch("gh_issue._run", return_value=_completed("")) as m:
            self.assertTrue(gh_issue._set_single_select("IT_1", "PVT_1", fields, "Venture", "Platform"))
        self.assertIn("OPT_platform", m.call_args[0][0])


class TestThemeVentureWriteFailureIsLoud(unittest.TestCase):
    """Same hard-fail shape as TestTierWriteFailureIsLoud, for Theme/Venture
    (harmonic-forge#308) -- an unset required reporting-slice field must not
    exit 0, matching hrse#966's requirement."""

    def _ctx(self):
        return {"project_id": "PVT_project", "fields": json.loads(FIELDS_JSON)["fields"]}

    @staticmethod
    def _status_ok(fail_fields=()):
        """harmonic-forge#329: Status now shares `_set_single_select` with
        Tier/Theme/Venture, so a blanket True/False return would also flip
        the Status call these tests don't intend to exercise. Status always
        succeeds here; only the named field(s) fail."""
        def effect(item_id, project_id, fields, field_name, value):
            return field_name not in fail_fields
        return effect

    def test_failed_theme_write_makes_add_to_board_fail(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", side_effect=self._status_ok(fail_fields=("Theme",))):
            self.assertFalse(gh_issue.add_to_board("https://x/1", "o", "1", None, theme="Career"))

    def test_failed_venture_write_makes_add_to_board_fail(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", side_effect=self._status_ok(fail_fields=("Venture",))):
            self.assertFalse(gh_issue.add_to_board("https://x/1", "o", "1", None, venture="CymaGraph"))

    def test_successful_theme_and_venture_write_succeeds(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", return_value=True):
            self.assertTrue(gh_issue.add_to_board(
                "https://x/1", "o", "1", None, theme="Career", venture="CymaGraph"))

    def test_no_theme_or_venture_requested_is_unaffected(self):
        with patch("gh_issue._run", return_value=_completed('{"id":"IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=self._ctx()), \
             patch("gh_issue._set_single_select", side_effect=self._status_ok()) as shared:
            self.assertTrue(gh_issue.add_to_board("https://x/1", "o", "1", None))
        called_fields = [c.args[3] for c in shared.call_args_list]
        self.assertEqual(called_fields, ["Status"])


class TestNoBoardConfiguredHardFailsForThemeVenture(unittest.TestCase):
    """Same shape as the --tier case in main()'s 'no project board configured'
    branch, generalized: harmonic-forge#308 must not let --theme/--venture
    silently no-op just because --tier is the only one currently checked.

    This branch is reachable only when `resolve_board_for_repo` itself
    returns an empty board (never true for a mapped repo, by design) --
    mocked directly rather than routed through an unmapped repo string,
    which hits `resolve_board_for_repo`'s own SystemExit first and never
    reaches this branch at all (found while writing this test: the branch
    was already untested before this change, for the pre-existing --tier
    case too)."""

    def _main(self, argv):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GH_PROJECT_OWNER", "GH_PROJECT_NUMBER")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1"), \
             patch("gh_issue.resolve_board_for_repo", return_value=("", "")):
            try:
                return gh_issue.main()
            except SystemExit as exc:
                return exc

    def test_theme_requested_with_no_board_exits_nonzero(self):
        result = self._main(["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t", "--theme", "Career"])
        self.assertNotEqual(result, 0)

    def test_venture_requested_with_no_board_exits_nonzero(self):
        result = self._main(["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t", "--venture", "CymaGraph"])
        self.assertNotEqual(result, 0)

    def test_neither_requested_with_no_board_exits_zero(self):
        result = self._main(["gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t"])
        self.assertEqual(result, 0)


class TestThemeVentureCliWiring(unittest.TestCase):
    """--theme/--venture must actually reach add_to_board, not just parse."""

    def test_both_flags_flow_through_to_add_to_board(self):
        with patch.object(sys, "argv", ["gh_issue.py", "--repo", "vitalharmony/hrse",
                                        "--title", "t", "--theme", "Career", "--venture", "CymaGraph"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1"), \
             patch("gh_issue.add_to_board", return_value=True) as add:
            gh_issue.main()
        self.assertEqual(add.call_args[0][4], "Career")
        self.assertEqual(add.call_args[0][5], "CymaGraph")


class TestFindRetiredCitations(unittest.TestCase):
    """harmonic-forge#379. Unit-level: the matcher itself, no gh_issue.main()."""

    def test_backtick_quoted_retired_artifact_is_found(self):
        hits = gh_issue.find_retired_citations(
            "Still references `board_sync.py` for reconciliation."
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "board_sync.py")
        self.assertIn("hrse#839", hits[0][1])

    def test_bare_word_does_not_match(self):
        # Same name, no backticks -- must not fire.
        hits = gh_issue.find_retired_citations(
            "Still references board_sync.py somewhere in prose."
        )
        self.assertEqual(hits, [])

    def test_estimate_free_text_line_does_not_warn(self):
        # planning.md requires every issue to state a free-text estimate.
        # That line is never backtick-quoted, so it must be silent by
        # construction, not by a special case in the matcher.
        hits = gh_issue.find_retired_citations(
            "## Estimate\n\n2 points -- small check, most of the work is "
            "deciding where the retired list lives."
        )
        self.assertEqual(hits, [])

    def test_backtick_quoted_estimate_field_name_does_warn(self):
        # Contrast case: the retired *board field* named Estimate, quoted
        # as a field reference, is exactly what this check exists to catch.
        hits = gh_issue.find_retired_citations(
            "The board still writes to the retired `Estimate` field."
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "Estimate")

    def test_non_retired_backtick_span_does_not_match(self):
        hits = gh_issue.find_retired_citations(
            "See `scripts/gh_issue.py` for the live implementation."
        )
        self.assertEqual(hits, [])

    def test_duplicate_citations_are_deduplicated(self):
        hits = gh_issue.find_retired_citations(
            "`hrse_manager.py` is gone. Also see `hrse_manager.py` again."
        )
        self.assertEqual(len(hits), 1)

    def test_multiple_distinct_citations_are_all_found(self):
        hits = gh_issue.find_retired_citations(
            "Both `board_sync.py` and `hrse_manager.py` are cited here."
        )
        names = {name for name, _ in hits}
        self.assertEqual(names, {"board_sync.py", "hrse_manager.py"})


class TestRetiredCitationWarningNeverBlocksFiling(unittest.TestCase):
    """harmonic-forge#379 AC: warning, never a block. A legitimate cleanup
    issue that names its own target (harmonic-forge#319's shape: "Devin
    scrub, remaining corpus") must still file."""

    def _main_capturing_stderr(self, body):
        stderr = io.StringIO()
        with patch.object(sys, "argv", [
                "gh_issue.py", "--repo", "vitalharmony/harmonic-forge",
                "--title", "t", "--body", body]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created, \
             patch("gh_issue.resolve_board_for_repo", return_value=("", "")), \
             contextlib.redirect_stderr(stderr):
            result = gh_issue.main()
        return result, created, stderr.getvalue()

    def test_retired_citation_warns_but_still_files(self):
        result, created, stderr = self._main_capturing_stderr(
            "Cleaning up remaining references to `board_sync.py`."
        )
        self.assertEqual(result, 0)
        created.assert_called_once()
        self.assertIn("board_sync.py", stderr)
        self.assertIn("WARNING", stderr)

    def test_no_retired_citation_produces_no_warning(self):
        result, created, stderr = self._main_capturing_stderr(
            "A perfectly ordinary issue body with no retired references."
        )
        self.assertEqual(result, 0)
        created.assert_called_once()
        self.assertNotIn("WARNING", stderr)

    def test_cleanup_issue_naming_its_own_target_still_files(self):
        # Mirrors harmonic-forge#319's shape: the issue's whole point is
        # citing the thing being cleaned up.
        result, created, stderr = self._main_capturing_stderr(
            "Devin scrub, remaining corpus: sweep leftover references to "
            "`hrse_manager.py` and `.devin/hooks.v1.json` across both repos."
        )
        self.assertEqual(result, 0)
        created.assert_called_once()
        self.assertIn("hrse_manager.py", stderr)
        self.assertIn(".devin/hooks.v1.json", stderr)


class ToolingExceptionImpliesToolingTests(unittest.TestCase):
    """harmonic-forge#616. `tooling-exception` says Lane 1 may execute an issue
    alone; `tooling` is what keeps Lane 2 from picking it up. Two halves of one
    decision, applied by hand, so they drifted: 11 issues carried the exception
    without the label. Operator: "this is crucial for avoiding overlap."
    """

    def test_tooling_exception_adds_tooling(self):
        self.assertEqual(gh_issue.normalise_labels(["bug", "tooling-exception"]),
                         ["bug", "tooling-exception", "tooling"])

    def test_it_is_not_duplicated_when_already_present(self):
        self.assertEqual(
            gh_issue.normalise_labels(["bug", "tooling-exception", "tooling"]),
            ["bug", "tooling-exception", "tooling"])

    def test_unrelated_labels_are_untouched(self):
        """Additive only -- it never removes or rewrites what was asked for."""
        self.assertEqual(gh_issue.normalise_labels(["feature", "ui"]),
                         ["feature", "ui"])

    def test_empty_stays_empty(self):
        self.assertEqual(gh_issue.normalise_labels([]), [])

    def test_order_is_preserved(self):
        """The implied label is appended, so an existing order is not shuffled."""
        got = gh_issue.normalise_labels(["tooling-exception", "bug"])
        self.assertEqual(got[:2], ["tooling-exception", "bug"])

    def test_the_implication_is_transitive(self):
        """harmonic-forge#616 preclose finding. The first version iterated the
        CALLER's list, so a label added by an implication was never consulted
        and a chained row silently dropped -- making AC4's "a second implication
        is a data change" false."""
        with patch.dict(gh_issue._IMPLIED_LABELS, {"tooling": "infrastructure"}):
            self.assertEqual(gh_issue.normalise_labels(["tooling-exception"]),
                             ["tooling-exception", "tooling", "infrastructure"])

    def test_a_cyclic_table_terminates(self):
        """A cycle must not hang the filing tool."""
        with patch.dict(gh_issue._IMPLIED_LABELS, {"tooling": "tooling-exception"}):
            got = gh_issue.normalise_labels(["tooling-exception"])
        self.assertEqual(sorted(got), ["tooling", "tooling-exception"])


class TestToolingImplicationCliWiring(unittest.TestCase):
    """harmonic-forge#616 preclose finding: the ONE LINE that reaches the helper
    had no coverage. Reverting the call site to its pre-diff form left all 67
    tests green -- the same shape as the issue itself, a control that exists,
    is correct, and is not reached at the point of use.

    Mirrors `TestThemeVentureCliWiring`, which does this for a different flag."""

    def test_tooling_exception_reaches_create_issue_as_tooling(self):
        with patch.object(sys, "argv", ["gh_issue.py", "--repo", "vitalharmony/hrse",
                                        "--title", "t", "--labels", "bug,tooling-exception"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created, \
             patch("gh_issue.add_to_board", return_value=True):
            gh_issue.main()
        labels = created.call_args[0][3]
        self.assertIn("tooling", labels,
                      f"the CLI must apply the implication, got {labels!r}")
        self.assertIn("tooling-exception", labels)

    def test_a_plain_filing_is_unchanged_through_the_cli(self):
        with patch.object(sys, "argv", ["gh_issue.py", "--repo", "vitalharmony/hrse",
                                        "--title", "t", "--labels", "feature"]), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created, \
             patch("gh_issue.add_to_board", return_value=True):
            gh_issue.main()
        self.assertEqual(created.call_args[0][3], ["feature"])


class ToolingExceptionRequiresTheLabelTests(unittest.TestCase):
    """harmonic-forge#778 AC1. #769, #772 and #774 each declared Tooling
    Exception in the body and never carried the arming label -- so
    block_missing_preclose_inspection.py's opt-in gate never armed, and #774
    merged (harmonic-forge#775) with no pre-close pass. Checked before
    create_issue(), like every other parser.error() path -- see
    TestHalfOverrideValidatedBeforeCreate above for the same shape."""

    def _main(self, argv):
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.create_issue", return_value="https://x/1") as created, \
             contextlib.redirect_stderr(stderr):
            try:
                return gh_issue.main(), created, stderr.getvalue()
            except SystemExit as exc:
                return exc, created, stderr.getvalue()

    def test_tooling_exception_body_without_the_label_is_refused(self):
        result, created, stderr = self._main([
            "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
            "--body", "Tooling, so Lane 1 handles it under the Tooling Exception.",
            "--labels", "bug",
        ])
        self.assertIsInstance(result, SystemExit)
        self.assertIn("tooling-exception", stderr)
        created.assert_not_called()

    def test_tooling_exception_body_with_the_label_succeeds(self):
        with patch("gh_issue.add_to_board", return_value=True):
            result, created, _ = self._main([
                "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
                "--body", "Tooling Exception: Lane 1 implements.",
                "--labels", "bug,tooling-exception",
            ])
        self.assertEqual(result, 0)
        created.assert_called_once()

    def test_case_insensitive_and_comma_phrasing_is_still_caught(self):
        result, created, stderr = self._main([
            "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
            "--body", "TOOLING, EXCEPTION: no Lane 2 trigger.",
            "--labels", "bug",
        ])
        self.assertIsInstance(result, SystemExit)
        created.assert_not_called()

    def test_a_body_with_no_tooling_exception_mention_is_unaffected(self):
        with patch("gh_issue.add_to_board", return_value=True):
            result, created, _ = self._main([
                "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
                "--body", "A plain feature request.", "--labels", "feature",
            ])
        self.assertEqual(result, 0)
        created.assert_called_once()

    def test_body_file_is_checked_too_not_just_inline_body(self):
        """--body-file is the preferred path (harmonic-forge#266); the check
        must run on the resolved body, after the file is read, not on the
        raw --body-file argument."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as fh:
            fh.write("This is filed under the Tooling Exception.")
            path = fh.name
        try:
            result, created, stderr = self._main([
                "gh_issue.py", "--repo", "vitalharmony/hrse", "--title", "t",
                "--body-file", path, "--labels", "bug",
            ])
        finally:
            os.unlink(path)
        self.assertIsInstance(result, SystemExit)
        self.assertIn("tooling-exception", stderr)
        created.assert_not_called()


class TestPriorOwnerSearch(unittest.TestCase):
    """harmonic-forge#1032 (R-0397): one title search before filing."""

    TITLE = "gh_issue.py prints candidate prior owners before filing"
    HIT = "172\topen\tgh_issue.py: support parent/child filing\n"

    def _main(self, extra=(), search=None):
        """Run main() with a scripted search; returns (exit, created, searches, stderr)."""
        searches = []
        self.all_calls = []
        responses = list(search or [""])

        def fake_run(cmd, check=True, env=None):
            self.all_calls.append(cmd)
            if cmd[:5] == ["gh", "api", "-X", "GET", "search/issues"]:
                searches.append((cmd, env))
                out = responses.pop(0)
                if isinstance(out, int):
                    return _completed("", returncode=out)
                if out.startswith("RAW:"):
                    return _completed(out[4:])
                return _completed("incomplete\tfalse\n" + out)
            raise AssertionError(f"unexpected call {cmd}")

        argv = ["gh_issue.py", "--repo", "o/r", "--title", self.TITLE, *extra]
        err = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch("gh_issue.find_prior_owners", _ORIGINAL_FIND_PRIOR_OWNERS), \
             patch("gh_issue._run", side_effect=fake_run), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.apply_project_identity"), \
             patch("gh_issue.create_issue", return_value=None) as created, \
             contextlib.redirect_stderr(err):
            try:
                code = gh_issue.main()
            except SystemExit as exc:
                code = exc.code
        return code, created, searches, err.getvalue()

    def test_candidates_printed_before_create(self):
        _, created, searches, err = self._main(search=[self.HIT])
        self.assertIn("candidate prior owner: o/r#172 (open)", err)
        created.assert_not_called()
        self.assertEqual(searches[0][1]["GH_SHIM_CALLER"], "gh_issue")

    def test_candidates_without_override_refuses_and_files_nothing(self):
        code, created, _, err = self._main(search=[self.HIT])
        self.assertEqual(code, 2)
        self.assertIn("R-0397", err)
        self.assertIn("--no-prior-owner", err)
        created.assert_not_called()

    def test_override_with_reason_files(self):
        _, created, _, err = self._main(
            extra=["--no-prior-owner", "#172 is epic filing, not this"], search=[self.HIT])
        created.assert_called_once()
        self.assertIn("R-0397 override: #172 is epic filing, not this", err)

    def test_override_blank_reason_is_parser_error(self):
        code, created, searches, _ = self._main(extra=["--no-prior-owner", "  "])
        self.assertEqual(code, 2)
        self.assertEqual(searches, [])
        created.assert_not_called()

    def test_none_found_files_without_override(self):
        _, created, _, err = self._main(search=[""])
        created.assert_called_once()
        self.assertNotIn("candidate prior owner", err)

    def test_exactly_one_added_call(self):
        """AC3: exactly one added `_run` call on every path (create is stubbed)."""
        cases = (((), [self.HIT]), (("--no-prior-owner", "r"), [self.HIT]),
                 ((), [""]), (("--no-prior-owner", "r"), [1]),
                 (("--no-prior-owner", "r"), ["RAW:incomplete\ttrue\n"]))
        for extra, search in cases:
            self._main(extra=extra, search=search + [""] * 3)
            self.assertEqual(len(self.all_calls), 1, (extra, search))

    def test_degraded_search_states_fail_closed(self):
        """Each untrusted result refuses without the override and files with it."""
        for bad in (1, "RAW:incomplete\ttrue\n", "RAW:", "RAW:garbage\n",
                    "not-a-row\n"):
            code, created, _, _ = self._main(search=[bad])
            self.assertIn("refusing to file", str(code), bad)
            created.assert_not_called()
            _, created, _, _ = self._main(
                extra=["--no-prior-owner", "search unavailable: x"], search=[bad])
            created.assert_called_once()

    def test_and_query_never_or(self):
        for search in ([self.HIT], [""]):
            _, _, searches, _ = self._main(search=search + [""] * 3)
            q = [a for a in searches[0][0] if a.startswith("q=")][0]
            self.assertNotIn(" OR ", q)
            self.assertIn("in:title gh_issue.py candidate prior owners filing", q)

    def test_novel_title_zero_candidates(self):
        _, created, searches, err = self._main(search=["", ""])
        self.assertEqual(len(searches), 1)
        self.assertNotIn("candidate prior owner", err)
        created.assert_called_once()

    def test_search_failure_override_files(self):
        _, created, _, err = self._main(
            extra=["--no-prior-owner", "search unavailable: rate limit"], search=[1])
        created.assert_called_once()
        self.assertIn("30 requests/minute", err)

    def test_no_keyword_title_refuses_without_override(self):
        self.TITLE = "Fix MCP SSO bug"
        try:
            code, created, searches, err = self._main()
            self.assertEqual(code, 2)
            self.assertEqual(searches, [])
            self.assertIn("no searchable keyword", err)
            created.assert_not_called()
            _, created, _, _ = self._main(extra=["--no-prior-owner", "trivial title"])
            created.assert_called_once()
        finally:
            del self.TITLE

    def test_search_failure_refuses(self):
        code, created, _, _ = self._main(search=[1])
        created.assert_not_called()
        self.assertIn("30 requests/minute", str(code))
        self.assertIn("resets within 60 seconds", str(code))
        self.assertIn("search unavailable", str(code))

    def test_title_keywords_drop_short_and_stopwords(self):
        self.assertEqual(gh_issue.title_keywords("Fix the bug in a thing when it breaks"),
                         ["thing", "breaks"])
        self.assertEqual(gh_issue.title_keywords("a b c"), [])
