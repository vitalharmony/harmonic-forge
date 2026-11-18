"""harmonic-forge#1401 (lane-status V1, S1a): one vocabulary for lane state.

AC1: every state key has owner, owes and next_lane.
AC2: `implemented.awaiting-gate` keeps its key and names the child in `subkey`.
AC3: the belt watcher's `KIND_OWNER`/`OWES` agree with the table through its
`kind` column.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "gh"))

import lane_state  # noqa: E402
import watch_lane_posts as wlp  # noqa: E402

OWNERS = {"l1", "l2", "l3", "CI", "operator", None}
LANES = {"l1", "l2", "l3", None}

READY_FOR_L3 = ("Ready for Lane 3.\n\n<!-- l1-post v1; kind=ready-for-l3; sha=abc; "
                f"body-sha256={'a' * 64}; checks=x -->")
LEGACY_READY = "Implemented.\n\n<!-- l1-post v1; kind=ready-for-l3; sha=abc -->"
L2D = "## L2D -- done\n\ntext"
HANDOFF = ("## Handoff: Issue #1 -- x\n\n### Delegated Judgment Calls\nnone\n\n"
           "<!-- l1-post v1; kind=handoff; plan-first=false; sha=abc; "
           f"body-sha256={'a' * 64}; checks=x -->")


def _record(*bodies: str) -> dict:
    """The shape `test_milestone_summary.LaneStateTests.record` builds."""
    return {"key": "H1", "number": 1, "title": "t", "repo": "vitalharmony/hrse",
            "fields": {"Tier": "standard"}, "deps": [], "scope": [],
            "tooling": None, "comments": [{"body": body} for body in bodies]}


class Vocabulary(unittest.TestCase):
    def test_every_state_key_has_owner_owes_and_next_lane(self) -> None:
        """AC1."""
        for key in lane_state.STATE_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, lane_state.VOCABULARY)
                entry = lane_state.VOCABULARY[key]
                self.assertIn(entry.owner, OWNERS)
                self.assertTrue(entry.owes)
                self.assertIn(entry.next_lane, LANES)

    def test_no_ci_state_keys_yet(self) -> None:
        """The four CI rows are R2's, after CI slice 6's handoff."""
        for key in lane_state.VOCABULARY:
            self.assertNotIn("ci", key.split(".")[0])


class Subkey(unittest.TestCase):
    def _state(self, *bodies: str) -> lane_state.LaneState:
        return lane_state.classify(_record(*bodies), [])

    def test_after_an_l2d_lane_1_owes_the_review(self) -> None:
        """AC2."""
        state = self._state(HANDOFF, L2D)
        self.assertEqual(state.key, lane_state.KEY_AWAITING_GATE)
        self.assertEqual(state.subkey, lane_state.SUBKEY_AWAITING_REVIEW)

    def test_after_a_ready_for_l3_lane_3_owes_the_spec(self) -> None:
        """AC2."""
        state = self._state(HANDOFF, L2D, READY_FOR_L3)
        self.assertEqual(state.key, lane_state.KEY_AWAITING_GATE)
        self.assertEqual(state.subkey, lane_state.SUBKEY_AWAITING_SPEC)

    def test_a_legacy_lane_2_footer_still_reads_as_the_review(self) -> None:
        """An unvalidated `ready-for-l3` footer was Lane 2's completion shape
        (hrse#1441, hrse#1573), not Lane 1's readiness post."""
        state = self._state(HANDOFF, LEGACY_READY)
        self.assertEqual(state.subkey, lane_state.SUBKEY_AWAITING_REVIEW)

    def test_other_keys_carry_no_subkey(self) -> None:
        self.assertIsNone(self._state(HANDOFF).subkey)


class AgreesWithTheWatcher(unittest.TestCase):
    #: `completion` is Lane 1's on the belt (`KIND_OWNER`) but queues nothing
    #: (absent from `QUEUE_KINDS` and `OWES`): Lane 1 picks an L2D up from
    #: its worktrees, not its queue.
    #: `blocked` likewise: Lane 1's on the belt, queued nowhere.
    OWES_EXEMPT = {"completion", "blocked"}

    def test_owner_matches_kind_owner(self) -> None:
        """AC3."""
        for key, entry in lane_state.VOCABULARY.items():
            if entry.kind is None:
                continue
            with self.subTest(key=key, kind=entry.kind):
                self.assertEqual(entry.owner, wlp.KIND_OWNER[entry.kind])

    def test_owes_matches_the_watcher(self) -> None:
        """AC3. A plan-first handoff renders `PLAN_FIRST_OWES` on the belt."""
        for key, entry in lane_state.VOCABULARY.items():
            if entry.kind is None or entry.kind in self.OWES_EXEMPT:
                continue
            with self.subTest(key=key, kind=entry.kind):
                expected = wlp.OWES[entry.owner][entry.kind]
                if key == lane_state.KEY_READY_PLAN:
                    expected = wlp.PLAN_FIRST_OWES
                self.assertEqual(entry.owes, expected)

    def test_every_queued_kind_has_a_row(self) -> None:
        """Every kind a lane's queue carries maps to some state here, except
        a bare `ae` and a bare `sweep`, which are halves of R-0208's one
        action and land on `gate.ae-without-sweep`/`gate.executable`."""
        kinds = {e.kind for e in lane_state.VOCABULARY.values()}
        for lane, queued in wlp.QUEUE_KINDS.items():
            for kind in queued:
                if kind in {"ae", "sweep"}:
                    continue
                with self.subTest(lane=lane, kind=kind):
                    self.assertIn(kind, kinds)



class Rendered(unittest.TestCase):
    """AC4: the summary row names the owner of the split state."""

    def test_the_row_names_the_owner(self) -> None:
        import milestone_summary_render as render
        record = {"key": "H1", "number": 1, "title": "t", "repo": "vitalharmony/hrse",
                  "tooling": None, "fields": {"Status": "Todo", "Tier": "fast"},
                  "lane": lane_state.AWAITING_GATE,
                  "lane_subkey": lane_state.SUBKEY_AWAITING_SPEC}
        self.assertIn("| L3 owes the spec]", render._line(record))
        record["lane_subkey"] = None
        self.assertNotIn("owes", render._line(record))

if __name__ == "__main__":
    unittest.main()
