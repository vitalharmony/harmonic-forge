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
        # harmonic-forge#871's lesson: a detached auto-gc/maintenance run left
        # writing into the repo races tearDown's rmtree under load.
        git(root, "config", "gc.auto", "0")
        git(root, "config", "maintenance.auto", "false")

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
    """The boundary is the newest reachable `vX.Y.0` tag (harmonic-forge#1372)."""

    def _history(self):
        self.repo.commit("pre", version="2.7.9")
        self.minor = self.repo.commit("bump 2.8.0", version="2.8.0")
        git(self.repo.root, "tag", "v2.8.0")
        self.repo.commit("work a")
        self.repo.commit("bump 2.8.1", version="2.8.1")
        self.repo.commit("work b")
        self.repo.commit("bump 2.8.2", version="2.8.2")

    def test_boundary_is_the_minor_tag_not_the_patch_bump(self):
        self._history()
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["bump 2.8.2", "work b", "bump 2.8.1", "work a"])
        _, description = tl.resolve_boundary(self.repo.root, f"version-minor:{PKG}")
        self.assertIn(self.minor[:8], description)

    def test_a_version_string_that_returns_later_does_not_move_the_boundary(self):
        # AC1: "2.8.0" appears early (an untagged stray), is removed, and
        # returns at the commit tagged v2.8.0. The tag wins, not the first hit.
        self.repo.commit("stray early", version="2.8.0")
        self.repo.commit("moved on", version="2.7.9")
        self.repo.commit("work before")
        tagged = self.repo.commit("real 2.8.0", version="2.8.0")
        git(self.repo.root, "tag", "v2.8.0")
        self.repo.commit("work after")
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["work after"])
        _, description = tl.resolve_boundary(self.repo.root, f"version-minor:{PKG}")
        self.assertIn(tagged[:8], description)

    def test_a_patch_bump_after_the_tag_does_not_move_the_boundary(self):
        # AC3.
        self._history()
        git(self.repo.root, "tag", "v2.8.2")  # a patch tag is not a minor tag
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["bump 2.8.2", "work b", "bump 2.8.1", "work a"])

    def test_the_newest_reachable_minor_tag_is_taken(self):
        self.repo.commit("one", version="2.8.0")
        git(self.repo.root, "tag", "v2.8.0")
        self.repo.commit("two")
        self.repo.commit("three", version="2.9.0")
        git(self.repo.root, "tag", "v2.9.0")
        self.repo.commit("four")
        self.assertEqual(self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}")), ["four"])

    def test_the_relpath_is_not_read(self):
        # No caller edits: any path is accepted, and the file need not exist.
        self.repo.commit("one")
        git(self.repo.root, "tag", "v1.0.0")
        self.repo.commit("two")
        self.assertEqual(self.subjects(tl.render(self.repo.root, "version-minor:no/such/file.json")), ["two"])

    def test_uncommitted_working_tree_bump_is_ignored(self):
        self._history()
        (self.repo.root / PKG).write_text(json.dumps({"version": "2.9.0"}) + "\n")
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["bump 2.8.2", "work b", "bump 2.8.1", "work a"])

    def test_head_is_the_tagged_release_gives_an_empty_view(self):
        self.repo.commit("pre", version="2.8.5")
        self.repo.commit("bump 2.9.0", version="2.9.0")
        git(self.repo.root, "tag", "v2.9.0")
        self.assertEqual(tl.render(self.repo.root, f"version-minor:{PKG}"), "")

    def test_no_minor_tag_raises(self):
        # AC2: a version string alone is no boundary, and nothing falls back.
        self.repo.commit("start", version="3.1.0")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_only_a_patch_tag_raises(self):
        self.repo.commit("start", version="3.1.4")
        git(self.repo.root, "tag", "v3.1.4")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_a_prerelease_tag_is_not_a_boundary(self):
        # The describe glob also matches `v3.1.0-rc.0`; it must not be taken.
        self.repo.commit("start")
        git(self.repo.root, "tag", "v3.1.0-rc.0")
        self.repo.commit("next")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_a_nearer_prerelease_or_suffixed_tag_does_not_hide_the_release_tag(self):
        # Both refuters' finding: describe picks the NEAREST match, so an rc
        # tag after v3.1.0 used to raise instead of resolving to v3.1.0.
        self.repo.commit("release", version="3.1.0")
        git(self.repo.root, "tag", "v3.1.0")
        self.repo.commit("after release")
        git(self.repo.root, "tag", "v3.1.4-pre-v3.2.0")
        self.repo.commit("rc work")
        git(self.repo.root, "tag", "v3.2.0-rc.0")
        self.repo.commit("more rc")
        git(self.repo.root, "tag", "v3x.2y.0")
        git(self.repo.root, "tag", "v3.2.0+build.0")
        self.repo.commit("latest")
        got = self.subjects(tl.render(self.repo.root, f"version-minor:{PKG}"))
        self.assertEqual(got, ["latest", "more rc", "rc work", "after release"])

    def test_the_description_keeps_its_since_the_x_y_z_bump_wording(self):
        # HRSE2's directive_provenance tests and the session-start block read it.
        self.repo.commit("one", version="2.9.0")
        git(self.repo.root, "tag", "v2.9.0")
        self.repo.commit("two")
        _, description = tl.resolve_boundary(self.repo.root, f"version-minor:{PKG}")
        self.assertTrue(description.startswith("Commits on HEAD since the 2.9.0 bump ("), description)

    def test_a_tag_not_reachable_from_head_is_ignored(self):
        self.repo.commit("base")
        git(self.repo.root, "checkout", "-q", "-b", "side")
        self.repo.commit("side work")
        git(self.repo.root, "tag", "v9.9.0")
        git(self.repo.root, "checkout", "-q", "main")
        self.repo.commit("main work")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, f"version-minor:{PKG}")

    def test_version_minor_needs_a_path(self):
        self.repo.commit("x")
        with self.assertRaises(ValueError):
            tl.render(self.repo.root, "version-minor:")

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
