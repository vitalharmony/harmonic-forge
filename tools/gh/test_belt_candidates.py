#!/usr/bin/env python3
"""Unit tests for belt_candidates.py (harmonic-forge#691, rescoped).

Run: python3 tools/gh/test_belt_candidates.py
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import belt_candidates as bc  # noqa: E402
import deferral_store as ds  # noqa: E402  (harmonic-forge#1564)

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
            # `.lock` is the store's writer lock (harmonic-forge#854), not a temp file.
            names = {p.name for p in base.iterdir()} - {".lock"}
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
        default -- see `read_candidates`' own docstring, and
        `tools/run_tests.py`'s `redirected_belt_candidates_dir`, for why a
        caller that forgets `base_dir` still must not touch the real
        directory."""
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


class PostedAtOutTests(unittest.TestCase):
    """harmonic-forge#1516: `posted_at_out` hands the comment watch each returned
    pair's recorded `posted_at` from the one shared reader."""

    def test_each_returned_pair_is_recorded_with_its_posted_at_and_the_set_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "discussion", "l1", base_dir=base)
            bc.record_candidate("vitalharmony/hrse", 2, "handoff", "l1", base_dir=base)
            out: dict = {}
            kinds, posters = {"w": ("discussion",)}, {"w": ("l1",)}
            result = bc.read_candidates(["vitalharmony/hrse"], "w", queue_kinds=kinds,
                                        queue_posters=posters, base_dir=base, posted_at_out=out)
            plain = bc.read_candidates(["vitalharmony/hrse"], "w", queue_kinds=kinds,
                                       queue_posters=posters, base_dir=base)
            self.assertEqual(result, {("vitalharmony/hrse", 1)})
            self.assertEqual(plain, result)
            self.assertEqual(set(out), result, "an excluded entry must not appear in the output")
            self.assertAlmostEqual(out[("vitalharmony/hrse", 1)].timestamp(),
                                   datetime.now(UTC).timestamp(), delta=60)

    def test_a_float_age_window_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bc.record_candidate("vitalharmony/hrse", 1, "discussion", "l1", base_dir=base)
            kinds, posters = {"w": ("discussion",)}, {"w": ("l1",)}
            later = datetime.now(UTC) + timedelta(hours=30)
            self.assertEqual(bc.read_candidates(["vitalharmony/hrse"], "w", queue_kinds=kinds,
                                                queue_posters=posters, base_dir=base, now=later,
                                                max_age_days=1.0), set())
            self.assertEqual(bc.read_candidates(["vitalharmony/hrse"], "w", queue_kinds=kinds,
                                                queue_posters=posters, base_dir=base, now=later,
                                                max_age_days=1.5), {("vitalharmony/hrse", 1)})


class DeferralStoreTests(unittest.TestCase):
    """harmonic-forge#1564 AC2: the deferral store (`deferral_store.py`), one
    directory below the candidate store, at the same basename."""

    REPO = "vitalharmony/hrse"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_the_entry_lives_at_the_candidate_basename_one_directory_down(self):
        ds.record_deferral(self.REPO, 2757, "filing", base_dir=self.base)
        path = bc._candidate_path(ds._deferrals_dir(self.base), self.REPO, 2757)
        self.assertEqual(path, self.base / "deferrals" / "vitalharmony__hrse__2757.json")
        entry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(entry["source"], "filing")
        self.assertNotIn("operator_login", entry)
        self.assertEqual(ds.deferral_entry(self.REPO, 2757, base_dir=self.base), entry)

    def test_the_operator_login_is_recorded(self):
        ds.record_deferral(self.REPO, 2757, "filing", operator_login="vitalharmony", base_dir=self.base)
        self.assertEqual(ds.deferral_entry(self.REPO, 2757, base_dir=self.base)["operator_login"],
                         "vitalharmony")

    def test_a_later_record_candidate_leaves_the_deferral_intact(self):
        ds.record_deferral(self.REPO, 2757, "filing", base_dir=self.base)
        before = ds.deferral_entry(self.REPO, 2757, base_dir=self.base)
        bc.record_candidate(self.REPO, 2757, "plan", "l2", base_dir=self.base)
        after = ds.deferral_entry(self.REPO, 2757, base_dir=self.base)
        self.assertIsNotNone(after)
        self.assertEqual(after.get("source"), "filing")
        self.assertEqual(after.get("recorded_at"), before["recorded_at"])
        # and the candidate store still sees only the candidate entry
        self.assertEqual(bc.read_candidates([self.REPO], "l1", queue_kinds=_QUEUE_KINDS,
                                            queue_posters=_QUEUE_POSTERS, base_dir=self.base),
                         {(self.REPO, 2757)})

    def test_read_deferrals_applies_no_age_cutoff(self):
        ds.record_deferral(self.REPO, 2766, "backfill", base_dir=self.base)
        path = bc._candidate_path(ds._deferrals_dir(self.base), self.REPO, 2766)
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["recorded_at"] = (datetime.now(UTC) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertEqual(ds.read_deferrals([self.REPO], base_dir=self.base), {(self.REPO, 2766)})

    def test_read_deferrals_filters_repos_and_skips_malformed_files(self):
        ds.record_deferral(self.REPO, 1, "filing", base_dir=self.base)
        ds.record_deferral("vitalharmony/harmonic-forge", 2, "filing", base_dir=self.base)
        (ds._deferrals_dir(self.base) / "junk.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(ds.read_deferrals([self.REPO], base_dir=self.base), {(self.REPO, 1)})

    def test_every_spelling_of_a_repo_lands_on_the_canonical_key(self):
        """harmonic-forge#1564 AC11 (preclose #4, #9)."""
        for spelling in ("vitalharmony/HRSE", "https://github.com/VitalHarmony/hrse.git"):
            with self.subTest(spelling=spelling):
                self.assertTrue(ds.record_deferral(spelling, 7, "backfill", base_dir=self.base))
                self.assertEqual(sorted(p.name for p in ds._deferrals_dir(self.base).glob("*.json")),
                                 ["vitalharmony__hrse__7.json"])
                self.assertEqual(ds.deferral_entry(self.REPO, 7, base_dir=self.base)["repo"], self.REPO)
                self.assertIsNotNone(ds.deferral_entry(spelling, 7, base_dir=self.base))
        self.assertEqual(ds.read_deferrals(["vitalharmony/HRSE"], base_dir=self.base),
                         {("vitalharmony/HRSE", 7)})
        self.assertTrue(ds.discharge_deferral("VitalHarmony/Hrse", 7, "closed", base_dir=self.base))
        self.assertIsNone(ds.deferral_entry(self.REPO, 7, base_dir=self.base))

    def test_a_malformed_key_never_raises(self):
        """AC11: `ManifestError` is caught on every store path."""
        ds.record_deferral(self.REPO, 8, "filing", base_dir=self.base)
        bad = "not a repo"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(ds.record_deferral(bad, 8, "filing", base_dir=self.base))
            self.assertIsNone(ds.deferral_entry(bad, 8, base_dir=self.base))
            self.assertFalse(ds.discharge_deferral(bad, 8, "closed", base_dir=self.base))
            self.assertEqual(ds.read_deferrals([bad, self.REPO], base_dir=self.base), {(self.REPO, 8)})
        self.assertIsNotNone(ds.deferral_entry(self.REPO, 8, base_dir=self.base))

    def test_discharge_removes_the_entry(self):
        ds.record_deferral(self.REPO, 5, "filing", base_dir=self.base)
        self.assertTrue(ds.discharge_deferral(self.REPO, 5, "closed", base_dir=self.base))
        self.assertIsNone(ds.deferral_entry(self.REPO, 5, base_dir=self.base))
        self.assertFalse(ds.discharge_deferral(self.REPO, 5, "closed", base_dir=self.base))


if __name__ == "__main__":
    unittest.main()
