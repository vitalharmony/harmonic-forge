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
import dataclasses
import http.server
import inspect
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forge_onboard_install as foi  # noqa: E402
import worktree_table as wt  # noqa: E402

FO = HERE / "forge_onboard.py"
LANE_DIR = HERE.parent / "lane"
sys.path.insert(0, str(LANE_DIR))
import check_installed_lane_sources as checker  # noqa: E402


def by_path(records) -> dict:
    return {r.path.resolve(): r for r in records}


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

    def run_main(self, *args: str, env: dict | None = None, repo: Path | None = None) -> tuple[int, str, str]:
        """`forge_onboard.main` in-process (so a fault can be injected), with
        the same hermetic environment the CLI tests get."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **(env or {})}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = foi.fo.main(["--install", "--repo", str(repo or self.app), *args])
        return code, out.getvalue(), err.getvalue()

    def advance_origin(self, ref: str = "main") -> str:
        """One new commit on origin's `ref`, pushed from the seed clone; its SHA."""
        seed = self.tmp / "seed"
        (seed / "README").write_text(f"{ref} {os.urandom(4).hex()}\n", encoding="utf-8")
        self.git("commit", "-qam", f"advance {ref}", cwd=seed)
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{ref}", cwd=seed)
        return self.git("rev-parse", "HEAD", cwd=seed).strip()

    def origin_main(self) -> str:
        return self.git("ls-remote", str(self.tmp / "origin.git"), "refs/heads/main", cwd=self.tmp).split("\t")[0]

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


#: tools/lane/lane2:27-35, verbatim but for the repo argument.
_LAUNCHER_TARGET = ('root="$(git -C "$1" rev-parse --show-toplevel)"; parent="$(dirname "$root")"; '
                    'base="$(basename "$root")"; [[ "$base" =~ ^(.+)-lane[0-9]+$ ]] && '
                    'base="${BASH_REMATCH[1]}"; printf %s "$parent/$base-lane2"')


class _Unauthorized(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="t"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


class PassTwo(InstallBase):
    """harmonic-forge#1532 pass-2: the two-phase boundary and the local fixes."""

    def chmod(self, path: Path, mode: int) -> None:
        old = path.stat().st_mode & 0o7777
        path.chmod(mode)
        self.addCleanup(path.chmod, old)

    # Finding :180 / :107 -- the per-item wrap.
    def test_permission_error_on_second_link_keeps_every_check(self):
        real, calls = Path.symlink_to, []

        def flaky(path, target, *a, **k):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, target, *a, **k)

        with mock.patch.object(Path, "symlink_to", flaky):
            code, out, err = self.run_main()
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", err)
        first, second = calls[0].name, calls[1].name
        self.assertRegex(out, rf"\[  ok  \] launcher {first} — linked ")
        self.assertRegex(out, rf"\[ FAIL \] launcher {second} — linking .* Permission denied")
        for name in set(checker.LANES) - {second}:
            self.assertTrue((self.bin / name).is_symlink(), name)
        self.assertIn("worktree app-lane2 — created", out)

    def test_unwritable_bin_parent_fails_every_launcher_exit_1(self):
        (self.home / ".local").mkdir()
        self.chmod(self.home / ".local", 0o555)
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        for name in checker.LANES:
            self.assertRegex(result.stdout, rf"\[ FAIL \] launcher {name} — cannot create .*Permission denied")
        self.assertIn("worktree app-lane2 — created", result.stdout)

    def test_unreadable_bin_parent_dry_run_fails_per_item(self):
        self.bin.mkdir(parents=True)
        self.chmod(self.home / ".local", 0o000)
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        for name in checker.LANES:
            self.assertRegex(result.stdout, rf"\[ FAIL \] launcher {name} — cannot inspect .*Permission denied")

    def test_write_phase_never_raises_install_error(self):
        preflight = [f.__name__ for f in foi.PREFLIGHT]
        for fn in foi.WRITE_PHASE:
            source = inspect.getsource(fn)
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("InstallError", source)
                for name in preflight:
                    self.assertNotIn(f"{name}(", source)

    # Finding :60 -- `_git` returns a failed result; the caller maps it by phase.
    def test_git_missing_exits_2_named_without_traceback(self):
        empty = self.tmp / "nogit"
        empty.mkdir()
        result = self.install(env={"PATH": str(empty)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot run git", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_git_launch_failure_after_first_write_is_a_fail_check(self):
        real = foi.subprocess.run

        def no_git_for_add(cmd, *a, **k):
            if "add" in cmd:
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real(cmd, *a, **k)

        with mock.patch.object(foi.subprocess, "run", no_git_for_add):
            code, out, err = self.run_main()
        self.assertEqual(code, 1, out + err)
        self.assertRegex(out, r"\[ FAIL \] worktree app-lane2 — cannot run git")
        self.assertRegex(out, r"\[  ok  \] launcher lane1 — linked ")

    # Finding :77 -- the linked-worktree guard fails closed.
    def test_unreadable_forge_git_dir_is_refused(self):
        self.git("add", "-A", cwd=self.forge)
        self.git("commit", "-qm", "forge", cwd=self.forge)
        linked = self.tmp / "forge-wt"
        self.git("worktree", "add", "-q", "--detach", str(linked), cwd=self.forge)
        (linked / ".git").write_text(f"gitdir: {self.tmp / 'gone'}\n", encoding="utf-8")
        for extra in ((), ("--dry-run",)):
            with self.subTest(args=extra):
                result = self.install(*extra, env={"HARMONIC_FORGE_ROOT": str(linked)})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("cannot tell whether", result.stderr)
                self.assertNotIn("would link", result.stdout)
                self.assertFalse(self.bin.exists())

    # Finding :144 -- a failed worktree listing is never read as an empty one.
    def test_failed_worktree_listing_is_install_error(self):
        real = foi._git_raw

        def failing_list(args, cwd):
            if args[:2] == ["worktree", "list"]:
                good = real(args, cwd)
                return subprocess.CompletedProcess(args, 128, good.stdout, b"fatal: boom")
            return real(args, cwd)

        with mock.patch.object(foi, "_git_raw", failing_list), self.assertRaisesRegex(foi.InstallError, "boom"):
            foi.plan_worktrees(self.app)

    # Finding :180 -- origin/main is reached before the first write.
    def no_main_origin(self) -> None:
        empty = self.tmp / "empty.git"
        self.git("init", "-q", "--bare", str(empty), cwd=self.tmp)
        self.git("remote", "set-url", "origin", str(empty), cwd=self.app)

    def test_fetch_failure_exits_2_before_any_write(self):
        real = foi._git

        def failing_fetch(args, cwd):
            if args[:1] == ["fetch"]:
                return subprocess.CompletedProcess(args, 128, "", "fatal: unreachable")
            return real(args, cwd)

        with mock.patch.object(foi, "_git", failing_fetch):
            code, out, err = self.run_main()
        self.assertEqual(code, 2, out + err)
        self.assertIn("git fetch origin main failed", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.lane2.exists() or self.lane3.exists())

    def test_missing_origin_main_exits_2_before_any_write(self):
        self.no_main_origin()
        result = self.install()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("origin has no main branch", result.stderr)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.lane2.exists() or self.lane3.exists())

    def test_dry_run_predicts_missing_origin_main(self):
        self.no_main_origin()
        result = self.install("--dry-run")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("origin has no main branch", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_no_remote_probe_when_both_worktrees_exist(self):
        self.assertEqual(self.install().returncode, 0)
        self.no_main_origin()
        for extra in ((), ("--dry-run",)):
            with self.subTest(args=extra):
                result = self.install(*extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # Finding :165 -- a worktree's identity, not its listing.
    def test_reinitialised_worktree_is_not_present(self):
        self.assertEqual(self.install().returncode, 0)
        shutil.rmtree(self.lane2)
        self.lane2.mkdir()
        self.git("init", "-q", cwd=self.lane2)
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\[ FAIL \] worktree app-lane2 — .* is listed as a worktree of .* "
                                        r"but is not one")
        self.assertIn("worktree app-lane3 — already present", result.stdout)

    def test_prunable_worktree_that_exists_names_both_steps(self):
        self.assertEqual(self.install().returncode, 0)
        (self.lane3 / ".git").unlink()
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\[ FAIL \] worktree app-lane3 — .*prunable.*move it aside, then "
                                        r"run: git -C .* worktree prune")
        self.assertTrue(self.lane3.is_dir())

    def test_symlinked_worktree_path_is_not_present(self):
        self.assertEqual(self.install().returncode, 0)
        self.git("worktree", "remove", "--force", str(self.lane2), cwd=self.app)
        self.lane2.symlink_to(self.app)
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\[ FAIL \] worktree app-lane2 — .* is a symlink")
        self.assertTrue(self.lane2.is_symlink())

    # Finding :204 -- every launcher is probed for a PATH shadow.
    def test_shadow_probe_covers_every_launcher(self):
        self.assertEqual(self.install().returncode, 0)
        early = self.tmp / "early"
        early.mkdir()
        stale = early / "lane3"
        stale.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stale.chmod(0o755)
        result = self.install(env={"PATH": os.pathsep.join([str(early), str(self.bin), self.env["PATH"]])})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"lane3 on PATH resolves to {stale}", result.stdout)
        self.assertNotIn("lane2 on PATH resolves", result.stdout)

    # Finding :94 -- only the trailing newline is removed.
    def test_trailing_space_checkout_name_matches_the_launcher(self):
        for name in ("X ", "X-lane2 ", "X\t"):
            with self.subTest(name=name):
                repo = self.tmp / "s" / name
                repo.mkdir(parents=True)
                self.git("init", "-q", cwd=repo)
                bash = subprocess.run(["bash", "-c", _LAUNCHER_TARGET, "_", str(repo)], env=self.env,
                                      capture_output=True, text=True, check=True).stdout
                self.assertEqual(foi.lane_worktree_paths(repo)[0], Path(bash))

    # Finding :61 -- no askpass, no controlling terminal.
    def test_git_env_is_non_interactive(self):
        seen = {}

        def record(cmd, *a, **k):
            seen.update(k)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with mock.patch.object(foi.subprocess, "run", record):
            foi._git(["status"], self.app)
        self.assertTrue(seen["start_new_session"])
        self.assertNotIn("text", seen)
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        for key, value in {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false",
                           "SSH_ASKPASS_REQUIRE": "never", "GCM_INTERACTIVE": "false"}.items():
            self.assertEqual(seen["env"].get(key), value, key)
        self.assertNotIn("GIT_SSH_COMMAND", seen["env"])

    def test_configured_askpass_is_never_run(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Unauthorized)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        marker = self.tmp / "asked"
        askpass = self.tmp / "askpass"
        askpass.write_text(f"#!/bin/sh\necho asked >> {marker}\necho x\n", encoding="utf-8")
        askpass.chmod(0o755)
        self.git("remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/r.git", cwd=self.app)
        env = {**self.env, "GIT_ASKPASS": str(askpass), "SSH_ASKPASS": str(askpass),
               "NO_PROXY": "*", "no_proxy": "*"}
        with mock.patch.dict(os.environ, env, clear=True):
            proc = foi._git(["ls-remote", "origin"], self.app)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(marker.exists(), "a configured askpass program was run")


class OriginSha(InstallBase):
    """harmonic-forge#1532 two-pass PATCH, P1: the worktree is cut at exactly
    the SHA `git ls-remote` named, never at the local origin/main."""

    def heads(self) -> list[str]:
        return [self.git("rev-parse", "HEAD", cwd=p).strip() for p in (self.lane2, self.lane3)]

    def test_stale_local_origin_main_is_never_used(self):  # K9
        self.git("config", "remote.origin.fetch", "+refs/heads/dev:refs/remotes/origin/dev", cwd=self.app)
        stale = self.git("rev-parse", "refs/remotes/origin/main", cwd=self.app).strip()
        fresh = self.advance_origin()
        self.assertNotEqual(stale, fresh)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.heads(), [fresh, fresh])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=self.app).strip(), stale)
        self.assertIn(f"detached at {fresh[:12]} (origin/main)", result.stdout)

    def test_single_branch_clone_of_another_branch_succeeds(self):  # K10
        self.advance_origin("dev")
        shutil.rmtree(self.app)
        self.git("clone", "-q", "--single-branch", "--branch", "dev", str(self.tmp / "origin.git"),
                 str(self.app), cwd=self.tmp)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.heads(), [self.origin_main()] * 2)

    def test_fetch_that_brings_nothing_is_install_error(self):  # K11
        self.advance_origin()
        real = foi._git

        def empty_fetch(args, cwd):
            if args[:1] == ["fetch"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return real(args, cwd)

        with mock.patch.object(foi, "_git", empty_fetch):
            code, out, err = self.run_main()
        self.assertEqual(code, 2, out + err)
        self.assertIn("is not here after git fetch origin main", err)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.lane2.exists() or self.lane3.exists())

    def test_decoy_ref_matching_by_tail_is_ignored(self):
        main = self.origin_main()
        decoy = self.advance_origin("x/refs/heads/main")
        self.assertNotEqual(decoy, main)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.heads(), [main, main])

    def test_fast_forward_between_ls_remote_and_fetch_lands_on_the_proved_sha(self):
        proved, real, moved = self.origin_main(), foi._git, []

        def racing_fetch(args, cwd):
            if args[:1] == ["fetch"] and not moved:
                moved.append(self.advance_origin())
            return real(args, cwd)

        with mock.patch.object(foi, "_git", racing_fetch):
            code, out, err = self.run_main()
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(moved[0], proved)
        self.assertEqual(self.heads(), [proved, proved])

    def test_dry_run_names_the_sha_and_never_fetches(self):
        real, calls = foi._git, []

        def record(args, cwd):
            calls.append(args)
            return real(args, cwd)

        with mock.patch.object(foi, "_git", record):
            code, out, err = self.run_main("--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"would create {self.lane2} at {self.origin_main()[:12]} (origin/main)", out)
        self.assertFalse([c for c in calls if c[:1] == ["fetch"]], calls)

    def test_malformed_ls_remote_sha_is_install_error(self):
        real = foi._git

        def bad(args, cwd):
            if args[:1] == ["ls-remote"]:
                return subprocess.CompletedProcess(args, 0, "abc\trefs/heads/main\n", "")
            return real(args, cwd)

        with mock.patch.object(foi, "_git", bad), self.assertRaisesRegex(foi.InstallError, "printed"):
            foi._reach_origin_main(self.app, dry_run=True)

    def test_old_git_is_install_error(self):
        real = foi._git

        def old(args, cwd):
            if args == ["version"]:
                return subprocess.CompletedProcess(args, 0, "git version 2.35.9\n", "")
            return real(args, cwd)

        with mock.patch.object(foi, "_git", old):
            code, out, err = self.run_main()
        self.assertEqual(code, 2, out + err)
        self.assertIn("too old", err)
        self.assertFalse(self.bin.exists())


class WorktreeTable(InstallBase):
    """harmonic-forge#1532 reforge (D86): one test per row of
    `worktree_table.ROWS` (rows 1, 4, 5, 6, 7, 9 and 11 also keep their
    earlier end-to-end tests in InstallEdges / PassTwo). `inspect` returns
    the Check `forge_onboard_install` makes from the typed Outcome."""

    def outcome(self, path: Path) -> wt.Outcome:
        registered = wt.read_listing(self.app, foi._git_raw)
        common = wt.common_dir(self.app, foi._git_raw)
        return wt.classify(path, self.app, common, registered, foi._git_raw)

    def inspect(self, path: Path):
        return foi._outcome_check(path, self.outcome(path))

    def installed(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_row01_symlink(self):
        self.lane2.symlink_to(self.app)
        self.assertRegex(self.inspect(self.lane2).detail, "is a symlink")

    def test_row02_main_checkout_is_install_error_without_an_identity_probe(self):  # K12
        main = self.app.parent / "app-lane2"
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(main), cwd=self.tmp)
        with mock.patch.object(wt, "probe_identity", return_value="fatal: stubbed unreadable"):
            code, out, err = self.run_main(repo=main)
        self.assertEqual(code, 2, out + err)
        self.assertIn("main checkout", err)
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.app.parent / "app-lane3").exists())

    def test_row02_bare_main_record(self):
        bare = self.tmp / "origin.git"
        linked = self.tmp / "bare-wt"
        self.git("worktree", "add", "-q", "--detach", str(linked), "main", cwd=bare)
        records = wt.read_listing(bare, foi._git_raw)
        registered = by_path(records)
        main = registered[bare.resolve()]
        self.assertTrue(main.is_main and main.bare, main)
        self.assertFalse(registered[linked.resolve()].is_main)
        outcome = wt.classify(bare, bare, bare.resolve(), records, foi._git_raw)
        self.assertEqual((outcome.row, outcome.kind), (2, wt.MAIN))
        with self.assertRaisesRegex(foi.InstallError, "main checkout"):
            foi._outcome_check(bare, outcome)

    def test_row03_absent_locked_names_unlock_then_prune(self):  # K13
        self.installed()
        self.git("worktree", "lock", str(self.lane3), cwd=self.app)
        shutil.rmtree(self.lane3)
        for extra in (("--dry-run",), ()):
            with self.subTest(args=extra):
                result = self.install(*extra)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"git -C {self.app} worktree unlock {self.lane3} && "
                              f"git -C {self.app} worktree prune", result.stdout)
                self.assertNotIn("would create", result.stdout)
                self.assertFalse(self.lane3.exists())

    def test_row04_absent_prunable(self):
        self.installed()
        shutil.rmtree(self.lane3)
        check = self.inspect(self.lane3)
        self.assertEqual(check.status, foi.FAIL)
        self.assertIn("still registered", check.detail)
        self.assertNotIn("unlock", check.detail)

    def test_row05_absent_unlisted_is_created(self):
        self.assertIsNone(self.inspect(self.lane2))

    def test_row06_present_prunable(self):
        self.installed()
        (self.lane3 / ".git").unlink()
        self.assertIn("prunable worktree", self.inspect(self.lane3).detail)
        self.assertIn("move it aside", self.inspect(self.lane3).detail)

    def test_row07_present_unlisted(self):
        self.lane3.mkdir()
        self.assertIn("is not a worktree of", self.inspect(self.lane3).detail)

    def test_row08_listed_but_unreadable(self):
        self.installed()
        with mock.patch.object(wt, "probe_identity", return_value="fatal: stubbed"):
            self.assertIn("git cannot read it", self.inspect(self.lane2).detail)

    def test_row09_identity_mismatch(self):
        self.installed()
        shutil.rmtree(self.lane2)
        self.lane2.mkdir()
        self.git("init", "-q", cwd=self.lane2)
        self.assertIn("but is not one", self.inspect(self.lane2).detail)

    def test_row10_git_dir_backstop_catches_a_main_checkout_not_marked_main(self):  # K14
        main = self.app.parent / "app-lane2"
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(main), cwd=self.tmp)
        real = wt.read_listing

        def unmarked(repo, run):
            return tuple(dataclasses.replace(r, is_main=False) for r in real(repo, run))

        with mock.patch.object(wt, "read_listing", unmarked):
            code, out, err = self.run_main(repo=main)
        self.assertEqual(code, 2, out + err)
        self.assertIn("main checkout", err)
        self.assertFalse(self.bin.exists())

    def test_row11_linked_locked_worktree_is_present(self):
        self.installed()
        self.git("worktree", "lock", str(self.lane3), cwd=self.app)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("worktree app-lane3 — already present", result.stdout)

    def test_row12_default_deny_names_the_record(self):
        record = wt.Record(path=self.lane2.resolve(), is_main=False)
        outcome = wt.classify(self.lane2, self.app, self.app / ".git", (record,), foi._git_raw)
        self.assertEqual((outcome.row, outcome.kind), (12, wt.FAIL))
        self.assertIn("Record(", outcome.detail)
        self.assertEqual(foi._outcome_check(self.lane2, outcome).status, foi.FAIL)

    def test_newline_path_does_not_shift_attributes(self):
        self.installed()
        odd = self.app.parent / "x\nlocked trap"
        self.git("worktree", "add", "-q", "--detach", str(odd), cwd=self.app)
        self.git("worktree", "lock", "--reason", "two\nlines", str(self.lane3), cwd=self.app)
        registered = by_path(wt.read_listing(self.app, foi._git_raw))
        self.assertFalse(registered[odd.resolve()].locked)
        self.assertTrue(registered[self.lane3.resolve()].locked)
        self.assertFalse(registered[self.lane2.resolve()].locked)
        self.assertTrue(registered[self.app.resolve()].is_main)

    def test_malformed_listing_is_install_error(self):
        real = foi._git_raw

        def garbled(args, cwd):
            if args[:2] == ["worktree", "list"]:
                return subprocess.CompletedProcess(args, 0, b"HEAD abc\0\0", b"")
            return real(args, cwd)

        with mock.patch.object(foi, "_git_raw", garbled), self.assertRaisesRegex(foi.InstallError, "worktree list"):
            foi.plan_worktrees(self.app)

    def test_row11_present_is_ok_via_the_typed_result(self):  # K21
        self.installed()
        outcome = self.outcome(self.lane2)
        self.assertEqual((outcome.row, outcome.kind), (11, wt.PRESENT))
        self.assertEqual(self.inspect(self.lane2).status, foi.OK)

    def test_row12_unknown_key_at_a_lane_path_is_default_denied(self):  # K27
        self.installed()
        records = tuple(dataclasses.replace(r, unknown=("newattr",)) if r.path.resolve() == self.lane2.resolve()
                        else r for r in wt.read_listing(self.app, foi._git_raw))
        common = wt.common_dir(self.app, foi._git_raw)
        outcome = wt.classify(self.lane2, self.app, common, records, foi._git_raw)
        self.assertEqual((outcome.row, outcome.kind), (12, wt.FAIL))
        self.assertIn("newattr", outcome.detail)
        self.assertEqual(wt.classify(self.lane3, self.app, common, records, foi._git_raw).row, 11)

    def test_row12_two_records_at_one_lane_path_are_default_denied(self):  # K22
        self.installed()
        records = wt.read_listing(self.app, foi._git_raw)
        twin = [r for r in records if r.path.resolve() == self.lane2.resolve()][0]
        outcome = wt.classify(self.lane2, self.app, wt.common_dir(self.app, foi._git_raw),
                              (*records, dataclasses.replace(twin, locked=True)), foi._git_raw)
        self.assertEqual((outcome.row, outcome.kind), (12, wt.FAIL))
        self.assertIn("locked=True", outcome.detail)
        self.assertIn("locked=False", outcome.detail)


class ListingParser(unittest.TestCase):
    """harmonic-forge#1532 reforge (D86), S1: every malformed listing is a
    ListingError, never an empty or partial listing."""

    def refused(self, raw: bytes, why: str) -> None:
        with self.assertRaisesRegex(wt.ListingError, why):
            wt.parse_listing(raw)

    def test_well_formed_shapes_parse(self):
        records = wt.parse_listing(b"worktree /m\0HEAD " + b"a" * 40 + b"\0branch refs/heads/main\0\0"
                                   b"worktree /l\0HEAD " + b"0" * 40 + b"\0detached\0locked\0\0"
                                   b"worktree /r\0HEAD " + b"c" * 40 + b"\0detached\0locked two\nlines\0"
                                   b"prunable gitdir file points to non-existent location\0\0")
        self.assertEqual([(str(r.path), r.is_main, r.locked, r.prunable) for r in records],
                         [("/m", True, False, False), ("/l", False, True, False), ("/r", False, True, True)])
        bare = wt.parse_listing(b"worktree /b.git\0bare\0\0")
        self.assertTrue(bare[0].is_main and bare[0].bare)

    def test_empty_listing(self):  # K23
        self.refused(b"", "printed nothing")

    def test_record_without_trailing_terminator(self):  # K24
        for raw in (b"worktree /a\0HEAD x\0", b"worktree /a"):
            with self.subTest(raw=raw):
                self.refused(raw, "ends mid-record")

    def test_record_without_terminator_mid_listing(self):  # K25 (the post-verdict survivor string)
        self.refused(b"worktree /a\0HEAD x\0worktree /a-lane3\0locked\0\0", "repeats `worktree`")

    def test_duplicate_attribute(self):  # K25
        self.refused(b"worktree /a\0locked\0locked x\0\0", "repeats `locked`")
        self.refused(b"worktree /a\0newattr\0newattr\0\0", "repeats `newattr`")

    def test_record_not_starting_with_worktree_or_with_an_empty_field(self):  # K26
        self.refused(b"HEAD abc\0\0", "does not start with")
        self.refused(b"worktreeX /a\0\0", "does not start with")
        self.refused(b"worktree /a\0\0\0", "empty field")
        self.refused(b"worktree /a\0\0\0\0worktree /b\0\0", "does not start with")

    def test_unknown_key_is_recorded_not_refused(self):  # K27
        (record,) = wt.parse_listing(b"worktree /a\0HEAD " + b"a" * 40 + b"\0newattr some value\0\0")
        self.assertEqual(record.unknown, ("newattr",))

    def test_attribute_in_the_wrong_form(self):  # K28
        for raw in (b"worktree /a\0bare x\0\0", b"worktree /a\0detached y\0\0", b"worktree /a\0HEAD\0\0",
                    b"worktree \0\0", b"worktree\0\0", b"worktree /a\0locked \0\0", b"worktree /a\0prunable \0\0"):
            with self.subTest(raw=raw):
                self.refused(raw, "documented form")

    def test_non_utf8_path(self):  # K29
        self.refused(b"worktree /a\xff\0\0", "not UTF-8")


class PathBytes(InstallBase):
    """harmonic-forge#1532 reforge (D86), S2/S3, F9, the identity probe and
    the toplevel: path bytes reach every comparison exactly as git and the
    filesystem hold them."""

    def odd_clone(self, parent: str | bytes) -> Path:
        main = Path(os.fsdecode(os.path.join(os.fsencode(self.tmp), os.fsencode(parent)))) / "app-lane2"
        main.parent.mkdir()
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(main), cwd=self.tmp)
        return main

    def test_cr_in_a_main_checkout_path_is_still_the_main_checkout(self):  # K30
        main = self.odd_clone("a\rb")
        code, out, err = self.run_main(repo=main)
        self.assertEqual(code, 2, out + err)
        self.assertIn("main checkout", err)
        self.assertFalse(self.bin.exists())

    def test_cr_in_the_toplevel_reaches_the_lane_paths(self):  # K31
        main = self.odd_clone("c\rd")
        self.assertEqual(foi.lane_worktree_paths(main), [main.parent / "app-lane2", main.parent / "app-lane3"])

    def test_listing_that_exits_0_and_prints_nothing_is_install_error(self):  # K23, end to end
        main = self.odd_clone("e")
        real = foi._git_raw

        def empty(args, cwd):
            if args[:2] == ["worktree", "list"]:
                return subprocess.CompletedProcess(args, 0, b"", b"")
            return real(args, cwd)

        with mock.patch.object(foi, "_git_raw", empty):
            code, out, err = self.run_main(repo=main)
        self.assertEqual(code, 2, out + err)
        self.assertIn("printed nothing", err)
        self.assertFalse(self.bin.exists())

    def test_non_utf8_worktree_anywhere_is_install_error(self):  # K29, end to end
        odd = os.fsencode(self.app.parent) + b"/odd\xff"
        subprocess.run([b"git", b"worktree", b"add", b"-q", b"--detach", odd], cwd=self.app, env=self.env,
                       check=True, capture_output=True)
        with self.assertRaisesRegex(foi.InstallError, "not UTF-8"):
            foi.plan_worktrees(self.app)

    def test_non_utf8_toplevel_is_install_error(self):  # K36
        main = self.odd_clone(b"t\xff")
        with self.assertRaisesRegex(foi.InstallError, "toplevel .* is not UTF-8"):
            foi.lane_worktree_paths(main)

    def test_unreadable_common_dir_is_install_error(self):  # K35
        real = foi._git_raw
        for printed in (b"/x\xff\n", b"", b"\n", b"/x"):
            def odd(args, cwd, printed=printed):
                if args == ["rev-parse", "--path-format=absolute", "--git-common-dir"] and cwd == self.app:
                    return subprocess.CompletedProcess(args, 0, printed, b"")
                return real(args, cwd)

            with self.subTest(printed=printed), mock.patch.object(foi, "_git_raw", odd), \
                    self.assertRaisesRegex(foi.InstallError, "common dir"):
                foi.plan_worktrees(self.app)

    def test_newline_in_a_parent_directory_keeps_a_present_worktree_ok(self):  # K33
        nl = self.tmp / "n\nl"
        nl.mkdir()
        self.app.rename(nl / "app")
        self.app, self.lane2, self.lane3 = nl / "app", nl / "app-lane2", nl / "app-lane3"
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("worktree app-lane2 — already present", result.stdout)

    def test_relisting_failure_after_a_failed_add_is_a_fail_check(self):  # K34
        real_git, real_list = foi._git, wt.read_listing
        calls = []

        def failing_add(args, cwd):
            if args[:2] == ["worktree", "add"]:
                return subprocess.CompletedProcess(args, 128, "", "fatal: stubbed add failure")
            return real_git(args, cwd)

        def listing(repo, run):
            calls.append(repo)
            if len(calls) > 1:
                raise wt.ListingError("stubbed re-list failure")
            return real_list(repo, run)

        with mock.patch.object(foi, "_git", failing_add), mock.patch.object(wt, "read_listing", listing):
            code, out, err = self.run_main()
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("Traceback", err)
        self.assertRegex(out, r"\[ FAIL \] worktree app-lane2 — fatal: stubbed add failure; could not re-list "
                              r"worktrees \(stubbed re-list failure\)")



class EpochOnePassOne(InstallBase):
    """harmonic-forge#1532 reforge epoch 1, pass-1 survivors."""

    def test_newline_in_the_checkout_name_strips_the_suffix_as_bash_does(self):
        name = "a\nb-lane2"
        bash = subprocess.run(["bash", "-c", 'b="$1"; [[ "$b" =~ ^(.+)-lane[0-9]+$ ]] && b="${BASH_REMATCH[1]}"; '
                               'printf %s "$b"', "_", name], capture_output=True, text=True, check=True).stdout
        self.assertEqual(bash, "a\nb")
        clone = self.tmp / "nl" / name
        clone.parent.mkdir()
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(clone), cwd=self.tmp)
        self.assertEqual(foi.lane_worktree_paths(clone),
                         [clone.parent / f"{bash}-lane2", clone.parent / f"{bash}-lane3"])

    def test_non_utf8_repo_path_in_a_check_message_prints_escaped(self):
        sub = Path(os.fsdecode(os.fsencode(self.app) + b"/sub\xff"))
        sub.mkdir()
        self.lane2.mkdir()  # a plain directory: its FAIL Check names the repo path
        result = subprocess.run([sys.executable, str(FO), "--install", "--repo", str(sub)], cwd=self.app,
                                env={**self.env, "PYTHONIOENCODING": "utf-8:strict"}, capture_output=True,
                                text=True, errors="surrogateescape", stdin=subprocess.DEVNULL, timeout=120)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sub\\udcff", result.stdout)

    def test_symlink_loop_runtime_error_on_path_is_a_warn_check(self):
        loop = self.tmp / "la"
        real = Path.resolve

        def resolve(path, *a, **k):  # Python < 3.13: resolve() raises RuntimeError on a loop
            if path == loop:
                raise RuntimeError(f"Symlink loop from {path!r}")
            return real(path, *a, **k)

        with mock.patch.object(Path, "resolve", resolve):
            code, out, err = self.run_main(env={"PATH": f"{loop}{os.pathsep}{self.env['PATH']}"})
        self.assertNotIn("Traceback", err)
        self.assertIn("[ warn ] PATH — could not probe PATH: RuntimeError: Symlink loop", out)


class MainPrintsSafeEncoding(InstallBase):
    """harmonic-forge#1683: `main()`'s two `--install` prints go through `_emit`,
    so a strict or handler-rejecting stream codec cannot escape the exit-code mapping."""

    NAMED = "is not inside a git repository"

    def nogit(self) -> Path:
        path = self.tmp / "nogit-é"
        path.mkdir()
        return path

    def nogit_cli(self, encoding: str) -> subprocess.CompletedProcess:
        return self.cli("--install", "--dry-run", "--repo", str(self.nogit()),
                        env={"PYTHONIOENCODING": encoding})

    def test_ac2_ascii_strict_env_exits_2_named_regression_guard(self):
        # Not a kill check: CPython forces stderr's handler to backslashreplace.
        result = self.nogit_cli("ascii:strict")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("forge-onboard: ", result.stderr)
        self.assertIn("nogit-\\xe9 " + self.NAMED, result.stderr)

    def test_ac3_in_process_strict_ascii_stderr_returns_2_named(self):
        repo = self.nogit()
        raw = io.BytesIO()
        strict = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        # The fixed hermetic env, as run_main uses: install_source() reads the live env.
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, "stderr", strict):
            code = foi.fo.main(["--install", "--dry-run", "--repo", str(repo)])
        strict.flush()
        err = raw.getvalue().decode("ascii")
        self.assertEqual(code, 2, err)
        self.assertIn("forge-onboard: ", err)
        self.assertIn("nogit-\\xe9 " + self.NAMED, err)

    def test_ac4_idna_env_exits_2_named_without_traceback(self):
        result = self.nogit_cli("idna")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("forge-onboard: ", result.stderr)
        self.assertIn(self.NAMED, result.stderr)

    def test_ac5_idna_env_check_line_on_stdout_escaped(self):
        sub = Path(os.fsdecode(os.fsencode(self.app) + b"/sub\xff"))
        sub.mkdir()
        self.lane2.mkdir()  # a plain directory: its FAIL Check names the repo path
        result = subprocess.run([sys.executable, str(FO), "--install", "--repo", str(sub)], cwd=self.app,
                                env={**self.env, "PYTHONIOENCODING": "idna"}, capture_output=True,
                                text=True, errors="surrogateescape", stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sub\\udcff", result.stdout)
        self.assertEqual(result.stderr, "")

    def closed_fd_cli(self, redirect: str, *args: str) -> subprocess.CompletedProcess:
        """The real CLI started with one fd closed by the shell (`2>&-` or `>&-`),
        so Python sees `sys.stderr` or `sys.stdout` as `None`."""
        return subprocess.run(["bash", "-c", f'exec "$@" {redirect}', "_", sys.executable, str(FO), *args],
                              cwd=self.app, env=self.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_closed_stderr_preflight_error_still_exits_2(self):
        result = self.closed_fd_cli("2>&-", "--install", "--dry-run", "--repo", str(self.nogit()))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertNotIn("Traceback", result.stdout)

    def test_closed_stdout_checks_keep_the_open_stdout_exit_code(self):
        args = ("--install", "--dry-run", "--repo", str(self.app))
        expected = self.cli(*args).returncode
        self.assertEqual(expected, 0)
        result = self.closed_fd_cli(">&-", *args)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)


#: One maker per type the four rounds' survivors were (harmonic-forge#1532 epoch-1 sticky PATCH);
#: each carries a unique sentinel so a row passes only on its own exception.
BACKSTOP_TYPES = (
    lambda s: OSError(s),
    lambda s: RuntimeError(s),
    lambda s: UnicodeEncodeError("utf-8", "\udcff", 0, 1, s),
    lambda s: ValueError(s),
)


class PhaseBackstop(InstallBase):
    """harmonic-forge#1532 epoch-1 sticky-wicket PATCH: the phase decides the
    channel for ANY Exception, not a per-site list of types."""

    def fresh(self) -> None:
        """A new hermetic fixture per case: a write-phase run leaves links behind."""
        self.setUp()

    def untouched(self) -> None:
        self.assertFalse(self.bin.exists())
        self.assertFalse(os.path.lexists(self.lane2))
        self.assertFalse(os.path.lexists(self.lane3))

    def preflight_row(self, target, attr: str, exc: Exception, sentinel: str) -> None:
        calls = []

        def boom(*_a, **_k):
            calls.append(True)
            raise exc

        with mock.patch.object(target, attr, boom):
            code, out, err = self.run_main()
        self.assertTrue(calls, f"{attr} was never reached")
        self.assertEqual(code, 2, out + err)
        self.assertTrue(err.startswith("forge-onboard:"), err)
        self.assertRegex(err, rf"{type(exc).__name__}: .*{sentinel}")
        self.assertNotIn("Traceback", err)
        self.untouched()

    def test_any_exception_in_preflight_is_install_error_exit_2(self):
        for fn in foi.PREFLIGHT:
            for i, make in enumerate(BACKSTOP_TYPES):
                sentinel = f"sentinel-{fn.__name__}-{i}"
                exc = make(sentinel)
                with self.subTest(fn=fn.__name__, exc=type(exc).__name__):
                    self.preflight_row(foi, fn.__name__, exc, sentinel)
        with self.subTest(fn="Path.home"):  # the bin-dir default is preflight too
            self.preflight_row(Path, "home", RuntimeError("sentinel-home"), "sentinel-home")

    def write_row(self, target, attr: str, exc: Exception, sentinel: str, *, first_passes: bool,
                  status: str = "FAIL", code: int = 1, linked: bool = True, created: bool = True,
                  when=lambda *a, **k: True) -> str:
        self.fresh()
        real, calls = getattr(target, attr), []

        def boom(*a, **k):
            if not when(*a, **k):
                return real(*a, **k)
            calls.append(True)
            if first_passes and len(calls) == 1:  # the first item still completes
                return real(*a, **k)
            raise exc

        with mock.patch.object(target, attr, boom):
            got, out, err = self.run_main()
        self.assertTrue(calls, f"{attr} was never reached")
        self.assertNotIn("Traceback", err)
        self.assertEqual(got, code, out + err)
        self.assertRegex(out, rf"\[ {status} \] .* — .*{type(exc).__name__}: .*{sentinel}")
        if linked:
            self.assertRegex(out, r"\[  ok  \] launcher \S+ — linked ")
        if created:
            self.assertIn("worktree app-lane2 — created", out)
        self.assertRegex(out, r"\[(?: FAIL |  ok  )\] install — ")  # later Checks still print
        return out

    def test_any_exception_in_the_write_phase_is_a_check(self):
        for fn in foi.WRITE_PHASE:
            for i, make in enumerate(BACKSTOP_TYPES):
                sentinel = f"sentinel-{fn.__name__}-{i}"
                exc = make(sentinel)
                with self.subTest(fn=fn.__name__, exc=type(exc).__name__):
                    if fn is foi.path_lines:  # an unprobeable PATH is advisory, whatever the type
                        self.write_row(foi, fn.__name__, exc, sentinel, first_passes=False,
                                       status="warn", code=0)
                    else:
                        self.write_row(foi, fn.__name__, exc, sentinel,
                                       first_passes=fn in (foi._inspect_link, foi._write_link),
                                       linked=fn is not foi.link_launchers,
                                       created=fn is not foi.create_worktrees)

    def test_any_exception_in_a_worktree_add_or_the_bin_mkdir_is_a_fail_check(self):
        for i, make in enumerate(BACKSTOP_TYPES):
            exc = make(f"sentinel-add-{i}")
            with self.subTest(site="worktree add", exc=type(exc).__name__):
                self.write_row(foi, "_git", exc, f"sentinel-add-{i}", first_passes=True,
                               when=lambda args, cwd: args[:2] == ["worktree", "add"])
            exc = make(f"sentinel-mkdir-{i}")
            with self.subTest(site="bin mkdir", exc=type(exc).__name__):
                out = self.write_row(Path, "mkdir", exc, f"sentinel-mkdir-{i}", first_passes=False,
                                     linked=False, when=lambda path, *a, **k: path == self.bin)
                for name in checker.LANES:  # each launcher names the mkdir failure, item by item
                    self.assertRegex(out, rf"\[ FAIL \] launcher {name} — cannot create .*sentinel-mkdir-{i}")

    def test_deleted_cwd(self):
        self.addCleanup(os.chdir, Path.cwd())
        gone = self.tmp / "gone"
        gone.mkdir()
        os.chdir(gone)
        gone.rmdir()
        with self.subTest(repo="given"):
            # Nothing reads the cwd once `git version` drops `-C <cwd>`: the run completes.
            code, out, err = self.run_main()
            self.assertNotIn("Traceback", err)
            self.assertEqual(code, 0, out + err)
            self.assertIn("worktree app-lane2 — created", out)
        with self.subTest(repo="omitted"):
            self.fresh()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = foi.fo.main(["--install"])
            self.assertEqual(code, 2, out.getvalue() + err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertIn("the current directory no longer exists; pass --repo", err.getvalue())
            self.untouched()


if __name__ == "__main__":
    unittest.main()
