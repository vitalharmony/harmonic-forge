#!/usr/bin/env python3
"""Unit tests for belt_candidates.py (harmonic-forge#691, rescoped).

Run: python3 tools/gh/test_belt_candidates.py
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import belt_candidates as bc  # noqa: E402

UTC = timezone.utc

_QUEUE_KINDS = {
    "l3": ("ready-for-l3", "ae", "sweep", "ae-and-sweep"),
    "l2": ("handoff", "rework"),
    "l1": ("plan",),
}
_QUEUE_POSTERS = {
    "l3": ("l1",),
    "l2": ("l1",),
    "l1": ("l2", "l3"),
}


class RecordCandidateTests(unittest.TestCase):
    def test_records_one_file_per_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1921, "handoff", "l1", base_dir=base)
            files = list(base.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "vitalharmony__hrse__1921.json")
            entry = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(entry["repo"], "vitalharmony/hrse")
            self.assertEqual(entry["issue"], 1921)
            self.assertEqual(entry["kind"], "handoff")
            self.assertEqual(entry["posted_by"], "l1")
            self.assertIn("posted_at", entry)

    def test_a_second_record_overwrites_rather_than_appends(self):
        """AC3': one file per issue, bounded by open-issue count, not post
        count -- a hot issue with ten posts still occupies one file, and
        only the newest entry survives."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            bc.record_candidate("vitalharmony/hrse", 1, "rework", "l1", base_dir=base)
            files = list(base.glob("*.json"))
            self.assertEqual(len(files), 1)
            entry = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(entry["kind"], "rework")

    def test_two_different_issues_produce_two_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            bc.record_candidate("vitalharmony/harmonic-forge", 2, "plan", "l2", base_dir=base)
            self.assertEqual(len(list(base.glob("*.json"))), 2)

    def test_the_parent_directory_is_created_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nested" / "dir"
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            self.assertTrue(base.exists())

    def test_a_write_failure_does_not_raise(self):
        """Best-effort: the belt's convenience record must never fail the
        post itself -- the comment is already on GitHub by the time this
        runs."""
        bc.record_candidate(
            "vitalharmony/hrse", 1, "handoff", "l1",
            base_dir=Path("/nonexistent/dir/of/no/permission"),
        )  # must not raise

    def test_no_temp_file_is_left_behind_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            names = {p.name for p in base.iterdir()}
            self.assertEqual(names, {"vitalharmony__hrse__1.json"})


class ReadCandidatesEligibilityTests(unittest.TestCase):
    """AC2': the reader returns a candidate only when its newest entry is
    queue-eligible for the requesting lane -- collapsing "every issue any
    writer touched in 14 days" (the kind-less pre-rescope design, measured
    at ~200 REST calls/tick) back to single digits."""

    def test_an_eligible_kind_and_poster_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, {("vitalharmony/hrse", 1)})

    def test_a_kind_not_in_queue_kinds_for_the_lane_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # `discussion` is never queue-eligible for any lane.
            bc.record_candidate("vitalharmony/hrse", 1, "discussion", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, set())

    def test_a_poster_not_in_queue_posters_for_the_lane_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # `plan` posted by l2 is eligible for l1's queue, not l2's own.
            bc.record_candidate("vitalharmony/hrse", 1, "plan", "l2", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, set())

    def test_lane1_plan_from_lane2_is_eligible_for_l1(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/harmonic-forge", 618, "plan", "l2", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/harmonic-forge"], "l1",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, {("vitalharmony/harmonic-forge", 618)})

    def test_lane3_is_covered_by_l1_posts_deliberately_ac7(self):
        """AC7': Lane 3 is included, not gated out -- its
        ready-for-l3/ae/sweep kinds are already l1_post.py-posted
        (posted_by='l1'), which QUEUE_POSTERS['l3'] already accepts, so it
        gets coverage at the same near-zero cost as Lane 1 and Lane 2."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1530, "ready-for-l3", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l3",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, {("vitalharmony/hrse", 1530)})

    def test_a_repo_not_requested_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/harmonic-forge", 1, "handoff", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, set())

    def test_an_entry_older_than_max_age_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
                now=datetime.now(UTC) + timedelta(days=15),
            )
            self.assertEqual(result, set())

    def test_a_stale_entry_is_pruned_when_opted_in(self):
        """AC3': pruning a stale entry is a safe, isolated unlink -- never
        a rewrite of a shared structure. Opt-in (`prune=True`), not the
        default -- see `BeltCandidatesRealDirUntouchedTests` for why."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
                now=datetime.now(UTC) + timedelta(days=15), prune=True,
            )
            self.assertEqual(list(base.glob("*.json")), [])

    def test_prune_defaults_to_false_and_leaves_the_stale_file_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
                now=datetime.now(UTC) + timedelta(days=15),
            )
            self.assertEqual(len(list(base.glob("*.json"))), 1)

    def test_a_malformed_file_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            base.mkdir(parents=True, exist_ok=True)
            (base / "vitalharmony__hrse__1.json").write_text("{not json", encoding="utf-8")
            bc.record_candidate("vitalharmony/hrse", 2, "handoff", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, {("vitalharmony/hrse", 2)})

    def test_an_empty_or_missing_directory_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "does-not-exist-yet"
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l2",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, set())

    def test_an_unknown_lane_matches_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "handoff", "l1", base_dir=base)
            result = bc.read_candidates(
                ["vitalharmony/hrse"], "l4-does-not-exist",
                queue_kinds=_QUEUE_KINDS, queue_posters=_QUEUE_POSTERS, base_dir=base,
            )
            self.assertEqual(result, set())


def _snapshot_real_dir():
    real_dir = bc.DEFAULT_CANDIDATES_DIR
    if not real_dir.exists():
        return None
    return {
        p.name: p.read_bytes()
        for p in sorted(real_dir.glob("*"))
        if p.is_file()
    }


#: AC4'. Module-level, not a class fixture -- `unittest`'s loader walks
#: `dir(module)` alphabetically, so a per-CLASS setUp/assert pair here would
#: run and check itself before (or between) the other classes in this file
#: rather than bracketing the whole module's run. `setUpModule`/
#: `tearDownModule` are the only hooks `unittest` guarantees run once,
#: strictly before and after every test in this file, regardless of class
#: name ordering.
_REAL_DIR_BEFORE = None


def setUpModule():
    global _REAL_DIR_BEFORE
    _REAL_DIR_BEFORE = _snapshot_real_dir()


def tearDownModule():
    after = _snapshot_real_dir()
    if after != _REAL_DIR_BEFORE:
        raise AssertionError(
            "a test in test_belt_candidates.py wrote to the real belt "
            f"candidates directory ({bc.DEFAULT_CANDIDATES_DIR}) without "
            "passing base_dir -- every call site in this module must pass "
            "an explicit base_dir (harmonic-forge#691 AC4')"
        )


if __name__ == "__main__":
    unittest.main()
