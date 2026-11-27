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
        """The push read's `gh api`, stubbed in `manifest_invoking` alone (git still runs).
        The stub refuses any read but `repos/<repo> --jq '.owner.type, .permissions.push'`
        (preclose round 1: a `.permissions.pull` read must not pass)."""
        result = subprocess.CompletedProcess([], code, stdout=stdout, stderr="HTTP 502")

        def run(argv, **_kwargs):
            if argv[3:] != ["--jq", ".owner.type, .permissions.push"]:
                raise AssertionError(f"the push read queried {argv!r}")
            return result
        fake = mock.Mock(run=mock.Mock(side_effect=run), SubprocessError=subprocess.SubprocessError)
        return mock.patch.object(minv, "subprocess", fake)

    def gh_api(self, response: dict):
        """The push read answered from a repos API `response`, evaluating the `--jq`
        paths it is given, so the answer depends on the permission queried."""
        def run(argv, **_kwargs):
            out = []
            for field in argv[argv.index("--jq") + 1].split(","):
                value = response
                for part in field.strip().lstrip(".").split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                out.append("null" if value is None
                           else str(value).lower() if isinstance(value, bool) else str(value))
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(out) + "\n", stderr="")
        fake = mock.Mock(run=mock.Mock(side_effect=run), SubprocessError=subprocess.SubprocessError)
        return mock.patch.object(minv, "subprocess", fake)

    def apply(self, login: str, repo: str = "acmeorg/app") -> str:
        with mock.patch.object(mi, "_probe_login", return_value=login):
            return mi.apply_project_identity(repo)

    def test_an_org_member_with_push_posts(self) -> None:
        ambient = str(self.tmp / "ambient")
        os.environ["GH_CONFIG_DIR"] = ambient
        with self.gh("Organization\ntrue\n") as run:
            self.assertEqual(self.apply("alice"), "acmeorg")
        self.assertEqual(run.run.call_args.args[0],
                         ["gh", "api", "repos/acmeorg/app", "--jq", ".owner.type, .permissions.push"])
        self.assertEqual(os.environ["GH_CONFIG_DIR"], ambient)  # ruling D11: never a slot

    def test_the_push_read_queries_push_not_pull(self) -> None:
        """Preclose round 1, survivor 3: a pull-only member refuses, a push member posts."""
        pull_only = {"owner": {"type": "Organization"}, "permissions": {"pull": True, "push": False}}
        with self.gh_api(pull_only), self.assertRaisesRegex(SystemExit, "identity mismatch"):
            self.apply("alice")
        pusher = {"owner": {"type": "Organization"}, "permissions": {"pull": False, "push": True}}
        with self.gh_api(pusher):
            self.assertEqual(self.apply("alice"), "acmeorg")

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
        with self.gh("User\ntrue\n"), self.assertRaisesRegex(SystemExit, "identity mismatch"):
            self.apply("alice", "acme/app")

    def test_a_registered_repo_never_asks_for_push(self) -> None:
        with self.gh("Organization\ntrue\n") as run:
            with self.assertRaisesRegex(SystemExit, "identity mismatch"):
                self.apply("alice", "vitalharmony/hrse")
            run.run.assert_not_called()


class D11OverlayOnlyIsAmbient(InvokingCase):
    """Ruling D11 (preclose round 1, survivors 1 and 2) as amended by D14: an
    overlay-only account never uses a credential slot; outside a lane the session's
    current login posts, and inside a lane the post refuses before any gh call."""

    def setUp(self) -> None:
        super().setUp()
        self.ambient = str(self.tmp / "ambient")
        os.environ.update(GH_CONFIG_DIR=self.ambient, GH_TOKEN="ambient-token")
        self.seen: list[tuple[str | None, str | None]] = []

    def probe(self, logins: dict[str, str]):
        """`_probe_login` answering by the GH_CONFIG_DIR it runs under, recording the env."""
        def login() -> str:
            self.seen.append((os.environ.get("GH_CONFIG_DIR"), os.environ.get("GH_TOKEN")))
            return logins.get(os.environ.get("GH_CONFIG_DIR", ""), "")
        return mock.patch.object(mi, "_probe_login", side_effect=login)

    def test_outside_a_lane_posts_under_the_ambient_login(self) -> None:
        os.chdir(self.app)
        for lane in (None, "", "0", "4"):
            with self.subTest(lane=lane):
                self.seen.clear()
                if lane is None:
                    os.environ.pop("LANE", None)
                else:
                    os.environ["LANE"] = lane
                with self.probe({self.ambient: "acme"}):
                    self.assertEqual(mi.apply_project_identity("acme/app"), "acme")
                self.assertEqual(self.seen, [(self.ambient, "ambient-token")])
                self.assertEqual((os.environ["GH_CONFIG_DIR"], os.environ["GH_TOKEN"]),
                                 (self.ambient, "ambient-token"))

    def test_every_lane_refuses_overlay_only_posting(self) -> None:
        """Ruling D14: LANE 1, 2 and 3 refuse by name, with no probe and no env change,
        so nothing runs on the shared keyring's login."""
        os.chdir(self.app)
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                self.seen.clear()
                os.environ["LANE"] = lane
                with self.probe({self.ambient: "acme"}), self.assertRaisesRegex(
                        SystemExit, "overlay-only posting is not available inside a lane until "
                        r"harmonic-forge#1627 \(ruling D14 on #1579\)"):
                    mi.apply_project_identity("acme/app")
                self.assertEqual(self.seen, [])
                self.assertEqual((os.environ["GH_CONFIG_DIR"], os.environ["GH_TOKEN"]),
                                 (self.ambient, "ambient-token"))

    def test_a_registered_repo_in_a_lane_is_unaffected(self) -> None:
        """D14 reaches the overlay-only branch only: a projects.toml repo in a lane still
        takes its lane slot (here a token-less one, which refuses on #1470's message)."""
        os.chdir(self.app)
        os.environ["LANE"] = "1"
        (self.slots / "vitalharmony-lane1").mkdir()
        with self.assertRaisesRegex(SystemExit, "has no token in") as ctx:
            mi.apply_project_identity("vitalharmony/hrse")
        self.assertNotIn("overlay-only", str(ctx.exception))

    def test_an_owner_named_like_a_slot_dir_never_reads_it(self) -> None:
        """`slots/acmeorg` exists and authenticates as acmeorg; the ambient login is
        alice, without push, so the post refuses rather than borrowing the slot."""
        os.chdir(self.repo("org", "acmeorg/app"))
        fake = mock.Mock(run=mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout="Organization\nfalse\n", stderr="")),
            SubprocessError=subprocess.SubprocessError)
        logins = {self.ambient: "alice", str(self.slots / "acmeorg"): "acmeorg"}
        with self.probe(logins), mock.patch.object(minv, "subprocess", fake):
            with self.assertRaisesRegex(SystemExit, "identity mismatch"):
                mi.apply_project_identity("acmeorg/app")
        self.assertEqual(self.seen, [(self.ambient, "ambient-token")])


class RequireOnboardedRefuses(InvokingCase):
    """Preclose round 1, survivor 4: each refusal branch of `require_onboarded`."""

    def overlay_repo(self, name: str, extra: str) -> Path:
        return make_repo(self.tmp / name, f"https://github.com/acme/{name}.git",
                         overlay_text(name=name, repo=f"acme/{name}", account="acme",
                                      prefix="Q", extra=extra))

    def test_an_overlay_not_onboarded_refuses(self) -> None:
        for name, extra in (("noob", "onboarded = false\n" + PROTOCOL), ("unset", PROTOCOL)):
            with self.subTest(name=name):
                where = self.overlay_repo(name, extra)
                self.assertEqual(minv.project_for_repo(f"acme/{name}", cwd=where).repo, f"acme/{name}")
                with self.assertRaisesRegex(ManifestError,
                                            f"acme/{name} is declared with onboarded = false"):
                    minv.require_onboarded(f"acme/{name}", cwd=where)

    def test_an_overlay_without_protocol_refuses(self) -> None:
        """A real onboarded overlay with no protocol already fails validation, so the
        `protocol is None` branch is reached with a resolved Project patched in."""
        where = self.overlay_repo("noproto", 'onboarded = true\nworkspace = "acme-noproto"\n')
        with self.assertRaisesRegex(ManifestError, r"onboarded but declares no \[project.protocol\]"):
            minv.require_onboarded("acme/noproto", cwd=where)
        bare = mf.Project(name="noproto", prefix="Q", account="acme", repo="acme/noproto",
                          onboarded=True, workspace="acme-noproto")
        self.assertIsNone(bare.protocol)
        with mock.patch.object(minv, "_fallback", return_value=bare):
            with self.assertRaisesRegex(ManifestError, r"acme/noproto has no \[project.protocol\]"):
                minv.require_onboarded("acme/noproto", cwd=where)

    def test_a_registered_repo_not_onboarded_refuses_without_the_overlay(self) -> None:
        """`vitalharmony/hrse` is in BASE with no `onboarded` (false): the base-hit path
        is `manifest.require_onboarded_repo`, which refuses it, from any cwd."""
        self.assertFalse(mf.by_repo()["vitalharmony/hrse"].onboarded)
        for where in (self.outside, self.app):
            with self.subTest(where=where):
                with self.assertRaisesRegex(ManifestError,
                                            "vitalharmony/hrse is declared with onboarded = false"):
                    minv.require_onboarded("vitalharmony/hrse", cwd=where)


D13 = r"acme/app is registered only by its forge\.toml; board/CI support is harmonic-forge#1625/#1626"


class D13NoBoardNoCI(InvokingCase):
    """Ruling D13: a repo known only from its forge.toml gets no board and no CI
    account. Every board step and the CI read refuses by name, and a board the
    overlay names (here vitalharmony's, the round-2 hostile-author case) is never used."""

    def setUp(self) -> None:
        super().setUp()
        gh = str(HERE.parent / "gh")
        if gh not in sys.path:
            sys.path.insert(0, gh)
        self.app = make_repo(
            self.tmp / "boarded", "https://github.com/acme/app.git",
            overlay_text(name="app", repo="acme/app", account="acme", prefix="Q",
                         extra='workspace = "acme-app"\nboard_owner = "vitalharmony"\n'
                               'board_number = "1"\n' + ONBOARDED))
        os.chdir(self.app)
        self.assertEqual(minv.require_onboarded("acme/app").board, ("vitalharmony", "1"))

    def test_the_guard_refuses_only_an_overlay_only_repo(self) -> None:
        with self.assertRaisesRegex(minv.OverlayOnlyRefusal, D13 + ".*so its CI read refuses"):
            minv.refuse_board_ci("acme/app", "CI read")
        minv.refuse_board_ci("vitalharmony/hrse", "CI read")  # projects.toml: no refusal
        os.chdir(self.outside)
        minv.refuse_board_ci("acme/app", "CI read")  # not registered here: the caller's own path

    def test_l1_post_board_field_list_refuses_by_name(self) -> None:
        import l1_post  # noqa: PLC0415
        boom = AssertionError("a board command ran")
        with mock.patch.object(l1_post, "board_field_names", side_effect=boom), \
                mock.patch.object(l1_post, "run", side_effect=boom):
            with self.assertRaisesRegex(SystemExit, D13 + ".*so its board field list refuses"):
                l1_post.validate_board_fields_set("acme/app", 1)

    def test_l1_post_tier_read_refuses_by_name(self) -> None:
        import l1_post  # noqa: PLC0415
        boom = AssertionError("a board command ran")
        with mock.patch.object(l1_post, "resolve_board_tier", side_effect=boom), \
                mock.patch.object(l1_post, "run", side_effect=boom):
            with self.assertRaisesRegex(SystemExit, D13 + ".*so its board Tier read refuses"):
                l1_post.LeadCtx("acme/app", 1, []).tier()

    def test_gh_issue_board_lookup_refuses_by_name(self) -> None:
        import gh_issue  # noqa: PLC0415
        with self.assertRaisesRegex(SystemExit, r"\[GH\] " + D13 + ".*so its board item-add"):
            gh_issue.resolve_board_for_repo("acme/app")
        os.chdir(self.outside)  # an unmapped repo keeps the old refusal
        with self.assertRaisesRegex(SystemExit, "No project board mapped"):
            gh_issue.resolve_board_for_repo("acme/app")

    def test_gh_issue_files_nothing_for_an_overlay_only_repo(self) -> None:
        """The filing refuses before identity, the milestone read and create_issue."""
        import gh_issue  # noqa: PLC0415
        boom = AssertionError("gh_issue went past the D13 check")
        argv = ["gh_issue.py", "--repo", "acme/app", "--title", "t", "--body", "b"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(gh_issue, "apply_project_identity", side_effect=boom), \
                mock.patch.object(gh_issue, "fetch_milestones", side_effect=boom), \
                mock.patch.object(gh_issue, "create_issue", side_effect=boom) as create:
            with self.assertRaisesRegex(SystemExit, D13 + ".*before the issue is created"):
                gh_issue.main()
        create.assert_not_called()

    def test_gate_ci_ci_read_refuses_by_name(self) -> None:
        import gate_ci  # noqa: PLC0415
        import l1_post  # noqa: PLC0415
        with mock.patch("_etag_cache.conditional_get",
                        side_effect=AssertionError("a CI read ran")):
            with self.assertRaisesRegex(minv.OverlayOnlyRefusal, D13 + ".*so its CI read refuses"):
                gate_ci.s0_reader("acme/app")
            with self.assertRaisesRegex(SystemExit, r"\[l1-post\] " + D13):
                l1_post._ci_reader("acme/app")
        self.assertTrue(callable(gate_ci.s0_reader("vitalharmony/hrse")))

    def test_every_gate_ci_read_path_refuses_by_name(self) -> None:
        """Reforge pass 1 survivor 1: check_gate_result (so gate_ci.main and
        post_lane_discussion's PASS check) and _carry_forward.check_ci refuse by
        name before any read; a FAIL stays publishable."""
        import _carry_forward  # noqa: PLC0415
        import gate_ci  # noqa: PLC0415
        no_read = mock.Mock(side_effect=AssertionError("a CI read ran"))
        passed = "## Lane 3 Gate\n**Verdict:** PASS\nHead-SHA: " + "1" * 40 + "\n"
        ok, message = gate_ci.check_gate_result("acme/app", passed, run=no_read)
        self.assertFalse(ok)
        self.assertRegex(message, r"\[GATE\] REFUSED: " + D13 + ".*so its CI read refuses")
        self.assertTrue(gate_ci.check_gate_result("acme/app", passed.replace("PASS", "FAIL"),
                                                  run=no_read)[0])
        refusals = _carry_forward.check_ci("acme/app", "1" * 40, run=no_read)
        self.assertEqual(len(refusals), 1)
        self.assertRegex(str(refusals[0]), D13 + ".*so its CI read refuses")
        report = self.tmp / "report.md"
        report.write_text(passed)
        with mock.patch.object(gate_ci, "_run", no_read), \
                mock.patch("manifest_identity.apply_project_identity"), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(gate_ci.main(["--repo", "acme/app", "--file", str(report)]), 1)
        self.assertRegex(printed.call_args.args[0], D13)
        no_read.assert_not_called()

    def test_l1_post_and_preclose_check_resolve_an_overlay_only_repo(self) -> None:
        """Reforge pass 1 survivor 2: AC1 at both entrypoints' repo resolution;
        reverting either to the projects.toml-only lookup fails here."""
        import l1_post  # noqa: PLC0415
        import preclose_check  # noqa: PLC0415
        self.assertEqual(l1_post.resolve_repo("acme/app"), "acme/app")
        self.assertEqual(l1_post.resolve_repo(None, self.app), "acme/app")
        self.assertEqual(preclose_check.registered_repo("acme/app"), "acme/app")
        os.chdir(self.outside)
        with self.assertRaises(SystemExit):
            l1_post.resolve_repo("acme/app")
        with self.assertRaises(SystemExit):
            preclose_check.registered_repo("acme/app")


class HooksArePinnedOut(unittest.TestCase):
    """Pitch revision 2: the third consumer is reached from the posting entrypoints only."""

    def grep(self, *args: str) -> list[str]:
        return subprocess.run(["git", "-C", str(HERE.parents[1]), "grep", *args],
                              capture_output=True, text=True, check=False).stdout.split("\n")

    def test_only_the_named_files_name_the_module(self) -> None:
        found = [f for f in self.grep("-l", "manifest_invoking", "--", "*.py", ":!**/test_*") if f]
        self.assertEqual(sorted(found), ["tools/gh/gate_ci.py", "tools/gh/gh_issue.py",
                                         "tools/gh/l1_post.py", "tools/gh/preclose_check.py",
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
        # Production hooks only: a test under tools/hooks may exercise identity
        # (test_manifest_scoping's grant-scope funnel, harmonic-forge#1579 D23).
        self.assertEqual([f for f in self.grep("-n", "apply_project_identity(", "--", "tools/hooks",
                                               ":!**/test_*") if f], [])


if __name__ == "__main__":
    unittest.main()
