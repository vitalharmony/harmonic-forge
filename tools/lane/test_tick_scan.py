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

    def test_an_own_lane_step_already_running_is_not_reworked(self) -> None:
        self.assertFalse(tick_scan.owed("F1399 — filed · waiting on Lane 1 handoff (pitch-inspection running)",
                                        "pending", "1"))
        self.assertFalse(tick_scan.owed("F1 — x · waiting on Lane 1 check (bg)", "pending", "1"))

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

    def test_a_missing_store_is_named(self) -> None:
        self.assertEqual(tick_scan.scan(self.store / "nope", "1"),
                         [f"tick-scan: no task list at {self.store / 'nope'}"])

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
