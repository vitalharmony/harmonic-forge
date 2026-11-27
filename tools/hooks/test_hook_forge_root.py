"""harmonic-forge#1566: the four forge-root fallbacks read the launcher's
resolution, never `~/harmonic-forge`.

TC1: compaction_marker and tier_downshift_reminder name the configured root.
TC2 (amended, operator D25/D26): check_receipt's identity is the checkout it
runs from; a declared root that is a different checkout makes it None ("forge
root mismatch"), so a mismatch misses a reuse and never allows a false one.
TC3 (generated lane tasks) and TC5 (wrapper parity) are in
tools/onboard/test_lane_tasks_root.py.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
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
    """check_receipt runs from `self.forge` (FORGE_ROOT patched to it, as if this
    file lived there); the declaration varies."""

    def setUp(self) -> None:
        super().setUp()
        import check_receipt  # noqa: PLC0415
        self.C = check_receipt
        for target in (mock.patch.object(check_receipt, "FORGE_ROOT", self.forge),
                       mock.patch.dict(os.environ, self.env, clear=True)):
            target.start()
            self.addCleanup(target.stop)

    def test_identity_is_the_running_checkout(self) -> None:
        self.assertEqual(Path(importlib.import_module("check_receipt").__file__).resolve()
                         .parents[2], HERE.parents[1])
        self.declare(None)
        self.assertEqual(self.C.forge_sha(), self.forge_sha)

    def test_declared_through_a_symlink_to_the_same_checkout(self) -> None:
        link = self.tmp / "forge-link"
        link.symlink_to(self.forge)
        self.declare(str(link))
        self.assertEqual(self.C.forge_sha(), self.forge_sha)
        self.assertIsNone(self.C.forge_unusable_reason())

    def test_a_declared_different_checkout_is_a_mismatch(self) -> None:
        """The pass-1 survivor: wrappers may run forge from the declared root
        while this file runs from another; the identity is then None."""
        other = (self.tmp / "other-forge").resolve()
        git_repo(other)
        self.declare(str(other))
        self.assertIsNone(self.C.forge_sha())
        self.assertIn("forge root mismatch", self.C.forge_unusable_reason())

    def test_a_scratch_check_says_its_receipt_will_not_be_reused(self) -> None:
        """D26: a forge-repo check recorded with l1_post's forge SHA that is not
        this checkout's HEAD records no identity and says so."""
        self.declare(None)
        src = self.tmp / "src"
        git_repo(src)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/app.git"],
                       cwd=src, check=True, capture_output=True)
        err = io.StringIO()
        with mock.patch.object(self.C, "STORE", self.tmp / "store"), \
                mock.patch.dict(os.environ, {self.C.FORGE_SHA_ENV: "0" * 40}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.C.record(str(src))
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, check=True,
                             capture_output=True, text=True).stdout.strip()
        data = json.loads((self.tmp / "store" / "acme__app" / sha).read_text())
        self.assertIsNone(data["forge_sha"])
        self.assertIn("will not be reused", err.getvalue())


if __name__ == "__main__":
    unittest.main()
