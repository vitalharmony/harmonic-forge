#!/usr/bin/env python3
"""Tests for the belt wake-up hook (harmonic-forge#518 AC7)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import belt_wakeup  # noqa: E402
from belt_wakeup import build_wakeup, handle  # noqa: E402


class TestWakeupStatesTheRole(unittest.TestCase):
    def test_each_lane_gets_its_own_role_line(self):
        for lane in ("1", "2", "3"):
            text = build_wakeup(lane, "env")
            self.assertIsNotNone(text)
            self.assertIn(f"LANE={lane}", text)

    def test_lane_2_is_not_told_it_may_merge_or_close(self):
        """The defect the whole issue exists for, in the one place a session
        reads before anything else."""
        text = build_wakeup("2", "env").lower()
        self.assertIn("never push, merge, close", text)
        self.assertNotIn("ae-and-sweep;", text)

    def test_lane_1_carries_its_merge_authority(self):
        self.assertIn("merge and close", build_wakeup("1", "env"))


class TestBeltDoesNotAutoArm(unittest.TestCase):
    """AC7's second half, and the operator decision behind it."""

    def test_every_role_line_says_not_armed(self):
        for lane in ("1", "2", "3"):
            self.assertIn("NOT armed", build_wakeup(lane, "env"))

    def test_the_hook_arms_nothing(self):
        """A source-level assertion, because this is the kind of thing that
        gets added later 'for convenience' and reverses an operator decision
        silently."""
        src = (Path(__file__).parent / "belt_wakeup.py").read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1]  # skip the module docstring
        for forbidden in ("Monitor(", "CronCreate", "subprocess.run", "os.system"):
            self.assertNotIn(forbidden, body, f"wake-up must not arm anything: {forbidden}")


class TestNoLaneMeansNoProtocols(unittest.TestCase):
    def setUp(self):
        """`handle()` records every fire now, so any test calling it writes to
        the operator's REAL log unless redirected — and that log is the artifact
        this issue designates as its evidence. Nine fabricated records with
        `"source": null`, shape-indistinguishable from a genuine no-lane fire,
        were already sitting in it from gate runs before this was caught."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(belt_wakeup, "FIRE_LOG",
                                    Path(tmp.name) / "fires.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unset_lane_produces_nothing(self):
        self.assertIsNone(build_wakeup("", "none"))
        self.assertIsNone(build_wakeup("unknown", "none"))

    def test_handle_is_silent_rather_than_erroring(self):
        """A no-lane session is normal, not a fault."""
        import os
        saved = os.environ.pop("LANE", None)
        try:
            self.assertEqual(handle({"cwd": "/tmp"}), {})
        finally:
            if saved is not None:
                os.environ["LANE"] = saved


class TestCompactionSeamCarriesIt(unittest.TestCase):
    """AC7: fires at SessionStart *and again* after compaction. A compacted
    session never sees `belt_wakeup`, because a different hook owns that
    matcher."""

    def test_build_context_mentions_the_skill(self):
        from compaction_marker import build_context
        text = build_context("2026-09-09T00:00:00Z", "2",
                             "/home/mmangus/Harmonic_Projects/HRSE2-lane2")
        self.assertIn("/belt-and-suspenders", text)
        self.assertIn("not armed", text.lower())



class TestSessionStartSourceCoverage(unittest.TestCase):
    """harmonic-forge#560: the hook worked and was silent for the way lane
    sessions are actually launched.

    `clear` is a distinct `SessionStart` source, not a variant of `startup`, so
    `startup|resume` never fired for `lane2 /clear`. Asserted against the
    settings file rather than described in prose, because a matcher string is
    exactly the kind of thing edited by someone who does not know which of the
    two candidate mechanisms they are working against.

    **Scoped to THIS repo's own settings file, found relative to this test.**
    The first draft walked `~/Harmonic_Projects/<repo>/.claude/settings.json`
    for all four consuming repos — which is operator-local state, exactly the
    class harmonic-forge#504 exists to prevent and exactly the trap that turned
    harmonic-forge#552's CI red one issue earlier. A repo's CI can only speak
    for that repo. AC3's cross-repo claim is NOT carried by this file — it lives only in
    harmonic-forge, and an earlier draft of this docstring claimed it travelled
    "to each repo that has one", which no repo does. The cross-repo guard is
    `forge_onboard.check_hooks`, which reads every declared project's settings
    AND every worktree's.
    """

    #: `compact` is deliberately absent — it has its own entry running
    #: `compaction_marker.py`, and matching it here would inject twice.
    REQUIRED_SOURCES = {"startup", "resume", "clear", "fork"}

    def setUp(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"
        if not path.is_file():
            self.skipTest(f"no settings at {path}")
        self.settings = json.loads(path.read_text(encoding="utf-8"))

    def _matcher_for(self, script):
        for block in (self.settings.get("hooks") or {}).get("SessionStart") or []:
            for hook in block.get("hooks") or []:
                if script in hook.get("command", ""):
                    return block.get("matcher", "")
        return None

    def test_the_wakeup_matches_clear_and_fork(self):
        """AC1. `lane<N> /clear` is a normal launch across all three lanes, and
        a safety mechanism a normal launch disables is not one. `fork` is here
        for the same reason: excluding it rebuilds this bug on another path."""
        matcher = self._matcher_for("belt_wakeup.py")
        self.assertIsNotNone(matcher, "belt_wakeup is not wired in this repo")
        self.assertLessEqual(self.REQUIRED_SOURCES, set(matcher.split("|")))

    def test_compact_is_excluded_from_the_wakeup_matcher(self):
        """Not an oversight. `compaction_marker.build_context()` already
        carries a wake-up line, so matching `compact` here injects twice."""
        matcher = self._matcher_for("belt_wakeup.py") or ""
        self.assertNotIn("compact", matcher.split("|"))

    def test_the_compaction_marker_is_wired_on_compact(self):
        """AC4, for THIS repo only — which is all this assertion can see.

        cymagraph-infra and openclaw-projects also carried no `compact` entry
        and both now have one, but nothing here verifies that: an earlier
        docstring claimed it did, and deleting the new block from either repo
        left this test green. The mechanical guard for the cross-repo half is
        `forge_onboard.check_hooks`, which reads every declared project's
        settings AND every worktree's. This is the local half only.
        """
        self.assertEqual(self._matcher_for("compaction_marker.py"), "compact")


class TestFireLog(unittest.TestCase):
    """The fire log is #560's acceptance test, made mechanical.

    AC1 says "verified by asking a fresh session what told it its lane, not by
    reading configuration". A session's self-report is the weakest evidence
    this platform accepts, and #560 exists because one such report was wrong.
    The log records what actually happened instead.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "fires.jsonl"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_it_records_the_sessionstart_source_not_the_lane_source(self):
        """Two different things, and conflating them would make the log answer
        a question nobody asked. `resolve_lane`'s source says HOW the lane was
        determined ("env"); the payload's says which session-start event fired
        ("clear")."""
        belt_wakeup.record_fire({"source": "clear", "cwd": "/x"}, "2", True,
                                path=self.path)
        entry = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(entry["source"], "clear")
        self.assertEqual(entry["lane"], "2")
        self.assertTrue(entry["injected"])

    def test_a_no_lane_session_is_recorded_as_not_injected(self):
        """The discriminating record: fired, but injected nothing."""
        belt_wakeup.record_fire({"source": "startup", "cwd": "/x"}, None, False,
                                path=self.path)
        entry = json.loads(self.path.read_text().splitlines()[0])
        self.assertIsNone(entry["lane"])
        self.assertFalse(entry["injected"])

    def test_it_never_raises_and_reports_failure(self):
        """A logging failure must never stop a session from starting."""
        self.assertFalse(
            belt_wakeup.record_fire({}, "1", True,
                                    path=Path("/proc/nonexistent/fires.jsonl")))

    def test_the_log_is_bounded(self):
        for _ in range(belt_wakeup.FIRE_LOG_MAX + 20):
            belt_wakeup.record_fire({"source": "startup"}, "1", True,
                                    path=self.path)
        self.assertLessEqual(len(self.path.read_text().splitlines()),
                             belt_wakeup.FIRE_LOG_MAX)

    def test_handle_records_a_fire_even_when_it_injects_nothing(self):
        """Silence is the symptom under investigation, so silence must leave a
        trace. A hook that logs only when it speaks cannot answer "did it
        fire?" — which is #560's entire question."""
        with mock.patch.object(belt_wakeup, "FIRE_LOG", self.path), \
                mock.patch.dict(os.environ, {"LANE": ""}, clear=False):
            belt_wakeup.handle({"source": "clear", "cwd": "/tmp"})
        self.assertTrue(self.path.is_file())
        self.assertEqual(
            json.loads(self.path.read_text().splitlines()[0])["source"], "clear")


if __name__ == "__main__":
    # MUST stay last. It sat mid-file, so `python3 test_belt_wakeup.py` ran only
    # the 8 classes above it and printed OK while every harmonic-forge#560
    # assertion below went unexecuted — including with the matcher reverted.
    # Verifying this hook the obvious way returned a green that asserted
    # nothing about the bug it was written for.
    unittest.main(verbosity=2)
