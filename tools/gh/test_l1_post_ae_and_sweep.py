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


def _fake_run(*args, **kwargs):
    """Stands in for every git/gh subprocess call main() makes on the
    ae-and-sweep path (resolve_sha, spec fetch, world_checks' git/gh
    calls). Keyed on recognizable argv fragments rather than exact
    command strings, since exact flag order isn't the thing under test."""
    result = subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    argv = args[0] if args and isinstance(args[0], (list, tuple)) else args
    joined = " ".join(str(a) for a in argv)
    if "rev-parse" in joined:
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


if __name__ == "__main__":
    unittest.main()


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
