"""harmonic-forge#1018: Lane 1's own clean rebase of a Lane 2 branch.

Every case runs against a temporary bare origin plus clones; nothing touches
a real repo. The two network reads (newest lane marker, issue labels) are
stubbed."""
import json
import os
import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane1_rebase  # noqa: E402

BRANCH = "feat/7-thing"
ISSUE = 7


def sh(cwd, *args, check=True):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def commit(cwd, name, content, msg):
    Path(cwd, name).write_text(content)
    sh(cwd, "add", name)
    sh(cwd, "commit", "-q", "-m", msg)
    return sh(cwd, "rev-parse", "HEAD")


class _Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "LANE1_REBASE_WORKTREES": str(self.tmp / "worktrees"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)],
                       check=True, capture_output=True)
        sh(self.repo, "checkout", "-q", "-b", "main")
        commit(self.repo, "base.txt", "base\n", "base")
        sh(self.repo, "push", "-q", "origin", "main")
        sh(self.repo, "checkout", "-q", "-b", BRANCH)
        self.branch_sha = commit(self.repo, "feature.txt", "feature\n", "feature")
        sh(self.repo, "push", "-q", "origin", BRANCH)
        sh(self.repo, "checkout", "-q", "main")
        self.impl = self.tmp / "impl"
        sh(self.repo, "worktree", "add", "-q", str(self.impl), BRANCH)
        self.other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)],
                       check=True, capture_output=True)
        self.marker = mock.patch.object(lane1_rebase, "newest_lane_marker", return_value="l2.done")
        self.labels = mock.patch.object(lane1_rebase, "issue_labels", return_value={"bug"})
        self.marker.start()
        self.labels.start()
        self.addCleanup(self.marker.stop)
        self.addCleanup(self.labels.stop)

    def advance_main(self, name="main.txt", content="main\n"):
        sh(self.other, "checkout", "-q", "main")
        sh(self.other, "pull", "-q", "origin", "main")
        sha = commit(self.other, name, content, "main moves")
        sh(self.other, "push", "-q", "origin", "main")
        return sha

    def origin_branch(self):
        return sh(self.origin, "rev-parse", f"refs/heads/{BRANCH}")

    def local_branch(self):
        return sh(self.repo, "rev-parse", f"refs/heads/{BRANCH}")

    def run_main(self):
        return lane1_rebase.main([str(self.repo), str(ISSUE), BRANCH])

    def prep_left(self):
        return lane1_rebase.prep_path(str(self.repo), ISSUE).exists()

class Lane1RebaseTest(_Fixture):
    def test_clean_rebase_pushes_and_moves_impl_worktree(self):
        main_sha = self.advance_main()
        self.assertEqual(self.run_main(), 0)
        new = self.origin_branch()
        self.assertNotEqual(new, self.branch_sha)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "merge-base",
                                         "--is-ancestor", main_sha, new]).returncode, 0)
        self.assertEqual(self.local_branch(), new)
        self.assertEqual(sh(self.impl, "rev-parse", "HEAD"), new)
        self.assertFalse(self.prep_left())

    def test_conflict_exits_2_and_pushes_nothing(self):
        self.advance_main("feature.txt", "conflicting\n")
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.origin_branch(), self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertFalse(self.prep_left())
        # The prep worktree's rebase was aborted, not left mid-rebase: a
        # re-run reaches the conflict again rather than a stale-state error.
        self.assertEqual(self.run_main(), 2)

    def test_lease_mismatch_does_not_push(self):
        self.advance_main()
        real_fetch = lane1_rebase.fetch

        def fetch_then_race(repo_dir, branch):
            real_fetch(repo_dir, branch)
            sh(self.other, "fetch", "-q", "origin", BRANCH)
            sh(self.other, "checkout", "-q", "-b", "race", f"origin/{BRANCH}")
            commit(self.other, "race.txt", "race\n", "lane 2 pushed meanwhile")
            sh(self.other, "push", "-q", "origin", f"race:{BRANCH}")

        with mock.patch.object(lane1_rebase, "fetch", side_effect=fetch_then_race):
            self.assertEqual(self.run_main(), 1)
        racer = sh(self.other, "rev-parse", "race")
        self.assertEqual(self.origin_branch(), racer)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertFalse(self.prep_left())

    def test_branch_not_checked_out_update_ref(self):
        sh(self.repo, "worktree", "remove", str(self.impl))
        self.advance_main()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.local_branch(), self.origin_branch())
        self.assertNotEqual(self.local_branch(), self.branch_sha)

    def test_dirty_impl_worktree_exits_3(self):
        self.advance_main()
        Path(self.impl, "feature.txt").write_text("uncommitted\n")
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertEqual(Path(self.impl, "feature.txt").read_text(), "uncommitted\n")

    def test_rerun_after_crash_completes(self):
        self.advance_main()
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash after push")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        pushed = self.origin_branch()
        self.assertNotEqual(pushed, self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertFalse(self.prep_left())
        # The re-run takes the already-contains-main shortcut: no second
        # rebase, no second push, only the local-ref step.
        with mock.patch.object(lane1_rebase, "rebase_and_push",
                               wraps=lane1_rebase.rebase_and_push) as rebased:
            self.assertEqual(self.run_main(), 0)
        rebased.assert_not_called()
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), pushed)

    def test_pre_push_refusal_exits_5(self):
        self.advance_main()
        hooks = Path(sh(self.repo, "rev-parse", "--git-common-dir"))
        hooks = (hooks if hooks.is_absolute() else self.repo / hooks) / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\necho 'pre-push: no receipt' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.assertEqual(self.run_main(), 5)
        self.assertEqual(self.origin_branch(), self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertFalse(self.prep_left())

    def test_not_l2_done_exits_4(self):
        self.advance_main()
        with mock.patch.object(lane1_rebase, "newest_lane_marker", return_value="l1.rework"):
            self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_tooling_exception_exits_4(self):
        self.advance_main()
        with mock.patch.object(lane1_rebase, "issue_labels", return_value={"tooling-exception"}):
            self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_open_lane3_gate_on_old_sha_exits_4(self):
        self.advance_main()
        gate = self.tmp / "gate"
        sh(self.repo, "worktree", "add", "-q", "--detach", str(gate), self.branch_sha)
        Path(sh(gate, "rev-parse", "--absolute-git-dir"), "LANE3_ACTIVE").touch()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)


class PrecloseFixTests(unittest.TestCase):
    """harmonic-forge#1018 preclose pass 1: real markers, branch ownership, one run per issue."""

    def test_real_l2_completion_marker_is_l2_done(self):
        body = "## L2D\n\n<!-- l1-post v1; kind=completion; posted-by=LANE2 -->"
        watch = lane1_rebase._watch()
        with mock.patch.object(watch, "_fetch_all_comments", return_value=[{"body": body}]), \
                mock.patch.object(lane1_rebase, "repo_slug", return_value="o/r"):
            self.assertEqual(lane1_rebase.newest_lane_marker("/x", 7), "l2.done")

    def _marker_after(self, *footers):
        bodies = [{"body": "## L2D\n\n<!-- l1-post v1; kind=completion; posted-by=LANE2 -->"}]
        bodies += [{"body": f"## x\n\n<!-- l1-post v1; kind={k}; sha=a; body-sha256=b -->"} for k in footers]
        watch = lane1_rebase._watch()
        with mock.patch.object(watch, "_fetch_all_comments", return_value=bodies), \
                mock.patch.object(lane1_rebase, "repo_slug", return_value="o/r"):
            return lane1_rebase.newest_lane_marker("/x", 7)

    def test_lane1_ready_and_ae_after_l2d_keep_it_rebaseable(self):
        self.assertEqual(self._marker_after("ready-for-l3", "ae", "sweep"), "l2.done")

    def test_lane1_rework_after_l2d_blocks(self):
        self.assertNotEqual(self._marker_after("ready-for-l3", "rework"), "l2.done")

    def test_branch_of_another_issue_exits_4(self):
        with self.assertRaises(lane1_rebase.Exit) as caught:
            lane1_rebase.check_preconditions("/x", 7, "feat/1016-other", "a" * 40)
        self.assertEqual(caught.exception.code, 4)

    def test_concurrent_run_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            # _run_locked is stubbed to succeed, so only the lock can make run() exit 1.
            with mock.patch.object(lane1_rebase, "prep_path", return_value=Path(tmp) / "r-7-prep"), \
                    mock.patch.object(lane1_rebase, "_run_locked", return_value="ran"):
                self.assertEqual(lane1_rebase.run("/x", 7, "feat/7-thing"), "ran")
                lock = open(Path(tmp) / "r-7-prep.lock", "w")
                self.addCleanup(lock.close)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(lane1_rebase.Exit) as caught:
                    lane1_rebase.run("/x", 7, "feat/7-thing")
                self.assertEqual(caught.exception.code, 1)


L2D = "## L2D\n\n<!-- l1-post v1; kind=completion; posted-by=LANE2 -->"


def gate(verdict, sha, footer="kind=gate-result; posted-by=LANE3", in_heading=False):
    head = f"## Lane 3 Gate Results \u2014 {verdict}"
    if in_heading:
        return f"{head} at `{sha}`\n\nTCs ran.\n\n<!-- l1-post v1; {footer} -->"
    return f"{head}\n\nHead-SHA: `{sha}`\n\n<!-- l1-post v1; {footer} -->"


REWORK = "## x\n\n<!-- l1-post v1; kind=rework; sha=a; body-sha256=b -->"


class PassPreconditionTests(_Fixture):
    """harmonic-forge#1034: a Lane 3 PASS on the branch head is rebaseable.
    The setUp marker stub is lifted; real bodies run the verdict/SHA readers."""

    def setUp(self):
        super().setUp()
        self.marker.stop()
        self.bodies = []
        watch = lane1_rebase._watch()
        for patcher in (mock.patch.object(watch, "_fetch_all_comments",
                                          side_effect=lambda *a: [{"body": b} for b in self.bodies]),
                        mock.patch.object(lane1_rebase, "repo_slug", return_value="o/r")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_pass_on_head_rebases_and_pushes(self):
        self.bodies = [L2D, gate("PASS", self.branch_sha[:8], in_heading=True)]
        self.advance_main()
        self.assertEqual(self.run_main(), 0)
        self.assertNotEqual(self.origin_branch(), self.branch_sha)
        self.assertEqual(self.local_branch(), self.origin_branch())

    def test_b_fail_exits_4(self):
        self.bodies = [L2D, gate("FAIL", self.branch_sha)]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_c_pass_on_other_sha_exits_4(self):
        self.bodies = [L2D, gate("PASS", "0123456789abcdef0123456789abcdef01234567")]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_d_pass_then_rework_exits_4(self):
        self.bodies = [L2D, gate("PASS", self.branch_sha), REWORK]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def _crash_after_push(self):
        self.bodies = [L2D, gate("PASS", self.branch_sha)]
        self.advance_main()
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash after push")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        pushed = self.origin_branch()
        self.assertNotEqual(pushed, self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        return pushed

    def test_f_rerun_after_crash_on_pass_path_completes(self):
        pushed = self._crash_after_push()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), pushed)
        self.assertEqual(sh(self.impl, "rev-parse", "HEAD"), pushed)

    def test_f3_rerun_with_unpassed_commit_on_origin_exits_4(self):
        # preclose F1034 pass 1: origin carries the rebase of the PASS plus an
        # extra commit; the crash branch must not cover it.
        pushed = self._crash_after_push()
        sh(self.other, "fetch", "-q", "origin", BRANCH)
        sh(self.other, "checkout", "-q", "-B", "extra", f"origin/{BRANCH}")
        extra = commit(self.other, "unpassed.txt", "x\n", "not gated")
        sh(self.other, "push", "-q", "origin", f"extra:{BRANCH}")
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), extra)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertNotEqual(extra, pushed)

    def test_f2_rerun_with_local_at_third_sha_exits_4(self):
        pushed = self._crash_after_push()
        # Patch-equivalent to the PASS (reworded), so only the local-equals-
        # PASS condition can refuse it, not the receipt match.
        sh(self.impl, "commit", "-q", "--amend", "-m", "reworded")
        third = sh(self.impl, "rev-parse", "HEAD")
        self.assertNotEqual(third, self.branch_sha)
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), third)

    # Reforge r2 (operator ruling, F1034): a gate report is recognized by
    # ALLOWLIST only -- footer kind=gate-result AND posted-by=LANE3.

    def test_e_l2d_then_lane_unset_fail_is_not_a_gate_report(self):
        # Allowlist reality (replaces handoff test e): a FAIL stamped
        # posted-by=LANE-unset is a Lane 1 discussion, so the L2D stands.
        self.bodies = [L2D, gate("FAIL", self.branch_sha,
                                 footer="kind=discussion; posted-by=LANE-unset")]
        self.advance_main()
        self.assertEqual(self.run_main(), 0)

    def test_e2_footerless_fail_exits_4(self):
        # A footer-less FAIL classifies by its heading as an l3 marker that
        # is not l2.done; refused without ever being read as a gate report.
        self.bodies = [L2D, gate("FAIL", self.branch_sha).rsplit("\n\n<!--", 1)[0]]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_e3_footerless_pass_on_head_exits_4(self):
        self.bodies = [L2D, gate("PASS", self.branch_sha).rsplit("\n\n<!--", 1)[0]]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def _quoting_pass_exits_4(self, footer, before):
        self.bodies = before + ["Earlier:\n\n" + gate("PASS", self.branch_sha, footer=footer)]
        self.advance_main()
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), self.branch_sha)

    def test_g_rework_quoting_a_pass_heading_exits_4(self):
        self._quoting_pass_exits_4("kind=rework; sha=a; body-sha256=b",
                                   [L2D, gate("PASS", self.branch_sha)])

    def test_g2_l2b_quoting_a_pass_heading_exits_4(self):
        self._quoting_pass_exits_4("kind=blocked; posted-by=LANE2", [L2D])

    def test_g3_l2s_quoting_a_pass_heading_exits_4(self):
        self._quoting_pass_exits_4("kind=plan; posted-by=LANE2", [L2D])

    def test_g4_lane1_discussion_quoting_a_pass_heading_exits_4(self):
        self._quoting_pass_exits_4("kind=discussion; posted-by=LANE1",
                                   [L2D, gate("FAIL", self.branch_sha)])

    def test_g5_gate_result_not_posted_by_lane3_exits_4(self):
        # Kill target for the posted-by=LANE3 condition: a gate-result footer
        # stamped by Lane 1 is not Lane 3's verdict.
        self._quoting_pass_exits_4("kind=gate-result; posted-by=LANE1",
                                   [L2D, gate("FAIL", self.branch_sha)])

    def test_g6_l2d_quoting_a_fail_heading_is_still_l2_done(self):
        self.bodies = [L2D.replace("## L2D", "## L2D\n\n" + gate(
            "FAIL", self.branch_sha).rsplit("\n\n<!--", 1)[0])]
        self.advance_main()
        self.assertEqual(self.run_main(), 0)

    def _replace_origin(self, build):
        sh(self.other, "fetch", "-q", "origin")
        sha = build()
        sh(self.other, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")
        return sha

    def test_f4_rerun_with_merge_commit_on_origin_exits_4(self):
        # F1034 pass 2 #2: origin is the PASS merged with main, and the merge
        # resolution adds an ungated file; no receipt names that SHA.
        self._crash_after_push()

        def build():
            sh(self.other, "checkout", "-q", "-B", "m", self.branch_sha)
            sh(self.other, "merge", "-q", "--no-commit", "origin/main")
            Path(self.other, "ungated.txt").write_text("x\n")
            sh(self.other, "add", "ungated.txt")
            sh(self.other, "commit", "-q", "-m", "merge")
            return sh(self.other, "rev-parse", "HEAD")
        merged = self._replace_origin(build)
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), merged)
        self.assertEqual(self.local_branch(), self.branch_sha)

    def test_f5_rerun_same_count_different_content_exits_4(self):
        # F1034 pass 2 #3: one commit on main, as the rebase would be, but
        # with different content.
        self._crash_after_push()

        def build():
            sh(self.other, "checkout", "-q", "-B", "s", "origin/main")
            return commit(self.other, "feature.txt", "different\n", "feature")
        other = self._replace_origin(build)
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), other)
        self.assertEqual(self.local_branch(), self.branch_sha)

    # Preclose F1034 reforge pass 1 #2: each receipt-match term isolated.

    def test_f6_rerun_pass_names_other_sha_than_receipt_exits_4(self):
        pushed = self._crash_after_push()
        self.bodies = [L2D, gate("PASS", "0123456789abcdef0123456789abcdef01234567")]
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), self.branch_sha)

    def test_f7_rerun_receipt_for_another_issue_exits_4(self):
        pushed = self._crash_after_push()
        path = lane1_rebase.receipt_path(str(self.repo), ISSUE)
        data = json.loads(path.read_text())
        data["issue"] = ISSUE + 1
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertEqual(self.origin_branch(), pushed)

    def test_f8_double_crash_after_main_moves_completes(self):
        # Pass 1 #1: a re-rebase carries the passed SHA forward.
        first = self._crash_after_push()
        self.advance_main("main2.txt", "again\n")
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash again")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        second = self.origin_branch()
        self.assertNotEqual(second, first)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.local_branch(), second)
        self.assertIsNone(lane1_rebase.read_receipt(str(self.repo), ISSUE))

    def test_receipt_dropped_after_local_move(self):
        self._crash_after_push()
        self.assertIsNotNone(lane1_rebase.read_receipt(str(self.repo), ISSUE))
        self.assertEqual(self.run_main(), 0)
        self.assertIsNone(lane1_rebase.read_receipt(str(self.repo), ISSUE))


if __name__ == "__main__":
    unittest.main()
