"""harmonic-forge#600 AC1/AC4/AC5 — the belt's read-only view of BATCH state."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import belt_batch_view  # noqa: E402
import watch_lane_posts  # noqa: E402

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _state(tmp: str, payload: object) -> Path:
    path = Path(tmp) / "batch-authorized.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


def _entry(offset_hours: float) -> dict:
    return {"expires_at": (_NOW + timedelta(hours=offset_hours)).isoformat(),
            "targets": []}


class LiveBatchKeysTests(unittest.TestCase):
    def test_an_unexpired_entry_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, {"F326": _entry(3)})
            self.assertEqual(("F326",), belt_batch_view.live_batch_keys(_NOW, path))

    def test_an_expired_entry_is_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, {"F326": _entry(-3)})
            self.assertEqual((), belt_batch_view.live_batch_keys(_NOW, path))

    def test_keys_come_back_sorted_so_the_notice_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, {"F504": _entry(1), "F326": _entry(1)})
            self.assertEqual(("F326", "F504"), belt_batch_view.live_batch_keys(_NOW, path))

    def test_a_missing_file_is_no_live_batch_not_a_crash(self):
        self.assertEqual((), belt_batch_view.live_batch_keys(_NOW, Path("/nonexistent/x.json")))

    def test_malformed_json_is_no_live_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, "{not json")
            self.assertEqual((), belt_batch_view.live_batch_keys(_NOW, path))

    def test_a_top_level_non_mapping_is_no_live_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, ["F326"])
            self.assertEqual((), belt_batch_view.live_batch_keys(_NOW, path))

    def test_a_timezone_naive_expires_at_is_not_live_rather_than_raising(self):
        """The shape harmonic-forge#567 found ten of in the live file: it
        parses, then raises TypeError on comparison against an aware `now`.
        A poll loop must not die on it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, {"F326": {"expires_at": "2026-09-10T15:00:00"}})
            self.assertEqual((), belt_batch_view.live_batch_keys(_NOW, path))

    def test_a_live_entry_beside_a_broken_one_still_reports(self):
        """One bad row must not blind the view to the rest."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, {"BAD": {}, "F326": _entry(2)})
            self.assertEqual(("F326",), belt_batch_view.live_batch_keys(_NOW, path))

    def test_it_imports_nothing_from_tools_hooks(self):
        """AC4, asserted rather than remembered: the coupling is the state
        file, not `batch_auth`'s internals."""
        source = (Path(belt_batch_view.__file__)).read_text()
        for forbidden in ("import batch_auth", "from batch_auth", "tools.hooks"):
            self.assertNotIn(forbidden, source)


class DeferralNoticeTests(unittest.TestCase):
    def test_no_live_batch_means_no_notice(self):
        self.assertIsNone(belt_batch_view.deferral_notice((), 3))

    def test_no_queued_items_means_no_notice(self):
        self.assertIsNone(belt_batch_view.deferral_notice(("F326",), 0))

    def test_the_notice_names_the_live_keys(self):
        notice = belt_batch_view.deferral_notice(("F326", "F504"), 2)
        self.assertIn("F326", notice)
        self.assertIn("F504", notice)

    def test_the_notice_says_the_work_is_kept_not_dropped(self):
        """AC1's whole point. A line that says only 'a batch is live' reads as
        a reason the item will not be offered."""
        notice = belt_batch_view.deferral_notice(("F326",), 2)
        self.assertIn("nothing is dropped", notice)
        self.assertIn("offered again", notice)

    def test_the_notice_names_the_remedy_and_addresses_the_operator(self):
        notice = belt_batch_view.deferral_notice(("F326",), 1)
        self.assertIn("top-up", notice)
        self.assertIn("OPERATOR", notice)

    def test_singular_and_plural_read_correctly(self):
        self.assertIn("1 queued item ", belt_batch_view.deferral_notice(("F326",), 1))
        self.assertIn("2 queued items ", belt_batch_view.deferral_notice(("F326",), 2))


class QueueCycleEmitsTheNoticeTests(unittest.TestCase):
    """AC1 + AC5 at the call site, with `discover_queue` NOT mocked — the
    harmonic-forge#602 lesson: patching the unit under change lets the fix be
    reverted with the suite still green."""

    @staticmethod
    def _wallclock_entry(offset_hours: float) -> dict:
        """Relative to the REAL clock, not the fixed `_NOW` above: this path
        goes through `queue_cycle`, which does not thread a `now` into the
        view, so a fixture pinned to a past date is simply expired."""
        expires = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
        return {"expires_at": expires.isoformat(), "targets": []}

    def _run(self, state_payload: object):
        body = "body\n<!-- l1-post v1; kind=ready-for-l3; posted-by=LANE-unset -->"
        with tempfile.TemporaryDirectory() as tmp:
            path = _state(tmp, state_payload)
            err = io.StringIO()
            with patch("watch_lane_posts._search_candidates", return_value={1530}), \
                 patch("watch_lane_posts._fetch_all_comments", return_value=[{"body": body}]), \
                 contextlib.redirect_stderr(err):
                queue, lines, ok = watch_lane_posts.queue_cycle(
                    ["vitalharmony/hrse"], "l3", {}, {},
                    "2026-09-10T00:00:00Z", batch_state_path=path)
            return queue, lines, err.getvalue()

    def test_a_live_batch_produces_the_notice_on_stderr(self):
        _, lines, err = self._run({"F326": self._wallclock_entry(5)})
        self.assertTrue(lines, "precondition: the cycle must have queued something")
        self.assertIn("BATCH F326 is live", err)

    def test_no_live_batch_produces_no_notice(self):
        _, lines, err = self._run({"F326": self._wallclock_entry(-5)})
        self.assertTrue(lines)
        self.assertNotIn("BATCH", err)

    def test_the_stdout_rows_are_unchanged_by_a_live_batch(self):
        """AC5: a Monitor parses these. The notice sits beside them, never in
        them — so the row set must be byte-identical with and without a live
        batch."""
        _, with_batch, _ = self._run({"F326": self._wallclock_entry(5)})
        _, without_batch, _ = self._run({"F326": self._wallclock_entry(-5)})
        self.assertEqual(without_batch, with_batch)
        self.assertEqual(["vitalharmony/hrse#1530 queued-for-l3 kind=ready-for-l3"],
                         with_batch)

    def test_the_belt_does_not_import_batch_auth(self):
        source = Path(watch_lane_posts.__file__).read_text()
        for forbidden in ("import batch_auth", "from batch_auth"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
