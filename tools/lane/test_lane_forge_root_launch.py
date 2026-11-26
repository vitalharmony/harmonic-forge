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

import shutil
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
            shutil.rmtree(tree.forge)
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
            shutil.copytree(WORKTREE / "tools" / "hooks", root / "tools" / "hooks",
                            ignore=shutil.ignore_patterns("__pycache__"))
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


class RootRefusalLaunch(unittest.TestCase):
    """Round 1 survivor 3: a root that does not resolve refuses the launch at
    `_lane_forge_root.sh`'s own refusal line, before the pull or the build."""

    def test_a_root_with_no_tools_hooks_launches_nothing(self):
        with tll._FixtureTree() as tree:
            for name in ("missing", "bare"):
                root = tree.root / name
                if name == "bare":
                    root.mkdir()
                with self.subTest(root=name):
                    cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT=str(root))
                    self.assertFalse(cell["launched"], cell["stderr"])
                    self.assertNotEqual(cell["returncode"], 0)
                    self.assertIn("the forge root could not be resolved", cell["stderr"])
                    self.assertIn(f"forge root {str(root)!r} (from HARMONIC_FORGE_ROOT", cell["stderr"])
                    self.assertNotIn("sync_rules.py --pull failed", cell["stderr"])


class EmptyRootFieldsRefuse(unittest.TestCase):
    """Post-verdict check 2, survivor 2: a `root` producer that exits 0 with an
    empty field refuses the launch at `_lane_forge_root.sh`, naming the value
    as written where there is one, before the pull or any other hookset call."""

    _STUB = ("import sys\nfrom pathlib import Path\n"
             "if sys.argv[1:2] != ['root']:\n"
             "    Path(__file__).with_name('called-past-root').write_text(' '.join(sys.argv[1:]))\n"
             "    sys.exit(1)\n"
             "sys.stdout.write(''.join(f'{f}\\0' for f in {fields!r}))\n")

    def _launch(self, fields: list[str]) -> tuple[dict, Path]:
        with tll._FixtureTree() as tree:
            tree.lane_dir = tll.RegistryIntegrity._copy_lane_dir(None, tree.root)
            stub = tree.lane_dir / "lane_hookset.py"
            stub.unlink()
            stub.write_text(self._STUB.replace("{fields!r}", repr(fields)))
            cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT="")
            return cell, (tree.lane_dir / "called-past-root").exists()

    def test_four_empty_fields_refuse(self):
        cell, past = self._launch(["", "", "", ""])
        self.assertFalse(cell["launched"], cell["stderr"])
        self.assertNotEqual(cell["returncode"], 0)
        self.assertIn("the root producer exited 0 with empty field(s): set root source as-written",
                      cell["stderr"])
        self.assertNotIn("sync_rules.py --pull failed", cell["stderr"])
        self.assertFalse(past, cell["stderr"])

    def test_an_empty_root_names_the_value_as_written(self):
        cell, past = self._launch(["/fx/lane_hooks.json", "", "HARMONIC_FORGE_ROOT", "/fx/as-written"])
        self.assertFalse(cell["launched"], cell["stderr"])
        self.assertIn("forge root '/fx/as-written' (from HARMONIC_FORGE_ROOT): the root producer "
                      "exited 0 with empty field(s): root", cell["stderr"])
        self.assertNotIn("forge root  (from", cell["stderr"])
        self.assertFalse(past, cell["stderr"])


class BuildRefusalNamesTheValueAsWritten(unittest.TestCase):
    """Sticky-wicket C1 (and D1's build refusal under a non-default source): a
    root that resolves but lacks a target refuses at the build, naming the
    `forge_root` value exactly as written, then its source, then its
    resolved form."""

    def test_a_home_relative_forge_root_is_named_as_written(self):
        with tll._FixtureTree() as tree:
            tree.lane_dir = tll.RegistryIntegrity._copy_lane_dir(None, tree.root)
            (tree.home / "fx").mkdir()
            fx2 = tree.home / "fx2"
            shutil.copytree(WORKTREE / "tools" / "hooks", fx2 / "tools" / "hooks",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (fx2 / "tools" / "hooks" / "block_blind_repeat.py").unlink()
            with tree.instance_toml.open("a", encoding="utf-8") as fh:
                fh.write('forge_root = "~/fx/../fx2"\n')
            cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT="")
            self.assertFalse(cell["launched"], cell["stderr"])
            self.assertIn("could not be built", cell["stderr"])
            self.assertIn(f"forge root '~/fx/../fx2' (from {tree.instance_toml} forge_root; "
                          f"resolves to {str(fx2)!r})", cell["stderr"])
            self.assertIn("block_blind_repeat.py", cell["stderr"])

    def test_a_tab_in_the_instance_toml_path_keeps_every_field(self):
        """Post-verdict survivor 2: the launch view crosses into the launcher
        NUL-ended, so a tab in the source's instance.toml path (under a HOME
        holding one) cannot shift the value as written into the source."""
        with tll._FixtureTree() as tree:
            tree.lane_dir = tll.RegistryIntegrity._copy_lane_dir(None, tree.root)
            fx2 = tree.root / "fx2"
            shutil.copytree(WORKTREE / "tools" / "hooks", fx2 / "tools" / "hooks",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (fx2 / "tools" / "hooks" / "block_blind_repeat.py").unlink()
            home = tree.root / "ho\tme"
            shutil.copytree(tree.home, home, symlinks=True)
            toml = home / ".config" / "harmonic-forge" / "instance.toml"
            written = f"{fx2}/tools/.."
            with toml.open("a", encoding="utf-8") as fh:
                fh.write(f'forge_root = "{written}"\n')
            cell = tree.run("1", [], LANE_CLI="claude", HARMONIC_FORGE_ROOT="", HOME=str(home))
            self.assertFalse(cell["launched"], cell["stderr"])
            self.assertIn("could not be built", cell["stderr"])
            self.assertIn(f"forge root {written!r} (from {toml} forge_root; resolves to {str(fx2)!r})",
                          cell["stderr"])


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
