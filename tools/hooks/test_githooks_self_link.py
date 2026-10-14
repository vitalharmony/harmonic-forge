"""A forge checkout links to itself; a consuming repo links to the installed
forge (harmonic-forge#987, AC1-AC3).

Each test builds throwaway repos in a temp directory: a shared clone of this
checkout with `core.hooksPath=.githooks`, a worktree added from it, and a
minimal consuming repo. Nothing touches the operator's real checkouts.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def git(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, check=True, timeout=120)


def links(root: Path) -> list[Path]:
    out = []
    for sub in ("skills", "rules", "agents"):
        d = root / ".claude" / sub
        if d.is_dir():
            out += [p for p in d.iterdir() if p.is_symlink()]
    return out


class SelfLink(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clone = self.tmp / "forge"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(REPO), str(self.clone)],
                       check=True, capture_output=True, timeout=120)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                              text=True, check=True).stdout.strip()
        git("checkout", "-q", "-b", "selflink-base", head, cwd=self.clone)
        # Every lane branch is feat/<N>-... or fix/<N>-..., and the clone
        # carries whichever one REPO has checked out. Hold both families here
        # on any checkout, so a scratch name that collides with them fails on
        # main too, not only on a feat/* branch (harmonic-forge#990).
        for family in ("feat", "fix"):
            git("branch", f"{family}/selflink-guard", head, cwd=self.clone)
        # The hooks under test are this checkout's, not whatever HEAD carries.
        for name in ("post-checkout", "post-merge"):
            (self.clone / ".githooks" / name).write_text((REPO / ".githooks" / name).read_text())
        git("config", "core.hooksPath", ".githooks", cwd=self.clone)
        # The hard case: HARMONIC_FORGE_ROOT names a DIFFERENT forge.
        self.env = {**os.environ, "HARMONIC_FORGE_ROOT": str(REPO)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assert_self_linked(self, root: Path) -> None:
        found = links(root)
        self.assertTrue(found, f"no platform links were created in {root}")
        for link in found:
            target = Path(os.readlink(link))
            self.assertTrue(str(target).startswith(str(root)), f"{link} -> {target}")
        report = subprocess.run([sys.executable, str(root / "tools/onboard/platform_link_report.py"),
                                 "--project", str(root)], capture_output=True, text=True, timeout=120)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)

    def test_ac1_a_new_worktree_links_to_itself(self) -> None:
        wt = self.tmp / "wt"
        git("worktree", "add", "-q", "-b", "selflink-feat", str(wt), cwd=self.clone, env=self.env)
        self.assert_self_linked(wt)

    def test_ac1_post_merge_relinks_to_itself(self) -> None:
        git("checkout", "-q", "-b", "selflink-side", cwd=self.clone, env=self.env)
        git("commit", "-q", "--allow-empty", "-m", "side", cwd=self.clone, env=self.env)
        git("checkout", "-q", "selflink-base", cwd=self.clone, env=self.env)
        for link in links(self.clone):
            link.unlink()
        git("merge", "-q", "--no-ff", "--no-edit", "selflink-side", cwd=self.clone, env=self.env)
        self.assert_self_linked(self.clone)

    def test_ac2_a_consuming_repo_links_to_the_installed_forge(self) -> None:
        consumer = self.tmp / "consumer"
        consumer.mkdir()
        git("init", "-q", cwd=consumer)
        (consumer / ".claude").mkdir()
        (consumer / ".claude" / "platform-skills.toml").write_text('skills = ["auto-ae"]\n')
        (consumer / ".githooks").mkdir()
        hook = consumer / ".githooks" / "post-checkout"
        hook.write_text((REPO / ".githooks" / "post-checkout").read_text())
        hook.chmod(0o755)
        git("config", "core.hooksPath", ".githooks", cwd=consumer)
        git("commit", "-q", "--allow-empty", "-m", "x", cwd=consumer)
        git("checkout", "-q", "-b", "other", cwd=consumer, env={**os.environ, "HARMONIC_FORGE_ROOT": str(self.clone)})
        target = Path(os.readlink(consumer / ".claude" / "skills" / "auto-ae"))
        self.assertTrue(str(target).startswith(str(self.clone)), target)

    def test_ac3_hooks_exit_0_when_the_linker_fails(self) -> None:
        (self.clone / "sync_rules.py").write_text("raise SystemExit(7)\n")
        for name in ("post-checkout", "post-merge"):
            result = subprocess.run(["bash", str(self.clone / ".githooks" / name), "a", "b", "1"],
                                    cwd=self.clone, env=self.env, capture_output=True, text=True,
                                    timeout=60)
            self.assertEqual(result.returncode, 0, name)


if __name__ == "__main__":
    unittest.main()
