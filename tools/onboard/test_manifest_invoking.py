#!/usr/bin/env python3
"""Tests for `manifest_invoking.py` (harmonic-forge#1579): the posting
entrypoints resolve an overlay-only repo from the checkout they run in, and
nothing else does.

Every case uses scratch `git init` repos, a scratch base manifest in
`FORGE_PROJECTS_MANIFEST`, a scratch slot root and a scratch cwd restored on
cleanup. The base declares no `acme` account: an overlay-only repo whose owner
`projects.toml` declares is refused (operator ruling, second round), which
`test_manifest_overlay.FILLER` would trigger.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest as mf  # noqa: E402
import manifest_identity as mi  # noqa: E402
import manifest_invoking as minv  # noqa: E402
import manifest_overlay as mo  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402
from test_manifest_overlay import PROTOCOL, _git, make_repo, overlay_text  # noqa: E402

BASE = """\
[[project]]
name = "hrse"
prefix = "H"
repo = "vitalharmony/hrse"
account = "vitalharmony"
path = "/nonexistent/hrse"

[[project]]
name = "kenekted-ai"
prefix = "Y"
repo = "kenekted/kenekted-ai"
account = "harmonicarchitect"
path = "/nonexistent/kenekted-ai"

[accounts.vitalharmony]
lane_slots = true
"""
ONBOARDED = "onboarded = true\n" + PROTOCOL
MISS = r"no \[\[project\]\] entry for acme/app; refusing to fall back to the global gh login"
CLOSED = r"no \[\[project\]\] entry for acme/app; the registry is closed"


class InvokingCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.base = self.tmp / "projects.toml"
        self.base.write_text(BASE)
        self.slots = self.tmp / "slots"
        for account in ("acme", "acmeorg", "vitalharmony"):
            (self.slots / account).mkdir(parents=True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.outside)
        env = mock.patch.dict(os.environ, {"FORGE_PROJECTS_MANIFEST": str(self.base),
                                           "GH_ACCT_HOME": str(self.slots)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LANE", None)
        mi._VERIFIED.clear()
        self.app = self.repo("app", "acme/app")

    def repo(self, name: str, repo: str, overlay_repo: str | None = None) -> Path:
        overlay_repo = overlay_repo or repo
        owner, short = overlay_repo.split("/")
        return make_repo(self.tmp / name, f"https://github.com/{repo}.git",
                         overlay_text(name=short, repo=overlay_repo, account=owner,
                                      prefix="Q", extra=f'workspace = "{owner}-{short}"\n' + ONBOARDED))


class AC1ResolvesInsideItsCheckout(InvokingCase):
    def test_tc1_project_and_onboarded_from_the_checkout_and_a_subdirectory(self) -> None:
        (self.app / "sub").mkdir()
        for where in (self.app, self.app / "sub"):
            with self.subTest(where=where):
                os.chdir(where)
                self.assertEqual(minv.project_for_repo("acme/app").account, "acme")
                self.assertEqual(minv.require_onboarded("acme/app").repo, "acme/app")
        os.chdir(self.outside)
        self.assertEqual(minv.project_for_repo("acme/app", cwd=self.app).account, "acme")

    def test_tc1_a_linked_worktree_reads_its_main_checkouts_overlay(self) -> None:
        _git("-C", str(self.app), "commit", "-q", "--allow-empty", "-m", "init")
        _git("-C", str(self.app), "worktree", "add", "-q", "--detach", str(self.tmp / "wt"))
        self.assertEqual(minv.project_for_repo("acme/app", cwd=self.tmp / "wt").repo, "acme/app")

    def test_tc1_post_comment_gets_past_identity_in_a_subprocess(self) -> None:
        bindir = self.tmp / "bin"
        bindir.mkdir()
        log = self.tmp / "gh.log"
        stub = bindir / "gh"
        stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n'
                        'if [ "$1 $2" = "api user" ]; then echo acme; exit 0; fi\n'
                        'echo "stub: no network" >&2; exit 1\n')
        stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(self.tmp)}
        script = str(HERE.parent / "gh" / "post_comment.py")
        args = [sys.executable, script, "--repo", "acme/app", "--issue", "1", "--body", "x"]
        inside = subprocess.run(args, cwd=self.app, env=env, capture_output=True, text=True,
                                timeout=120, check=False)
        calls = log.read_text().splitlines()
        self.assertEqual(calls[0], "api user --jq .login", inside.stderr)
        self.assertGreater(len(calls), 1, inside.stderr)  # it went on past identity
        self.assertNotIn("no [[project]] entry", inside.stderr)
        log.unlink()
        outside = subprocess.run(args, cwd=self.outside, env=env, capture_output=True, text=True,
                                 timeout=120, check=False)
        self.assertNotEqual(outside.returncode, 0)
        self.assertIn("no [[project]] entry for acme/app", outside.stderr)
        self.assertFalse(log.exists())


class AC2RefusesElsewhere(InvokingCase):
    def assert_refused(self, cwd: Path, *extra: str) -> None:
        for call, pattern in ((minv.project_for_repo, MISS), (minv.require_onboarded, CLOSED)):
            with self.subTest(call=call.__name__, cwd=cwd):
                with self.assertRaisesRegex(ManifestError, pattern) as ctx:
                    call("acme/app", cwd=cwd)
                for fragment in extra:
                    self.assertIn(fragment, str(ctx.exception))

    def test_tc2_from_a_directory_in_no_checkout(self) -> None:
        self.assert_refused(self.outside)

    def test_tc2_from_a_checkout_whose_overlay_names_another_repo(self) -> None:
        self.assert_refused(self.repo("other", "acme/other"), "this checkout's .claude/forge.toml")

    def test_tc2_from_a_fork_whose_overlay_names_the_fork(self) -> None:
        self.assert_refused(self.repo("fork", "evil/app"), "this checkout's .claude/forge.toml")

    def test_registered_owner_refuses(self) -> None:
        """Operator ruling, second round: an owner projects.toml declares, as an
        account or as the owner of a registered repo, registers there."""
        for repo in ("vitalharmony/newapp", "kenekted/newapp"):
            with self.subTest(repo=repo):
                where = self.repo(repo.replace("/", "-"), repo)
                for call in (minv.project_for_repo, minv.require_onboarded):
                    with self.assertRaisesRegex(ManifestError, "an account projects.toml declares"):
                        call(repo, cwd=where)


class AC3NoGlobalLookupSeesIt(InvokingCase):
    def test_tc3_global_lookups_and_hook_inputs_are_unchanged_inside(self) -> None:
        lookups = (mf.by_repo, mf.prefix_repos, mf.prefixes, mf.sweep_repos, mf.repo_boards,
                   mf.load, mi.workspaces)
        before = [repr(f()) for f in lookups]
        os.chdir(self.app)
        self.assertEqual([repr(f()) for f in lookups], before)
        self.assertNotIn("acme/app", mf.by_repo())
        self.assertIsNone(mi.slot_env_or_none("acme/app"))
        self.assertIsNone(mi.repo_for_path_or_none(self.app))
        with self.assertRaisesRegex(ManifestError, MISS):
            mi.account_for("acme/app")


class AC4BaseHitWins(InvokingCase):
    def test_tc4_a_registered_repo_reads_no_overlay(self) -> None:
        os.chdir(self.app)
        with mock.patch.object(mo, "read", side_effect=AssertionError("overlay read")) as read:
            project = minv.project_for_repo("vitalharmony/hrse")
        read.assert_not_called()  # project_for_checkout would wrap the raise, so check the call
        self.assertEqual(project, mf.by_repo()["vitalharmony/hrse"])


class Q3PushForAnOrganization(InvokingCase):
    def setUp(self) -> None:
        super().setUp()
        self.org = self.repo("org", "acmeorg/app")
        os.chdir(self.org)

    def gh(self, stdout: str, code: int = 0):
        """The push read's `gh api`, stubbed in `manifest_invoking` alone (git still runs)."""
        result = subprocess.CompletedProcess([], code, stdout=stdout, stderr="HTTP 502")
        fake = mock.Mock(run=mock.Mock(return_value=result), SubprocessError=subprocess.SubprocessError)
        return mock.patch.object(minv, "subprocess", fake)

    def apply(self, login: str, repo: str = "acmeorg/app") -> str:
        with mock.patch.object(mi, "_probe_login", return_value=login):
            return mi.apply_project_identity(repo)

    def test_an_org_member_with_push_posts(self) -> None:
        with self.gh("Organization\ntrue\n") as run:
            self.assertEqual(self.apply("alice"), "acmeorg")
        self.assertEqual(run.run.call_args.args[0][:3], ["gh", "api", "repos/acmeorg/app"])
        self.assertEqual(os.environ["GH_CONFIG_DIR"], str(self.slots / "acmeorg"))

    def test_a_login_without_push_refuses(self) -> None:
        for stdout in ("Organization\nfalse\n", "Organization\nnull\n", "User\ntrue\n", ""):
            with self.subTest(stdout=stdout), self.gh(stdout):
                with self.assertRaisesRegex(SystemExit, "identity mismatch"):
                    self.apply("alice")

    def test_an_unreadable_permission_refuses(self) -> None:
        with self.gh("", code=1), self.assertRaisesRegex(SystemExit, "could not read push"):
            self.apply("alice")

    def test_a_user_owned_overlay_repo_keeps_login_equals_account(self) -> None:
        os.chdir(self.app)
        with self.gh("Organization\ntrue\n") as run:
            self.assertEqual(self.apply("acme", "acme/app"), "acme")
            run.run.assert_not_called()
        mi._VERIFIED.clear()  # the probe is cached per slot
        with self.gh("User\ntrue\n"), self.assertRaisesRegex(SystemExit, "identity mismatch"):
            self.apply("alice", "acme/app")

    def test_a_registered_repo_never_asks_for_push(self) -> None:
        with self.gh("Organization\ntrue\n") as run:
            with self.assertRaisesRegex(SystemExit, "identity mismatch"):
                self.apply("alice", "vitalharmony/hrse")
            run.run.assert_not_called()


class HooksArePinnedOut(unittest.TestCase):
    """Pitch revision 2: the third consumer is reached from the posting entrypoints only."""

    def grep(self, *args: str) -> list[str]:
        return subprocess.run(["git", "-C", str(HERE.parents[1]), "grep", *args],
                              capture_output=True, text=True, check=False).stdout.split("\n")

    def test_only_the_named_files_name_the_module(self) -> None:
        found = [f for f in self.grep("-l", "manifest_invoking", "--", "*.py", ":!**/test_*") if f]
        self.assertEqual(sorted(found), ["tools/gh/l1_post.py", "tools/gh/preclose_check.py",
                                         "tools/onboard/manifest_identity.py",
                                         "tools/onboard/manifest_invoking.py",
                                         "tools/onboard/manifest_overlay.py"])

    def test_manifest_identity_names_it_only_in_apply_project_identity(self) -> None:
        source = (HERE / "manifest_identity.py").read_text()
        fn = next(n for n in ast.parse(source).body
                  if isinstance(n, ast.FunctionDef) and n.name == "apply_project_identity")
        lines = [i for i, line in enumerate(source.splitlines(), 1) if "manifest_invoking" in line]
        self.assertTrue(lines)
        self.assertTrue(all(fn.lineno <= i <= fn.end_lineno for i in lines), lines)

    def test_no_hook_calls_apply_project_identity(self) -> None:
        self.assertEqual([f for f in self.grep("-n", "apply_project_identity(", "--", "tools/hooks")
                          if f], [])


if __name__ == "__main__":
    unittest.main()
