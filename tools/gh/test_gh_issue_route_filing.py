#!/usr/bin/env python3
"""Tests for the filing route checkpoint and the Path board field (harmonic-forge#1232, A2)."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "routing"))
sys.path.insert(0, str(HERE.parent / "telemetry"))
import create_path_field  # noqa: E402
import emit  # noqa: E402
import gh_issue  # noqa: E402
import route_store  # noqa: E402
import scope  # noqa: E402

FORGE = "vitalharmony/harmonic-forge"
HRSE = "vitalharmony/hrse"
URL = f"https://github.com/{FORGE}/issues/77"


class TempStore(unittest.TestCase):
    """Route events go to a temp store, never the live telemetry store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {emit.STORE_ENV: str(Path(self.tmp.name) / "store")})
        patcher.start()
        self.addCleanup(patcher.stop)


class Scope(unittest.TestCase):
    def test_backticked_paths_once_each_in_order(self):
        body = "see `tools/a.py` and `backend/` then `tools/a.py` again, but not tools/b.py"
        self.assertEqual(scope.paths_from_body(body), ["tools/a.py", "backend/"])

    def test_common_path_forms_are_extracted(self):
        """Pass 1: directories, :line suffixes and extensionless files all counted."""
        cases = {
            "`tools/hooks/`": ["tools/hooks/"],
            "`tools/hooks/x.py:42`": ["tools/hooks/x.py"],
            "`tools/hooks/x.py:42-60`": ["tools/hooks/x.py"],
            "`tools/hooks/Makefile`": ["tools/hooks/Makefile"],
            "`.windsurfrules`": [".windsurfrules"],
            "`backend/`": ["backend/"],
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(scope.paths_from_body(body), expected)

    def test_those_forms_route_as_lane_machinery(self):
        import gates
        # A bare directory is extracted (above) but not listed here: A1's
        # `gates._path` normalizes `tools/hooks/` to `tools/hooks`, which the
        # lane-machinery patterns do not match; that errs to Path 1 (strict).
        for body in ("`tools/hooks/x.py:42`", "`tools/hooks/Makefile`"):
            with self.subTest(body=body):
                route = gates.evaluate(scope.scope_for_filing(FORGE, body, []))
                self.assertEqual((route.path, route.gate_id), (2, "lane-machinery"))

    def test_prose_and_spaced_backticks_are_not_paths(self):
        self.assertEqual(scope.paths_from_body("run `mise run check` and see tools/hooks/x.py"), [])

    def test_an_unparseable_path_like_span_is_reported_not_dropped(self):
        for body, token in (("`src/*.py`", "src/*.py"), ("`$HOME/x/y.py`", "$HOME/x/y.py"),
                            ("`tools/hooks/[x].py`", "tools/hooks/[x].py")):
            with self.subTest(body=body):
                self.assertIn(token, scope.unparsed_spans(body))

    def test_special_character_file_name_and_wrapped_span_are_unparsed(self):
        """Post-verdict read: a name with an extension but a stray character, and a
        span broken across a line, must be reported, not ignored."""
        self.assertIn("foo?.py", scope.unparsed_spans("`tools/hooks/x.py` `foo?.py`"))
        self.assertIn("*.py", scope.unparsed_spans("`tools/hooks/x.py` `src/\n*.py`"))

    def test_anchors_and_spaces_are_read_not_unparsed(self):
        self.assertEqual(scope.paths_from_body("`frontend/src/App.tsx#L5`"), ["frontend/src/App.tsx"])
        self.assertEqual(scope.unparsed_spans("`frontend/src/App.tsx#L5-L9` and `backend/my file.py`"), [])
        self.assertIn("backend/my", scope.paths_from_body("`backend/my file.py`"))

    def test_no_path_is_an_empty_scope(self):
        self.assertEqual(scope.scope_for_filing(FORGE, "no paths here", []).halves, {})

    def test_scope_is_never_runner_eligible_and_has_no_threat_model(self):
        s = scope.scope_for_filing(FORGE, "`tools/a.py`", ["tooling-exception"])
        self.assertFalse(s.runner_eligible)
        self.assertIsNone(s.threat_model)
        self.assertEqual(s.labels, ["tooling-exception"])


class FilingCheckpoint(TempStore):
    """AC2 to AC4 at the checkpoint function."""

    def test_lane_machinery_body_is_path_2(self):
        self.assertEqual(gh_issue._route_at_filing(FORGE, "edits `tools/hooks/x.py`", [], 77), "2")

    def test_hrse_product_body_is_path_1(self):
        self.assertEqual(gh_issue._route_at_filing(HRSE, "edits `backend/app/x.py`", [], 77), "1")

    def test_body_with_no_path_is_path_1_no_scope(self):
        self.assertEqual(gh_issue._route_at_filing(FORGE, "nothing to see", [], 77), "1")
        self.assertEqual(route_store.current(f"{FORGE}#77")["attrs"]["gate_id"], "no-scope")

    def test_one_filing_event_is_written(self):
        gh_issue._route_at_filing(FORGE, "edits `tools/hooks/x.py`", [], 77)
        events = route_store.load(f"{FORGE}#77")
        self.assertEqual([e["attrs"]["checkpoint"] for e in events], ["filing"])

    def test_adding_an_unparseable_token_never_lowers_the_route(self):
        """Sticky-wicket's property: no spelling of a path can give a less strict route."""
        bases = ["`tools/hooks/x.py`", "`backend/app/x.py`", "nothing to see", "`docs/a.md`"]
        unparseable = ["`src/*.py`", "`$HOME/x/y.py`", "`tools/hooks/[x].py`", "`~/z/w.py`",
                       "`foo?.py`", "`src/\n*.py`"]
        for repo in (FORGE, HRSE):
            for base in bases:
                before = int(gh_issue._route_at_filing(repo, base, [], 77))
                for token in unparseable:
                    with self.subTest(repo=repo, base=base, token=token):
                        after = int(gh_issue._route_at_filing(repo, f"{base} {token}", [], 77))
                        self.assertLessEqual(after, before)

    def test_unparseable_span_files_at_path_1(self):
        self.assertEqual(gh_issue._route_at_filing(FORGE, "edits `tools/hooks/x.py` and `src/*.py`", [], 77), "1")
        self.assertEqual(route_store.current(f"{FORGE}#77")["attrs"]["gate_id"], "unparsed-path")

    def test_evaluator_error_prints_unavailable_and_returns_none(self):
        err = io.StringIO()
        with patch.object(scope, "scope_for_filing", side_effect=KeyError("x")), redirect_stderr(err):
            self.assertIsNone(gh_issue._route_at_filing(FORGE, "`tools/a.py`", [], 77))
        self.assertIn("route: unavailable", err.getvalue())

    def test_record_error_still_returns_the_path(self):
        err = io.StringIO()
        with patch.object(route_store, "record", side_effect=RuntimeError("store down")), redirect_stderr(err):
            self.assertEqual(gh_issue._route_at_filing(FORGE, "`tools/hooks/x.py`", [], 77), "2")
        self.assertIn("route: not recorded", err.getvalue())


class MainWiring(TempStore):
    ARGV = ["gh_issue.py", "--repo", FORGE, "--title", "semantic routing filing checkpoint",
            "--body", "edits `tools/hooks/x.py`", "--no-prior-owner", "test fixture"]

    def _main(self, discharge_ok: bool):
        with patch.object(sys, "argv", self.ARGV), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.find_prior_owners", return_value=[]), \
             patch("gh_issue.create_issue", return_value=URL), \
             patch("gh_issue.handoff_owed.record"), \
             patch("gh_issue._discharge_exception", return_value=discharge_ok), \
             patch("gh_issue.resolve_board_for_repo", return_value=("o", "1")), \
             patch("gh_issue.add_to_board", return_value=True) as add:
            return gh_issue.main(), add

    def test_event_is_written_even_when_discharge_exits_nonzero(self):
        """AC3."""
        result, add = self._main(discharge_ok=False)
        self.assertEqual(result, 1)
        add.assert_not_called()
        self.assertEqual([e["attrs"]["checkpoint"] for e in route_store.load(f"{FORGE}#77")], ["filing"])

    def test_path_reaches_add_to_board(self):
        result, add = self._main(discharge_ok=True)
        self.assertEqual(result, 0)
        self.assertEqual(add.call_args[0][6], "2")


class PathWrite(unittest.TestCase):
    """Path is a soft write, attempted before the hard ones."""

    def _board(self, set_single_select):
        ctx = {"project_id": "P", "fields": []}
        with patch("gh_issue._run", return_value=SimpleNamespace(returncode=0, stdout='{"id": "I1"}', stderr="")), \
             patch("gh_issue._fetch_project_context", return_value=ctx), \
             patch("gh_issue._set_single_select", side_effect=set_single_select) as setter:
            ok = gh_issue.add_to_board(URL, "o", "1", None, route_path="2")
        return ok, setter

    def test_rejected_path_write_does_not_fail_the_filing(self):
        ok, _ = self._board(lambda item, project, fields, name, value: name != "Path")
        self.assertTrue(ok)

    def test_path_is_written_before_status(self):
        _, setter = self._board(lambda *a: True)
        self.assertEqual([c.args[3] for c in setter.call_args_list][:2], ["Path", "Status"])

    def test_path_is_attempted_when_a_later_hard_write_fails(self):
        ok, setter = self._board(lambda item, project, fields, name, value: name != "Status")
        self.assertFalse(ok)
        self.assertEqual(setter.call_args_list[0].args[3], "Path")

    def test_no_path_means_no_path_write(self):
        ctx = {"project_id": "P", "fields": []}
        with patch("gh_issue._run", return_value=SimpleNamespace(returncode=0, stdout='{"id": "I1"}', stderr="")), \
             patch("gh_issue._fetch_project_context", return_value=ctx), \
             patch("gh_issue._set_single_select", return_value=True) as setter:
            gh_issue.add_to_board(URL, "o", "1", None)
        self.assertNotIn("Path", [c.args[3] for c in setter.call_args_list])


class RouteNoticeEveryExit(TempStore):
    """The `route: unavailable` line prints exactly once whenever a route was
    computed and Path was not written, from ONE check around main(), so it holds
    for every exit including ones nobody has written yet (sticky-wicket)."""

    ARGV = ["gh_issue.py", "--repo", FORGE, "--title", "semantic routing filing checkpoint",
            "--body", "edits `tools/hooks/x.py`", "--no-prior-owner", "test fixture"]
    NOTICE = "route: unavailable — Path="
    OK_ITEM = SimpleNamespace(returncode=0, stdout='{"id": "I1"}', stderr="")

    def _drive(self, *, discharge_ok=True, board=("o", "1"), run=None, find_existing=None,
               context="ok", set_single_select=None):
        err = io.StringIO()
        ctx = {"project_id": "P", "fields": []}
        with contextlib.ExitStack() as stack:
            for target, kwargs in (
                    ("fetch_milestones", {"return_value": {}}), ("find_prior_owners", {"return_value": []}),
                    ("create_issue", {"return_value": URL}), ("handoff_owed.record", {}),
                    ("_discharge_exception", {"return_value": discharge_ok}),
                    ("resolve_board_for_repo", {"return_value": board}),
                    ("_run", {"return_value": run or self.OK_ITEM}),
                    ("_find_existing_item", {"return_value": find_existing}),
                    ("_fetch_project_context", {"return_value": ctx if context == "ok" else None}),
                    ("_set_single_select", {"side_effect": set_single_select or (lambda *a: True)})):
                stack.enter_context(patch(f"gh_issue.{target}", **kwargs))
            stack.enter_context(patch.object(sys, "argv", self.ARGV))
            stack.enter_context(redirect_stderr(err))
            code = gh_issue.main_with_route_check()
        return code, err.getvalue()

    def test_written_path_prints_nothing(self):
        code, err = self._drive()
        self.assertEqual(code, 0)
        self.assertEqual(err.count(self.NOTICE), 0)

    def test_failed_discharge_exit_prints_it_once(self):
        code, err = self._drive(discharge_ok=False)
        self.assertEqual(code, 1)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_no_board_prints_it_once(self):
        code, err = self._drive(board=("", ""))
        self.assertEqual(code, 0)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_item_add_failure_prints_it_once(self):
        code, err = self._drive(run=SimpleNamespace(returncode=1, stdout="", stderr="boom"))
        self.assertEqual(code, 1)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_unreadable_item_id_prints_it_once(self):
        code, err = self._drive(run=SimpleNamespace(returncode=0, stdout="not json", stderr=""))
        self.assertEqual(code, 1)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_missing_project_context_prints_it_once(self):
        code, err = self._drive(context="none")
        self.assertEqual(code, 1)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_board_rejecting_path_prints_it_once_and_still_files(self):
        code, err = self._drive(set_single_select=lambda item, project, fields, name, value: name != "Path")
        self.assertEqual(code, 0)
        self.assertEqual(err.count(self.NOTICE), 1)

    def test_a_later_hard_failure_after_a_written_path_prints_nothing(self):
        code, err = self._drive(set_single_select=lambda item, project, fields, name, value: name != "Status")
        self.assertEqual(code, 1)
        self.assertEqual(err.count(self.NOTICE), 0)

    def test_a_stale_state_from_a_prior_filing_does_not_leak(self):
        self._drive(discharge_ok=False)
        code, err = self._drive()
        self.assertEqual(err.count(self.NOTICE), 0)

    def test_a_second_invocation_that_computes_no_route_prints_nothing(self):
        """Post-verdict read: the state is reset on entry, not only when a route is computed."""
        self._drive(discharge_ok=False)
        err = io.StringIO()
        with patch("gh_issue.main", return_value=0), redirect_stderr(err):
            self.assertEqual(gh_issue.main_with_route_check(), 0)
        self.assertEqual(err.getvalue().count(self.NOTICE), 0)


class NoOptionEdits(unittest.TestCase):
    """AC5: nothing edits a single-select's options (harmonic-forge#300)."""

    def test_no_code_path_edits_field_options(self):
        root = HERE.parent
        needle = re.compile("updateProjectV2" + "Field|field-" + "edit|field-" + "delete")
        hits = []
        for path in root.rglob("*"):
            if (not path.is_file() or path.suffix not in {".py", ".sh", ".toml", ".md", ".yml", ".yaml"}
                    or path.resolve() == Path(__file__).resolve() or ".git" in path.parts
                    or "__pycache__" in path.parts):
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if needle.search(text):
                hits.append(str(path.relative_to(root)))
        self.assertEqual(hits, [], "field-option edits wipe every assignment (harmonic-forge#300)")


class CreatePathField(unittest.TestCase):
    BOARD = "vitalharmony/3"

    def _runner(self, fields):
        calls = []

        def run(cmd):
            calls.append(cmd)
            if "field-list" in cmd:
                return SimpleNamespace(returncode=0, stdout='{"fields": %s}' % json.dumps(fields), stderr="")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        return run, calls

    def test_existing_path_makes_no_change(self):
        """AC6."""
        run, calls = self._runner([{"name": "Path", "type": "ProjectV2SingleSelectField",
                                    "options": [{"name": "1"}, {"name": "2"}, {"name": "3"}]}])
        self.assertEqual(create_path_field.ensure_path_field(self.BOARD, run=run, out=lambda s: None), "present")
        self.assertFalse(any("field-create" in c for c in calls))

    def test_absent_path_is_created_with_1_2_3(self):
        run, calls = self._runner([{"name": "Status"}])
        self.assertEqual(create_path_field.ensure_path_field(self.BOARD, run=run, out=lambda s: None), "created")
        create = next(c for c in calls if "field-create" in c)
        self.assertIn("1,2,3", create)
        self.assertIn("SINGLE_SELECT", create)

    def test_read_only_never_creates(self):
        run, calls = self._runner([{"name": "Status"}])
        self.assertEqual(create_path_field.ensure_path_field(self.BOARD, read_only=True, run=run,
                                                             out=lambda s: None), "would-create")
        self.assertFalse(any("field-create" in c for c in calls))

    def test_read_is_printed_before_any_create(self):
        lines = []
        run, calls = self._runner([{"name": "Status", "type": "SINGLE_SELECT"}])
        create_path_field.ensure_path_field(self.BOARD, run=run, out=lines.append)
        self.assertIn("Status", lines[0])

    def test_bad_board_is_refused(self):
        with self.assertRaises(ValueError):
            create_path_field.split_board("vitalharmony")


if __name__ == "__main__":
    unittest.main()
