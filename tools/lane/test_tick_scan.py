#!/usr/bin/env python3
"""Tests for tick_scan (harmonic-forge#1477): ownership is `metadata.owner`."""
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

#: Every subject a preclose pass used to break the old prose parser.
ADVERSARIAL = (
    "F1 — reviewed · waiting on nothing push/PR",
    "F1 — reviewed · waiting on nobody push",
    "F1 — x · waiting on lane-1 review",
    "F1 — x · waiting on Lane: 1 review",
    "F1 — x · waiting on Lane 1_review",
    "F1 — x · waiting on Lane 1 rerun the running-total fix",
    "F1 — closed the stale PR · waiting on Lane 1 open a new PR",
    "F1 — closed-loop test added · waiting on Lane 1 review",
    "F4 — x · waiting on the moon (bg)",
    "H2463 — L2D posted (a1b2c3d), waiting on Lane 2 implement",
    "F1 — fixed waiting on Lane 1 bug · waiting on Lane 2 implement",
    "F1 — x · waiting on Lane 1 review · note: was waiting on Lane 2",
    "YOU: run the red-team sessions",
    "F1 — closed",
    "F1 no waiting clause",
)


def entry(subject: str, owner: str | None, status: str = "pending", **meta) -> dict:
    metadata = dict(meta)
    if owner is not None:
        metadata["owner"] = owner
    return {"subject": subject, "status": status, "metadata": metadata}


class Classification(unittest.TestCase):
    def test_this_lanes_owner_is_owed(self) -> None:
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane1"), "1"), "owed")
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane1", "in_progress"), "1"), "owed")

    def test_every_other_owner_is_silent(self) -> None:
        for owner in ("lane2", "lane3", "operator", "ci", "bg"):
            with self.subTest(owner=owner):
                self.assertEqual(tick_scan.classify(entry("F1 — x", owner, bg_ref="b1"), "1"), "other")

    def test_the_lane_is_this_sessions(self) -> None:
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane2"), "2"), "owed")
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane1"), "2"), "other")

    def test_no_or_an_unknown_owner_is_unparsed(self) -> None:
        self.assertEqual(tick_scan.classify(entry("F1 — x", None), "1"), "unparsed")
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane10"), "1"), "unparsed")
        self.assertEqual(tick_scan.classify(entry("F1 — x", "Lane 1"), "1"), "unparsed")

    def test_a_completed_entry_is_never_owed(self) -> None:
        self.assertEqual(tick_scan.classify(entry("F1 — x", "lane1", "completed"), "1"), "other")
        self.assertEqual(tick_scan.classify(entry("F1 — x", None, "completed"), "1"), "other")

    def test_every_adversarial_subject_classifies_by_its_owner_alone(self) -> None:
        """Sticky-wicket required test: putting any subject parsing back must
        make one of these fail."""
        for subject in ADVERSARIAL:
            for owner, want in (("lane1", "owed"), ("lane2", "other"), ("operator", "other")):
                with self.subTest(subject=subject, owner=owner):
                    self.assertEqual(tick_scan.classify(entry(subject, owner), "1"), want)


class Scan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)

    def put(self, name: str, data) -> None:
        (self.store / f"{name}.json").write_text(json.dumps(data))

    def test_owed_and_unparsed_entries_print_in_id_order(self) -> None:
        self.put("10", entry("F10 — x", "lane1"))
        self.put("2", entry("F2 — x", "operator"))
        self.put("1", entry("F1 — x", None))
        self.assertEqual(tick_scan.scan(self.store, "1"),
                         ["tick-scan: unparsed F1 — x", "owed-own-queue F10 — x"])

    def test_an_unreadable_entry_is_named_and_the_scan_continues(self) -> None:
        (self.store / "3.json").write_text("{not json")
        self.put("4", {"subject": None, "status": "pending"})
        self.put("5", entry("F5 — x", "lane1"))
        self.assertEqual(tick_scan.scan(self.store, "1"),
                         ["tick-scan: unreadable 3.json", "tick-scan: unreadable 4.json",
                          "owed-own-queue F5 — x"])

    def test_a_missing_store_is_an_empty_list(self) -> None:
        self.assertEqual(tick_scan.scan(self.store / "nope", "1"), [])

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
        (session / "1.json").write_text(json.dumps(entry("F9 todo", "lane1")))
        out = io.StringIO()
        with mock.patch.object(tick_scan, "TASKS_DIR", self.store), \
                mock.patch.dict(os.environ, {"LANE": "1", "CLAUDE_CODE_SESSION_ID": "sess-1"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(tick_scan.main([]), 0)
        self.assertEqual(out.getvalue(), "owed-own-queue F9 todo\n")


if __name__ == "__main__":
    unittest.main()
