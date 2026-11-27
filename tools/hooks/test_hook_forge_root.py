"""harmonic-forge#1566: the four forge-root fallbacks read the launcher's
resolution, never `~/harmonic-forge`.

TC1: compaction_marker and tier_downshift_reminder name the configured root.
TC2: check_receipt reads the configured root's HEAD, and has no own-checkout
fallback (its recorder and readers run from different checkouts).
TC3 (generated lane tasks) and TC5 (wrapper parity) are in
tools/onboard/test_lane_tasks_root.py.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hook_forge_root  # noqa: E402

LITERAL = 'Path.home() / "harmonic-forge"'


def git_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tools" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / "f").write_text("x", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


class Fixture(unittest.TestCase):
    """A scratch forge checkout and an instance.toml naming it, no env root."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.forge = (self.tmp / "srv" / "forge").resolve()
        self.forge_sha = git_repo(self.forge)
        self.xdg = self.tmp / "xdg"
        (self.xdg / "harmonic-forge").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k != "HARMONIC_FORGE_ROOT"}
        self.env["XDG_CONFIG_HOME"] = str(self.xdg)

    def declare(self, value: str | None) -> None:
        text = "" if value is None else f'forge_root = "{value}"\n'
        (self.xdg / "harmonic-forge" / "instance.toml").write_text(text, encoding="utf-8")

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)


class TC1AdvisoryHooks(Fixture):
    def test_compaction_marker_names_the_configured_root(self) -> None:
        import compaction_marker  # noqa: PLC0415
        self.declare(str(self.forge))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(compaction_marker.forge_root(), str(self.forge))
            paths = compaction_marker.corpus_for("1")
        self.assertTrue(paths and all(p.startswith(f"{self.forge}/") for p in paths), paths)

    def test_tier_downshift_reads_worktrees_of_the_configured_root(self) -> None:
        import tier_downshift_reminder as t  # noqa: PLC0415
        self.declare(str(self.forge))
        seen: list[str] = []
        fake = mock.Mock(git_lines=lambda base, *a: seen.append(base) or [],
                         is_impl_worktree=lambda p: False)
        with mock.patch.dict(sys.modules, {"worktree_issue": fake}):
            t._worktree_candidates(str(self.tmp), self.env)
        self.assertIn(str(self.forge), seen)

    def test_an_unresolvable_root_falls_back_to_the_hook_checkout(self) -> None:
        env = {**self.env, "HARMONIC_FORGE_ROOT": "relative/not/absolute"}
        self.assertEqual(hook_forge_root.resolve(str(HERE / "x.py"), env), HERE.parents[1])

    def test_no_home_literal_remains(self) -> None:
        for name in ("compaction_marker.py", "tier_downshift_reminder.py", "check_receipt.py"):
            self.assertNotIn(LITERAL, (HERE / name).read_text(encoding="utf-8"), name)


class TC2CheckReceipt(Fixture):
    def reload(self):
        import check_receipt  # noqa: PLC0415
        with mock.patch.dict(os.environ, self.env, clear=True):
            return importlib.reload(check_receipt)

    def test_reads_the_configured_roots_head(self) -> None:
        self.declare(str(self.forge))
        receipt = self.reload()
        self.assertEqual(receipt.FORGE_ROOT, self.forge)
        self.assertEqual(receipt.forge_sha(), self.forge_sha)

    def test_a_root_without_tools_hooks_reads_none(self) -> None:
        bare = self.tmp / "bare"
        git_repo(bare)
        subprocess.run(["rm", "-rf", str(bare / "tools")], check=True)
        self.declare(str(bare))
        self.assertIsNone(self.reload().forge_sha())

    def test_undeclared_never_takes_the_own_checkout(self) -> None:
        """The recorder runs from a second checkout with FORGE_SHA_ENV set from
        the first; with no root declared its forge identity is None."""
        self.declare(None)
        receipt = self.reload()
        self.assertEqual(receipt.FORGE_ROOT, receipt.UNRESOLVED_FORGE_ROOT)
        self.assertIsNone(receipt.forge_sha())
        self.assertIn("forge root unresolved", receipt.forge_unusable_reason())

    def tearDown(self) -> None:
        import check_receipt  # noqa: PLC0415
        importlib.reload(check_receipt)
        super().tearDown()


if __name__ == "__main__":
    unittest.main()
