"""harmonic-forge#1058: lane1-rebase re-renders declared CI visual baselines,
forward-only (sticky-wicket REFORGE 6244217307, ruling 6244229339).

Temporary bare origin plus clones, as test_lane1_rebase; the dispatch, run
listing, wait, artifact read and fetch task are stubbed (no network)."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline_render as br  # noqa: E402
import lane1_rebase  # noqa: E402
import test_lane1_rebase as base  # noqa: E402

real_wait_run = br.wait_run

sh, BRANCH = base.sh, base.BRANCH
PNG = "frontend/e2e/visual/overture-snapshots/overture-screens-linux.png"
DEEP = "frontend/e2e/visual/sub/x-snapshots/a.png"
CFG = {"glob": "frontend/e2e/visual/*-snapshots/*.png",
       "workflow": "update-visual-baselines.yml",
       "fetch_task": "visual-baselines-fetch --run {run_id}"}


class RenderTests(base._Fixture):
    write = base.VersionConflictTests.write
    on_branch = base.VersionConflictTests.on_branch
    on_main = base.VersionConflictTests.on_main
    run_captured = base.VersionConflictTests.run_captured

    def setUp(self):
        super().setUp()
        self.before, self.after = {"100"}, {"100", "R1"}
        self.conclusion, self.timeout = "success", False
        self.artifact = None  # None: named for the dispatched SHA
        self.stray = False
        self.dispatched: list[str] = []
        self.on_dispatch = self.on_wait = None
        self.fetches: list[str] = []
        listed = iter([self.before])  # the first listing is before the dispatch

        def ids(_lr, _slug, _wf):
            return set(next(listed, self.after))

        def dispatch(_lr, _slug, _wf, sha):
            self.dispatched.append(sha)
            if self.on_dispatch:
                self.on_dispatch()

        def wait(_lr, _slug, _run):
            if self.on_wait:
                self.on_wait()
            return None if self.timeout else {"status": "completed",
                                              "conclusion": self.conclusion}

        def artifacts(_lr, _slug, _run):
            return [self.artifact or f"visual-baselines-{self.dispatched[-1]}"]

        def fetch(prep, task, timeout=None):
            # As hrse's real fetch: refuse unless HEAD is the rendered SHA.
            if sh(prep, "rev-parse", "HEAD") != self.dispatched[-1]:
                return subprocess.CompletedProcess([], 1, "", "HEAD is not the rendered SHA")
            self.fetches.append(task)
            Path(prep, PNG).write_text("rendered\n")
            if self.stray:
                Path(prep, "base.txt").write_text("rewritten\n")
            return subprocess.CompletedProcess([], 0, "", "")

        self.pushes = mock.patch.object(lane1_rebase, "push", wraps=lane1_rebase.push)
        for patch in (mock.patch.object(br, "config", return_value=dict(CFG)),
                      mock.patch.object(br, "list_dispatch_ids", side_effect=ids),
                      mock.patch.object(br, "dispatch", side_effect=dispatch),
                      mock.patch.object(br, "wait_run", side_effect=wait),
                      mock.patch.object(br, "run_artifacts", side_effect=artifacts),
                      mock.patch.object(br, "_sleep"),
                      mock.patch.object(lane1_rebase, "run_task", side_effect=fetch)):
            patch.start()
            self.addCleanup(patch.stop)
        self.push_spy = self.pushes.start()
        self.addCleanup(self.pushes.stop)

    def conflict(self, path=PNG):
        self.on_branch({path: "branch\n"}, "branch screens")
        self.on_main({path: "main\n"})

    def advance_branch(self):
        """Another actor pushes on top of origin/<branch> mid-run."""
        sh(self.other, "fetch", "-q", "origin", BRANCH)
        sh(self.other, "checkout", "-q", "-B", "late", "FETCH_HEAD")
        base.commit(self.other, "late.txt", "x\n", "late")
        sh(self.other, "push", "-q", "origin", f"HEAD:{BRANCH}")
        sh(self.other, "checkout", "-q", "main")

    def show(self, ref, path=PNG):
        return sh(self.origin, "show", f"{ref}:{path}")

    def trailer(self, ref):
        return br.marker_of(lane1_rebase, self.origin, ref)

    def assert_exit6_marked(self, code, err):
        self.assertEqual(code, 6, err)
        self.assertEqual(self.trailer(self.origin_branch()), "R1")
        self.assertEqual(self.show(f"{self.origin_branch()}^"), "main")
        self.assertIn("render run R1", err)
        self.assertIn("Recover by hand", err)
        self.assertFalse(self.prep_left())

    def assert_exit6_undispatched(self, code, err):
        self.assertEqual(code, 6, err)
        head = self.origin_branch()
        self.assertEqual(self.trailer(head), "undispatched")
        self.assertEqual(self.dispatched, [sh(self.origin, "rev-parse", f"{head}^")])
        self.assertIn("Recover by hand", err)
        self.assertFalse(self.prep_left())

    # AC2: one forward pass, three pushes, render-run printed.
    def test_baseline_only_conflict_renders_and_exits_0(self):
        self.conflict()
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        head = self.origin_branch()
        self.assertEqual(self.show(head), "rendered")
        self.assertIsNone(self.trailer(head))
        self.assertEqual(self.trailer(f"{head}^"), "R1")
        self.assertEqual(self.trailer(f"{head}^^"), "undispatched")
        self.assertEqual(self.dispatched, [sh(self.origin, "rev-parse", f"{head}^^^")])
        self.assertEqual(self.push_spy.call_count, 3)
        self.assertEqual(self.fetches, ["visual-baselines-fetch --run R1"])
        self.assertIn("render-run=R1", out)
        self.assertIn(f"new={head}", out)
        self.assertEqual(self.local_branch(), head)

    # AC3: a failed or timed-out render leaves the marker and exits 6.
    def test_failed_render_exits_6_marker_kept(self):
        self.conflict()
        self.conclusion = "failure"
        code, _, err = self.run_captured()
        self.assert_exit6_marked(code, err)
        self.assertIn("concluded 'failure'", err)

    def test_timed_out_render_exits_6_marker_kept(self):
        self.conflict()
        self.timeout = True
        code, _, err = self.run_captured()
        self.assert_exit6_marked(code, err)
        self.assertIn("did not complete", err)

    def test_artifact_for_another_sha_exits_6(self):
        self.conflict()
        self.artifact = "visual-baselines-" + "0" * 40
        code, _, err = self.run_captured()
        self.assert_exit6_marked(code, err)
        self.assertEqual(self.fetches, [])

    def test_fetch_changing_a_non_declared_path_exits_6(self):
        self.conflict()
        self.stray = True
        code, _, err = self.run_captured()
        self.assert_exit6_marked(code, err)
        self.assertIn("base.txt", err)

    # Ruling 6244229339: the single new workflow_dispatch id, else no marker.
    def test_two_new_run_ids_exit_6_no_marker(self):
        self.conflict()
        self.after = {"100", "R1", "R2"}
        code, _, err = self.run_captured()
        self.assert_exit6_undispatched(code, err)
        self.assertIn("2 new workflow_dispatch runs", err)

    def test_zero_new_run_ids_exit_6_no_marker(self):
        self.conflict()
        self.after = {"100"}
        code, _, err = self.run_captured()
        self.assert_exit6_undispatched(code, err)
        self.assertIn("0 new workflow_dispatch runs", err)

    def test_marker_push_lease_refused_exits_6(self):
        self.conflict()
        self.on_dispatch = self.advance_branch
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("push refused", err)
        self.assertIsNone(self.trailer(self.origin_branch()))

    def test_final_push_lease_refused_exits_6(self):
        self.conflict()
        self.on_wait = self.advance_branch
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("push refused", err)
        self.assertIn("render run R1", err)

    # Preclose 1: push 1 is already marked, so a failure before the run is
    # identified leaves `Render-Pending: undispatched` and a re-run exits 6.
    def test_dispatch_failure_leaves_undispatched_and_rerun_exits_6(self):
        self.conflict()

        def boom():
            raise lane1_rebase.Exit(1, "gh workflow run failed: boom")
        self.on_dispatch = boom
        code, _, err = self.run_captured()
        self.assert_exit6_undispatched(code, err)
        self.assertIn("no render run was identified", err)
        head = self.origin_branch()
        self.on_dispatch = None
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("Render-Pending: undispatched", err)
        self.assertEqual(self.origin_branch(), head)

    def test_timeout_after_push_1_exits_6(self):
        self.conflict()

        def stall():
            raise subprocess.TimeoutExpired(["gh", "workflow", "run"], br.CALL_TIMEOUT)
        self.on_dispatch = stall
        code, _, err = self.run_captured()
        self.assert_exit6_undispatched(code, err)
        self.assertIn("TimeoutExpired", err)

    def test_gh_calls_carry_a_timeout(self):
        done = subprocess.CompletedProcess([], 0, "[]", "")
        with mock.patch.object(br.subprocess, "run", return_value=done) as run:
            br._gh(lane1_rebase, "api", "repos/o/r/actions/runs/1")
        self.assertEqual(run.call_args.kwargs.get("timeout"), br.CALL_TIMEOUT)

    def test_wait_is_bounded_by_wall_clock(self):
        ticks = iter(range(0, 10**6, 600))  # each read of the clock: 10 minutes on
        with mock.patch.object(br, "read_run", return_value={"status": "in_progress"}) as read, \
             mock.patch.object(br, "_clock", side_effect=lambda: next(ticks)):
            self.assertIsNone(real_wait_run(lane1_rebase, "o/r", "R1"))
        self.assertLessEqual(read.call_count, br.RENDER_TIMEOUT // 600 + 1)

    def test_cleanup_exception_in_handler_still_exits_6(self):
        self.conflict()
        self.conclusion = "failure"
        real_git = lane1_rebase.git

        def git(cwd, *args, **kw):
            if args[:1] == ("reset",) and "--hard" in args:
                raise OSError("cleanup boom")
            return real_git(cwd, *args, **kw)
        with mock.patch.object(lane1_rebase, "git", side_effect=git):
            code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("cleanup git reset failed: OSError: cleanup boom", err)
        self.assertIn("render run R1", err)
        self.assertIn("Recover by hand", err)
        self.assertEqual(self.trailer(self.origin_branch()), "R1")

    # Before any work: a marked head on origin is exit 6, nothing touched.
    def test_marked_head_on_entry_exits_6_touches_nothing(self):
        sh(self.impl, "commit", "-q", "--allow-empty", "-m", "pending\n\nRender-Pending: R9")
        sh(self.impl, "push", "-q", "origin", f"HEAD:{BRANCH}")
        head, local = self.origin_branch(), self.local_branch()
        self.on_main({"main.txt": "moved\n"})
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("Render-Pending: R9", err)
        self.assertIn("Recover by hand", err)
        self.assertEqual(self.origin_branch(), head)
        self.assertEqual(self.local_branch(), local)
        self.assertEqual(self.dispatched, [])
        self.assertFalse(self.prep_left())

    # AC4: an undeclared conflict is still exit 2 with nothing pushed.
    def test_undeclared_conflict_exits_2_nothing_pushed(self):
        self.conflict("docs/screen.png")
        before = self.origin_branch()
        code, _, err = self.run_captured()
        self.assertEqual(code, 2, err)
        self.assertEqual(self.origin_branch(), before)
        self.assertEqual(self.push_spy.call_count, 0)
        self.assertEqual(self.dispatched, [])

    def test_multi_segment_path_is_not_a_baseline_exits_2(self):
        self.assertFalse(br.is_baseline(DEEP, CFG["glob"]))
        self.conflict(DEEP)
        before = self.origin_branch()
        code, _, err = self.run_captured()
        self.assertEqual(code, 2, err)
        self.assertIn(DEEP, err)
        self.assertEqual(self.origin_branch(), before)
        self.assertEqual(self.dispatched, [])

    def test_clean_rebase_dispatches_nothing(self):
        self.on_branch({PNG: "branch\n"}, "branch screens")
        self.on_main({"main.txt": "moved\n"})
        code, out, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.dispatched, [])
        self.assertNotIn("render-run=", out)
        self.assertEqual(self.push_spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
