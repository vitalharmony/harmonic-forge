#!/usr/bin/env python3
"""harmonic-forge#1565 TCs 28-30, 41, 42: the check-full sidecar.

Real `check_receipt.record` runs against a temp store, a temp source repo whose
origin is vitalharmony/hrse, and a temp forge root; nothing is stubbed but
`os.replace` in TC42.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_receipt as C  # noqa: E402

SLUG = "vitalharmony__hrse"


def git_repo(root: Path, origin: str | None = None) -> str:
    """A one-commit repo at `root`; its HEAD."""
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    if origin:
        run("remote", "add", "origin", origin)
    (root / "f").write_text("x")
    run("add", "f")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


class Sidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store, self.forge_root, self.src = base / "store", base / "forge", base / "src"
        for path in (self.forge_root, self.src):
            path.mkdir()
        self.forge = git_repo(self.forge_root)
        self.head = git_repo(self.src, "https://github.com/vitalharmony/hrse.git")
        for attr, value in (("STORE", self.store), ("FORGE_ROOT", self.forge_root)):
            patch = mock.patch.object(C, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in (C.SOURCE_ENV, C.FORGE_SHA_ENV):
            os.environ.pop(key, None)
        err = mock.patch("sys.stderr", new_callable=io.StringIO)
        self.stderr = err.start()
        self.addCleanup(err.stop)

    def record(self, source: str = "check", scratch: bool = False) -> None:
        with mock.patch.dict(os.environ, {C.SOURCE_ENV: C.L1_POST_SOURCE} if scratch else {}):
            C.record(str(self.src), source=source)

    def sidecars(self) -> list[str]:
        directory = self.store / SLUG
        return sorted(p.name for p in directory.iterdir() if ".check-full." in p.name) if directory.is_dir() else []

    def test_28_a_scratch_check_after_check_full_leaves_the_sidecar(self):
        self.record(C.CHECK_FULL_SOURCE)
        before = C.check_full_path(SLUG, self.head, self.forge).read_bytes()
        self.record(scratch=True)
        self.assertEqual(C.lookup(SLUG, self.head)["source"], C.L1_POST_SOURCE)
        self.assertEqual(C.check_full_path(SLUG, self.head, self.forge).read_bytes(), before)
        self.assertEqual(C.check_full(SLUG, self.head, self.forge)["source"], "check-full")

    def test_29_a_lane_check_after_check_full_leaves_the_sidecar_and_push_passes(self):
        self.record(C.CHECK_FULL_SOURCE)
        self.assertIsNone(C.push_refusal(SLUG, "fix/2889-x", self.head, {"2889": ["tooling-exception"]}))
        self.record()
        self.assertEqual(C.lookup(SLUG, self.head)["source"], "check")
        self.assertIsNotNone(C.check_full(SLUG, self.head, self.forge))

    def test_30_check_or_local_check_alone_writes_no_sidecar(self):
        for source in ("check", C.LOCAL_CHECK_SOURCE):
            with self.subTest(source=source):
                self.record(source)
                self.assertEqual(self.sidecars(), [])
        self.assertIsNotNone(C.lookup(SLUG, self.head))

    def test_41_check_full_on_a_dirty_forge_writes_the_slot_and_no_sidecar(self):
        (self.forge_root / "f").write_text("dirty")
        self.record(C.CHECK_FULL_SOURCE)
        slot = C.lookup(SLUG, self.head)
        self.assertEqual((slot["source"], slot["forge_sha"]), ("check-full", None))
        self.assertEqual(self.sidecars(), [])
        self.assertIn("forge tree dirty: f; no check-full sidecar recorded", self.stderr.getvalue())

    def test_42_a_failed_replace_leaves_the_old_sidecar_and_no_temp_file(self):
        path = C.check_full_path(SLUG, self.head, self.forge)
        path.parent.mkdir(parents=True)
        old = json.dumps({"repo": SLUG, "sha": self.head, "forge_sha": self.forge,
                          "source": "check-full", "note": "old"}) + "\n"
        path.write_text(old)
        with mock.patch.object(C.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(C.record(str(self.src), source=C.CHECK_FULL_SOURCE), 0)
        self.assertEqual(path.read_text(), old)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), sorted([self.head, path.name]))
        self.assertIn("disk full", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
