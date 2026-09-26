"""harmonic-forge#761 AC9: `_lane_refresh.sh` via lane1/lane2, and the
`lane_launch_notice.py` SessionStart hook.

Lane 3's refresh path is covered by `test_lane_launchers.Lane3RefreshesAtLaunch`.
Every case runs against a disposable fixture tree whose `origin` is a local
bare repository, so nothing touches the network.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_lane_launchers as tll  # noqa: E402

# Referenced through the module, not imported by name: a TestCase class bound
# in this namespace would be collected and run a second time here.
_FixtureTree = tll._FixtureTree
_advance = tll.Lane3RefreshesAtLaunch._advance_origin_from_elsewhere

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "lane_launch_notice.py"
_spec = importlib.util.spec_from_file_location("lane_launch_notice", HOOK)
notice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notice)

KEYS = "LANE_REFRESH_STATUS,LANE_REFRESH_FROM,LANE_REFRESH_TO"


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def _run(tree, lane, args=(), **env):
    return tree.run(lane, list(args), LANE_CLI=env.pop("agent", "claude"),
                    LANE_CAPTURE_EXTRA_ENV=KEYS,
                    LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"), **env)


class Lane1BranchMode(unittest.TestCase):
    """TC3, TC9: lane1 fast-forwards a clean `main`, and otherwise warns and
    starts without moving anything."""

    def test_clean_and_behind_fast_forwards_main(self):
        with _FixtureTree() as tree:
            remote_sha = _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), remote_sha)
            self.assertEqual(_git(tree.main, "symbolic-ref", "HEAD"), "refs/heads/main")
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "updated")

    def test_dirty_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            (tree.main / "README.md").write_text("local edit\n")
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-dirty")
            self.assertIn("README.md", cell["stderr"])

    def test_off_main_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            _git(tree.main, "checkout", "-q", "-b", "side")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_detached_main_checkout_is_never_moved(self):
        """TC9: `merge --ff-only` would succeed on a detached HEAD and move it
        silently; the helper must refuse that case up front."""
        with _FixtureTree() as tree:
            _git(tree.main, "checkout", "-q", "--detach")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_local_commits_ahead_record_current(self):
        with _FixtureTree() as tree:
            (tree.main / "LOCAL.md").write_text("local\n")
            _git(tree.main, "add", "LOCAL.md")
            _git(tree.main, "commit", "-q", "-m", "local")
            before = _git(tree.main, "rev-parse", "HEAD")
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")

    def test_diverged_warns_starts_and_moves_nothing(self):
        with _FixtureTree() as tree:
            (tree.main / "LOCAL.md").write_text("local\n")
            _git(tree.main, "add", "LOCAL.md")
            _git(tree.main, "commit", "-q", "-m", "local")
            before = _git(tree.main, "rev-parse", "HEAD")
            _advance(tree)
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.main, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-diverged")

    def test_unreachable_origin_warns_and_starts(self):
        """TC4 (lane1 path)."""
        with _FixtureTree() as tree:
            _git(tree.main, "remote", "set-url", "origin",
                 str(tree.root / "does-not-exist.git"))
            cell = _run(tree, "1")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "fetch-failed")

    def test_restart_reminder_prints_for_every_agent(self):
        """TC11."""
        with _FixtureTree() as tree:
            for agent in ("claude", "codex", "gemini"):
                with self.subTest(agent=agent):
                    cell = _run(tree, "1", agent=agent)
                    self.assertTrue(cell["launched"], cell.get("stderr"))
                    self.assertIn("mise run restart --no-bump --no-git", cell["stderr"])


class Lane2DetachedMode(unittest.TestCase):
    def test_behind_is_updated_and_logged(self):
        """TC1 (lane2 path)."""
        with _FixtureTree() as tree:
            remote_sha = _advance(tree)
            cell = _run(tree, "2")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane2, "rev-parse", "HEAD"), remote_sha)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "updated")
            log = (tree.root / "refresh-log" / "refresh.log").read_text().splitlines()
            self.assertEqual(len(log), 1)
            self.assertIn("\tlane2\t", log[0])

    def test_tracked_changes_refuse_and_name_the_file(self):
        """TC2 (lane2 path)."""
        with _FixtureTree() as tree:
            before = _git(tree.lane2, "rev-parse", "HEAD")
            _advance(tree)
            (tree.lane2 / "README.md").write_text("local edit\n")
            cell = _run(tree, "2")
            self.assertFalse(cell["launched"])
            self.assertIn("README.md", cell["stderr"])
            self.assertEqual(_git(tree.lane2, "rev-parse", "HEAD"), before)

    def test_unreachable_origin_refuses(self):
        """TC4 (lane2 path)."""
        with _FixtureTree() as tree:
            _git(tree.main, "remote", "set-url", "origin",
                 str(tree.root / "does-not-exist.git"))
            cell = _run(tree, "2")
            self.assertFalse(cell["launched"])
            self.assertIn("could not determine or reach origin/main", cell["stderr"])

    def test_current_is_silent(self):
        with _FixtureTree() as tree:
            cell = _run(tree, "2")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "current")
            self.assertNotIn("lane2: checkout", cell["stderr"])



class PrecloseFindings(unittest.TestCase):
    """harmonic-forge#761 preclose: each finding's scenario, driven through
    the real launchers."""

    KEYS3 = KEYS + ",LANE_REFRESH_DETAIL,LANE_REFRESH_ENV"

    def _run(self, tree, lane, args=()):
        return tree.run(lane, list(args), LANE_CLI="claude",
                        LANE_CAPTURE_EXTRA_ENV=self.KEYS3,
                        LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"))

    def test_a_pushed_branch_is_detached_and_named(self):
        """H1: no silent detach -- the branch is named in the record."""
        with _FixtureTree() as tree:
            _git(tree.lane3, "checkout", "-q", "-b", "gated")
            _git(tree.lane3, "push", "-q", "origin", "gated")
            remote_sha = _advance(tree)
            cell = self._run(tree, "3")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane3, "rev-parse", "HEAD"), remote_sha)
            self.assertIn("gated", cell["extra_env"]["LANE_REFRESH_DETAIL"])
            self.assertIn("gated", cell["stderr"])

    def test_a_branch_with_unpushed_commits_refuses_and_stays(self):
        """H1: never detach away from work no remote has."""
        for lane, wt in (("2", "lane2"), ("3", "lane3")):
            with self.subTest(lane=lane), _FixtureTree() as tree:
                path = getattr(tree, wt)
                _git(path, "checkout", "-q", "-b", "local-work")
                (path / "WORK.md").write_text("w\n")
                _git(path, "add", "WORK.md")
                _git(path, "-c", "user.email=w@example.invalid", "-c", "user.name=W",
                     "commit", "-q", "-m", "work")
                before = _git(path, "rev-parse", "HEAD")
                _advance(tree)
                cell = self._run(tree, lane)
                self.assertFalse(cell["launched"])
                self.assertIn("local-work", cell["stderr"])
                self.assertEqual(_git(path, "rev-parse", "HEAD"), before)
                self.assertEqual(_git(path, "symbolic-ref", "--short", "HEAD"), "local-work")

    def test_a_failed_checkout_is_named_not_called_a_fetch_failure(self):
        """M1: an untracked file in the way of origin/main's new file."""
        for lane, wt in (("2", "lane2"), ("3", "lane3")):
            with self.subTest(lane=lane), _FixtureTree() as tree:
                _advance(tree)  # adds ELSEWHERE.md on origin/main
                (getattr(tree, wt) / "ELSEWHERE.md").write_text("in the way\n")
                cell = self._run(tree, lane)
                self.assertFalse(cell["launched"])
                self.assertIn("checkout --detach", cell["stderr"])
                self.assertIn("ELSEWHERE.md", cell["stderr"])
                self.assertNotIn("could not determine or reach", cell["stderr"])
                self.assertNotIn("cannot determine whether", cell["stderr"])

    def test_a_busy_worktree_is_not_moved(self):
        """M3: a live process with its cwd in the worktree."""
        with _FixtureTree() as tree:
            before = _git(tree.lane3, "rev-parse", "HEAD")
            _advance(tree)
            proc = subprocess.Popen(["sleep", "60"], cwd=tree.lane3)
            try:
                cell = self._run(tree, "3")
            finally:
                proc.kill(); proc.wait()
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_git(tree.lane3, "rev-parse", "HEAD"), before)
            self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], "skipped-busy")
            self.assertIn("NOT updated", cell["stderr"])

    def test_a_real_env_file_is_kept_aside_not_deleted(self):
        """M2."""
        with _FixtureTree(with_backend_env=True) as tree:
            (tree.lane3 / "backend").mkdir()
            (tree.lane3 / "backend" / ".env").write_text("KEY=only-copy\n")
            cell = self._run(tree, "3")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertTrue((tree.lane3 / "backend" / ".env").is_symlink())
            kept = list((tree.lane3 / "backend").glob(".env.pre-relink-*"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_text(), "KEY=only-copy\n")

    def test_lane3_notice_never_claims_current_unless_current(self):
        """H2."""
        for status in ("ack-stale", "skipped-busy", "skipped-dirty",
                       "skipped-diverged", "fetch-failed", "checkout-failed", ""):
            with self.subTest(status=status):
                text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": status,
                                            "LANE_REFRESH_FROM": "a" * 40})
                self.assertNotIn("was current at launch", text)
                self.assertIn("Gate report must state", text)
        self.assertIn("was current at launch",
                      notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current"}))

class LaunchNotice(unittest.TestCase):
    """TC6, TC7."""

    def test_no_lane_emits_nothing(self):
        self.assertIsNone(notice.build_notice({"LANE_REFRESH_STATUS": "updated"}))

    def test_lane1_always_carries_the_restart_line(self):
        for status in ("updated", "current", "skipped-dirty", "fetch-failed", ""):
            with self.subTest(status=status):
                text = notice.build_notice({"LANE": "1", "LANE_REFRESH_STATUS": status})
                self.assertIn("Run `mise run restart --no-bump --no-git`", text)

    def _repo_with_change(self, path: str) -> tuple[str, str, str]:
        import tempfile
        d = tempfile.mkdtemp()
        _git(d, "init", "-q", "-b", "main")
        _git(d, "-c", "user.email=n@example.invalid", "-c", "user.name=N",
             "commit", "-q", "--allow-empty", "-m", "a")
        a = _git(d, "rev-parse", "HEAD")
        target = Path(d) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
        _git(d, "add", path)
        _git(d, "-c", "user.email=n@example.invalid", "-c", "user.name=N",
             "commit", "-q", "-m", "b")
        return d, a, _git(d, "rev-parse", "HEAD")

    def test_lane1_code_changed_note_only_for_backend_or_frontend(self):
        import os
        for path, expected in (("backend/app/x.py", True), ("frontend/src/x.ts", True),
                               ("docs/x.md", False)):
            with self.subTest(path=path):
                d, a, b = self._repo_with_change(path)
                cwd = os.getcwd()
                os.chdir(d)
                try:
                    text = notice.build_notice({"LANE": "1", "LANE_REFRESH_STATUS": "updated",
                                                "LANE_REFRESH_FROM": a, "LANE_REFRESH_TO": b})
                finally:
                    os.chdir(cwd)
                self.assertEqual("backend/frontend code changed" in text, expected)

    def test_lane3_states_the_outcome_for_the_gate_report(self):
        text = notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "updated",
                                    "LANE_REFRESH_FROM": "a" * 40, "LANE_REFRESH_TO": "b" * 40})
        self.assertIn("Gate report must state", text)
        self.assertIn("updated at launch", text)
        self.assertIn("was current",
                      notice.build_notice({"LANE": "3", "LANE_REFRESH_STATUS": "current"}))

    def test_hook_emits_session_start_json(self):
        import json
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("LANE")}
        env.update({"LANE": "1", "LANE_REFRESH_STATUS": "current"})
        out = subprocess.run([sys.executable, str(HOOK)], env=env,
                             capture_output=True, text=True, check=True).stdout
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("mise run restart", payload["hookSpecificOutput"]["additionalContext"])

    def test_hook_is_silent_outside_a_lane(self):
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("LANE")}
        out = subprocess.run([sys.executable, str(HOOK)], env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
