#!/usr/bin/env python3
"""harmonic-forge#1457: l1_post stores an overlap ruling per branch pair, so
`--ack-overlap` is entered once.

The real `world_checks` and `post_kind` run; only their git/GitHub reads and
the comment publish are patched. The store is redirected to a temp directory
through `L1_POST_OVERLAP_RULINGS_DIR`. Run:
`python3 tools/gh/test_l1_post_overlap_rulings.py`."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import l1_post  # noqa: E402

REPO = "o/r"
ISSUE = 7
BRANCH = "feat/7-thing"
SIBLING = "feat/9-sibling"
OTHER = "feat/11-other"
TARGET, FROM = "b" * 40, "a" * 40
PASS_ID = 100
REASON = "ruling 2026-11-19: H7 merges first, H9 rebases"
BODY = (f"## Gate carry-forward — F7\n\n**Target:** {TARGET}\n"
        f"**Carried:** PASS {PASS_ID}\n**Next:** merge\n")


class OverlapRulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {l1_post.OVERLAP_RULINGS_ENV: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.active = {BRANCH, "main", SIBLING}
        self.overlaps = [(SIBLING, {"tools/x.py"})]

    # -- helpers ---------------------------------------------------------
    def store(self) -> list[dict]:
        return l1_post.load_overlap_rulings(REPO)

    def store_path(self) -> Path:
        return Path(self.tmp.name) / "o__r.json"

    def _patches(self, comment_body=None):
        def fake_run(*cmd, **_kwargs):
            out = f"{TARGET}\trefs/heads/{BRANCH}\n" if "ls-remote" in cmd else "c" * 40 + "\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")

        def default_comment_body(repo, issue, body):
            self.posted = body
            return f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-1", 1

        return [
            mock.patch.object(l1_post, "issue_is_open"),
            mock.patch.object(l1_post, "run", fake_run),
            mock.patch.object(l1_post, "changed_files", return_value={"tools/x.py"}),
            mock.patch.object(l1_post, "active_worktree_branches", side_effect=lambda: set(self.active)),
            mock.patch.object(l1_post, "sibling_overlaps",
                              side_effect=lambda files, sibs: [(s, set(o)) for s, o in self.overlaps if s in sibs]),
            mock.patch.object(l1_post, "comment_body", comment_body or default_comment_body),
            mock.patch.object(l1_post, "write_receipt"),
            mock.patch.object(l1_post, "_record_attrib"),
            mock.patch.object(l1_post, "_tasklist_owed", None),
            mock.patch.object(l1_post, "validate_sticky_wicket"),
        ]

    def post(self, ack_overlap, comment_body=None) -> str:
        self.posted = None
        patches = self._patches(comment_body)
        for p in patches:
            p.start()
        try:
            l1_post.post_kind(REPO, ISSUE, "carry-forward", BODY, TARGET, BRANCH,
                              ack_overlap=ack_overlap, carry_forward=(FROM, PASS_ID))
        finally:
            for p in reversed(patches):
                p.stop()
        return self.posted

    def world(self, ack_overlap=None):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return l1_post.world_checks(REPO, ISSUE, TARGET, BRANCH, ack_overlap=ack_overlap)
        finally:
            for p in reversed(patches):
                p.stop()

    # -- AC1 -------------------------------------------------------------
    def test_ack_records_ruling_keyed_by_sorted_pair(self):
        self.post(REASON)
        [ruling] = self.store()
        self.assertEqual(ruling["branches"], sorted((BRANCH, SIBLING)))
        self.assertEqual(ruling["reason"], REASON)
        self.assertEqual(ruling["paths"], ["tools/x.py"])

    def test_ruling_is_found_from_either_branch(self):
        self.post(REASON)
        self.assertIsNotNone(l1_post.find_overlap_ruling(self.store(), SIBLING, BRANCH))

    # -- AC2 -------------------------------------------------------------
    def test_later_post_passes_without_flag_and_names_stored_ruling(self):
        self.post(REASON)
        body = self.post(None)
        self.assertIn("### Sibling-overlap override (operator-acknowledged)", body)
        self.assertIn(f"active sibling branch {SIBLING!r} overlaps target files: tools/x.py", body)
        self.assertIn(f"acknowledged: stored ruling: {REASON}", body)

    def test_overlap_subset_of_stored_paths_passes(self):
        self.overlaps = [(SIBLING, {"tools/x.py", "tools/y.py"})]
        self.post(REASON)
        self.overlaps = [(SIBLING, {"tools/y.py"})]
        _, warnings = self.world()
        self.assertEqual(len(warnings), 1)

    # -- AC3 -------------------------------------------------------------
    def test_no_ruling_refuses_as_today(self):
        with self.assertRaises(SystemExit) as raised:
            self.post(None)
        self.assertIn(f"active sibling branch {SIBLING!r} overlaps target files", str(raised.exception))

    def test_new_path_outside_ruling_refuses(self):
        self.post(REASON)
        self.overlaps = [(SIBLING, {"tools/x.py", "tools/new.py"})]
        with self.assertRaises(SystemExit) as raised:
            self.world()
        self.assertIn("tools/new.py", str(raised.exception))

    def test_sibling_without_ruling_refuses(self):
        self.post(REASON)
        self.active.add(OTHER)
        self.overlaps = [(SIBLING, {"tools/x.py"}), (OTHER, {"tools/x.py"})]
        with self.assertRaises(SystemExit) as raised:
            self.world()
        self.assertIn(OTHER, str(raised.exception))

    def test_ruling_is_scoped_to_its_repo(self):
        self.post(REASON)
        self.assertEqual(l1_post.load_overlap_rulings("o/other"), [])

    # -- AC4 -------------------------------------------------------------
    def test_ruling_survives_a_rebase(self):
        """The key holds no SHA: a new target SHA with the same paths passes."""
        self.post(REASON)
        global TARGET
        old = TARGET
        TARGET = "d" * 40
        try:
            _, warnings = self.world()
        finally:
            TARGET = old
        self.assertIn("stored ruling", warnings[0])
        self.assertNotIn(old, json.dumps(self.store()))

    def test_ruling_dropped_when_a_branch_leaves_the_worktrees(self):
        self.post(REASON)
        self.active.discard(SIBLING)
        self.post(None)  # no overlap now; the post path prunes the stale ruling
        self.assertEqual(self.store(), [])
        self.active.add(SIBLING)  # a new branch reusing the name gets no ruling
        with self.assertRaises(SystemExit):
            self.world()

    # -- AC5 -------------------------------------------------------------
    def test_explicit_flag_wins_and_updates_reason(self):
        self.post(REASON)
        body = self.post("new ruling")
        self.assertIn("acknowledged: new ruling", body)
        self.assertNotIn("stored ruling", body)
        [ruling] = self.store()
        self.assertEqual(ruling["reason"], "new ruling")

    # -- write only after publish ------------------------------------------
    def test_world_checks_alone_writes_nothing(self):
        self.world(ack_overlap=REASON)
        self.assertFalse(self.store_path().exists())

    def test_dry_run_steps_write_nothing(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(l1_post, "refresh_main"):
                steps = l1_post._publish_checks(REPO, ISSUE, "handoff", TARGET, BRANCH,
                                                ack_overlap=REASON, ack_no_pr_required=None,
                                                grant_on_main=False)
                for step in steps:
                    step()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertFalse(self.store_path().exists())

    def test_failed_publish_writes_nothing(self):
        def refused(repo, issue, body):
            l1_post.fail("GitHub comment failed: boom")
        with self.assertRaises(SystemExit):
            self.post(REASON, comment_body=refused)
        self.assertFalse(self.store_path().exists())


if __name__ == "__main__":
    unittest.main()
