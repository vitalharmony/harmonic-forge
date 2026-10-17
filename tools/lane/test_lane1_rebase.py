"""harmonic-forge#1018: Lane 1's own clean rebase of a Lane 2 branch.

Every case runs against a temporary bare origin plus clones; nothing touches
a real repo. The two network reads (newest lane marker, issue labels) are
stubbed."""
import contextlib
import io
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

real_run_task = lane1_rebase.run_task

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

    def test_f2_rerun_with_local_at_third_sha_exits_3(self):
        # Operator ruling amending 6219927415 (sticky-wicket PATCH): the
        # local branch is no longer a PASS condition, so the receipt covers
        # the re-run and move_local_ref decides. A third local commit that
        # origin lacks is unpushed work: exit 3, both refs untouched.
        pushed = self._crash_after_push()
        third = commit(self.impl, "local-only.txt", "x\n", "local only")
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), third)

    def test_f14_rerun_with_local_merge_commit_exits_3(self):
        # Operator ruling F1034 option A: `git cherry` skips merges, so a
        # local merge with unique edits must be refused, not reset away.
        self._crash_after_push()
        sh(self.impl, "fetch", "-q", "origin", "main")
        sh(self.impl, "merge", "-q", "--no-ff", "--no-commit", "FETCH_HEAD")
        (self.impl / "merge-edit.txt").write_text("unique\n")
        sh(self.impl, "add", "merge-edit.txt")
        sh(self.impl, "commit", "-q", "-m", "merge main with unique edit")
        merge = self.local_branch()
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.local_branch(), merge)

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

    # Sticky-wicket PATCH (F1034): receipt keyed by the PASS Head-SHA.

    def test_f9_new_pass_on_recorded_rebased_head_then_crash_completes(self):
        first = self._crash_after_push()  # stale receipt {A, [first]}
        self.bodies = [L2D, gate("PASS", first)]
        self.advance_main("main3.txt", "three\n")
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash on new PASS")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        second = self.origin_branch()
        self.assertNotEqual(second, first)
        receipt = lane1_rebase.read_receipt(str(self.repo), ISSUE)
        self.assertEqual(receipt["passed_sha"], first)
        self.assertEqual(receipt["rebased_shas"], [second])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.local_branch(), second)

    def test_f10_crash_between_local_move_and_receipt_drop_completes(self):
        self.bodies = [L2D, gate("PASS", self.branch_sha)]
        self.advance_main()
        with mock.patch.object(lane1_rebase, "drop_receipt",
                               side_effect=RuntimeError("crash before drop")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        pushed = self.origin_branch()
        self.assertEqual(self.local_branch(), pushed)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.local_branch(), pushed)
        self.assertIsNone(lane1_rebase.read_receipt(str(self.repo), ISSUE))

    def test_f11_receipt_names_other_passed_sha_exits_4(self):
        pushed = self._crash_after_push()
        path = lane1_rebase.receipt_path(str(self.repo), ISSUE)
        data = json.loads(path.read_text())
        data["passed_sha"] = "0123456789abcdef0123456789abcdef01234567"
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_main(), 4)
        self.assertEqual(self.origin_branch(), pushed)

    def test_f12_l2_done_rebase_writes_no_receipt(self):
        self.bodies = [L2D]
        self.advance_main()
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        self.assertIsNone(lane1_rebase.read_receipt(str(self.repo), ISSUE))

    def test_f13_same_pass_unrelated_old_writes_fresh_receipt(self):
        # Carry-forward guard: the prior receipt names this PASS, but `old`
        # is not a SHA it says was pushed, so the receipt starts fresh.
        lane1_rebase.receipt_path(str(self.repo), ISSUE).write_text(json.dumps(
            {"issue": ISSUE, "passed_sha": self.branch_sha,
             "rebased_shas": ["0123456789abcdef0123456789abcdef01234567"]}))
        self.bodies = [L2D, gate("PASS", self.branch_sha)]
        self.advance_main()
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        receipt = lane1_rebase.read_receipt(str(self.repo), ISSUE)
        self.assertEqual(receipt["rebased_shas"], [self.origin_branch()])


PKG = "frontend/package.json"


def pkg(version, dep="1.0.0", private="true"):
    return ('{\n  "name": "app",\n'
            f'  "private": {private},\n'
            f'  "version": "{version}",\n  "type": "module",\n'
            '  "scripts": {\n    "dev": "vite",\n    "build": "vite build",\n'
            '    "lint": "eslint ."\n  },\n'
            f'  "dependencies": {{\n    "a": "{dep}"\n  }}\n}}\n')


class VersionConflictTests(_Fixture):
    """harmonic-forge#1035: a conflict confined to package.json's top-level
    version resolves to origin/main's version plus one patch."""

    def setUp(self):
        super().setUp()
        # Put package.json on main, then rebuild the branch on top of it.
        sh(self.other, "checkout", "-q", "main")
        sh(self.other, "pull", "-q", "origin", "main")
        self.write(self.other, {PKG: pkg("2.9.40")}, "add package.json")
        sh(self.other, "push", "-q", "origin", "main")
        sh(self.impl, "fetch", "-q", "origin")
        sh(self.impl, "reset", "-q", "--hard", "origin/main")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")

    def write(self, cwd, files, msg):
        for name, content in files.items():
            path = Path(cwd, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            sh(cwd, "add", name)
        sh(cwd, "commit", "-q", "-m", msg)
        return sh(cwd, "rev-parse", "HEAD")

    def on_branch(self, files, msg):
        sha = self.write(self.impl, files, msg)
        sh(self.impl, "push", "-q", "origin", f"HEAD:{BRANCH}")
        self.branch_sha = sha
        return sha

    def on_main(self, files):
        sh(self.other, "pull", "-q", "origin", "main")
        self.write(self.other, files, "main moves")
        sh(self.other, "push", "-q", "origin", "main")

    def run_captured(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.run_main()
        return code, out.getvalue(), err.getvalue()

    def head_version(self):
        return json.loads(sh(self.origin, "show", f"refs/heads/{BRANCH}:{PKG}"))["version"]

    def assert_refused(self, code, err):
        self.assertEqual(code, 2, err)
        self.assertEqual(self.origin_branch(), self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        self.assertFalse(self.prep_left())

    def test_a_version_only_conflict_resolves_and_pushes(self):
        self.on_branch({"code.py": "x = 1\n", PKG: pkg("2.9.41")}, "feature + Auto-bump")
        self.on_main({"main.txt": "m\n", PKG: pkg("2.9.42")})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head_version(), "2.9.43")
        self.assertEqual(sh(self.origin, "show", f"refs/heads/{BRANCH}:code.py"), "x = 1")
        self.assertEqual(out.splitlines()[-1], "version-resolved=2.9.43")
        self.assertTrue(out.splitlines()[-2].startswith("new="))
        self.assertEqual(self.local_branch(), self.origin_branch())

    def test_b_two_bumps_resolve_to_same_target_second_dropped(self):
        self.on_branch({"code.py": "x = 1\n", PKG: pkg("2.9.41")}, "feature + bump")
        self.on_branch({PKG: pkg("2.9.42")}, "second bump")
        self.on_main({PKG: pkg("2.9.42")})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head_version(), "2.9.43")
        own = sh(self.origin, "rev-list", "--count", f"main..refs/heads/{BRANCH}")
        self.assertEqual(own, "1")
        self.assertIn("version-resolved=2.9.43", out)

    def test_c_dependency_change_exits_2(self):
        self.on_branch({PKG: pkg("2.9.41", dep="2.0.0")}, "bump + dep")
        self.on_main({PKG: pkg("2.9.42")})
        self.assert_refused(*self.run_captured()[::2])

    def test_d_second_conflicting_path_exits_2(self):
        self.on_branch({"feature.txt": "branch\n", PKG: pkg("2.9.41")}, "bump + feature")
        self.on_main({"feature.txt": "main\n", PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("feature.txt", err)

    def test_d2_second_version_only_json_not_named_package_json_exits_2(self):
        self.on_main({"app.json": pkg("2.9.40")})
        sh(self.impl, "pull", "-q", "--rebase", "origin", "main")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")
        self.on_branch({"app.json": pkg("2.9.41"), PKG: pkg("2.9.41")}, "bump both")
        self.on_main({"app.json": pkg("2.9.42"), PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("app.json", err)

    def test_e_non_semver_version_exits_2(self):
        self.on_branch({PKG: pkg("next")}, "bump")
        self.on_main({PKG: pkg("2.9.42")})
        self.assert_refused(*self.run_captured()[::2])

    def test_f_clean_rebase_prints_no_version_line(self):
        self.on_branch({"code.py": "x = 1\n"}, "feature")
        self.on_main({"main.txt": "m\n"})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertNotIn("version-resolved=", out)

    def test_g_bump_then_other_conflict_exits_2(self):
        self.on_branch({PKG: pkg("2.9.41")}, "bump only")
        self.on_branch({"feature.txt": "branch\n"}, "feature")
        self.on_main({"feature.txt": "main\n", PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("feature.txt", err)

    def test_h_minor_bump_on_branch_exits_2(self):
        self.on_main({PKG: pkg("2.9.46")})
        sh(self.impl, "fetch", "-q", "origin")
        self.on_branch({PKG: pkg("2.10.0")}, "minor bump")
        # Base the conflict: main moves again after the branch forked.
        self.on_main({PKG: pkg("2.9.47")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("2.10.0", err)
        self.assertIn("2.9.47", err)


    def test_c2_bool_vs_int_is_a_difference_exits_2(self):
        # preclose F1035 pass 1 #2: Python's `True == 1` must not resolve.
        self.on_branch({PKG: pkg("2.9.41", private="1")}, "bump + private=1")
        self.on_main({PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("differs beyond the top-level version", err)

    def test_c3_out_of_range_numbers_differ_exits_2(self):
        # preclose F1035 pass 2 #1: 1e400 and 1e401 both parse to Infinity.
        self.on_branch({PKG: pkg("2.9.41", private="1e401")}, "bump + 1e401")
        self.on_main({PKG: pkg("2.9.42", private="1e400")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("differs beyond the top-level version", err)

    def test_c4_string_vs_number_differs_exits_2(self):
        # post-verdict F1035 survivor: "1" and 1 must not compare equal.
        self.on_branch({PKG: pkg("2.9.41", private='"1"')}, 'bump + "1"')
        self.on_main({PKG: pkg("2.9.42", private="1")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("differs beyond the top-level version", err)

    def test_c5_whitespace_only_difference_exits_2(self):
        # Operator ruling on #1035: plain text compare, formatting included.
        self.on_branch({PKG: pkg("2.9.41").replace('"name": "app"', '"name":  "app"')},
                       "bump + reformat")
        self.on_main({PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("differs beyond the top-level version", err)

    def test_c6_inline_top_version_plus_nested_version_exits_2(self):
        # post-verdict F1035 survivor 1: the line regex finds the nested
        # "version", not the inline top-level one; T must never land there.
        def nested(top, inner):
            return (f'{{"name": "app", "version": "{top}",\n  "engines": {{\n'
                    f'    "version": "{inner}"\n  }}\n}}\n')
        self.on_main({PKG: nested("2.9.40", "1.0.0")})
        sh(self.impl, "pull", "-q", "--rebase", "origin", "main")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")
        self.on_branch({PKG: nested("2.9.40", "1.0.1")}, "nested bump")
        self.on_main({PKG: nested("2.9.40", "1.0.2")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("not exactly one top-level version line per side", err)

    def test_c7_crlf_vs_lf_differs_exits_2(self):
        # post-verdict F1035 survivor 2: text mode translated CRLF away.
        self.on_branch({PKG: pkg("2.9.41").replace("\n", "\r\n")}, "bump + CRLF")
        self.on_main({PKG: pkg("2.9.42")})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("differs beyond the top-level version", err)

    def _resolve_then_crash(self):
        self.on_branch({"code.py": "x = 1\n", PKG: pkg("2.9.41")}, "feature + bump")
        self.on_main({PKG: pkg("2.9.42")})
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash after push")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        pushed = self.origin_branch()
        self.assertNotEqual(pushed, self.branch_sha)
        self.assertEqual(self.local_branch(), self.branch_sha)
        return pushed

    def test_i_crash_rerun_after_resolution_exits_3(self):
        # Sticky-wicket PATCH (F1035): nothing persists across runs, so the
        # resolved commit is not patch-equivalent to local: fail closed.
        pushed = self._resolve_then_crash()
        code, _, err = self.run_captured()
        self.assertEqual(code, 3, err)
        self.assertEqual(self.origin_branch(), pushed)
        self.assertEqual(self.local_branch(), self.branch_sha)


LOCK = "frontend/package-lock.json"


def lock(version, dep="1.0.0"):
    return ('{\n  "name": "app",\n'
            f'  "version": "{version}",\n  "lockfileVersion": 3,\n'
            '  "packages": {\n    "": {\n      "name": "app",\n'
            f'      "version": "{version}"\n    }},\n'
            '    "node_modules/a": {\n'
            f'      "version": "{dep}"\n    }}\n  }}\n}}\n')


def _generate(prep):
    """The stub regeneration: gen.txt lists every src*.txt, so the right answer
    depends on both sides and taking either side alone is detectably wrong."""
    srcs = sorted(p.name for p in Path(prep).glob("src*.txt"))
    Path(prep, "gen.txt").write_text("".join(f"{n}\n" for n in srcs))


class RegenerateTests(VersionConflictTests):
    """harmonic-forge#1057: declared generated files take main's side during
    the rebase and are regenerated once after it."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.declared = {"gen.txt": "gen"}
        self.fail_task = None
        self.stray = False
        cfg = mock.patch.object(lane1_rebase, "regenerate_config",
                                side_effect=lambda _r: (dict(self.declared), "prov"))
        task = mock.patch.object(lane1_rebase, "run_task", side_effect=self._task)
        cfg.start()
        task.start()
        self.addCleanup(cfg.stop)
        self.addCleanup(task.stop)

    def _task(self, prep, name):
        self.calls.append(name)
        if name == self.fail_task:
            return subprocess.CompletedProcess([], 1, "", "boom")
        if name == "gen":
            _generate(prep)
            if self.stray:
                Path(prep, "README.md").write_text("rewritten\n")
        return subprocess.CompletedProcess([], 0, "", "")

    def gen_on_origin(self):
        return sh(self.origin, "show", f"refs/heads/{BRANCH}:gen.txt")

    def test_r1_generated_only_conflict_regenerates_and_pushes(self):
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gen_on_origin(), "src1.txt\nsrc2.txt")
        self.assertEqual(self.calls, ["prov", "gen"])
        self.assertIn("regenerated=gen.txt", out)
        self.assertEqual(self.local_branch(), self.origin_branch())

    def test_r2_regeneration_failure_exits_2_nothing_pushed(self):
        self.fail_task = "gen"
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("boom", err)

    def test_r3_provision_failure_exits_2(self):
        self.fail_task = "prov"
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        self.assert_refused(*self.run_captured()[::2])
        self.assertEqual(self.calls, ["prov"])

    def test_r4_change_outside_declared_paths_exits_2(self):
        self.stray = True
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("README.md", err)

    def test_r5_undeclared_conflict_alongside_exits_2(self):
        self.on_branch({"gen.txt": "src1.txt\n", "feature.txt": "branch\n"}, "feature")
        self.on_main({"gen.txt": "src2.txt\n", "feature.txt": "main\n"})
        self.assert_refused(*self.run_captured()[::2])
        self.assertEqual(self.calls, [])

    def test_r6_undeclared_generated_file_exits_2(self):
        self.declared = {"other.txt": "gen"}
        self.on_branch({"gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"gen.txt": "src2.txt\n"})
        self.assert_refused(*self.run_captured()[::2])

    def test_r7_delete_modify_on_declared_file_exits_2(self):
        self.on_main({"gen.txt": "v1\n"})
        sh(self.impl, "fetch", "-q", "origin")
        sh(self.impl, "reset", "-q", "--hard", "origin/main")
        self.on_branch({"gen.txt": "v2\n"}, "branch edits gen")
        sh(self.other, "pull", "-q", "origin", "main")
        sh(self.other, "rm", "-q", "gen.txt")
        sh(self.other, "commit", "-q", "-m", "main deletes gen")
        sh(self.other, "push", "-q", "origin", "main")
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("delete/modify", err)

    def test_r8_conflicts_at_two_stops_regenerate_once(self):
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "first")
        self.on_branch({"src3.txt": "c\n", "gen.txt": "src1.txt\nsrc3.txt\n"}, "second")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        code, _, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gen_on_origin(), "src1.txt\nsrc2.txt\nsrc3.txt")
        self.assertEqual(self.calls.count("gen"), 1)

    def test_r9_version_and_generated_conflict_at_one_stop(self):
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n", PKG: pkg("2.9.41")},
                       "feature + bump")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n", PKG: pkg("2.9.42")})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head_version(), "2.9.43")
        self.assertEqual(self.gen_on_origin(), "src1.txt\nsrc2.txt")
        self.assertIn("version-resolved=2.9.43", out)

    def test_r11_branch_deletes_declared_file_main_modifies_exits_2(self):
        self.on_main({"gen.txt": "v1\n"})
        sh(self.impl, "fetch", "-q", "origin")
        sh(self.impl, "reset", "-q", "--hard", "origin/main")
        sh(self.impl, "rm", "-q", "gen.txt")
        sh(self.impl, "commit", "-q", "-m", "branch deletes gen")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")
        self.branch_sha = sh(self.impl, "rev-parse", "HEAD")
        self.on_main({"gen.txt": "v2\n"})
        code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("delete/modify", err)

    def test_r12_task_that_cannot_launch_exits_2(self):
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        real_run = subprocess.run

        def no_mise(argv, *args, **kwargs):
            if argv and argv[0] == "mise":
                raise FileNotFoundError("mise")
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(lane1_rebase, "run_task", side_effect=real_run_task):
            with mock.patch.object(lane1_rebase.subprocess, "run", side_effect=no_mise):
                code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("exit 127", err)

    def test_r13_task_with_unmatched_quote_exits_2(self):
        self.declared = {"gen.txt": 'gen "'}
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})

        def real_for_gen(prep, task):
            if task == "prov":
                return subprocess.CompletedProcess([], 0, "", "")
            return real_run_task(prep, task)

        with mock.patch.object(lane1_rebase, "run_task", side_effect=real_for_gen):
            code, _, err = self.run_captured()
        self.assert_refused(code, err)
        self.assertIn("exit 127", err)

    def test_r10_crash_after_regeneration_push_rerun_exits_3(self):
        self.on_branch({"src1.txt": "a\n", "gen.txt": "src1.txt\n"}, "feature")
        self.on_main({"src2.txt": "b\n", "gen.txt": "src2.txt\n"})
        with mock.patch.object(lane1_rebase, "move_local_ref",
                               side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.run_main()
        pushed = self.origin_branch()
        self.assertNotEqual(pushed, self.branch_sha)
        code, _, err = self.run_captured()
        self.assertEqual(code, 3, err)
        self.assertEqual(self.origin_branch(), pushed)


class LockVersionTests(VersionConflictTests):
    """harmonic-forge#1057 gap 1: package-lock.json's two version keys."""

    def setUp(self):
        super().setUp()
        self.on_main({LOCK: lock("2.9.40")})
        sh(self.impl, "fetch", "-q", "origin")
        sh(self.impl, "reset", "-q", "--hard", "origin/main")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")

    def lock_on_origin(self):
        return json.loads(sh(self.origin, "show", f"refs/heads/{BRANCH}:{LOCK}"))

    def test_l1_lock_and_package_version_conflict_resolve_to_one_target(self):
        self.on_branch({"code.py": "x = 1\n", PKG: pkg("2.9.41"), LOCK: lock("2.9.41")}, "bump")
        self.on_main({PKG: pkg("2.9.42"), LOCK: lock("2.9.42")})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        data = self.lock_on_origin()
        # The lock keeps origin/main's version (preclose): T is package.json's.
        self.assertEqual((data["version"], data["packages"][""]["version"]), ("2.9.42", "2.9.42"))
        self.assertEqual(data["packages"]["node_modules/a"]["version"], "1.0.0")
        self.assertEqual(self.head_version(), "2.9.43")

    def test_l2_lock_dependency_difference_exits_2(self):
        self.on_branch({PKG: pkg("2.9.41"), LOCK: lock("2.9.41", dep="2.0.0")}, "bump + dep")
        self.on_main({PKG: pkg("2.9.42"), LOCK: lock("2.9.42")})
        self.assert_refused(*self.run_captured()[::2])


    def test_l4_lagging_lock_does_not_veto_a_package_json_resolution(self):
        # hrse's real shape (F1057 preclose): the auto-bump never updates the lock.
        self.on_main({LOCK: lock("2.9.28")})
        sh(self.impl, "fetch", "-q", "origin")
        sh(self.impl, "reset", "-q", "--hard", "origin/main")
        sh(self.impl, "push", "-q", "-f", "origin", f"HEAD:{BRANCH}")
        self.on_branch({"code.py": "x = 1\n", PKG: pkg("2.9.41"), LOCK: lock("2.9.28", dep="2.0.0")},
                       "bump + dependency")
        self.on_main({PKG: pkg("2.9.42")})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head_version(), "2.9.43")
        self.assertIn("version-resolved=2.9.43", out)

    def test_l3_lock_root_versions_disagree_exits_2(self):
        bad = lock("2.9.41").replace('"version": "2.9.41"\n    }', '"version": "2.9.39"\n    }')
        self.on_branch({PKG: pkg("2.9.41"), LOCK: bad}, "bump")
        self.on_main({PKG: pkg("2.9.42"), LOCK: lock("2.9.42")})
        self.assert_refused(*self.run_captured()[::2])


if __name__ == "__main__":
    unittest.main()
