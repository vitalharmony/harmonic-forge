#!/usr/bin/env python3
"""harmonic-forge#1507 reforge: account_for_origin is the lane launcher's whole
origin-to-account decision -- a github.com host (or an absolute local fixture
path) only, one normalization, one exact refusal rule, one direct lookup."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest_identity as mi  # noqa: E402
from manifest import ManifestError  # noqa: E402

MANIFEST = """\
[[project]]
name = "kenekted-ai"
prefix = "Y"
repo = "kenekted/kenekted-ai"
account = "harmonicarchitect"
path = "/nonexistent/kenekted-ai"
onboarded = false
workspace = "ka"
"""


class AccountForOrigin(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manifest = Path(tmp.name) / "projects.toml"
        self.manifest.write_text(MANIFEST)

    def account(self, url: str) -> str:
        return mi.account_for_origin(url, self.manifest)

    def test_accepted_shapes_resolve(self) -> None:
        for url in ("https://github.com/Kenekted/kenekted-ai.git",
                    "https://github.com/kenekted/kenekted-ai",
                    "https://github.com/kenekted/kenekted-ai/",
                    "git@github.com:kenekted/kenekted-ai.git",
                    "ssh://git@github.com/kenekted/kenekted-ai.git",
                    "/tmp/x/github.com/kenekted/kenekted-ai.git"):
            with self.subTest(url=url):
                self.assertEqual(self.account(url), "harmonicarchitect")

    def test_another_host_refuses(self) -> None:
        for url in ("https://notgithub.com/kenekted/kenekted-ai",
                    "https://evil.example/github.com/kenekted/kenekted-ai.git",
                    "https://github.com.evil.example/kenekted/kenekted-ai",
                    "git@evil.example:github.com/kenekted/kenekted-ai.git",
                    "ssh://git@github.com:22/kenekted/kenekted-ai.git",
                    "relative/github.com/kenekted/kenekted-ai.git",
                    "file:///tmp/github.com/kenekted/kenekted-ai.git",
                    "https://tok@github.com/kenekted/kenekted-ai.git",
                    ""):
            with self.subTest(url=url):
                with self.assertRaises(ManifestError):
                    self.account(url)

    def test_a_key_normalization_would_rewrite_refuses(self) -> None:
        # Pass 2's cross-family survivor (.GIT) and pitch-inspection's .git.GIT.
        for url in ("https://github.com/kenekted/kenekted-ai.GIT",  # normalize strips only lowercase .git
                    "https://github.com/kenekted/kenekted-ai.git.git",
                    "https://github.com/kenekted/kenekted-ai.git.GIT",
                    "https://github.com/kenekted/kenekted-ai.GIT.git",
                    "https://github.com/kenekted/..",
                    "https://github.com/../kenekted-ai"):
            with self.subTest(url=url):
                with self.assertRaises(ManifestError):
                    self.account(url)

    def test_an_unregistered_repo_refuses(self) -> None:
        with self.assertRaises(ManifestError):
            self.account("https://github.com/some-unregistered-owner/x.git")


if __name__ == "__main__":
    unittest.main()
