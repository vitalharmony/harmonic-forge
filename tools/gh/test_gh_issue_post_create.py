"""harmonic-forge#1578: every post-create failure of a filing is reported and
exits 1. One class per acceptance criterion, each driving `gh_issue.main()`
with `TestPostCreateFailureTable._drive`'s patch set (test_gh_issue.py)."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_issue  # noqa: E402
import test_gh_issue as base  # noqa: E402

#: The table's attributes, never its class: a TestCase bound here would run twice.
ROWS, BOARD_ARGS = base.TestPostCreateFailureTable.ROWS, base.TestPostCreateFailureTable.BOARD_ARGS
PARKING, EXCEPTION = base.TestPostCreateFailureTable.PARKING, base.TestPostCreateFailureTable.EXCEPTION
FORGE = base.FORGE
URL = "https://github.com/o/r/issues/42"
RECORDED = ["--repo", FORGE, "--milestone", "0.3", "--handoff-exception",
            "deferred-design-record", "--handoff-exception-reason", "design waits"]
POSTED = json.dumps({"id": 1, "user": {"login": "vitalharmony"}})
CTX = {"project_id": "PVT_1", "fields": []}
#: `_contained`'s step label for each ROWS call.
LABELS = {"handoff_owed.record": "obligation record", "_post_deferral": "R-0352 deferral",
          "_discharge_exception": "R-0039 exception record", "add_to_board": "board add"}


def _drive(argv, post_stdout="", **over):
    """`main()` with every external edge patched; `over` replaces a patch
    target's kwargs, or drops the patch when given None (the real code runs)."""
    targets = {
        "gh_issue.find_prior_owners": {"return_value": []},
        "gh_issue.fetch_milestones": {"return_value": base.FORGE_MILESTONES},
        "gh_issue.apply_project_identity": {},
        "gh_issue.create_issue": {"return_value": URL},
        "gh_issue._route_at_filing": {"return_value": None},
        "gh_issue.resolve_board_for_repo": {"return_value": ("o", "1")},
        "gh_issue._run": {"return_value": base._completed(post_stdout)},
        "gh_issue.handoff_owed.record": {"return_value": True},
        "gh_issue.handoff_owed.discharge": {"return_value": 1},
        "gh_issue.add_to_board": {"return_value": True},
    }
    for key, value in over.items():
        targets[f"gh_issue.{key}"] = value
    mocks = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", ["gh_issue.py", "--title", "t"]
                                         + argv + BOARD_ARGS))
        for target, kw in targets.items():
            if kw is not None:
                mocks[target.split(".")[-1]] = stack.enter_context(patch(target, **kw))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            code = gh_issue.main()
        except SystemExit as exc:
            code = exc.code
    return code, err.getvalue(), mocks


def _line_after(err: str, marker: str) -> str:
    lines = err.splitlines()
    return next(b for a, b in itertools.pairwise(lines) if marker in a)


class AC1FailedDischargeExitsOne(unittest.TestCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_exception_path_with_a_real_unwritable_store(self):
        import handoff_owed
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "owed"
        with patch.object(handoff_owed, "OWED_DIR", store):
            handoff_owed.record(FORGE, 42, URL, key="s1")
            store.chmod(0o500)
            self.addCleanup(store.chmod, 0o700)
            code, err, _ = _drive(EXCEPTION, **{"handoff_owed.discharge": None})
            self.assertEqual(len(handoff_owed.outstanding("s1")), 1)
        self.assertEqual(code, 1, err)
        repair = _line_after(err, "handoff obligation could not be cleared")
        self.assertIn("python3 -c", repair)
        self.assertIn(f'handoff_owed.discharge("{FORGE}", 42) < 0', repair)
        self.assertNotIn("l1-post", err)

    def test_deferral_path_prints_the_defer_existing_retry(self):
        code, err, _ = _drive(PARKING, **{"handoff_owed.discharge": {"return_value": -1}})
        self.assertEqual(code, 1, err)
        self.assertIn("--defer-existing 42", _line_after(err, "could not be cleared"))
        self.assertIn("R-0352 deferral", err)

    def test_defer_existing_exits_one(self):
        def run(cmd, check=False):
            joined = " ".join(cmd)
            if "/comments?" in joined:
                return base._completed("## R-0352 deferral: parked in `Later`\n")
            return base._completed(f"https://github.com/{FORGE}/issues/42\tLater\n")
        with patch.object(sys, "argv", ["gh_issue.py", "--repo", FORGE, "--defer-existing", "42"]
                          + base.DEFER), \
             patch("gh_issue.fetch_milestones", return_value=base.FORGE_MILESTONES), \
             patch("gh_issue.apply_project_identity"), patch("gh_issue._run", side_effect=run), \
             patch("gh_issue.handoff_owed.discharge", return_value=-1), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            code = gh_issue.main()
        self.assertEqual(code, 1)
        self.assertIn("--defer-existing 42", _line_after(err.getvalue(), "could not be cleared"))

    def test_an_unwritten_deferral_watch_entry_exits_one(self):
        """Operator ruling 2026-11-25: F1564's `record_deferral` returning False."""
        with patch("gh_issue.deferral_store.record_deferral", return_value=False):
            code, err, mocks = _drive(RECORDED, post_stdout=POSTED)
        self.assertEqual(code, 1, err)
        self.assertIn("deferral watch record: python3 tools/gh/record_deferral.py --repo "
                      f"{FORGE} --issue 42 --operator-login vitalharmony", err)
        mocks["add_to_board"].assert_called_once()

    def test_an_unreadable_login_exits_one(self):
        with patch("gh_issue.deferral_store.record_deferral", return_value=True):
            code, err, mocks = _drive(RECORDED, post_stdout="")
        self.assertEqual(code, 1, err)
        self.assertIn("deferral watch record: python3 tools/gh/record_deferral.py --repo "
                      f"{FORGE} --issue 42 --operator-login <login>", err)
        mocks["discharge"].assert_called_once_with(FORGE, 42)


class AC2EveryFieldIsAttempted(unittest.TestCase):
    def _board(self, select):
        with patch("gh_issue._run_board", return_value=base._completed('{"id": "IT_1"}')), \
             patch("gh_issue._fetch_project_context", return_value=CTX), \
             patch("gh_issue._set_single_select", side_effect=select) as sel, \
             patch("gh_issue._set_tier", return_value=True) as tier:
            code, err, _ = _drive(PARKING, add_to_board=None)
        return code, err, [c.args[3:5] for c in sel.call_args_list], tier

    def test_a_failed_status_write_still_attempts_tier_theme_venture(self):
        code, err, fields, tier = self._board(lambda *a: a[3] not in ("Status", "Theme"))
        self.assertEqual(code, 1, err)
        tier.assert_called_once_with("IT_1", "PVT_1", [], "fast")
        self.assertEqual(fields, [("Status", gh_issue.STATUS_OPTION_NAME),
                                  ("Theme", "Tooling"), ("Venture", "Platform")])
        self.assertIn(f"- Status={gh_issue.STATUS_OPTION_NAME} not written", err)
        self.assertIn("- Theme=Tooling not written", err)

    def test_a_raising_status_write_still_attempts_the_rest(self):
        def select(*a):
            if a[3] == "Status":
                raise OSError("could not start gh")
            return True
        code, err, fields, tier = self._board(select)
        self.assertEqual(code, 1, err)
        tier.assert_called_once()
        self.assertEqual([f for f, _ in fields], ["Status", "Theme", "Venture"])
        self.assertIn(f"Status={gh_issue.STATUS_OPTION_NAME} raised OSError", err)


class AC3NoStepRaisesPastTheSummary(unittest.TestCase):
    ERRORS = (OSError("could not start gh"),
              UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
              RecursionError("maximum recursion depth exceeded"))

    def test_each_row_step_raising_is_one_unfinished_step(self):
        for call, argv, *_rest in ROWS.values():
            for exc in self.ERRORS:
                with self.subTest(call=call, exc=type(exc).__name__):
                    code, err, mocks = _drive(argv, **{call: {"side_effect": exc}})
                    self.assertEqual(code, 1, err)
                    self.assertIn(f"{LABELS[call]} raised {type(exc).__name__}", err)
                    self.assertIn(f"[GH]   - {LABELS[call]}: ", err)
                    mocks["record"].assert_called_once()
                    mocks["add_to_board"].assert_called_once()
                    self.assertEqual(mocks["add_to_board"].call_args.args[3:6],
                                     ("fast", "Tooling", "Platform"))

    def test_a_raising_board_lookup_is_an_unfinished_step(self):
        code, err, mocks = _drive(PARKING, resolve_board_for_repo={
            "side_effect": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")})
        self.assertEqual(code, 1, err)
        self.assertIn("board resolution raised UnicodeDecodeError", err)
        self.assertIn("- board resolution:", err)
        mocks["discharge"].assert_called_once()


class AC4SummaryMatchesWhatRan(unittest.TestCase):
    def test_an_unmapped_board_names_the_skipped_steps(self):
        code, err, mocks = _drive(PARKING, resolve_board_for_repo={
            "side_effect": SystemExit("[GH] No project board mapped")})
        self.assertEqual(code, 1, err)
        self.assertIn("board add and Tier/Theme/Venture not attempted", err)
        self.assertNotIn("every other step ran", err)
        mocks["add_to_board"].assert_not_called()

    def test_a_failed_board_add_names_the_skipped_fields(self):
        with patch("gh_issue._run_board", return_value=base._completed("", returncode=1)), \
             patch("gh_issue._find_existing_item", return_value=None):
            code, err, _ = _drive(PARKING, add_to_board=None)
        self.assertEqual(code, 1, err)
        self.assertIn("- board add: add " + URL, err)
        self.assertIn("Status/Tier/Theme/Venture not attempted", err)
        self.assertNotIn("every other step ran", err)


if __name__ == "__main__":
    unittest.main()
