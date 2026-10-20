"""harmonic-forge#1058 preclose pass 2 (sticky-wicket PATCH): the one exit-6
handler spans push 1 through the outer `finally`, every render-path git/gh
call is time-bounded, and the recovery text names the rendered SHA."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline_render as br  # noqa: E402
import lane1_rebase  # noqa: E402
from test_baseline_render import BRANCH, RenderFixture, sh  # noqa: E402


class HandlerTests(RenderFixture):
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

    # Survivor 1: the outer `finally`'s prep removal never replaces exit 6.
    def test_prep_removal_failure_keeps_exit_6(self):
        self.conflict()
        self.conclusion = "failure"
        real_remove, calls = lane1_rebase.remove_prep, []

        def remove(repo_dir, prep, force=False):
            calls.append(force)
            if len(calls) > 1:  # the stale-prune at start is call 1
                raise OSError("remove boom")
            return real_remove(repo_dir, prep, force)
        with mock.patch.object(lane1_rebase, "remove_prep", side_effect=remove):
            code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("concluded 'failure'", err)
        self.assertIn("Recover by hand", err)
        self.assertIn("prep removal failed: OSError: remove boom", err)

    # harmonic-forge#1087: the prep cleanup's git calls carry CALL_TIMEOUT.
    def _cleanup_git(self, raise_after_first_prune):
        """Patch `git` so `worktree` calls record their kwargs and, once the
        start-of-run stale prune has run, optionally time out."""
        real_git, seen = lane1_rebase.git, {"prunes": 0, "calls": []}

        def git(cwd, *args, **kw):
            if args[:1] == ("worktree",) and args[1:2] in (("remove",), ("prune",)):
                seen["calls"].append((args[1], kw.get("timeout")))
                if raise_after_first_prune and seen["prunes"] >= 1:
                    raise subprocess.TimeoutExpired(["git", *args], br.CALL_TIMEOUT)
                if args[1] == "prune":
                    seen["prunes"] += 1
            return real_git(cwd, *args, **kw)
        return git, seen

    def test_cleanup_timeout_keeps_exit_6(self):
        """AC2: exit 6 and its recovery text survive a cleanup timeout."""
        self.conflict()
        self.conclusion = "failure"
        git, _ = self._cleanup_git(raise_after_first_prune=True)
        with mock.patch.object(lane1_rebase, "git", side_effect=git):
            code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("Recover by hand", err)
        self.assertIn("prep removal failed: TimeoutExpired", err)

    def test_cleanup_timeout_on_success_propagates(self):
        """AC3: with no prior failure the cleanup TimeoutExpired propagates."""
        self.conflict()
        git, _ = self._cleanup_git(raise_after_first_prune=True)
        with mock.patch.object(lane1_rebase, "git", side_effect=git), \
             self.assertRaises(subprocess.TimeoutExpired):
            self.run_captured()

    def test_cleanup_git_calls_carry_call_timeout(self):
        """AC1 (the kill test): both cleanup calls pass CALL_TIMEOUT, checked
        separately, on a run where the prep worktree exists at cleanup."""
        self.conflict()
        git, seen = self._cleanup_git(raise_after_first_prune=False)
        with mock.patch.object(lane1_rebase, "git", side_effect=git):
            self.run_captured()
        after_start = seen["calls"][1:]  # drop the start-of-run stale prune
        removes = [t for name, t in after_start if name == "remove"]
        prunes = [t for name, t in after_start if name == "prune"]
        self.assertTrue(removes, seen["calls"])
        self.assertTrue(prunes, seen["calls"])
        self.assertEqual(set(removes), {br.CALL_TIMEOUT})
        self.assertEqual(set(prunes), {br.CALL_TIMEOUT})

    # Survivor 2: push 1 is inside the handler.
    def test_push_1_timeout_exits_6_with_recovery(self):
        self.conflict()
        before = self.origin_branch()

        def stall(*_a, **_k):
            raise subprocess.TimeoutExpired(["git", "push"], br.CALL_TIMEOUT)
        self.push_spy.side_effect = stall
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn("TimeoutExpired", err)
        self.assertIn("Recover by hand", err)
        self.assertEqual(self.origin_branch(), before)
        self.assertEqual(self.dispatched, [])
        self.assertFalse(self.prep_left())

    def test_push_1_lease_refusal_keeps_exit_1(self):
        self.conflict()
        real_push = self.push_spy._mock_wraps  # the unpatched push

        def late(*args, **kw):
            self.advance_branch()
            return real_push(*args, **kw)
        self.push_spy.side_effect = late
        code, _, err = self.run_captured()
        self.assertEqual(code, 1, err)
        self.assertIn("push refused", err)
        self.assertNotIn("Recover by hand", err)
        self.assertEqual(self.dispatched, [])

    def test_push_1_hook_refusal_keeps_exit_5(self):
        self.conflict()

        def hook(*_a, **_k):
            raise lane1_rebase.Exit(5, "pre-push hook refused the push")
        self.push_spy.side_effect = hook
        code, _, err = self.run_captured()
        self.assertEqual(code, 5, err)
        self.assertNotIn("Recover by hand", err)

    # Survivor 3: every git/gh subprocess from push 1 on carries a timeout.
    def test_render_path_subprocesses_carry_a_timeout(self):
        self.conflict()
        real_run, real_render, inside, seen = subprocess.run, br.render, [], []

        def run(argv, *a, **kw):
            frame = sys._getframe(1)  # the first caller outside unittest.mock
            while frame.f_code.co_filename.endswith("mock.py"):
                frame = frame.f_back
            caller = frame.f_code.co_filename  # not the test stubs' own git
            if inside and argv[0] in ("git", "gh") and not Path(caller).name.startswith("test_"):
                seen.append((argv, kw.get("timeout")))
            return real_run(argv, *a, **kw)

        def render(*a, **kw):
            inside.append(1)
            try:
                return real_render(*a, **kw)
            finally:
                inside.clear()
        with mock.patch.object(subprocess, "run", side_effect=run), \
             mock.patch.object(br, "render", side_effect=render):
            code, _, err = self.run_captured()
        self.assertEqual(code, 0, err)
        self.assertTrue(seen)
        self.assertEqual([argv for argv, t in seen if t is None], [])

    def test_repo_slug_and_hook_lookup_carry_a_timeout(self):
        calls = []

        def git(cwd, *args, **kw):
            calls.append((args, kw.get("timeout")))
            out = "git@github.com:o/r.git" if args[:1] == ("remote",) else ""
            return subprocess.CompletedProcess([], 1 if args[0] == "push" else 0, out, "HTTP 500")
        with mock.patch.object(lane1_rebase, "git", side_effect=git):
            self.assertEqual(lane1_rebase.repo_slug("."), "o/r")
            with self.assertRaises(lane1_rebase.Exit):
                lane1_rebase.push(Path("."), BRANCH, "0" * 40, timeout=br.CALL_TIMEOUT)
        self.assertEqual([a for a, t in calls if t is None], [])

    # Survivor 5: the recovery names the commit before every marker.
    def test_marked_head_recovery_names_the_rendered_sha(self):
        rendered = sh(self.impl, "rev-parse", "HEAD")
        sh(self.impl, "commit", "-q", "--allow-empty", "-m", "p\n\nRender-Pending: undispatched")
        sh(self.impl, "commit", "-q", "--allow-empty", "-m", "p\n\nRender-Pending: R9")
        sh(self.impl, "push", "-q", "origin", f"HEAD:{BRANCH}")
        self.on_main({"main.txt": "moved\n"})
        code, _, err = self.run_captured()
        self.assertEqual(code, 6, err)
        self.assertIn(f"ref={rendered}", err)
        self.assertIn(f"worktree at {rendered[:12]}", err)


if __name__ == "__main__":
    unittest.main()
