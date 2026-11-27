#!/usr/bin/env python3
"""Tests for `forge_onboard.py --install` (harmonic-forge#1532).

Hermetic: HOME, XDG_CONFIG_HOME, the global git config, the bin dir, the forge
source, the `origin` and the app repo are all under a temp dir. The forge
source is a copy of this checkout's `tools/lane`, reached through
HARMONIC_FORGE_ROOT, so nothing links into this worktree or the operator's
real `~/.local/bin`.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forge_onboard_install as foi  # noqa: E402

FO = HERE / "forge_onboard.py"
LANE_DIR = HERE.parent / "lane"
sys.path.insert(0, str(LANE_DIR))
import check_installed_lane_sources as checker  # noqa: E402


class InstallBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        (self.home / ".config").mkdir(parents=True)
        self.bin = self.home / ".local" / "bin"
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n"
                             "[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n[receive]\n\tautogc = false\n",
                             encoding="utf-8")
        manifest = self.tmp / "projects.toml"
        manifest.write_text('[[project]]\nname = "app"\nprefix = "Z"\n', encoding="utf-8")
        self.forge = self.tmp / "forge"
        shutil.copytree(LANE_DIR, self.forge / "tools" / "lane",
                        ignore=shutil.ignore_patterns("__pycache__", "testdata", "test_*.py"))
        (self.forge / "tools" / "hooks").mkdir()
        (self.forge / "tools" / "hooks" / "README").write_text("fixture\n", encoding="utf-8")
        self.env = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1", "HARMONIC_FORGE_ROOT": str(self.forge),
            "FORGE_PROJECTS_MANIFEST": str(manifest), "LANG": "C.UTF-8",
        }
        self.git("init", "-q", cwd=self.forge)
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", str(origin), cwd=self.tmp)
        seed = self.tmp / "seed"
        self.git("clone", "-q", str(origin), str(seed), cwd=self.tmp)
        (seed / "README").write_text("seed\n", encoding="utf-8")
        self.git("add", "README", cwd=seed)
        self.git("commit", "-qm", "seed", cwd=seed)
        self.git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.app = self.tmp / "src" / "app"
        self.app.parent.mkdir()
        self.git("clone", "-q", str(origin), str(self.app), cwd=self.tmp)
        (self.app / ".claude").mkdir()
        (self.app / ".claude" / "forge.toml").write_text("# S4 overlay\n", encoding="utf-8")
        self.lane2 = self.app.parent / "app-lane2"
        self.lane3 = self.app.parent / "app-lane3"

    def git(self, *args: str, cwd: Path) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL).stdout

    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """The real CLI, no TTY: stdin is /dev/null and output is captured."""
        return subprocess.run([sys.executable, str(FO), *args], cwd=self.app, env={**self.env, **(env or {})},
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    def install(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return self.cli("--install", "--repo", str(self.app), *args, env=env)

    def stamp(self) -> dict:
        return {n: (os.lstat(self.bin / n).st_ino, os.lstat(self.bin / n).st_mtime_ns) for n in checker.LANES}


class Install(InstallBase):
    def test_tc1_links_five_launchers_and_creates_both_worktrees(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in checker.LANES:
            link = self.bin / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(link.resolve(), self.forge / "tools" / "lane" / name)
        origin_main = self.git("rev-parse", "origin/main", cwd=self.app).strip()
        for path in (self.lane2, self.lane3):
            self.assertTrue(path.is_dir(), path)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=path).strip(), origin_main)
            self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip(), "HEAD")
        dry = subprocess.run([str(self.bin / "lane2"), "--agent"], cwd=self.app, env=self.env,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(dry.returncode, 1)
        self.assertIn("lane2: --agent requires a value", dry.stderr)

    def test_tc2_rerun_changes_nothing(self):
        self.assertEqual(self.install().returncode, 0)
        before, worktrees = self.stamp(), self.git("worktree", "list", "--porcelain", cwd=self.app)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing changed", result.stdout)
        self.assertEqual(self.stamp(), before)
        self.assertEqual(self.git("worktree", "list", "--porcelain", cwd=self.app), worktrees)

    def test_tc2b_alias_spelled_link_is_already_in_place(self):
        alias = self.tmp / "alias"
        alias.symlink_to(self.forge)
        self.bin.mkdir(parents=True)
        spelled = str(alias / "tools" / "lane" / "lane2")
        (self.bin / "lane2").symlink_to(spelled)
        inode = os.lstat(self.bin / "lane2").st_ino
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"launcher lane2 — already in place")
        self.assertEqual(os.readlink(self.bin / "lane2"), spelled)
        self.assertEqual(os.lstat(self.bin / "lane2").st_ino, inode)

    def test_tc3_foreign_link_left_alone_unless_forced(self):
        self.bin.mkdir(parents=True)
        elsewhere = str(self.tmp / "elsewhere" / "lane2")
        (self.bin / "lane2").symlink_to(elsewhere)
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(str(self.bin / "lane2"), result.stdout)
        self.assertIn(elsewhere, result.stdout)
        self.assertEqual(os.readlink(self.bin / "lane2"), elsewhere)
        for name in set(checker.LANES) - {"lane2"}:
            self.assertEqual((self.bin / name).resolve(), self.forge / "tools" / "lane" / name)
        forced = self.install("--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertEqual((self.bin / "lane2").resolve(), self.forge / "tools" / "lane" / "lane2")

    def test_tc4_real_file_never_replaced(self):
        self.bin.mkdir(parents=True)
        (self.bin / "lane2").write_text("mine\n", encoding="utf-8")
        for extra in ((), ("--force",)):
            with self.subTest(args=extra):
                result = self.install(*extra)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse((self.bin / "lane2").is_symlink())
                self.assertEqual((self.bin / "lane2").read_text(encoding="utf-8"), "mine\n")

    def test_tc5_source_checker_passes_after_install(self):
        self.assertEqual(self.install().returncode, 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(checker.check(bin_dir=self.bin), 0)
            self.assertEqual(checker.check(self.forge / "tools" / "lane", self.bin), 0)


class InstallEdges(InstallBase):
    def test_tc6_repo_at_a_lane_worktree_reports_the_same_pair(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.cli("--install", "--repo", str(self.lane2))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("worktree app-lane2 — already present", result.stdout)
        self.assertIn("worktree app-lane3 — already present", result.stdout)
        self.assertFalse((self.app.parent / "app-lane2-lane2").exists())

    def test_tc6_argument_exclusions_exit_2(self):
        for args in (("--install", "--apply"), ("--install", "--project", "app"),
                     ("--install", "--manifest", "x.toml"), ("--force",), ("--repo", ".")):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)
        self.assertFalse(self.bin.exists())

    def test_tc6_path_line_only_when_bin_is_off_path(self):
        off = self.install()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', off.stdout)
        on = self.install(env={"PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}"})
        self.assertNotIn("export PATH=", on.stdout)

    def test_tc6_missing_manifest_exits_2_without_traceback(self):
        result = self.install(env={"FORGE_PROJECTS_MANIFEST": str(self.tmp / "nope.toml")})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_tc6_deleted_worktree_is_reported_never_pruned(self):
        self.assertEqual(self.install().returncode, 0)
        shutil.rmtree(self.lane3)
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"git -C {self.app} worktree prune", result.stdout)
        self.assertIn(str(self.lane3), self.git("worktree", "list", "--porcelain", cwd=self.app))
        self.assertFalse(self.lane3.exists())

    def test_present_path_not_a_worktree_is_left_alone(self):
        self.lane3.mkdir()
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("is not a worktree of", result.stdout)
        self.assertEqual(list(self.lane3.iterdir()), [])

    def test_linked_worktree_source_is_refused(self):
        self.git("add", "-A", cwd=self.forge)
        self.git("commit", "-qm", "forge", cwd=self.forge)
        linked = self.tmp / "forge-wt"
        self.git("worktree", "add", "-q", "--detach", str(linked), cwd=self.forge)
        result = self.install(env={"HARMONIC_FORGE_ROOT": str(linked)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("linked git worktree", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_not_a_repo_or_no_origin_exits_2_before_writing(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(self.cli("--install", "--repo", str(plain)).returncode, 2)
        self.git("remote", "remove", "origin", cwd=self.app)
        result = self.install()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("origin", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_dry_run_writes_nothing(self):
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would link", result.stdout)
        self.assertIn("would create", result.stdout)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.lane2.exists() or self.lane3.exists())

    def test_derivation_matches_the_launchers_bash_rule(self):
        for name in ("X", "X-lane2", "X-lane12", "X-lane", "a-lane2-lane3"):
            with self.subTest(name=name):
                repo = self.tmp / "d" / name
                repo.mkdir(parents=True)
                self.git("init", "-q", cwd=repo)
                bash = subprocess.run(["bash", "-c", 'b="$1"; [[ "$b" =~ ^(.+)-lane[0-9]+$ ]] && '
                                       'b="${BASH_REMATCH[1]}"; printf %s "$b"', "_", name],
                                      capture_output=True, text=True, check=True).stdout
                self.assertEqual(foi.lane_worktree_paths(repo),
                                 [repo.parent / f"{bash}-lane2", repo.parent / f"{bash}-lane3"])


if __name__ == "__main__":
    unittest.main()
