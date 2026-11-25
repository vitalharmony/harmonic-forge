#!/usr/bin/env python3
"""The forge root at a real lane launch (harmonic-forge#1526): the
launcher-checkout fallback, the rules pull at the resolved root, the root
notice line, and the pin that keeps a launch under test from pulling the
checkout under test.

Uses `test_lane_launchers.py`'s fixture tree (stubbed CLIs, a fixture HOME
declaring the operator overlay). Every launch but the fallback case pins
`HARMONIC_FORGE_ROOT` to a fixture root that holds no real checkout.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import baseline_capture as bc  # noqa: E402
import test_lane_launchers as tll  # noqa: E402

WORKTREE = HERE.parent.parent


def _git_state() -> tuple:
    """HEAD, reflog length and FETCH_HEAD mtime of the checkout under test.
    FETCH_HEAD is the proof no pull ran: `git pull --ff-only` fetches before it
    refuses, so HEAD and the reflog alone cannot show it."""
    def git(*args):
        return subprocess.run(["git", "-C", str(WORKTREE), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    fetch_head = Path(WORKTREE, git("rev-parse", "--git-path", "FETCH_HEAD"))
    return (git("rev-parse", "HEAD"), len(git("reflog").splitlines()),
            fetch_head.stat().st_mtime_ns if fetch_head.exists() else None)


def _roots(cell: dict) -> set[str]:
    """Each `--settings` command's script directory."""
    hooks = tll._settings_value(cell)["hooks"]
    return {h["command"].split('"')[1].rsplit("/", 1)[0]
            for blocks in hooks.values() for b in blocks for h in b["hooks"]}


def _notices(cell: dict) -> list[str]:
    return [line for line in cell["stderr"].splitlines() if "forge root " in line]


class LauncherCheckoutFallback(unittest.TestCase):
    """Test Case 3 and 10(b): no HARMONIC_FORGE_ROOT, no `forge_root`, and a
    HOME with no `harmonic-forge`: the launcher's own checkout is the root."""

    def test_the_launchers_own_checkout_is_used_and_no_notice_prints(self):
        with tll._FixtureTree() as tree:
            (tree.forge / "tools").unlink()
            tree.forge.rmdir()
            tree.lane_dir = tll.RegistryIntegrity._copy_lane_dir(None, tree.root)
            checkout = tree.lane_dir.parent.parent
            self.assertFalse(any((d / "sync_rules.py").exists() for d in (checkout, *checkout.parents)))
            cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT="")
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertEqual(_roots(cell), {f"{checkout}/tools/hooks"})
            self.assertEqual(_notices(cell), [])


class RulesPullAtTheRoot(unittest.TestCase):
    """Test Case 6 and 10(a): the pull runs the resolved root's
    `sync_rules.py`, never `$HOME/harmonic-forge`'s and never the checkout
    under test's; the notice names the root and its source, once."""

    def test_the_pull_runs_at_the_root_and_the_notice_names_it(self):
        with tll._FixtureTree() as tree:
            root = tree.root / "other-forge"
            root.mkdir()
            (root / "tools").symlink_to((WORKTREE / "tools").resolve())
            (root / "sync_rules.py").write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('pulled').write_text('yes')\n")
            before = _git_state()
            cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT=str(root))
            self.assertTrue(cell["launched"], cell.get("stderr"))
            self.assertTrue((root / "pulled").is_file(), cell["stderr"])
            self.assertEqual(_git_state(), before)
            notices = _notices(cell)
            self.assertEqual(len(notices), 1, cell["stderr"])
            self.assertIn(f"forge root {root} (from HARMONIC_FORGE_ROOT)", notices[0])
            self.assertEqual(_roots(cell), {f"{root}/tools/hooks"})


class ForgeRootPin(unittest.TestCase):
    """Test Case 8: an empty root over a lane dir inside a checkout holding
    `sync_rules.py` raises before any subprocess, at both launch helpers."""

    def test_capture_cell_raises_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bc.subprocess, "run", side_effect=AssertionError("launched")):
            with self.assertRaises(RuntimeError) as raised:
                bc.capture_cell(tll.LANE_DIR, Path(tmp), Path(tmp), "1", [],
                                env_overrides={"HOME": tmp, "HARMONIC_FORGE_ROOT": ""})
        self.assertIn(str(tll.LANE_DIR), str(raised.exception))
        self.assertIn("git pull --ff-only", str(raised.exception))

    def test_run_script_raises_too(self):
        with tll._FixtureTree() as tree, \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("launched")):
            with self.assertRaisesRegex(RuntimeError, "HARMONIC_FORGE_ROOT is empty"):
                tree.run_script("lane1", HARMONIC_FORGE_ROOT="")

    def test_a_set_root_passes_and_xdg_defaults_to_the_home(self):
        env = {"HOME": "/h", "HARMONIC_FORGE_ROOT": "/r"}
        bc.pin_forge_root(env, tll.LANE_DIR)
        self.assertEqual(env, {"HOME": "/h", "HARMONIC_FORGE_ROOT": "/r", "XDG_CONFIG_HOME": "/h/.config"})


if __name__ == "__main__":
    unittest.main()
