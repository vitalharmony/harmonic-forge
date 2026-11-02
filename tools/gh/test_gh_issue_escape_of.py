#!/usr/bin/env python3
"""Tests for `gh_issue.py --escape-of` (harmonic-forge#1248, semantic routing B3).

Events go to a temp store, never the live telemetry store; GitHub calls are patched.
Run: `python3 -m unittest tools.gh.test_gh_issue_escape_of`."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "telemetry"))
import emit  # noqa: E402
import gh_issue  # noqa: E402

FORGE = "vitalharmony/harmonic-forge"
URL = f"https://github.com/{FORGE}/issues/77"
BODY = "edits `tools/hooks/x.py`"


class EscapeOf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "store"
        patcher = patch.dict(os.environ, {emit.STORE_ENV: str(self.store),
                                          emit.TESTING_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, *extra, create_patch=True):
        argv = ["gh_issue.py", "--repo", FORGE, "--title", "an escaped bug",
                "--body", BODY, "--no-prior-owner", "test fixture", *extra]
        create_cm = patch("gh_issue.create_issue", return_value=URL) if create_patch \
            else contextlib.nullcontext(None)
        with patch.object(sys, "argv", argv), \
             patch("gh_issue.fetch_milestones", return_value={}), \
             patch("gh_issue.find_prior_owners", return_value=[]), \
             create_cm as create, \
             patch("gh_issue.handoff_owed.record"), \
             patch("gh_issue._discharge_exception", return_value=True), \
             patch("gh_issue.resolve_board_for_repo", return_value=("o", "1")), \
             patch("gh_issue.add_to_board", return_value=True):
            return gh_issue.main(), create

    def events(self) -> list[dict]:
        import json
        return [json.loads(line) for part in self.store.rglob("*.jsonl")
                for line in part.read_text().splitlines() if '"escape.filed"' in line]

    def test_flag_appends_the_line_and_records_one_event(self):
        result, create = self._main("--escape-of", "F1230")
        self.assertIn(result, (0, None))
        self.assertTrue(create.call_args.args[2].endswith("\n\nEscape-of: F1230\n"))
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["issue"], 77)
        self.assertEqual(events[0]["attrs"]["escape_of_key"], "F1230")

    def test_malformed_key_is_refused_before_filing(self):
        for key in ("L12", "f1230", "F", "1230", "H12x"):
            with self.subTest(key=key), redirect_stderr(io.StringIO()), \
                 patch("gh_issue.create_issue", return_value=URL) as create:
                with self.assertRaises(SystemExit) as caught:
                    self._main("--escape-of", key, create_patch=False)
                self.assertEqual(caught.exception.code, 2)
                create.assert_not_called()
                self.assertEqual(self.events(), [])

    def test_an_unparsed_issue_url_prints_the_skip(self):
        err = io.StringIO()
        with patch("gh_issue._issue_number", return_value=None), redirect_stderr(err):
            self._main("--escape-of", "F1230")
        self.assertIn("escape-of: not recorded", err.getvalue())
        self.assertEqual(self.events(), [])

    def test_a_valid_key_passes_the_same_harness(self):
        result, create = self._main("--escape-of", "H2562")
        self.assertIn(result, (0, None))
        create.assert_called_once()

    def test_emit_failure_never_fails_the_filing(self):
        err = io.StringIO()
        with patch.object(emit, "emit", side_effect=RuntimeError("store down")), redirect_stderr(err):
            result, create = self._main("--escape-of", "F1230")
        self.assertIn(result, (0, None))
        create.assert_called_once()
        self.assertIn("escape-of: not recorded", err.getvalue())

    def test_without_the_flag_the_body_is_unchanged(self):
        _, create = self._main()
        self.assertEqual(create.call_args.args[2], BODY)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
