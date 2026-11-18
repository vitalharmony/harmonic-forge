#!/usr/bin/env python3
"""harmonic-forge#1398 (round 2, sticky-wicket PATCH item 4/5): behavior tests, not AST position
checks. Each posting tool's success path runs against a temporary HOME and the lane's
`last_action` file is read back. A later step that fails (receipt, candidate record, task list)
must not leave the idle clock unchanged, because the action is recorded right after the post is
confirmed. A refused or failed post records nothing.

Run: python3 tools/gh/test_lane_action_recording.py
"""
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import afk_toggle  # noqa: E402
import l1_post  # noqa: E402
import l2_post as lp  # noqa: E402
import post_lane_discussion as pld  # noqa: E402


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorded(self, lane: str) -> bool:
        return afk_toggle.last_action_age(lane, now=4_000_000_000.0) is not None


class L2PostRecords(_Home):
    ARGV = ["l2_post.py", "post", "--repo", "vitalharmony/hrse", "--issue", "1921", "--kind", "plan",
            "--status", "s", "--change", "c", "--next", "n", "--receipts", "[]",
            "--narrative-file", "/dev/null"]

    def test_a_confirmed_post_records_lane_two(self):
        with mock.patch.object(sys, "argv", self.ARGV), \
                mock.patch.object(lp, "apply_project_identity"), \
                mock.patch.object(lp, "lock_blocks", return_value=False), \
                mock.patch.object(lp, "load_receipts", return_value=[]), \
                mock.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}), \
                mock.patch.object(lp, "_record_attrib"), \
                mock.patch.object(lp.belt_candidates, "record_candidate"), \
                mock.patch.object(lp.tasklist_owed, "record_post"):
            lp.main()
        self.assertTrue(self.recorded("2"))
        self.assertFalse(self.recorded("1"))

    def test_a_later_step_failing_still_leaves_the_action_recorded(self):
        for failing in ("_record_attrib", "candidate", "tasklist"):
            with self.subTest(failing=failing):
                (self.home / ".claude" / "state").mkdir(parents=True, exist_ok=True)
                for f in (self.home / ".claude" / "state").glob("*last_action*"):
                    f.unlink()
                boom = mock.Mock(side_effect=RuntimeError("later step failed"))
                with mock.patch.object(sys, "argv", self.ARGV), \
                        mock.patch.object(lp, "apply_project_identity"), \
                mock.patch.object(lp, "lock_blocks", return_value=False), \
                        mock.patch.object(lp, "load_receipts", return_value=[]), \
                        mock.patch.object(lp, "post", return_value={"comment_id": 1, "url": "u"}), \
                        mock.patch.object(lp, "_record_attrib",
                                          boom if failing == "_record_attrib" else mock.Mock()), \
                        mock.patch.object(lp.belt_candidates, "record_candidate",
                                          boom if failing == "candidate" else mock.Mock()), \
                        mock.patch.object(lp.tasklist_owed, "record_post",
                                          boom if failing == "tasklist" else mock.Mock()):
                    with self.assertRaises(RuntimeError):
                        lp.main()
                self.assertTrue(self.recorded("2"), f"{failing} failing must not lose the action")

    def test_a_failed_post_records_nothing(self):
        with mock.patch.object(sys, "argv", self.ARGV), \
                mock.patch.object(lp, "apply_project_identity"), \
                mock.patch.object(lp, "lock_blocks", return_value=False), \
                mock.patch.object(lp, "load_receipts", return_value=[]), \
                mock.patch.object(lp, "post", side_effect=RuntimeError("post refused")):
            with self.assertRaises(RuntimeError):
                lp.main()
        self.assertFalse(self.recorded("2"))


class PostLaneDiscussionRecords(_Home):
    def post(self, lane, comment_side_effect=None):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("just chatting")
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge",
                    "--issue", "618", "--file", str(body), "--kind", "discussion"]
            kw = {"side_effect": comment_side_effect} if comment_side_effect else {
                "return_value": ("https://example/1", 1)}
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"LANE": lane}), \
                    mock.patch.object(pld, "apply_project_identity"), \
                    mock.patch.object(pld, "comment_body", **kw), \
                    mock.patch.object(pld, "belt_candidates"):
                pld.main()

    def test_a_confirmed_post_records_the_posting_lane(self):
        self.post("3")
        self.assertTrue(self.recorded("3"))
        self.assertFalse(self.recorded("2"))

    def test_a_failed_post_records_nothing(self):
        with self.assertRaises(RuntimeError):
            self.post("2", comment_side_effect=RuntimeError("refetch failed"))
        self.assertFalse(self.recorded("2"))


class L1PostRecords(_Home):
    def run_post(self, receipt_fails: bool, comment_fails: bool = False):
        comment = (mock.patch.object(l1_post, "comment_body", side_effect=RuntimeError("post failed"))
                   if comment_fails else
                   mock.patch.object(l1_post, "comment_body", return_value=("https://example/1", 1)))
        receipt = mock.patch.object(l1_post, "write_receipt",
                                    side_effect=OSError("receipt store full") if receipt_fails
                                    else None)
        with comment, receipt, \
                mock.patch.object(l1_post, "world_checks", return_value=([], [])), \
                mock.patch.object(l1_post, "validate_sticky_wicket"), \
                mock.patch.object(l1_post, "_tasklist_owed", None), \
                mock.patch.object(l1_post, "_discharge_handoff_owed"):
            l1_post._post_kind("vitalharmony/harmonic-forge", 1412, "sweep",
                               "## Gate-readiness sweep\n\nbody\n", "a" * 40, "main")

    def test_a_confirmed_post_records_lane_one_even_when_the_receipt_write_fails(self):
        with self.assertRaises(OSError):
            self.run_post(receipt_fails=True)
        self.assertTrue(self.recorded("1"))

    def test_a_confirmed_post_records_lane_one(self):
        self.run_post(receipt_fails=False)
        self.assertTrue(self.recorded("1"))

    def test_a_failed_post_records_nothing(self):
        with self.assertRaises(RuntimeError):
            self.run_post(receipt_fails=False, comment_fails=True)
        self.assertFalse(self.recorded("1"))


class RecordFailureIsReported(_Home):
    def test_an_unwritable_state_dir_prints_to_stderr_and_returns_false(self):
        with mock.patch.object(afk_toggle, "_atomic_write", side_effect=OSError("disk full")), \
                mock.patch("sys.stderr") as err:
            self.assertFalse(afk_toggle.record_lane_action("1"))
        printed = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        self.assertIn("lane action not recorded", printed)


if __name__ == "__main__":
    unittest.main()
