#!/usr/bin/env python3
"""Tests for tick_scan (harmonic-forge#1477)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tick_scan  # noqa: E402


class Classification(unittest.TestCase):
    def test_an_entry_naming_this_lane_is_owed(self) -> None:
        self.assertTrue(tick_scan.owed("F1472 — handoff posted · waiting on Lane 1 implement after F1470 merges",
                                       "pending", "1"))
        self.assertTrue(tick_scan.owed("F1 — x · waiting on L1 review", "in_progress", "1"))

    def test_an_entry_with_no_waiting_on_is_owed(self) -> None:
        self.assertTrue(tick_scan.owed("F1399 V0b spike: not filed", "pending", "1"))

    def test_an_entry_waiting_on_someone_else_is_not(self) -> None:
        for subject in ("F1 — merged · waiting on operator `close F1`",
                        "F1 — posted · waiting on Lane 2 implement",
                        "F1 — PR open · waiting on CI",
                        "F1 — committed · waiting on 4 kill checks + check (bg)"):
            with self.subTest(subject=subject):
                self.assertFalse(tick_scan.owed(subject, "pending", "1"))

    def test_the_lane_is_a_whole_token(self) -> None:
        self.assertFalse(tick_scan.owed("F1 — x · waiting on L10 review", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — x · waiting on Lane 12 review", "pending", "1"))

    def test_an_own_lane_step_marked_bg_is_not_reworked(self) -> None:
        self.assertFalse(tick_scan.owed("F1 — x · waiting on Lane 1 check (bg)", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — x · waiting on nothing kill checks (bg)", "pending", "1"))

    def test_the_word_running_does_not_hide_owed_work(self) -> None:
        """Preclose pass 1: only the explicit `(bg)` marker means in flight."""
        self.assertTrue(tick_scan.owed("F1 — x · waiting on Lane 1 rerun the running-total fix", "pending", "1"))

    def test_nothing_and_nobody_mean_this_lane(self) -> None:
        """Preclose pass 1: `nothing` is the hold hook's own-lane `<who>`."""
        self.assertTrue(tick_scan.owed("F1 — reviewed · waiting on nothing push/PR", "pending", "1"))
        self.assertTrue(tick_scan.owed("F1 — reviewed · waiting on nobody push", "pending", "1"))

    def test_operator_items_and_closed_entries_are_never_owed(self) -> None:
        self.assertFalse(tick_scan.owed("YOU: run the red-team sessions", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — merged 5a1b · waiting on YOU: close", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — closed", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — closed (merge 5a1b2c3)", "pending", "1"))

    def test_prose_in_the_last_action_never_counts_as_the_clause(self) -> None:
        self.assertFalse(tick_scan.owed("F1 — fixed waiting on Lane 1 bug · waiting on Lane 2 implement",
                                        "pending", "1"))
        self.assertTrue(tick_scan.owed("F1 — no longer waiting on Lane 2 · waiting on Lane 1 review",
                                       "pending", "1"))
        self.assertTrue(tick_scan.owed("F1 — x · waiting on Lane 1 review · note: was waiting on Lane 2",
                                       "pending", "1"))

    def test_lane_spellings_with_separators(self) -> None:
        self.assertTrue(tick_scan.owed("F1 — x · waiting on lane-1 review", "pending", "1"))
        self.assertTrue(tick_scan.owed("F1 — x · waiting on Lane: 1 review", "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — x · waiting on Lane 1_review", "pending", "1"))

    def test_an_unplaceable_who_is_named_not_dropped(self) -> None:
        self.assertEqual(tick_scan.classify("F1 — x · waiting on the moon", "pending", "1"), "unparsed")

    def test_a_completed_entry_is_not_owed(self) -> None:
        self.assertFalse(tick_scan.owed("F1 — closed", "completed", "1"))
        self.assertFalse(tick_scan.owed("F1 — x · waiting on Lane 1 y", "completed", "1"))


class Scan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)

    def put(self, name: str, subject: str, status: str = "pending") -> None:
        (self.store / f"{name}.json").write_text(json.dumps({"id": name, "subject": subject, "status": status}))

    def test_only_owed_entries_print_in_id_order(self) -> None:
        self.put("10", "F10 — x · waiting on Lane 1 go")
        self.put("2", "F2 — x · waiting on operator")
        self.put("1", "F1 no waiting clause")
        self.assertEqual(tick_scan.scan(self.store, "1"),
                         ["owed-own-queue F1 no waiting clause", "owed-own-queue F10 — x · waiting on Lane 1 go"])

    def test_an_unreadable_entry_is_named_and_the_scan_continues(self) -> None:
        (self.store / "3.json").write_text("{not json")
        self.put("4", "F4 open")
        self.assertEqual(tick_scan.scan(self.store, "1"), ["tick-scan: unreadable 3.json", "owed-own-queue F4 open"])

    def test_a_missing_store_is_an_empty_list(self) -> None:
        """Preclose pass 1: as tasklist_owed.py reads it, so an empty lane can be silent."""
        self.assertEqual(tick_scan.scan(self.store / "nope", "1"), [])

    def test_an_unparsed_wait_and_a_null_subject_are_named(self) -> None:
        self.put("5", "F5 — x · waiting on the moon")
        (self.store / "6.json").write_text(json.dumps({"id": "6", "subject": None, "status": "pending"}))
        self.assertEqual(tick_scan.scan(self.store, "1"),
                         ["tick-scan: unparsed waiting-on in F5 — x · waiting on the moon",
                          "tick-scan: unreadable 6.json"])

    def test_main_names_a_missing_session_and_never_fails(self) -> None:
        with mock.patch.dict(os.environ, {"LANE": "1"}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tick_scan.main([]), 0)
        self.assertIn("CLAUDE_CODE_SESSION_ID is unset", out.getvalue())

    def test_main_reads_the_sessions_store(self) -> None:
        session = self.store / "sess-1"
        session.mkdir()
        (session / "1.json").write_text(json.dumps({"id": "1", "subject": "F9 todo", "status": "pending"}))
        out = io.StringIO()
        with mock.patch.object(tick_scan, "TASKS_DIR", self.store), \
                mock.patch.dict(os.environ, {"LANE": "1", "CLAUDE_CODE_SESSION_ID": "sess-1"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(tick_scan.main([]), 0)
        self.assertEqual(out.getvalue(), "owed-own-queue F9 todo\n")


if __name__ == "__main__":
    unittest.main()
