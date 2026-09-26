"""harmonic-forge#762: `l1_tools_env.sh` keeps Lane 1's two fixed origin/main
tools worktrees. Every case runs against disposable repos with a local bare
`origin`; nothing touches the network or a real checkout."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "l1_tools_env.sh"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo_with_origin(root: Path, name: str) -> Path:
    origin = root / f"{name}-origin.git"
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    main = root / name
    _git(root, "init", "-q", "-b", "main", str(main))
    _git(main, "config", "user.email", "t@example.invalid")
    _git(main, "config", "user.name", "T")
    (main / "README.md").write_text("one\n")
    _git(main, "add", "README.md")
    _git(main, "commit", "-q", "-m", "one")
    _git(main, "remote", "add", "origin", str(origin))
    _git(main, "push", "-q", "origin", "main")
    return main


def _advance(main: Path) -> str:
    (main / "NEXT.md").write_text("next\n")
    _git(main, "add", "NEXT.md")
    _git(main, "commit", "-q", "-m", "next")
    _git(main, "push", "-q", "origin", "main")
    return _git(main, "rev-parse", "HEAD")


class _Tree:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.project = _repo_with_origin(self.root, "HRSE2")
        self.forge = _repo_with_origin(self.root, "harmonic-forge")
        self.wt_root = self.root / "worktrees"
        self.provisioned = self.root / "provisioned.log"
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def run(self):
        env = dict(os.environ,
                   HARMONIC_FORGE_ROOT=str(self.forge),
                   L1_TOOLS_WORKTREE_ROOT=str(self.wt_root),
                   L1_TOOLS_PROVISION_CMD=f"sh -c 'pwd >> {self.provisioned}'")
        return subprocess.run(
            ["bash", "-c", f'source "{HELPER}" && l1_tools_env "{self.project}" '
                           f'&& echo "P=$L1_TOOLS_PROJECT" && echo "F=$L1_TOOLS_FORGE"'],
            env=env, capture_output=True, text=True)

    @property
    def project_wt(self):
        return self.wt_root / "hrse2-l1-tools"

    @property
    def forge_wt(self):
        return self.wt_root / "harmonic-forge-l1-tools"


class L1ToolsEnv(unittest.TestCase):
    def test_first_call_creates_both_at_origin_main_and_provisions_the_project(self):
        """TC1."""
        with _Tree() as t:
            proc = t.run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"P={t.project_wt}", proc.stdout)
            self.assertIn(f"F={t.forge_wt}", proc.stdout)
            self.assertEqual(_git(t.project_wt, "rev-parse", "HEAD"),
                             _git(t.project, "rev-parse", "origin/main"))
            self.assertEqual(_git(t.forge_wt, "rev-parse", "HEAD"),
                             _git(t.forge, "rev-parse", "origin/main"))
            self.assertEqual(t.provisioned.read_text().splitlines(), [str(t.project_wt)])

    def test_a_call_from_a_linked_worktree_uses_the_repository_name(self):
        """Smoke-test finding: a call from `hrse2-762-impl` must still use
        `hrse2-l1-tools`, never `hrse2-762-impl-l1-tools`."""
        with _Tree() as t:
            linked = t.root / "hrse2-762-impl"
            _git(t.project, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
            env = dict(os.environ, HARMONIC_FORGE_ROOT=str(t.forge),
                       L1_TOOLS_WORKTREE_ROOT=str(t.wt_root),
                       L1_TOOLS_PROVISION_CMD="true")
            proc = subprocess.run(
                ["bash", "-c", f'source "{HELPER}" && l1_tools_env "{linked}" && echo "P=$L1_TOOLS_PROJECT"'],
                env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"P={t.project_wt}", proc.stdout)
            self.assertFalse((t.wt_root / "hrse2-762-impl-l1-tools").exists())

    def test_a_new_origin_main_moves_both(self):
        """TC2."""
        with _Tree() as t:
            t.run()
            p_sha, f_sha = _advance(t.project), _advance(t.forge)
            proc = t.run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(_git(t.project_wt, "rev-parse", "HEAD"), p_sha)
            self.assertEqual(_git(t.forge_wt, "rev-parse", "HEAD"), f_sha)
            self.assertEqual(len(t.provisioned.read_text().splitlines()), 1,
                             "an existing worktree is not re-provisioned")

    def test_a_tracked_edit_is_recreated_clean_and_reprovisioned(self):
        """TC3, TC10."""
        with _Tree() as t:
            t.run()
            (t.project_wt / "README.md").write_text("stray edit\n")
            proc = t.run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((t.project_wt / "README.md").read_text(), "one\n")
            self.assertEqual(len(t.provisioned.read_text().splitlines()), 2)

    def test_concurrent_calls_both_succeed(self):
        """TC4."""
        with _Tree() as t:
            results = []
            threads = [threading.Thread(target=lambda: results.append(t.run())) for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual([r.returncode for r in results], [0, 0],
                             [r.stderr for r in results])
            self.assertEqual(t.provisioned.read_text().splitlines(), [str(t.project_wt)],
                             "exactly one creation+provision under contention")

    def test_an_in_flight_run_holds_off_a_concurrent_refresh(self):
        """Preclose silent-bypass F2/F3: the lock spans the caller's use of the
        worktree, not just the ensure step -- an exclusive request fails while
        a consumer is still running, and succeeds once it exits."""
        with _Tree() as t:
            ready, release = t.root / "ready", t.root / "release"
            env = dict(os.environ, HARMONIC_FORGE_ROOT=str(t.forge),
                       L1_TOOLS_WORKTREE_ROOT=str(t.wt_root),
                       L1_TOOLS_PROVISION_CMD="true")
            consumer = subprocess.Popen(
                ["bash", "-c", f'source "{HELPER}" && l1_tools_env "{t.project}" '
                               f'&& touch "{ready}" && while [ ! -e "{release}" ]; do sleep 0.05; done'],
                env=env)
            try:
                for _ in range(200):
                    if ready.exists():
                        break
                    threading.Event().wait(0.05)
                self.assertTrue(ready.exists())
                lock = t.wt_root / ".hrse2-l1-tools.lock"
                held = subprocess.run(["flock", "-n", "-x", str(lock), "true"])
                self.assertNotEqual(held.returncode, 0, "exclusive lock granted mid-run")
            finally:
                release.write_text("")
                consumer.wait(timeout=30)
            free = subprocess.run(["flock", "-n", "-x", str(lock), "true"])
            self.assertEqual(free.returncode, 0)

    def test_the_tools_worktrees_are_detached(self):
        """TC9: no `branch refs/heads/` line, so l1_post.py's overlap check
        never sees them."""
        with _Tree() as t:
            t.run()
            for repo, wt in ((t.project, t.project_wt), (t.forge, t.forge_wt)):
                porcelain = _git(repo, "worktree", "list", "--porcelain")
                block = porcelain.split(f"worktree {wt}")[1].split("\n\n")[0]
                self.assertIn("detached", block)
                self.assertNotIn("branch refs/heads/", block)

    def test_the_script_path_resolves_inside_the_forge_tools_worktree(self):
        """TC6, AC4."""
        with _Tree() as t:
            proc = t.run()
            forge = proc.stdout.split("F=")[1].strip()
            script = Path(forge) / "tools" / "gh" / "l1_post.py"
            self.assertTrue(str(script).startswith(str(t.forge_wt)))
            self.assertIn("harmonic-forge-l1-tools", str(script))

    def test_a_failed_fetch_fails_the_call(self):
        with _Tree() as t:
            _git(t.project, "remote", "set-url", "origin", str(t.root / "missing.git"))
            proc = t.run()
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("git fetch failed", proc.stderr)

    def test_a_failed_provision_is_retried_on_the_next_call(self):
        """Preclose fail-direction F1: a failed provision must not leave a
        created-but-unprovisioned worktree that later calls silently reuse."""
        with _Tree() as t:
            flag = t.root / "fail-once"
            flag.write_text("")
            env = dict(os.environ, HARMONIC_FORGE_ROOT=str(t.forge),
                       L1_TOOLS_WORKTREE_ROOT=str(t.wt_root),
                       L1_TOOLS_PROVISION_CMD=(
                           f"sh -c 'if [ -e {flag} ]; then rm {flag}; exit 1; fi; "
                           f"pwd >> {t.provisioned}'"))
            cmd = ["bash", "-c", f'source "{HELPER}" && l1_tools_env "{t.project}"']
            first = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse((t.project_wt / ".git").exists())
            second = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(t.provisioned.read_text().splitlines(), [str(t.project_wt)])

    def test_a_non_worktree_directory_at_the_path_is_set_aside(self):
        """Preclose fail-direction F2: a plain directory at the path must not
        wedge every later call; it is moved aside, never deleted."""
        with _Tree() as t:
            t.project_wt.mkdir(parents=True)
            (t.project_wt / "leftover.txt").write_text("keep me\n")
            proc = t.run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((t.project_wt / ".git").exists())
            aside = [p for p in t.wt_root.iterdir() if p.name.startswith("hrse2-l1-tools.stale-")]
            self.assertEqual(len(aside), 1)
            self.assertEqual((aside[0] / "leftover.txt").read_text(), "keep me\n")

    def test_untracked_residue_blocking_the_checkout_forces_a_recreate(self):
        """Preclose fail-direction F3."""
        with _Tree() as t:
            t.run()
            (t.project_wt / "NEXT.md").write_text("untracked residue\n")
            sha = _advance(t.project)  # origin/main now adds NEXT.md
            proc = t.run()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(_git(t.project_wt, "rev-parse", "HEAD"), sha)
            self.assertEqual((t.project_wt / "NEXT.md").read_text(), "next\n")

    def test_the_main_checkouts_are_never_touched(self):
        """AC7."""
        with _Tree() as t:
            before = (_git(t.project, "rev-parse", "HEAD"), _git(t.project, "status", "--porcelain"))
            _advance(t.forge)
            t.run()
            after = (_git(t.project, "rev-parse", "HEAD"), _git(t.project, "status", "--porcelain"))
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
