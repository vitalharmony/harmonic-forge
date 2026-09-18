#!/usr/bin/env python3
"""AC5' integration test: l2_post.py's `main()` actually invokes the
shared belt-candidate recorder on a successful post, not merely "the
function exists somewhere in the module."

Run: python3 tools/gh/test_l2_post_belt_candidate.py
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
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


if __name__ == "__main__":
    unittest.main()
