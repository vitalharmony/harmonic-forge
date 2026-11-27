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

    def run_main(self, *args: str, env: dict | None = None) -> tuple[int, str, str]:
        """`forge_onboard.main` in-process (so a fault can be injected), with
        the same hermetic environment the CLI tests get."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **(env or {})}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = foi.fo.main(["--install", "--repo", str(self.app), *args])
        return code, out.getvalue(), err.getvalue()

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
        real = foi._git

        def failing_list(args, cwd):
            if args[:2] == ["worktree", "list"]:
                return subprocess.CompletedProcess(args, 128, "", "fatal: boom")
            return real(args, cwd)

        with mock.patch.object(foi, "_git", failing_list), self.assertRaisesRegex(foi.InstallError, "boom"):
            foi.plan_worktrees(self.app)

    # Finding :180 -- origin/main is reached before the first write.
    def no_main_origin(self) -> None:
        empty = self.tmp / "empty.git"
        self.git("init", "-q", "--bare", str(empty), cwd=self.tmp)
        self.git("remote", "set-url", "origin", str(empty), cwd=self.app)

    def test_fetch_failure_exits_2_before_any_write(self):
        self.no_main_origin()
        result = self.install()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("git fetch origin main failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
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
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(foi.subprocess, "run", record):
            foi._git(["status"], self.app)
        self.assertTrue(seen["start_new_session"])
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


if __name__ == "__main__":
    unittest.main()
