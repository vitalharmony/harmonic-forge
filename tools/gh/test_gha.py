#!/usr/bin/env python3
"""Tests for `gha`, the thin alias to `gh-as` (harmonic-forge#804). A fake `gh-as`
on PATH records its argv, so nothing touches a real credential."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GHA = Path(__file__).resolve().parent / "gha"


class GhaTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.log = self.bin / "argv.log"
        fake = self.bin / "gh-as"
        fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{self.log}"\n')
        fake.chmod(0o755)

    def run_gha(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(["bash", str(GHA), *args], capture_output=True, text=True, env=env)

    def test_vh_maps_to_vitalharmony(self) -> None:
        self.assertEqual(self.run_gha("vh", "issue", "list").returncode, 0)
        self.assertEqual(self.log.read_text().strip(), "vitalharmony gh issue list")

    def test_every_kenekted_shortcut_maps_to_harmonicarchitect(self) -> None:
        for alias in ("kn", "kenekted", "ka", "harmonicarchitect"):
            self.log.unlink(missing_ok=True)
            self.run_gha(alias, "pr", "list")
            self.assertEqual(self.log.read_text().strip(), "harmonicarchitect gh pr list", alias)

    def test_an_unknown_account_exits_2_and_never_calls_gh_as(self) -> None:
        result = self.run_gha("nobody", "issue", "list")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_too_few_arguments_prints_usage(self) -> None:
        self.assertEqual(self.run_gha("vh").returncode, 2)

    def test_it_never_reads_the_keyring_or_injects_a_token(self) -> None:
        text = GHA.read_text()
        self.assertNotIn("auth token", text)
        self.assertNotIn("GH_TOKEN=", text)
        self.assertNotIn("auth switch", text)


if __name__ == "__main__":
    unittest.main()
