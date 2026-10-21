#!/usr/bin/env python3
"""Tests for harmonic-forge#381: --kind ae-and-sweep atomic posting.

Exercises `main()` itself (not just the underlying primitives) via a mocked
`run()` and `comment_body()`, so a regression that reintroduces the
two-invocation gap or breaks the validate-both-before-either-posts ordering
would actually fail these tests -- calling the primitives directly, as an
earlier draft of this file did, would pass even if main()'s own wiring were
wrong.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import patch
import unittest

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")


# harmonic-forge#1016: shared ci_conclusion stub -- ready-for-l3 reads CI
# before static_checks; never make a real `gh api` call from a test.
import unittest.mock as _ci_mock  # noqa: E402
_CI_PATCH = _ci_mock.patch.object(post.gate_ci, "ci_conclusion", return_value=("pending", "stub"))


# harmonic-forge#1115 AC9: a standalone AE reads the latest sweep's external
# calls from the thread; never a real `gh api` call from a test.
_SWEEP_CALLS_PATCH = _ci_mock.patch.object(post, "_latest_sweep_calls", return_value=[])

# harmonic-forge#1115: these tests check the AE/sweep logic, not the dry-run
# preflight gate (test_l1_post_dry_run.py owns that). With no session id the
# gate does not apply, so the outcome cannot depend on the caller's
# CLAUDE_CODE_SESSION_ID (set in any Claude session and in kill_check runs).
_NO_SESSION_PATCH = _ci_mock.patch.dict("os.environ")


def setUpModule():  # noqa: N802
    _CI_PATCH.start()
    _SWEEP_CALLS_PATCH.start()
    _NO_SESSION_PATCH.start()
    import os
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def tearDownModule():  # noqa: N802
    _CI_PATCH.stop()
    _SWEEP_CALLS_PATCH.stop()
    _NO_SESSION_PATCH.stop()

# harmonic-forge#472: every artifact in the ratified table now leads with
# its own summary block, so these fixtures carry one. They are the shape a
# real post must have, which is the point of using them here.
AE_BODY = ("## AE — F1\n\n**Authorized:** the spec in issuecomment-77, "
           "against `feat/x` @ `abc1234`.\n**Next:** Lane 3 executes and "
           "reports.\n\nApproved, execute against the spec.")
SPEC_BODY = "### Test Cases\n1. Does the thing.\n2. Does the other thing."
SWEEP_BODY = ("## Gate-readiness sweep — F1\n\n**Readiness:** both cases "
              "executable as written.\n**Blockers:** none.\n**Next:** Lane 3 "
              "executes.\n\nWrite tier: R\n\n1. TC1 -- ready\n2. TC2 -- ready")
MALFORMED_SWEEP_BODY = ("## Gate-readiness sweep — F1\n\n**Readiness:** one "
                        "case.\n**Blockers:** none.\n**Next:** go.\n\n"
                        "Write tier: R\n\n1. only one case listed")
FAKE_SHA = "a" * 40


GOOD_PROD_SCRIPT = (
    "import argparse\nfrom _migration_env import ensure_env\n"
    "def main():\n    p = argparse.ArgumentParser()\n"
    "    p.add_argument('--apply', action='store_true')\n    p.parse_args()\n    ensure_env()\n")


def _fake_run(*args, **kwargs):
    """Stands in for every git/gh subprocess call main() makes on the
    ae-and-sweep path (resolve_sha, spec fetch, world_checks' git/gh
    calls). Keyed on recognizable argv fragments rather than exact
    command strings, since exact flag order isn't the thing under test."""
    result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    argv = args[0] if args and isinstance(args[0], (list, tuple)) else args
    joined = " ".join(str(a) for a in argv)
    if argv[:2] == ("git", "show") or list(argv[:2]) == ["git", "show"]:
        # harmonic-forge#1026: a `--prod-run script=` AE reads the script at
        # the SHA; this fixture's script meets the runner's contract.
        result.stdout = GOOD_PROD_SCRIPT
    elif "rev-parse" in joined:
        result.stdout = FAKE_SHA
    elif "issues/comments/" in joined and "--jq" in joined:
        result.stdout = SPEC_BODY
    elif "ls-remote" in joined:
        result.stdout = f"{FAKE_SHA}\trefs/heads/fix/1-thing"
    elif "merge-base" in joined:
        result.stdout = FAKE_SHA
    elif ".state" in joined:
        result.stdout = "open"
    elif "repo" in argv and "view" in argv:
        # a private-repo incident: require_open_pr() resolves cwd's repo via `gh repo
        # view` before listing PRs against it.
        result.stdout = "vitalharmony/hrse"
    elif "pr" in argv and "list" in argv:
        # a private-repo incident: ready-for-l3/ae now require an open PR to exist. This
        # fixture's own scope is the ae-and-sweep atomicity guarantee, not
        # that check, so it stands in as already-satisfied here.
        result.stdout = '[{"number": 1, "state": "OPEN"}]'
    elif "diff" in joined or "worktree" in joined:
        result.stdout = ""
    return result


class ArgValidationTests(unittest.TestCase):
    """These guards run before resolve_sha()/any network call -- run() is
    intentionally left unmocked (and would fail loudly and differently, on
    an unresolvable fake SHA, if reached) so a passing test here means the
    guard itself fired, not that *something* downstream happened to exit
    first (harmonic-forge#381 preclose review: the original version of
    these two tests passed identically against a version of l1_post.py
    with both named guards deleted, because they only asserted
    `assertRaises(SystemExit)` with no message check and no run() mock to
    rule out a downstream cause)."""

    def test_ae_and_sweep_rejects_plain_file_flag(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with patch.object(sys, "argv", [
                "l1_post.py", "--issue", "1", "--kind", "ae-and-sweep",
                "--sha", FAKE_SHA, "--branch", "x",
                "--file", "/tmp/handoff.md", "--spec-comment", "123",
            ]):
                post.main()
        self.assertIn("requires --ae-file and --sweep-file", str(ctx.exception))

    def test_ae_and_sweep_requires_spec_comment(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with patch.object(sys, "argv", [
                "l1_post.py", "--issue", "1", "--kind", "ae-and-sweep",
                "--sha", FAKE_SHA, "--branch", "x",
                "--ae-file", "/tmp/ae.md", "--sweep-file", "/tmp/sweep.md",
            ]):
                post.main()
        self.assertIn("--spec-comment is required", str(ctx.exception))

    def test_ae_and_sweep_rejected_when_run_is_never_reached(self) -> None:
        """Confirms the two tests above genuinely stop before any
        subprocess call -- if either guard were deleted, this mock would
        record a call and the two tests above would need it to explain
        their SystemExit, which they don't."""
        with patch.object(post, "run") as mock_run:
            with self.assertRaises(SystemExit):
                with patch.object(sys, "argv", [
                    "l1_post.py", "--issue", "1", "--kind", "ae-and-sweep",
                    "--sha", FAKE_SHA, "--branch", "x",
                    "--file", "/tmp/handoff.md", "--spec-comment", "123",
                ]):
                    post.main()
            mock_run.assert_not_called()


class MainIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("/tmp/claude-1000-l1-post-test")
        self.tmp.mkdir(exist_ok=True)
        self.ae_file = self.tmp / "ae.md"
        self.sweep_file = self.tmp / "sweep.md"
        self.posted_kinds: list[str] = []
        # harmonic-forge#804: `main` acts as the repo's registered account, which
        # these fake repos have none of; that step has its own tests.
        identity = mock.patch.object(post, "apply_project_identity")
        identity.start()
        self.addCleanup(identity.stop)
        # ...and the cwd here is an unregistered worktree, so the manifest fallback
        # that replaced `gh repo view` in require_open_pr is stubbed the same way.
        cwd_repo = mock.patch.object(
            post, "project_for_path", return_value=mock.Mock(repo="vitalharmony/harmonic-forge"))
        cwd_repo.start()
        self.addCleanup(cwd_repo.stop)

    def tearDown(self) -> None:
        for f in (self.ae_file, self.sweep_file):
            f.unlink(missing_ok=True)

    def _fake_comment_body(self, repo, issue, body):
        kind = "ae" if "kind=ae;" in body else "sweep"
        self.posted_kinds.append(kind)
        return f"https://github.com/{repo}/issues/{issue}#issuecomment-{len(self.posted_kinds)}", len(self.posted_kinds)

    def _argv(self) -> list[str]:
        return [
            "l1_post.py", "--repo", "vitalharmony/harmonic-forge",
            "--issue", "1", "--kind", "ae-and-sweep",
            "--sha", FAKE_SHA, "--branch", "x",
            "--ae-file", str(self.ae_file), "--sweep-file", str(self.sweep_file),
            "--spec-comment", "999",
        ]

    def test_happy_path_posts_ae_then_sweep_through_main(self) -> None:
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(SWEEP_BODY)
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", self._argv()):
            post.main()
        self.assertEqual(self.posted_kinds, ["ae", "sweep"], "AE must post strictly before sweep, through main() itself")

    def test_main_resolves_an_omitted_repo(self) -> None:
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(SWEEP_BODY)
        argv = self._argv()
        del argv[1:3]
        with patch.object(post, "resolve_repo", return_value="vitalharmony/harmonic-forge") as resolve, \
             patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", argv):
            post.main()
        resolve.assert_called_once_with(None)

    def test_a_refused_auto_ae_posts_nothing_through_main(self) -> None:
        """harmonic-forge#874: main()'s ae-and-sweep path runs the auto-AE
        check before either half posts."""
        sys.path.insert(0, str(ROOT))
        import _auto_ae  # noqa: PLC0415
        import check_lane3_ready  # noqa: PLC0415
        self.ae_file.write_text(AE_BODY.replace("the spec in", "auto-AE (R-0378), the spec in"))
        self.sweep_file.write_text(SWEEP_BODY)
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(check_lane3_ready, "fetch_comments", return_value=[]), \
             patch.object(_auto_ae, "auto_ae_refusal", return_value="auto-AE is off") as refusal, \
             patch.object(sys, "argv", self._argv()):
            with self.assertRaises(SystemExit):
                post.main()
        refusal.assert_called_once()
        self.assertEqual(self.posted_kinds, [], "a refused auto-AE must post neither half")

    def test_a_standalone_auto_ae_is_refused_through_main(self) -> None:
        """harmonic-forge#874: an auto-AE posts only with its sweep."""
        sys.path.insert(0, str(ROOT))
        import check_lane3_ready  # noqa: PLC0415
        self.ae_file.write_text(AE_BODY.replace("the spec in", "auto-AE (R-0378), the spec in"))
        argv = ["l1_post.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "1", "--kind", "ae",
                "--sha", FAKE_SHA, "--branch", "x", "--file", str(self.ae_file)]
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(check_lane3_ready, "fetch_comments", return_value=[]), \
             patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                post.main()
        self.assertIn("ae-and-sweep", str(ctx.exception))
        self.assertEqual(self.posted_kinds, [])

    def test_malformed_sweep_posts_nothing_through_main(self) -> None:
        """AC3, exercised through main(): a malformed sweep body must reject
        before the AE is posted -- zero comments created."""
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(MALFORMED_SWEEP_BODY)
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", self._argv()):
            with self.assertRaises(SystemExit):
                post.main()
        self.assertEqual(self.posted_kinds, [], "no comment may be posted when the sweep body is malformed")

    def test_sweep_transport_failure_leaves_ae_standing_and_exits_loud(self) -> None:
        """AC2: a posting-time failure on the sweep (both bodies already
        valid) must not roll back the AE, and must exit non-zero with an
        explicit message naming the sweep as owed."""
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(SWEEP_BODY)

        def flaky_comment_body(repo, issue, body):
            if "kind=ae;" in body:
                self.posted_kinds.append("ae")
                return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1
            raise SystemExit("[l1-post] GitHub comment failed: simulated transport error")

        import io
        stderr_capture = io.StringIO()
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=flaky_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", self._argv()), \
             patch.object(sys, "stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                post.main()
        # AC2 explicitly requires "exit non-zero" -- a mutant that swallowed
        # the failure and exited 0 (e.g. `sys.exit(0)` instead of `raise`)
        # would satisfy a bare assertRaises(SystemExit) but not this.
        self.assertTrue(ctx.exception.code, "must exit non-zero, not swallow the failure")
        self.assertEqual(self.posted_kinds, ["ae"], "AE must remain posted; no rollback")
        # AC2's own wording: emit the loud message to stderr AND exit
        # non-zero. The exception itself is allowed to carry only the
        # original transport error -- the loud, explicit AE-posted-but-
        # sweep-owed message is a separate stderr print, checked here.
        self.assertIn("sweep", stderr_capture.getvalue().lower())
        self.assertIn("AE posted", stderr_capture.getvalue())
        # harmonic-forge#381 preclose review: an earlier version of this
        # message called resubmitting the AE "safe" -- false whenever the
        # sweep's POST actually succeeded and only its read-back
        # verification failed, since a duplicate AE then postdates the
        # real sweep and check_lane3_ready.py's strict ordering blocks on
        # it. The message must tell the operator to check the thread
        # first, never assert resubmitting AE is safe.
        self.assertNotIn("safe to resubmit", stderr_capture.getvalue().lower())
        self.assertIn("check the issue thread", stderr_capture.getvalue().lower())

    def test_standalone_kind_ae_still_works_through_post_kind(self) -> None:
        """AC5: the single-kind path (unchanged --kind ae, not ae-and-sweep)
        must still go through post_kind() correctly after the refactor --
        specifically that its footer says kind=ae, not something the
        ae-and-sweep refactor could have let leak across (e.g. always
        writing 'ae-and-sweep' into the footer, which would break
        check_lane3_ready.py's kind= footer scan -- AC4)."""
        self.ae_file.write_text(AE_BODY)
        captured_body = {}

        def capture_comment_body(repo, issue, body):
            captured_body["body"] = body
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1

        argv = [
            "l1_post.py", "--repo", "vitalharmony/harmonic-forge",
            "--issue", "1", "--kind", "ae",
            "--sha", FAKE_SHA, "--branch", "x",
            "--file", str(self.ae_file),
        ]
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=capture_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", argv):
            post.main()
        self.assertIn("kind=ae;", captured_body["body"])
        self.assertNotIn("kind=ae-and-sweep", captured_body["body"])


class LeadOnBothHalves(unittest.TestCase):
    """harmonic-forge#472 on this branch specifically.

    Both bodies are validated before EITHER is posted, and the lead check
    joins that set. A lead check on only one half would be worse than none:
    a sweep refused after the AE landed leaves exactly the bare AE this
    branch exists to prevent (a private-repo incident).
    """

    def setUp(self) -> None:
        self.tmp = Path("/tmp/claude-1000-l1-post-lead-test")
        self.tmp.mkdir(exist_ok=True)
        self.ae_file = self.tmp / "ae.md"
        self.sweep_file = self.tmp / "sweep.md"
        self.posted: list[str] = []

    def tearDown(self) -> None:
        for f in (self.ae_file, self.sweep_file):
            f.unlink(missing_ok=True)

    def _run(self, ae_body: str, sweep_body: str):
        self.ae_file.write_text(ae_body)
        self.sweep_file.write_text(sweep_body)
        argv = ["l1_post.py", "--repo", "vitalharmony/harmonic-forge",
                "--issue", "1", "--kind", "ae-and-sweep",
                "--sha", FAKE_SHA, "--branch", "x",
                "--ae-file", str(self.ae_file), "--sweep-file", str(self.sweep_file),
                "--spec-comment", "999"]
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body",
                          side_effect=lambda r, i, b: (self.posted.append(b) or ("u", len(self.posted)))), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", argv):
            post.main()

    def test_a_sweep_missing_its_lead_is_refused_and_no_ae_is_posted(self) -> None:
        stripped = SWEEP_BODY.replace("**Blockers:** none.\n", "")
        with self.assertRaises(SystemExit) as ctx:
            self._run(AE_BODY, stripped)
        self.assertIn("harmonic-forge#472", str(ctx.exception))
        self.assertEqual(self.posted, [], "nothing may post when either body is refused")

    def test_an_ae_missing_its_lead_is_refused(self) -> None:
        stripped = AE_BODY.replace("**Next:** Lane 3 executes and reports.", "")
        with self.assertRaises(SystemExit) as ctx:
            self._run(stripped, SWEEP_BODY)
        self.assertIn("harmonic-forge#472", str(ctx.exception))
        self.assertEqual(self.posted, [])



LIVE_SPEC = ("## Lane 3 Test Spec — F1\n\n### Cases\n| TC1 | live | x | y | z |\n"
             "| TC2 | ac | x | y | z |\n\n"
             "<!-- l1-post v1; kind=spec; posted-by=LANE3; classes=1:live,2:ac -->")
LIVE_SWEEP = ("## Gate-readiness sweep — F1\n\n**Readiness:** both cases executable.\n"
              "**Blockers:** none.\n**Next:** Lane 3 executes.\n\nWrite tier: W\n\n"
              "### Per-case readiness\n"
              "- TC1: ready, live UI run. External calls: Gemini generate, Voyage embed\n"
              "- TC2: ready, offline fixtures\n")


class ExternalCallsCrossCheck(LeadOnBothHalves):
    """harmonic-forge#1115 AC9: the AE authorizes every external call the
    sweep's live cases name; H2452's AE did not, and Lane 3 stopped."""

    def setUp(self) -> None:
        super().setUp()
        # As MainIntegrationTests: the fake repo has no registered identity or checkout.
        for name, kw in (("apply_project_identity", {}),
                         ("project_for_path", {"return_value": mock.Mock(repo="vitalharmony/harmonic-forge")})):
            patcher = mock.patch.object(post, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, ae_body: str, sweep_body: str):
        with patch.object(post, "_spec_body", return_value=LIVE_SPEC):
            super()._run(ae_body, sweep_body)

    def test_ae_missing_a_swept_external_call_is_refused(self) -> None:
        ae = AE_BODY.replace("against `feat/x`", "with Gemini generate, against `feat/x`")
        with self.assertRaises(SystemExit) as ctx:
            self._run(ae, LIVE_SWEEP)
        self.assertIn("Voyage embed", str(ctx.exception))
        self.assertNotIn("Gemini generate --", str(ctx.exception))
        self.assertEqual(self.posted, [], "nothing may post when the AE omits a swept call")

    def test_ae_naming_every_swept_external_call_posts(self) -> None:
        ae = AE_BODY.replace("against `feat/x`",
                             "with External calls Gemini generate and Voyage embed, against `feat/x`")
        self._run(ae, LIVE_SWEEP)
        self.assertEqual(len(self.posted), 2)

    def test_a_standalone_ae_is_checked_against_the_latest_sweep_on_its_sha(self) -> None:
        comments = [
            {"id": 1, "body": LIVE_SWEEP.replace("Voyage embed", "Old call")
             + f"\n<!-- l1-post v1; kind=sweep; sha={'b' * 40} -->"},
            {"id": 2, "body": LIVE_SWEEP + f"\n<!-- l1-post v1; kind=sweep; sha={FAKE_SHA} -->"},
        ]
        _SWEEP_CALLS_PATCH.stop()
        self.addCleanup(_SWEEP_CALLS_PATCH.start)
        import check_lane3_ready
        with patch.object(check_lane3_ready, "fetch_comments", return_value=comments):
            self.assertEqual(post._latest_sweep_calls("o/r", 1, FAKE_SHA), ["Gemini generate", "Voyage embed"])
            self.assertEqual(post._latest_sweep_calls("o/r", 1, "c" * 40), [])

    def test_a_live_case_with_no_external_calls_line_is_refused(self) -> None:
        sweep = LIVE_SWEEP.replace(" External calls: Gemini generate, Voyage embed", "")
        with self.assertRaises(SystemExit) as ctx:
            self._run(AE_BODY, sweep)
        self.assertIn("TC1 carry no `External calls:` line", str(ctx.exception))
        self.assertEqual(self.posted, [])


class ProdRunFooterTests(MainIntegrationTests):
    """harmonic-forge#878 (R-0377): `--prod-run` stamps the one production
    action an AE authorizes into its reserved footer -- the AE half only."""

    def setUp(self) -> None:
        super().setUp()
        self.bodies: list[str] = []

    def _fake_comment_body(self, repo, issue, body):
        self.bodies.append(body)
        return super()._fake_comment_body(repo, issue, body)

    def _post(self, *extra: str) -> None:
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(SWEEP_BODY)
        with patch.object(post, "run", side_effect=_fake_run), \
             patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", self._argv() + list(extra)):
            post.main()

    def test_prod_run_is_stamped_on_the_ae_footer_only(self) -> None:
        import _prod_run
        self._post("--prod-run", "script=scripts/1-1891-backfill.py,apply")
        ae, sweep = self.bodies
        self.assertEqual(_prod_run.declared(ae), {
            "issue": 1, "sha": FAKE_SHA, "script": "scripts/1-1891-backfill.py", "apply": True})
        self.assertIn(f"prod-run=issue:1,sha:{FAKE_SHA},script:scripts/1-1891-backfill.py,apply:true;", ae)
        self.assertIsNone(_prod_run.declared(sweep))
        self.assertNotIn("prod-run", sweep)

    def test_count_label_form(self) -> None:
        import _prod_run
        self._post("--prod-run", "count-label=Interpretation")
        self.assertEqual(_prod_run.declared(self.bodies[0]),
                         {"issue": 1, "sha": FAKE_SHA, "count_label": "Interpretation"})

    def test_no_prod_run_no_field(self) -> None:
        self._post()
        self.assertNotIn("prod-run", self.bodies[0])

    def test_a_script_breaking_the_runner_contract_posts_nothing(self) -> None:
        # harmonic-forge#1026: the H1896 shape at b86a207b, for both AE kinds.
        bad = ("from dotenv import load_dotenv\nimport argparse\n"
               "def main():\n    p = argparse.ArgumentParser()\n"
               "    p.add_argument('--apply', action='store_true')\n    load_dotenv('backend/.env')\n")
        def run_bad(*args, **kwargs):
            if list(args[:2]) == ["git", "show"]:
                return subprocess.CompletedProcess(args, 0, stdout=bad, stderr="")
            return _fake_run(*args, **kwargs)
        with patch.object(post, "run", side_effect=run_bad), \
                patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
                patch.object(sys, "argv", self._argv() + ["--prod-run", "script=scripts/1-1896-x.py"]):
            self.ae_file.write_text(AE_BODY)
            self.sweep_file.write_text(SWEEP_BODY)
            with self.assertRaises(SystemExit) as ctx:
                post.main()
        self.assertIn("no ensure_env() call", str(ctx.exception))
        self.assertIn("calls load_dotenv()", str(ctx.exception))
        self.assertEqual(self.bodies, [])

    def test_an_unreadable_script_refuses(self) -> None:
        def run_missing(*args, **kwargs):
            if list(args[:2]) == ["git", "show"]:
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: path does not exist")
            return _fake_run(*args, **kwargs)
        with patch.object(post, "run", side_effect=run_missing), \
                patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
                patch.object(sys, "argv", self._argv() + ["--prod-run", "script=scripts/1-x.py"]):
            with self.assertRaises(SystemExit) as ctx:
                post.main()
        self.assertIn("cannot read scripts/1-x.py", str(ctx.exception))
        self.assertEqual(self.bodies, [])

    def test_count_label_never_reads_a_script(self) -> None:
        seen = []
        def run_spy(*args, **kwargs):
            seen.append(list(args[:2]))
            return _fake_run(*args, **kwargs)
        self.ae_file.write_text(AE_BODY)
        self.sweep_file.write_text(SWEEP_BODY)
        with patch.object(post, "run", side_effect=run_spy), \
                patch.object(post, "comment_body", side_effect=self._fake_comment_body), \
                patch.object(post, "write_receipt"), \
                patch.object(sys, "argv", self._argv() + ["--prod-run", "count-label=Interpretation"]):
            post.main()
        self.assertNotIn(["git", "show"], seen)

    def test_malformed_spec_refuses_before_anything_runs(self) -> None:
        for spec in ("script=scripts/check_lane3_ready.py", "script=scripts/1-x.py,force",
                     "count-label=Person`) DETACH DELETE n", "cypher=MATCH (n) DETACH DELETE n"):
            with self.subTest(spec=spec), patch.object(post, "run") as mock_run, \
                    patch.object(sys, "argv", self._argv() + ["--prod-run", spec]):
                with self.assertRaises(SystemExit) as ctx:
                    post.main()
                self.assertIn("--prod-run", str(ctx.exception))
                mock_run.assert_not_called()

    def test_prod_run_refused_on_a_non_ae_kind(self) -> None:
        argv = ["l1_post.py", "--issue", "1", "--kind", "ready-for-l3", "--sha", FAKE_SHA,
                "--branch", "x", "--file", "/tmp/x.md", "--prod-run", "count-label=Person"]
        with patch.object(post, "run") as mock_run, patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                post.main()
        self.assertIn("valid only for --kind ae", str(ctx.exception))
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
