"""harmonic-forge#989 AC3: `board_fields.py set`. Every gh call is stubbed."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_fields  # noqa: E402

FIELDS = [
    {"id": "F_STATUS", "name": "Status", "options": [{"id": "s1", "name": "Todo"}]},
    {"id": "F_TIER", "name": "Tier", "options": [
        {"id": "t1", "name": "fast"}, {"id": "t2", "name": "standard"}, {"id": "t3", "name": "deep"}]},
    {"id": "F_THEME", "name": "Theme", "options": [{"id": "h1", "name": "Ops"}]},
    {"id": "F_VENTURE", "name": "Venture", "options": [{"id": "v1", "name": "CymaGraph"}]},
]


def _done(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _items(*nodes) -> str:
    return json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": list(nodes)}}}}})


def _node(item_id: str, number: int, owner: str) -> dict:
    return {"id": item_id, "project": {"number": number, "owner": {"login": owner}}}


class FakeGh:
    """Routes each gh argv to a canned response and records every call."""

    def __init__(self, items: str | SimpleNamespace = None, edit_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.items = items if items is not None else _items(_node("ITEM", 1, "vitalharmony"))
        self.edit_rc = edit_rc

    def __call__(self, cmd, check=False):
        self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "api", "graphql"]:
            return self.items if isinstance(self.items, SimpleNamespace) else _done(self.items)
        if cmd[:3] == ["gh", "project", "view"]:
            return _done(json.dumps({"id": "PROJ"}))
        if cmd[:3] == ["gh", "project", "field-list"]:
            return _done(json.dumps({"fields": FIELDS}))
        if cmd[:3] == ["gh", "project", "item-edit"]:
            return _done(returncode=self.edit_rc, stderr="boom" if self.edit_rc else "")
        raise AssertionError(f"unexpected gh call: {cmd}")

    def edited_field_ids(self) -> list[str]:
        return [c[c.index("--field-id") + 1] for c in self.calls if c[:3] == ["gh", "project", "item-edit"]]


class SetTests(unittest.TestCase):
    def run_set(self, argv, gh=None, board=("vitalharmony", "1"), reread=None):
        gh = gh or FakeGh()
        stored = {}

        def fake_read(repo, issue, number, field, run, ttl, owner):
            self.assertEqual(ttl, 0)
            self.assertEqual(owner, board[0], "the re-read must name the resolved board owner")
            return reread(field) if reread else stored.get(field)

        def record_write(item_id, project_id, fields, name, value):
            ok = original(item_id, project_id, fields, name, value)
            if ok:
                stored[name] = value
            return ok

        original = board_fields.gh_issue._set_single_select
        err, out = io.StringIO(), io.StringIO()
        with patch.object(board_fields.gh_issue, "_run", gh), \
             patch.object(board_fields.gh_issue, "resolve_board_for_repo", return_value=board), \
             patch.object(board_fields.gh_issue, "_set_single_select", side_effect=record_write), \
             patch.object(board_fields.item_list_cache, "fetch_issue_field", side_effect=fake_read), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = board_fields.main(["set", *argv])
        return code, gh, err.getvalue() + out.getvalue()

    def test_writes_only_the_named_fields_and_never_status(self) -> None:
        code, gh, _ = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--theme", "Ops"])
        self.assertEqual(code, 0)
        self.assertEqual(gh.edited_field_ids(), ["F_THEME"])

    def test_writes_all_three_and_verifies_each(self) -> None:
        code, gh, text = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5",
                                       "--tier", "deep", "--theme", "ops", "--venture", "CymaGraph"])
        self.assertEqual(code, 0)
        self.assertEqual(gh.edited_field_ids(), ["F_TIER", "F_THEME", "F_VENTURE"])
        for name in ("Tier", "Theme", "Venture"):
            self.assertIn(f"verified {name}", text)

    def test_a_reread_mismatch_exits_nonzero(self) -> None:
        """AC5's second kill check targets this: make the re-read always match and it passes."""
        code, _, text = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"],
                                     reread=lambda field: "fast")
        self.assertEqual(code, 1)
        self.assertIn("re-read as 'fast'", text)

    def test_an_unset_reread_exits_nonzero(self) -> None:
        code, _, _ = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"],
                                  reread=lambda field: None)
        self.assertEqual(code, 1)

    def test_a_failed_write_exits_nonzero(self) -> None:
        code, _, _ = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"],
                                  gh=FakeGh(edit_rc=1))
        self.assertEqual(code, 1)

    def test_an_unknown_option_names_the_valid_ones_and_writes_nothing(self) -> None:
        code, gh, text = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5",
                                       "--tier", "deep", "--theme", "Nope"])
        self.assertEqual(code, 2)
        self.assertIn("Valid options: Ops", text)
        self.assertEqual(gh.edited_field_ids(), [])

    def test_not_on_the_board_names_item_add(self) -> None:
        gh = FakeGh(items=_items(_node("OTHER", 3, "vitalharmony")))
        code, gh, text = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"],
                                      gh=gh)
        self.assertEqual(code, 2)
        self.assertIn("gh project item-add 1 --owner vitalharmony", text)
        self.assertEqual(gh.edited_field_ids(), [])

    def test_the_item_match_requires_the_owner_too(self) -> None:
        """Board number 1 under another account is a different board."""
        gh = FakeGh(items=_items(_node("ELSEWHERE", 1, "someone-else")))
        code, _, text = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"],
                                     gh=gh)
        self.assertEqual(code, 2)
        self.assertIn("item-add", text)

    def test_a_failed_item_query_is_never_not_on_board(self) -> None:
        for items in (_done(returncode=1, stderr="rate limited"),
                      _done(json.dumps({"errors": [{"message": "quota"}]})),
                      _done("not json")):
            with self.subTest(items=items):
                code, gh, text = self.run_set(
                    ["--repo", "vitalharmony/hrse", "--issue", "5", "--tier", "deep"], gh=FakeGh(items=items))
                self.assertEqual(code, 1)
                self.assertNotIn("item-add", text)
                self.assertEqual(gh.edited_field_ids(), [])

    def test_a_cross_owner_board_is_refused(self) -> None:
        """Operator ruling 2026-10-13: "refuse kenekted"."""
        code, gh, text = self.run_set(["--repo", "kenekted/kenekted-platform", "--issue", "5",
                                       "--tier", "deep"], board=("harmonicarchitect", "1"))
        self.assertEqual(code, 2)
        self.assertIn("Cross-owner boards are not supported", text)
        self.assertEqual(gh.calls, [])

    def test_nothing_to_set_is_refused(self) -> None:
        code, gh, _ = self.run_set(["--repo", "vitalharmony/hrse", "--issue", "5"])
        self.assertEqual(code, 2)
        self.assertEqual(gh.calls, [])


if __name__ == "__main__":
    unittest.main()
