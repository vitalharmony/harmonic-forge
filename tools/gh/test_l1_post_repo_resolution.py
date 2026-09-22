#!/usr/bin/env python3
"""Repository-default tests for the portable Lane 1 transports (F706)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import l1_post as post


PROTOCOL = """
[project.protocol]
worktree_name = "{checkout}-lane{lane}"
l1_post_task = "l1-post"
lane_comment_task = "lane-comment"
gate_checkout_task = "gate-checkout"
lane3_begin_task = "lane3-begin"
lane3_end_task = "lane3-end"
runs_lane3 = true
"""


class RepoResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "projects.toml"
        self.manifest.write_text(textwrap.dedent("""
            [[project]]
            name = "example"
            prefix = "X"
            repo = "example/project"
            onboarded = true
        """) + PROTOCOL, encoding="utf-8")
        subprocess.run(("git", "init", "-q", str(self.root / "checkout")), check=True)
        subprocess.run(("git", "-C", str(self.root / "checkout"), "remote", "add",
                        "origin", "https://github.com/example/project.git"), check=True)

    def test_omitted_repo_resolves_the_invoking_checkout_through_manifest(self) -> None:
        with patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(self.manifest)}):
            self.assertEqual(post.resolve_repo(None, self.root / "checkout"),
                             "example/project")

    def test_explicit_unlisted_repo_is_refused_by_the_same_manifest(self) -> None:
        with patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(self.manifest)}):
            with self.assertRaises(SystemExit) as caught:
                post.resolve_repo("example/unlisted")
        self.assertIn("registry is closed", str(caught.exception))

    def test_omitted_repo_without_origin_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            post.resolve_repo(None, self.root)
        self.assertIn("no readable origin", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
