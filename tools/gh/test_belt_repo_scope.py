#!/usr/bin/env python3
"""A belt can be scoped to one repo (harmonic-forge#1199)."""
import datetime as dt
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watch_lane_posts  # noqa: E402
from belt_mechanics import SeenSet, Watermarks  # noqa: E402

_BELT_PLAN = HERE.parent / "lane" / "belt_plan.py"


def _entry(lane: str, repo: str | None):
    return next(e for e in watch_lane_posts.CANONICAL_BELTS[lane]
                if e["workspace"] == "vh" and e.get("repo") == repo)


def _a_vh_repo() -> str:
    return next(e["repo"] for e in watch_lane_posts.CANONICAL_BELTS["1"]
                if e["workspace"] == "vh" and e.get("repo"))


class PlanTests(unittest.TestCase):
    def test_scoped_plan_carries_scope_repo_and_unknown_repo_exits_2(self):  # AC2
        sys.path.insert(0, str(_BELT_PLAN.parent))
        import belt_plan
        repo = _a_vh_repo()
        self.assertIn(f"--scope-repo {repo}", belt_plan.monitor_command("1", "vh", repo))
        self.assertNotIn("--scope-repo", belt_plan.monitor_command("1", "vh"))
        with self.assertRaises(KeyError):
            belt_plan.monitor_command("1", "vh", "kenekted/kenekted-platform")
        with patch.object(belt_plan, "workspace_for", return_value="vh"), \
                patch.dict(os.environ, {"LANE": "1"}), patch("sys.stderr", io.StringIO()):
            self.assertEqual(belt_plan.main(["--repo", "kenekted/kenekted-platform"]), 2)

    def test_unscoped_entry_keeps_lock_and_state_key(self):  # AC1
        entry = _entry("1", None)
        self.assertNotIn("--scope-repo", entry["argv"])
        self.assertEqual(entry["lock"], "belt-lane1-vh.lock")
        self.assertNotIn("workspace_lock", entry)


class ScopedWatchTests(unittest.TestCase):
    """The belt's main() up to its first cycle, with every external call patched."""

    def _run(self, argv, state, captured):
        repo_a, repo_b = "vitalharmony/harmonic-forge", "vitalharmony/hrse"
        projects = [SimpleNamespace(repo=repo_a, checkout=Path("/r/forge")),
                    SimpleNamespace(repo=repo_b, checkout=Path("/r/hrse"))]

        def cycle(repos, *args, **kwargs):
            captured["repos"] = list(repos)
            raise KeyboardInterrupt

        patches = [
            patch("watch_lane_posts._BELT_STATE", Path(state)),
            patch("watch_lane_posts._session_workspace", return_value="vh"),
            patch("watch_lane_posts._workspace_repos", return_value=[repo_a, repo_b]),
            patch.object(sys, "argv", ["watch_lane_posts.py", *argv]),
            patch("watch_lane_posts.assert_identity"),
            patch("watch_lane_posts._check_git_staleness"),
            patch("watch_lane_posts._acquire_belt_lock"),
            patch("watch_lane_posts.manifest_repos", return_value=[repo_a, repo_b]),
            patch("watch_lane_posts.manifest_worktree_roots", return_value=["/r/forge", "/r/hrse"]),
            patch("watch_lane_posts._manifest_projects", return_value=projects),
            patch("watch_lane_posts.enumerate_repo_roots",
                  side_effect=lambda roots: captured.setdefault("roots", list(roots)) and []),
            patch("watch_lane_posts.queue_cycle", side_effect=cycle),
            patch("watch_lane_posts.time.sleep"),
        ]
        err = io.StringIO()
        patches.append(patch("sys.stderr", err))
        with patch.dict(os.environ, {"LANE": "1"}):
            for p in patches:
                p.start()
            try:
                watch_lane_posts.main()
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                for p in reversed(patches):
                    p.stop()
        return err.getvalue()

    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, True)

    def test_scoped_belt_polls_only_its_repo(self):  # AC3
        argv = [*_entry("1", None)["argv"], "--scope-repo", "vitalharmony/harmonic-forge"]
        captured = {}
        self._run(argv, self.state, captured)
        self.assertEqual(captured["repos"], ["vitalharmony/harmonic-forge"])
        self.assertEqual(captured["roots"], ["/r/forge"])

    def test_scoped_belt_has_its_own_state(self):  # AC4
        (Path(self.state) / "seen-l2-l3+ql1@vh.tsv").write_text("keep\n")
        argv = [*_entry("1", None)["argv"], "--scope-repo", "vitalharmony/harmonic-forge"]
        err = self._run(argv, self.state, {})
        self.assertIn("ticks-l2-l3+ql1@vh~vitalharmony__harmonic-forge.jsonl", err)
        self.assertIn("first arm for this belt", err)  # primes; replays nothing
        self.assertEqual((Path(self.state) / "seen-l2-l3+ql1@vh.tsv").read_text(), "keep\n")

    def test_unscoped_belt_state_key_unchanged(self):  # AC1
        err = self._run(list(_entry("1", None)["argv"]), self.state, {})
        self.assertIn("ticks-l2-l3+ql1@vh.jsonl", err)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = patch("watch_lane_posts.BELT_LOCK_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _acquire(self, lock, workspace_lock=None):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            try:
                return watch_lane_posts._acquire_belt_lock(lock, workspace_lock), out.getvalue()
            except SystemExit as exc:
                return exc.code, out.getvalue()

    def test_two_scoped_belts_run_side_by_side(self):  # AC5
        a, _ = self._acquire("belt-lane1-vh~o__forge.lock", "belt-lane1-vh.lock")
        b, _ = self._acquire("belt-lane1-vh~o__hrse.lock", "belt-lane1-vh.lock")
        self.assertIsInstance(a, tuple)
        self.assertIsInstance(b, tuple)

    def test_scoped_refused_while_unscoped_runs(self):  # AC6
        held, _ = self._acquire("belt-lane1-vh.lock")
        self.assertNotIsInstance(held, int)
        code, out = self._acquire("belt-lane1-vh~o__forge.lock", "belt-lane1-vh.lock")
        self.assertEqual(code, 4)
        self.assertIn(f"pid {os.getpid()}", out)  # names the holder

    def test_scoped_arm_reclaims_a_dead_session_unscoped_holder(self):
        """Pass-2 survivor 2: an orphaned unscoped belt no longer blocks scoped arms forever."""
        import fcntl
        held = open(self.dir / "belt-lane1-vh.lock", "a+")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        held.write(json.dumps({"pid": 4242, "start": "x", "session_pid": 4243}))
        held.flush()

        def fake_kill(pid, sig):
            if pid == 4243 and sig == 0:
                raise ProcessLookupError  # the holder's session is dead
            if pid == 4242 and sig != 0:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)  # the holder exits on SIGTERM
        with patch("watch_lane_posts.os.kill", side_effect=fake_kill) as kill:
            result, _ = self._acquire("belt-lane1-vh~o__forge.lock", "belt-lane1-vh.lock")
        self.assertIsInstance(result, tuple)
        self.assertIn((4242, signal.SIGTERM), [c.args for c in kill.call_args_list])

    def test_unscoped_refusal_names_a_dead_scoped_holder(self):
        held = [self._acquire("belt-lane1-vh~o__forge.lock", "belt-lane1-vh.lock")]
        (self.dir / "belt-lane1-vh~o__forge.lock").write_text(
            json.dumps({"pid": 777, "start": "x", "session_pid": 778}))
        with patch("watch_lane_posts._proc_alive", side_effect=lambda pid: pid != 778):
            code, out = self._acquire("belt-lane1-vh.lock")
        self.assertEqual(code, 4)
        self.assertIn("pid 777 (session dead: stop it)", out)
        self.assertTrue(held)

    def test_unscoped_refused_while_scoped_run_without_reclaim(self):  # AC6
        # A past unscoped belt's holder record, left in the workspace lock file.
        (self.dir / "belt-lane1-vh.lock").write_text(json.dumps(
            {"pid": 999999, "start": "old run", "session_pid": 999998}))
        held = [self._acquire("belt-lane1-vh~o__forge.lock", "belt-lane1-vh.lock"),
                self._acquire("belt-lane1-vh~o__hrse.lock", "belt-lane1-vh.lock")]
        self.assertTrue(all(isinstance(h[0], tuple) for h in held))
        with patch("watch_lane_posts.os.kill") as kill:
            code, out = self._acquire("belt-lane1-vh.lock")
        self.assertEqual(code, 4)
        # Liveness probes (signal 0) are allowed; no real signal is ever sent.
        self.assertEqual([c for c in kill.call_args_list if c.args[1] != 0], [])
        self.assertIn("belt-lane1-vh~o__forge.lock", out)
        self.assertIn("belt-lane1-vh~o__hrse.lock", out)


class StaleWatermarkTests(unittest.TestCase):  # AC8
    HANDOFF = "## Handoff\n\n<!-- l1-post v1; kind=handoff; posted-by=LANE1 -->"

    def _cycle(self, mark_age_hours, post_ages_hours):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        now = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)
        repo, issue = "vitalharmony/hrse", 1530
        marks = Watermarks(root / "wm")
        marks.advance(watch_lane_posts._account_of(repo), watch_lane_posts._wm_key(repo, issue),
                      now - dt.timedelta(hours=mark_age_hours))
        seen = SeenSet(root / "seen.tsv")
        comments = []
        for n, age in enumerate(post_ages_hours):
            posted = (now - dt.timedelta(hours=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
            comments.append({"id": 70 + n, "created_at": posted, "updated_at": posted,
                             "html_url": f"https://example/{70 + n}", "body": self.HANDOFF,
                             "user": {"login": "vitalharmony"}})

        def fetch(_repo, _issue, since):  # what GitHub does with `since`
            return [c for c in comments if c["updated_at"] >= since]
        with patch("watch_lane_posts._fetch_comments", side_effect=fetch):
            lines, _failed = watch_lane_posts.comment_watch_cycle(
                [(repo, issue)], {"l1"}, now.strftime("%Y-%m-%dT%H:%M:%SZ"), marks, seen,
                set(), allow_priming=False, deferred_advances=[])
        return lines

    def test_stale_watermark_skips_only_posts_older_than_the_horizon(self):
        self.assertEqual(self._cycle(mark_age_hours=48, post_ages_hours=[30]), [])  # not replayed
        lines = self._cycle(mark_age_hours=48, post_ages_hours=[2])                # delivered
        self.assertTrue(lines)
        self.assertFalse([line for line in lines if "PRIMED" in line], lines)

    def test_the_skipped_interval_is_named(self):
        """Option A: a clamp is never silent; a fresh watermark prints nothing."""
        err = io.StringIO()
        with patch("sys.stderr", err):
            self._cycle(mark_age_hours=48, post_ages_hours=[2])
        self.assertIn("are not replayed", err.getvalue())
        self.assertIn("2026-10-26T12:00:00Z", err.getvalue())  # the horizon
        err = io.StringIO()
        with patch("sys.stderr", err):
            self._cycle(mark_age_hours=1, post_ages_hours=[0.5])
        self.assertNotIn("not replayed", err.getvalue())

    def test_fresh_watermark_still_delivers(self):
        lines = self._cycle(mark_age_hours=1, post_ages_hours=[0.5])
        self.assertTrue(lines)
        self.assertFalse([line for line in lines if "PRIMED" in line], lines)


if __name__ == "__main__":
    unittest.main()
