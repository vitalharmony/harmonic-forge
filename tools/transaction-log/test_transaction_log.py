#!/usr/bin/env python3
"""Tests for the read-time transaction-log renderer (harmonic-forge#883).

Every case builds a throwaway git repo in a tempdir; nothing reads this repo.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import transaction_log as tl  # noqa: E402

PKG = "frontend/package.json"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Repo:
    def __init__(self, root: Path):
        self.root = root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "t")
        git(root, "config", "commit.gpgsign", "false")
        git(root, "config", "core.hooksPath", "/dev/null")

    def commit(self, subject: str, body: str = "", version: str | None = None,
               touch: str | None = None) -> str:
        if version is not None:
            path = self.root / PKG
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"name": "x", "version": version}, indent=2) + "\n")
        name = touch or f"f{len(list(self.root.glob('f*')))}.txt"
        (self.root / name).write_text(subject + "\n")
        git(self.root, "add", "-A")
        message = subject + (f"\n\n{body}" if body else "")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def subjects(self, text: str) -> list[str]:
        return [line[3:] for line in text.splitlines() if line.startswith("## ")]


class RecentTests(_RepoCase):
    def test_recent_n_returns_exactly_n_newest_first(self):
        for i in range(35):
            self.repo.commit(f"c{i}")
        got = self.subjects(tl.render(self.repo.root, "recent:30"))
        self.assertEqual(got, [f"c{i}" for i in range(34, 4, -1)])

    def test_trailers_and_body_are_dropped(self):
        self.repo.commit("subject only", body=(
            "Long body line.\n\nCo-Authored-By: X <x@example.com>\n"
            "Claude-Session: https://example.com/s"))
        out = tl.render(self.repo.root, "recent:5")
        self.assertIn("## subject only", out)
        for leaked in ("Co-Authored-By", "Claude-Session", "Long body line"):
            self.assertNotIn(leaked, out)

    def test_each_entry_carries_its_shortstat(self):
        self.repo.commit("one file")
        self.assertIn("- 1 file changed, 1 insertion(+)", tl.render(self.repo.root, "recent:1"))

    def test_bad_recent_value_raises(self):
        self.repo.commit("x")
        for bad in ("recent:", "recent:0", "recent:abc"):
            with self.assertRaises(ValueError, msg=bad):
                tl.render(self.repo.root, bad)


class VersionMinorTests(_RepoCase):
    def _history(self):
        self.repo.commit("pre", version="2.7.9")
        self.minor = self.repo.commit("bump 2.8.0", version="2.8.0")
        self.repo.commit("work a")
        self.repo.commit("bump 2.8.1", version="2.8.1")
        self.repo.commit("work b")
        self.repo.commit("bump 2.8.2", version="2.8.2")

    def test_boundary_is_the_minor_bump_not_the_patch_bump(self):
        self._history()
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["bump 2.8.2", "work b", "bump 2.8.1", "work a"])

    def test_removal_commit_is_not_taken_as_the_boundary(self):
        # -S matches both the commit that added "2.8.0" and the one that
        # removed it (the 2.8.1 bump); the oldest is the introduction.
        self._history()
        _, description = tl.resolve_boundary(self.repo.root, f"version-minor:{PKG}")
        self.assertIn(self.minor[:8], description)

    def test_uncommitted_working_tree_bump_is_ignored(self):
        self._history()
        (self.repo.root / PKG).write_text(json.dumps({"version": "2.9.0"}) + "\n")
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["bump 2.8.2", "work b", "bump 2.8.1", "work a"])

    def test_head_is_the_minor_bump_gives_an_empty_view(self):
        self.repo.commit("pre", version="2.8.5")
        self.repo.commit("bump 2.9.0", version="2.9.0")
        self.assertEqual(tl.render(self.repo.root, f"version-minor:{PKG}"), "")

    def test_no_minor_commit_raises(self):
        # Started life at a patch version; X.Y.0 was never introduced.
        self.repo.commit("start", version="3.1.4")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_pickaxe_hit_with_wrong_version_raises(self):
        # The string appears first in an unrelated field, so the oldest -S hit
        # does not carry version X.Y.0 and must be refused, not trusted.
        path = self.repo.root / PKG
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": "1.0.0", "note": {"version": "2.8.0"}},
                                   indent=2) + "\n")
        git(self.repo.root, "add", "-A")
        git(self.repo.root, "commit", "-q", "-m", "decoy")
        self.repo.commit("bump", version="2.8.3")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_unknown_boundary_raises(self):
        self.repo.commit("x")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, "everything")


class CapTests(_RepoCase):
    def test_max_chars_truncates_and_names_the_omission(self):
        for i in range(10):
            self.repo.commit(f"commit number {i}")
        out = tl.render(self.repo.root, "recent:10", max_chars=150)
        kept = self.subjects(out)
        self.assertTrue(0 < len(kept) < 10)
        body = out.rsplit("\n- …", 1)[0]
        self.assertLessEqual(len(body), 150)
        self.assertIn(f"- … {10 - len(kept)} older entries omitted; run mise run "
                      "transaction-log --all", out)

    def test_max_entries_truncates_and_names_the_omission(self):
        for i in range(5):
            self.repo.commit(f"c{i}")
        out = tl.render(self.repo.root, "recent:5", max_entries=2)
        self.assertEqual(self.subjects(out), ["c4", "c3"])
        self.assertIn("3 older entries omitted", out)

    def test_no_caps_means_no_omission_line(self):
        for i in range(5):
            self.repo.commit(f"c{i}")
        self.assertNotIn("omitted", tl.render(self.repo.root, "recent:5"))


if __name__ == "__main__":
    unittest.main()
