#!/usr/bin/env python3
"""Tests for `forge_onboard_identity.py` (harmonic-forge#804). Hermetic: temp repos,
a temp `GH_ACCT_HOME`, and a stubbed slot login -- nothing reads a real credential."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import forge_onboard as fo  # noqa: E402
import forge_onboard_identity as fi  # noqa: E402
import manifest as mf  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


class IdentityBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        self.slots = self.root / "slots"
        env = mock.patch.dict(os.environ, {"GH_ACCT_HOME": str(self.slots)})
        env.start()
        self.addCleanup(env.stop)

    def project(self, **kw) -> mf.Project:
        fields = {"name": "p", "prefix": "P", "repo": "o/p", "account": "acct",
                  "path": str(self.repo)}
        fields.update(kw)
        return mf.Project(**fields)

    def make_slot(self) -> Path:
        slot = self.slots / "acct"
        slot.mkdir(parents=True)
        return slot

    def set_helper(self, value: str) -> None:
        _git(self.repo, "config", "--local", fi.HELPER_KEY, value)


class CheckIdentityTests(IdentityBase):
    def test_a_project_with_no_registered_checkout_is_skipped(self) -> None:
        self.assertEqual(fi.check_identity(self.project(path=None), fo.Check).status, fi.SKIP)
        self.assertEqual(fi.check_identity(self.project(account=None), fo.Check).status, fi.SKIP)

    def test_a_missing_slot_fails_and_names_the_repair(self) -> None:
        check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("gh-as --init acct", check.detail)

    def test_an_unauthenticated_slot_fails(self) -> None:
        self.make_slot()
        with mock.patch.object(fi, "_slot_login", return_value=""):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("not authenticated", check.detail)

    def test_a_slot_authenticating_as_the_wrong_login_fails(self) -> None:
        self.make_slot()
        with mock.patch.object(fi, "_slot_login", return_value="someoneelse"):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("authenticates as someoneelse", check.detail)

    def test_a_missing_helper_fails_and_shows_what_is_expected(self) -> None:
        slot = self.make_slot()
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("(none)", check.detail)
        self.assertIn(fi.expected_helper(slot), check.detail)

    def test_a_helper_pointing_at_the_wrong_slot_fails(self) -> None:
        """The pre-#804 kenekted state: a helper on the ad hoc config dir."""
        self.make_slot()
        self.set_helper("!GH_CONFIG_DIR=/home/x/.config/gh-harmonicarchitect /usr/bin/gh auth git-credential")
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("gh-harmonicarchitect", check.detail)

    def test_everything_aligned_passes_and_reports_the_email(self) -> None:
        slot = self.make_slot()
        self.set_helper(fi.expected_helper(slot))
        _git(self.repo, "config", "--local", "user.email", "marc@example.test")
        with mock.patch.object(fi, "_slot_login", return_value="ACCT"):  # case-insensitive
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.OK)
        self.assertIn("marc@example.test", check.detail)

    def test_an_unset_email_is_reported_not_failed(self) -> None:
        slot = self.make_slot()
        self.set_helper(fi.expected_helper(slot))
        with mock.patch.object(fi, "_slot_login", return_value="acct"), \
             mock.patch.object(fi, "_git_config", side_effect=[(0, fi.expected_helper(slot)), (1, "")]):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.OK)
        self.assertIn("(unset)", check.detail)


class ApplyIdentityTests(IdentityBase):
    def test_writes_the_helper_and_the_check_then_passes(self) -> None:
        slot = self.make_slot()
        done = fi.apply_identity(self.project(), fo.Check)
        self.assertEqual([c.status for c in done], [fi.OK])
        self.assertEqual(_git(self.repo, "config", "--local", "--get", fi.HELPER_KEY),
                         fi.expected_helper(slot))
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            self.assertEqual(fi.check_identity(self.project(), fo.Check).status, fi.OK)

    def test_replaces_a_wrong_helper_rather_than_adding_a_second(self) -> None:
        slot = self.make_slot()
        self.set_helper("!GH_CONFIG_DIR=/elsewhere /usr/bin/gh auth git-credential")
        fi.apply_identity(self.project(), fo.Check)
        values = subprocess.run(
            ["git", "-C", str(self.repo), "config", "--local", "--get-all", fi.HELPER_KEY],
            capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(values, [fi.expected_helper(slot)])

    def test_is_idempotent(self) -> None:
        self.make_slot()
        fi.apply_identity(self.project(), fo.Check)
        second = fi.apply_identity(self.project(), fo.Check)
        self.assertEqual(second[0].detail, "already points at the slot")

    def test_dry_run_writes_nothing(self) -> None:
        self.make_slot()
        done = fi.apply_identity(self.project(), fo.Check, dry_run=True)
        self.assertIn("would set", done[0].detail)
        rc = subprocess.run(["git", "-C", str(self.repo), "config", "--local", "--get", fi.HELPER_KEY],
                            capture_output=True).returncode
        self.assertNotEqual(rc, 0)

    def test_a_linked_worktree_inherits_the_one_write(self) -> None:
        """Every lane worktree shares the common .git/config, so one write covers all."""
        slot = self.make_slot()
        (self.repo / "f").write_text("x")
        _git(self.repo, "add", "-A")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
        worktree = self.root / "lane2"
        _git(self.repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        fi.apply_identity(self.project(), fo.Check)
        self.assertEqual(_git(worktree, "config", "--get", fi.HELPER_KEY),
                         fi.expected_helper(slot))

    def test_a_project_with_nothing_to_do_returns_no_checks(self) -> None:
        self.assertEqual(fi.apply_identity(self.project(path=None), fo.Check), [])


class RegisteredTests(unittest.TestCase):
    def test_check_identity_is_in_the_registry(self) -> None:
        self.assertIn(fo.check_identity, fo.CHECKS)


if __name__ == "__main__":
    unittest.main()
