#!/usr/bin/env python3
"""AC5' integration test: l2_post.py's `main()` actually invokes the
shared belt-candidate recorder on a successful post, not merely "the
function exists somewhere in the module."

Run: python3 tools/gh/test_l2_post_belt_candidate.py
"""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import belt_candidates  # noqa: E402
import l2_post as lp  # noqa: E402


class L2PostBeltCandidateIntegrationTests(unittest.TestCase):
    def test_main_calls_the_recorder_with_repo_issue_kind_and_l2(self):
        argv = [
            "l2_post.py", "post", "--repo", "vitalharmony/hrse", "--issue", "1921",
            "--kind", "plan", "--status", "s", "--change", "c", "--next", "n",
            "--receipts", "[]", "--narrative-file", "/dev/null",
        ]
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch.object(lp, "lock_blocks", return_value=False), \
             unittest.mock.patch.object(lp, "load_receipts", return_value=[]), \
             unittest.mock.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}), \
             unittest.mock.patch.object(lp.belt_candidates, "record_candidate") as recorder:
            lp.main()
        recorder.assert_called_once_with("vitalharmony/hrse", 1921, "plan", "l2")


class L2PostFindingDoesNotRecordTests(unittest.TestCase):
    """harmonic-forge#691 preclose finding 1: `--kind finding` used to call
    `record_candidate` unconditionally, silently overwriting the one file
    per issue and evicting whatever queue-relevant kind (a Lane 1
    `ready-for-l3`, a Lane 2 `handoff`, ...) was recorded there -- the
    issue then vanished from `read_candidates` for every lane, with no
    error. `finding` is never a member of any lane's `QUEUE_KINDS`
    (`watch_lane_posts.py`), so it has nothing to contribute to the
    candidate store and must not touch it at all."""

    def _argv(self, kind: str) -> list[str]:
        return [
            "l2_post.py", "post", "--repo", "vitalharmony/hrse", "--issue", "1921",
            "--kind", kind, "--status", "s", "--change", "c", "--next", "n",
            "--receipts", "[]", "--narrative-file", "/dev/null",
        ]

    def test_main_does_not_call_the_recorder_for_kind_finding(self):
        with unittest.mock.patch.object(sys, "argv", self._argv("finding")), \
             unittest.mock.patch.object(lp, "lock_blocks", return_value=False), \
             unittest.mock.patch.object(lp, "load_receipts", return_value=[]), \
             unittest.mock.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}), \
             unittest.mock.patch.object(lp.belt_candidates, "record_candidate") as recorder:
            lp.main()
        recorder.assert_not_called()

    def test_end_to_end_a_finding_does_not_evict_a_queued_ready_for_l3(self):
        """The reporter's exact repro, against the real (temp-dir) store,
        `record_candidate` NOT mocked: a Lane 1 `ready-for-l3` is recorded,
        then Lane 2 posts a `finding` on the same issue via `l2_post.main()`
        -- `read_candidates` must still surface the issue as an l3
        candidate afterward."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with unittest.mock.patch.object(belt_candidates, "DEFAULT_CANDIDATES_DIR", base), \
                 unittest.mock.patch.object(lp.belt_candidates, "DEFAULT_CANDIDATES_DIR", base):
                belt_candidates.record_candidate(
                    "vitalharmony/hrse", 1921, "ready-for-l3", "l1")

                with unittest.mock.patch.object(sys, "argv", self._argv("finding")), \
                     unittest.mock.patch.object(lp, "lock_blocks", return_value=False), \
                     unittest.mock.patch.object(lp, "load_receipts", return_value=[]), \
                     unittest.mock.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}):
                    lp.main()

                candidates = belt_candidates.read_candidates(
                    ["vitalharmony/hrse"], "l3",
                    queue_kinds={"l3": ("ready-for-l3", "ae", "sweep", "ae-and-sweep")},
                    queue_posters={"l3": ("l1",)},
                )
            self.assertIn(("vitalharmony/hrse", 1921), candidates,
                           "a finding posted on top of a ready-for-l3 must not "
                           "evict it from the candidate store")


if __name__ == "__main__":
    unittest.main()
