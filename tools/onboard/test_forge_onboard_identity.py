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
        # The operator's real global config is `helper =` (a reset) then
        # `!/usr/bin/gh auth git-credential`. Reproduce that shape in a temp file
        # so the merged-config reads below never see the real machine's config.
        self.global_config = self.root / "gitconfig-global"
        self.global_config.write_text(
            '[credential "https://github.com"]\n\thelper =\n'
            '\thelper = !/usr/bin/gh auth git-credential\n')
        env = mock.patch.dict(os.environ, {
            "GH_ACCT_HOME": str(self.slots),
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_CONFIG_SYSTEM": os.devnull,
        })
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
        """A correctly-written local list: a reset, then the helper."""
        _git(self.repo, "config", "--local", "--replace-all", fi.HELPER_KEY, "")
        _git(self.repo, "config", "--local", "--add", fi.HELPER_KEY, value)

    def set_helper_without_reset(self, value: str) -> None:
        """The naive write: appended BEHIND the global helper."""
        _git(self.repo, "config", "--local", "--replace-all", fi.HELPER_KEY, value)


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

    def test_a_missing_helper_fails_and_shows_what_git_will_use(self) -> None:
        """With nothing local, git consults the GLOBAL helper: that is what the
        report must show, not "(none)"."""
        slot = self.make_slot()
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)
        self.assertIn("!/usr/bin/gh auth git-credential", check.detail)
        self.assertIn(fi.expected_helper(slot), check.detail)

    def test_a_helper_appended_behind_the_global_one_fails(self) -> None:
        """The bug harmonic-forge#804's preclose found: the local helper is present
        (a `--local --get` would say so) but sits BEHIND the global one, which git
        consults first, so a push still uses the global account."""
        slot = self.make_slot()
        self.set_helper_without_reset(fi.expected_helper(slot))
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.FAIL)

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
             mock.patch.object(fi, "effective_helpers", return_value=[fi.expected_helper(slot)]), \
             mock.patch.object(fi, "_git_config", return_value=(1, "")):
            check = fi.check_identity(self.project(), fo.Check)
        self.assertEqual(check.status, fi.OK)
        self.assertIn("(unset)", check.detail)


class ApplyIdentityTests(IdentityBase):
    def test_writes_the_helper_and_the_check_then_passes(self) -> None:
        slot = self.make_slot()
        done = fi.apply_identity(self.project(), fo.Check)
        self.assertEqual([c.status for c in done], [fi.OK])
        self.assertEqual(fi.effective_helpers(self.repo), [fi.expected_helper(slot)])
        with mock.patch.object(fi, "_slot_login", return_value="acct"):
            self.assertEqual(fi.check_identity(self.project(), fo.Check).status, fi.OK)

    def test_a_wrong_local_helper_is_replaced_and_the_global_one_is_reset(self) -> None:
        slot = self.make_slot()
        self.set_helper_without_reset("!GH_CONFIG_DIR=/elsewhere /usr/bin/gh auth git-credential")
        fi.apply_identity(self.project(), fo.Check)
        local = subprocess.run(
            ["git", "-C", str(self.repo), "config", "--local", "--get-all", fi.HELPER_KEY],
            capture_output=True, text=True).stdout.split("\n")[:-1]
        self.assertEqual(local, ["", fi.expected_helper(slot)])
        self.assertEqual(fi.effective_helpers(self.repo), [fi.expected_helper(slot)])

    def test_is_idempotent(self) -> None:
        self.make_slot()
        fi.apply_identity(self.project(), fo.Check)
        second = fi.apply_identity(self.project(), fo.Check)
        self.assertEqual(second[0].detail, "already the only effective helper")

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
        self.assertEqual(fi.effective_helpers(worktree), [fi.expected_helper(slot)])

    def test_a_project_with_nothing_to_do_returns_no_checks(self) -> None:
        self.assertEqual(fi.apply_identity(self.project(path=None), fo.Check), [])


class GitConsultsHelpersInOrderTests(IdentityBase):
    """The mechanism the fix rests on, proven by running git rather than by reading
    config: with fake helpers that log when they are called, `git credential fill`
    answers from the FIRST helper, and a local reset removes the global one."""

    def _helper(self, name: str, password: str) -> Path:
        script = self.root / name
        log = self.root / f"{name}.called"
        script.write_text(f'#!/bin/sh\ntouch "{log}"\necho username={name}\necho password={password}\n')
        script.chmod(0o755)
        return script

    def _fill(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).stdout

    def test_without_a_reset_the_global_helper_answers_first(self) -> None:
        glob, local = self._helper("globalhelper", "g"), self._helper("localhelper", "l")
        self.global_config.write_text(
            f'[credential "https://github.com"]\n\thelper = {glob}\n')
        _git(self.repo, "config", "--local", "--add", fi.HELPER_KEY, str(local))
        self.assertIn("username=globalhelper", self._fill())
        self.assertFalse((self.root / "localhelper.called").exists())

    def test_a_local_reset_makes_the_local_helper_the_only_one(self) -> None:
        glob, local = self._helper("globalhelper", "g"), self._helper("localhelper", "l")
        self.global_config.write_text(
            f'[credential "https://github.com"]\n\thelper = {glob}\n')
        _git(self.repo, "config", "--local", "--replace-all", fi.HELPER_KEY, "")
        _git(self.repo, "config", "--local", "--add", fi.HELPER_KEY, str(local))
        self.assertIn("username=localhelper", self._fill())
        self.assertFalse((self.root / "globalhelper.called").exists())


class RegisteredTests(unittest.TestCase):
    def test_check_identity_is_in_the_registry(self) -> None:
        self.assertIn(fo.check_identity, fo.CHECKS)


if __name__ == "__main__":
    unittest.main()
