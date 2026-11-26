#!/usr/bin/env python3
"""Tests for `manifest_scope.home_project`, the home-repo rule (harmonic-forge#1596).

Scratch manifests are passed as `manifest=`; the partition case reads the
checked-in `projects.toml` on purpose (AC4: the operator's repos are unchanged).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest  # noqa: E402
import manifest_scope as ms  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402


def entry(prefix: str, repo: str, account: str | None, name: str | None = None) -> str:
    acct = f'account = "{account}"\n' if account is not None else ""
    return f'[[project]]\nname = "{name or prefix}"\nprefix = "{prefix}"\nrepo = "{repo}"\n{acct}'


class Scratch(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "projects.toml"
        path.write_text(text, encoding="utf-8")
        return path


class HomeProject(Scratch):
    def test_registered_and_owner_equals_account_is_home(self) -> None:
        path = self.write(entry("Z", "acme/app", "acme") + entry("Q", "Acme/Other", "ACME"))
        self.assertEqual(ms.home_project("acme/app", path).prefix, "Z")
        self.assertEqual(ms.home_project("https://github.com/ACME/App.git", path).prefix, "Z")
        self.assertEqual(ms.home_project("acme/other", path).prefix, "Q")

    def test_owner_other_than_account_unregistered_or_malformed_is_not_home(self) -> None:
        path = self.write(entry("Z", "acme/app", "someone") + entry("Q", "acme/q", "acme"))
        for repo in ("acme/app", "acme/unlisted", "not a repo", "a/b/c", ""):
            with self.subTest(repo=repo):
                self.assertIsNone(ms.home_project(repo, path))

    def test_a_non_ascii_account_is_never_home(self) -> None:
        """Pitch revision 4: `"ſcme".casefold()` is `"scme"`; ASCII lower is not."""
        path = self.write(entry("Z", "scme/app", "ſcme"))
        self.assertIsNone(ms.home_project("scme/app", path))

    def test_an_unreadable_or_invalid_manifest_is_never_home(self) -> None:
        invalid = self.write(entry("Z", "acme/app", "acme") + "onboarded = true\n")
        broken = self.write("[[project\n")
        for path in (invalid, broken, invalid.parent / "absent.toml"):
            with self.subTest(path=path.name):
                self.assertIsNone(ms.home_project("acme/app", path))
                self.assertIn("ManifestError", ms.manifest_error(path))
        self.assertIsNone(ms.manifest_error(self.write(entry("Z", "acme/app", "acme"))))


class LivePartition(unittest.TestCase):
    """TC4 / AC4: the home rule on the checked-in manifest is today's partition."""

    def test_h_f_i_o_are_home_and_k_y_d_p_are_not(self) -> None:
        by_prefix = {p.prefix: p.repo for p in manifest.load() if p.repo}
        for prefix in "HFIO":
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(ms.home_project(by_prefix[prefix]))
        for prefix in "KYDP":
            with self.subTest(prefix=prefix):
                self.assertIsNone(ms.home_project(by_prefix[prefix]))


class PrefixValidation(Scratch):
    """Operator ruling 2026-11-25 (second round): one ASCII letter, unique in any case."""

    def test_a_lowercase_twin_of_a_prefix_is_refused(self) -> None:
        path = self.write(entry("H", "acme/a", "acme", "a") + entry("h", "acme/b", "acme", "b"))
        with self.assertRaisesRegex(ManifestError, "claimed by both"):
            manifest.load(path)

    def test_a_non_ascii_letter_is_refused(self) -> None:
        path = self.write(entry("ı", "acme/a", "acme"))
        with self.assertRaisesRegex(ManifestError, "single letter"):
            manifest.load(path)


class OverlayStaysOutOfGrantScope(unittest.TestCase):
    """TC8 / R1: only the overlay's two consumers and the resolver name it."""

    def test_only_three_production_files_name_the_overlay_resolver(self) -> None:
        root = HERE.parents[1]
        found = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", "-E",
             "manifest_overlay|path_project|project_for_checkout", "--",
             "tools/**/*.py", "skills/**/*.py", ":!**/test_*"],
            capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(found.stdout.split(), ["tools/lane/ci_plan.py",
                                                "tools/onboard/manifest_identity.py",
                                                "tools/onboard/manifest_overlay.py"])


if __name__ == "__main__":
    unittest.main()
