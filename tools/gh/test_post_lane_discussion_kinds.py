#!/usr/bin/env python3
"""Tests for Lane 3's `kind=` footer emitter (harmonic-forge#473).

The premise both a private-repo incident and harmonic-forge#472 measured was that Lane 3's
Test Spec and Gate Results carry *no* footer. Measured precisely, they carry
one that says `kind=discussion` — the emitter existed, the argument did not.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402
import post_lane_discussion as P  # noqa: E402

SPEC = ("## Lane 3 Test Spec — H1\n\n"
        "**Cases:** 2 (write tier R).\n**Next:** submit for HITL approval.\n\n"
        "### Test cases\n1. TC1 — a thing.\n2. TC2 — another.\n")
# `Head-SHA` is REQUIRED as of harmonic-forge#504 AC2, and this fixture is what
# `EndToEnd` drives `main()` with. Without it, the moment the forge half of
# #504 lands on forge `main`, hrse CI resolves the real `gate_ci` module
# (ci.yml checks out harmonic-forge's default branch and symlinks it to
# $HOME/harmonic-forge) and this suite goes red in a repo whose PR never
# touched it — the same "main red for reasons unrelated to the PR that
# surfaced it" shape #504 exists to end, reproduced cross-repo by #504's own
# fix. Caught by preclose inspection, before either half merged.
GATE_PASS = ("## Lane 3 Gate Results — H1 — PASS\n\n"
             "**Verdict:** PASS\n**Head-SHA:** `0359854`\n"
             "**Finding:** none.\n**Next:** merge.\n\n"
             "| TC | Verdict |\n|---|---|\n| TC1 | PASS |\n\n"  # harmonic-forge#893: a case table
             "<details><summary>Evidence</summary>\n\nAll nine cases ran.\n\n</details>\n")
GATE_BLOCKED = ("## Lane 3 Gate Results — H1 — BLOCKED\n\n"
                "**Verdict:** BLOCKED\n**Finding:** no fixture available.\n"
                "**Next:** provision the fixture, then retest.\n\n"
                "Could not run: no fixture.\n")


def _case_flags(kind, body_path):
    """harmonic-forge#893: the per-case map a spec or gate result now requires,
    built from the body's own case ids, so these older tests keep exercising
    what they were written for."""
    import json as _json
    import tempfile as _tempfile
    if kind not in ("spec", "gate-result"):
        return []
    body = Path(body_path).read_text()
    ids = sorted(P.case_ids(body))
    verdict = P.verdict_of(body) if kind == "gate-result" else None
    value = "ac" if kind == "spec" else ("fail" if verdict == "FAIL" else "pass")
    handle = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    _json.dump({i: value for i in ids}, handle)
    handle.close()
    return ["--tc-classes" if kind == "spec" else "--tc-results", handle.name]


class DefaultPathUnchanged(unittest.TestCase):
    """Every existing caller passes no `--kind`. That path must be
    byte-identical, because `lane_state.py`, a private-repo incident's handoff-heading
    fallback and every already-posted comment all match the old footer."""

    def test_the_discussion_footer_is_byte_identical(self) -> None:
        self.assertEqual(
            P.footer("discussion", "anything", "LANE2"),
            "\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE2 -->\n")

    def test_discussion_gets_no_digest(self) -> None:
        """A digest on ordinary discussion would make every chat comment
        look like an attested artifact to `_validated`."""
        self.assertNotIn("body-sha256", P.footer("discussion", "x", "LANE1"))

    def test_discussion_needs_no_heading_and_is_not_quote_checked(self) -> None:
        P.validate_kind("discussion", "just talking about ## L2B in passing")

    def test_kind_defaults_to_discussion(self) -> None:
        self.assertEqual(P.KINDS[0], "discussion")


class FooterContract(unittest.TestCase):
    def test_spec_and_gate_result_carry_kind_and_digest(self) -> None:
        for kind, body in (("spec", SPEC), ("gate-result", GATE_PASS)):
            with self.subTest(kind=kind):
                out = P.footer(kind, body, "LANE3")
                self.assertIn(f"kind={kind};", out)
                self.assertIn("posted-by=LANE3", out)
                self.assertIn("body-sha256=", out)

    def test_the_digest_is_over_the_rstripped_body(self) -> None:
        """Same contract as `l1_post.post_kind` — a private-repo incident: the posted text
        is `body.rstrip() + footer`, so hashing the unstripped body records a
        digest no verifier can reconstruct, which reads as tampering on an
        artifact nobody edited."""
        body = SPEC + "\n\n\n"
        expected = hashlib.sha256(SPEC.rstrip("\n").encode()).hexdigest()
        self.assertIn(f"body-sha256={expected}", P.footer("spec", body, "LANE3"))


class HeadingCrossCheck(unittest.TestCase):
    def test_each_kind_requires_its_heading(self) -> None:
        for kind in ("spec", "gate-result"):
            with self.subTest(kind=kind):
                with self.assertRaises(SystemExit) as ctx:
                    P.validate_kind(kind, "no heading here at all\n")
                self.assertIn("cross-check", str(ctx.exception))

    def test_a_conforming_spec_and_gate_result_pass(self) -> None:
        P.validate_kind("spec", SPEC)
        P.validate_kind("gate-result", GATE_PASS)

    def test_a_blocked_gate_result_is_accepted(self) -> None:
        """BLOCKED names neither PASS nor FAIL and is a legitimate third
        outcome. Refusing it would push Lane 3 back to the untyped
        `discussion` path — the exact hole this issue closes."""
        P.validate_kind("gate-result", GATE_BLOCKED)

    def test_the_wrong_heading_for_the_kind_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            P.validate_kind("spec", GATE_PASS)


class LeadBlockRequired(unittest.TestCase):
    """a private-repo incident AC2: same lead-block/cap requirement as l1_post.py's own
    kinds, reached through `main()` — not `validate_kind`, which stays
    scoped to the heading/quote checks above. `discussion` carries no
    LEAD_FIELDS entry, so this is additive to `spec`/`gate-result` only."""

    def _refused(self, kind: str, body: str) -> str:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(body)
            argv = ["post_lane_discussion.py", "--issue", "1",
                    "--file", str(path), "--kind", kind] + _case_flags(kind, path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(P, "comment_body",
                                   side_effect=AssertionError("must not post")):
                with self.assertRaises(SystemExit) as ctx:
                    P.main()
        return str(ctx.exception)

    def test_a_spec_missing_cases_is_refused_before_posting(self) -> None:
        stripped = "\n".join(
            line for line in SPEC.splitlines() if not line.startswith("**Cases:**"))
        self.assertIn("`**Cases:** ...`", self._refused("spec", stripped))

    def test_a_gate_result_missing_verdict_is_refused_before_posting(self) -> None:
        stripped = "\n".join(
            line for line in GATE_PASS.splitlines() if not line.startswith("**Verdict:**"))
        self.assertIn("`**Verdict:** ...`", self._refused("gate-result", stripped))

    def test_discussion_is_unaffected_by_the_lead_requirement(self) -> None:
        """The pre-#1703 default path stays exactly as free-form as before —
        `discussion` has no LEAD_FIELDS entry, so `validate_lead` no-ops."""
        L.validate_lead("discussion", "just talking, no structure at all\n")

    def test_an_overlong_lead_is_refused_naming_size_and_cap(self) -> None:
        padded = SPEC.replace(
            "**Next:** submit for HITL approval.",
            "**Next:** " + "x" * L.LEAD_CAP_BYTES,
        )
        message = self._refused("spec", padded)
        self.assertIn(f"{L.LEAD_CAP_BYTES}-byte cap", message)
        self.assertIn("a private-repo incident", message)


class QuotedMarkerGuard(unittest.TestCase):
    """Measured against the merged a private-repo incident, not assumed."""

    def test_a_fenced_quote_is_allowed(self) -> None:
        """a private-repo incident strips fences before every marker scan, so a fenced
        quote is already inert — no reason to refuse it."""
        P.validate_kind("gate-result", GATE_PASS.replace(
            "All nine cases ran.",
            "Prior round:\n\n```\n## Lane 3 Gate Results — H1573 — FAIL\n```"))

    def test_an_unfenced_quote_inside_details_is_refused(self) -> None:
        """`<details>` is not a fence. harmonic-forge#472 requires evidence
        verbatim inside a collapsed block, which is precisely the shape that
        smuggles a marker past the reader and into the parser."""
        body = GATE_PASS.replace("All nine cases ran.",
                                 "Prior round:\n\n## L2B — receipt-backed status")
        with self.assertRaises(SystemExit) as ctx:
            P.validate_kind("gate-result", body)
        self.assertIn("harmonic-forge#473", str(ctx.exception))
        self.assertIn("L2B", str(ctx.exception))

    def test_the_refusal_names_the_fix(self) -> None:
        body = SPEC + "\n## AE — H1573\n"
        with self.assertRaises(SystemExit) as ctx:
            P.validate_kind("spec", body)
        self.assertIn("fence", str(ctx.exception))

    def test_the_artifacts_own_heading_is_not_treated_as_a_quote(self) -> None:
        P.validate_kind("gate-result", GATE_PASS)
        P.validate_kind("spec", SPEC)

    def test_a_quoted_footer_is_already_refused_upstream(self) -> None:
        """`reject_reserved_marker` refuses any body containing the reserved
        namespace, fenced or not — so this guard only has headings to do."""
        from l1_post import reject_reserved_marker
        with self.assertRaises(SystemExit):
            reject_reserved_marker(GATE_PASS + "\n<!-- l1-post v1; kind=ae; -->\n")


class EndToEnd(unittest.TestCase):
    """`check_gate_result` is stubbed here on purpose.

    These assert the POSTING path — footer, heading, transport. Left unstubbed,
    they reached the live GitHub API the moment harmonic-forge#504's forge half
    landed (hrse CI symlinks the forge default branch to $HOME/harmonic-forge),
    and hrse `main` went red on a change that never touched it. A unit test
    that becomes network-dependent when a SIBLING REPO merges is the same
    cross-repo shape #504 exists to end.
    """

    def _post(self, kind, body):
        captured = {}

        def fake_comment_body(repo, issue, text):
            captured["text"] = text
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(body)
            argv = ["post_lane_discussion.py", "--issue", "1",
                    "--file", str(path), "--kind", kind] + _case_flags(kind, path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict("os.environ", {"LANE": "3"}), \
                 mock.patch.object(P, "check_gate_result",
                                   lambda repo, body, ack_no_pr_required=None: (True, "[GATE] stubbed")), \
                 mock.patch.object(P, "comment_body", fake_comment_body):
                P.main()
        return captured["text"]

    def test_a_gate_result_posts_with_its_kind_footer(self) -> None:
        text = self._post("gate-result", GATE_PASS)
        self.assertIn("<!-- l1-post v1; kind=gate-result; posted-by=LANE3;", text)
        self.assertTrue(text.startswith("## Lane 3 Gate Results"))

    def test_a_refused_body_never_reaches_the_transport(self) -> None:
        posted = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(GATE_PASS.replace("All nine cases ran.", "## L3S — spec"))
            argv = ["post_lane_discussion.py", "--issue", "1",
                    "--file", str(path), "--kind", "gate-result"] + _case_flags("gate-result", path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(P, "comment_body",
                                   side_effect=lambda *a: posted.append(a) or ("u", 1)):
                with self.assertRaises(SystemExit):
                    P.main()
        self.assertEqual(posted, [])


class GateResultRequiresGreenCiTests(unittest.TestCase):
    """harmonic-forge#504: a PASS may not outrun the PR's own CI.

    The refusal lives at the one place a gate result becomes real. AC1 says a
    prose instruction in `testing-gate.md` does not satisfy the issue, because
    prose compliance degrades under context pressure — and a long gate run is
    exactly that pressure.
    """

    PASS_BODY = ("## Lane 3 Gate Results — H395\n\n"
                 "**Verdict:** PASS\n**Head-SHA:** 0359854\n")

    def test_a_pass_with_red_ci_is_refused(self):
        with mock.patch.object(P, "check_gate_result",
                               return_value=(False, "[GATE] REFUSED: CI is RED")):
            with self.assertRaises(SystemExit):
                P.require_green_ci("gate-result", "o/r", self.PASS_BODY)

    def test_a_pass_with_green_ci_goes_through(self):
        with mock.patch.object(P, "check_gate_result",
                               return_value=(True, "[GATE] CI green")):
            P.require_green_ci("gate-result", "o/r", self.PASS_BODY)

    def test_a_gate_report_stamped_as_discussion_is_still_gated(self):
        """These two tests previously asserted the OPPOSITE — that a
        `discussion` is never gated — which locked in the bypass with a
        passing test. `kind` is the author declaring what they are posting;
        omitting `--kind gate-result` skipped the check while `lane_state.py`
        still scored the comment `gate.pass` from its heading. 74 of 98 real
        gate comments carry no `kind=gate-result` footer."""
        seen = []
        with mock.patch.object(P, "check_gate_result",
                               lambda repo, body, ack_no_pr_required=None: (
                                   seen.append(kind_marker := repo),
                                   (True, "ok"))[1]):
            P.require_green_ci("discussion", "o/r", self.PASS_BODY)
        self.assertEqual(seen, ["o/r"])

    def test_recognition_is_delegated_not_duplicated(self):
        """`check_gate_result` decides what a gate report IS, by its heading,
        exactly as `lane_state.py` does. A second opinion here is a second
        thing to drift; a non-report returns cleanly from the checker."""
        seen = []
        with mock.patch.object(P, "check_gate_result",
                               lambda repo, body, ack_no_pr_required=None: (
                                   seen.append(body), (True, "ok"))[1]):
            P.require_green_ci("spec", "o/r", "## Lane 2 Plan — H1\n")
        self.assertEqual(len(seen), 1)

    def test_an_unavailable_checker_fails_closed(self):
        """The checker is colocated with this canonical platform script; if
        it cannot run, a PASS must not be published without verification."""
        with mock.patch.object(P, "check_gate_result", None):
            with self.assertRaises(TypeError):
                P.require_green_ci("gate-result", "o/r", self.PASS_BODY)


class GateCheckIsActuallyWiredTests(unittest.TestCase):
    """harmonic-forge#504 preclose: deleting the ONLY call site left the whole
    suite green.

    Every other test drove `require_green_ci` directly, so nothing asserted
    that `main()` calls it at all — the guard could be removed and no test
    would notice. These drive `main()`.
    """

    def _post(self, body, kind="gate-result"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text(body, encoding="utf-8")
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "1",
                    "--file", str(path), "--kind", kind] + _case_flags(kind, path)
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(P, "comment_body",
                                      return_value=("https://example/1", "")):
                P.main()

    def test_main_refuses_a_pass_the_checker_rejects(self):
        with mock.patch.object(P, "check_gate_result",
                               return_value=(False, "[GATE] REFUSED: CI is RED")):
            with self.assertRaises(SystemExit):
                self._post(GATE_PASS)

    def test_main_posts_a_pass_the_checker_accepts(self):
        with mock.patch.object(P, "check_gate_result",
                               return_value=(True, "[GATE] CI green")):
            self._post(GATE_PASS)

    def test_main_consults_the_checker_for_a_gate_report_posted_as_discussion(self):
        """74 of 98 real gate comments carry no `kind=gate-result` footer. The
        check must key on the BODY, not on the flag the author chose."""
        seen = []

        def spy(repo, body, ack_no_pr_required=None):
            seen.append(repo)
            return True, "ok"
        with mock.patch.object(P, "check_gate_result", spy):
            self._post(GATE_PASS, kind="discussion")
        self.assertEqual(seen, ["vitalharmony/harmonic-forge"], "a gate report posted as `discussion` "
                                        "skipped the CI check entirely")

    def test_main_resolves_an_omitted_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text("ordinary discussion", encoding="utf-8")
            argv = ["post_lane_discussion.py", "--issue", "1", "--file", str(path)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(P, "resolve_repo", return_value="vitalharmony/harmonic-forge") as resolve, \
                 mock.patch.object(P, "comment_body", return_value=("https://example/1", "")):
                P.main()
        resolve.assert_called_once_with(None)


class CallerRelativeFileResolutionTests(unittest.TestCase):
    def test_compatibility_shim_root_wins_over_platform_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            caller = Path(tmp)
            relative = Path("f706-fixtures") / "comment.md"
            expected = caller / relative
            expected.parent.mkdir()
            expected.write_text("from consumer\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"LANE_TRANSPORT_CALLER_ROOT": str(caller)}):
                self.assertEqual(P.resolve_body_path(relative), expected)

    def test_git_toplevel_is_used_when_no_shim_root_is_declared(self):
        relative = Path("f706-missing") / "comment.md"
        completed = __import__("subprocess").CompletedProcess(
            ("git", "rev-parse"), 0, stdout=str(P._FORGE_ROOT) + "\n", stderr=""
        )
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(P, "run", return_value=completed):
            with self.assertRaises(SystemExit) as caught:
                P.resolve_body_path(relative)
        self.assertIn(str(P._FORGE_ROOT / relative), str(caught.exception))


class NoPrRequiredOverrideTests(unittest.TestCase):
    """harmonic-forge#788: the hrse#2095 live incident, replayed at the
    posting layer. `require_green_ci` now returns whether the override was
    the reason a PASS went through, and `main()`/`footer()` must record it
    only in that case -- never when the flag was passed but unused (CI was
    genuinely green, or a real PR made the override inapplicable)."""

    PASS_BODY = ("## Lane 3 Gate Results — H2095 — PASS\n\n"
                 "**Verdict:** PASS\n**Head-SHA:** af35ca95\n"
                 "**Finding:** none.\n**Next:** merge.\n"
                 "\n| TC | Verdict |\n|---|---|\n| TC1 | PASS |\n")  # harmonic-forge#893

    def test_ack_no_pr_required_rejects_an_empty_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(self.PASS_BODY)
            argv = ["post_lane_discussion.py", "--issue", "1", "--file", str(path),
                    "--kind", "gate-result", "--ack-no-pr-required", "   "]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    P.main()

    def test_ack_no_pr_required_rejects_a_reason_that_escapes_the_footer(self):
        """Preclose finding: `-->` in the reason ended the HTML footer early
        and leaked the rest into the rendered comment."""
        for reason in ("backfill --> see thread", "a <!-- b", "two\nlines"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "body.md"
                path.write_text(self.PASS_BODY)
                argv = ["post_lane_discussion.py", "--issue", "1", "--file", str(path),
                        "--kind", "gate-result", "--ack-no-pr-required", reason]
                posted = []
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(P, "comment_body",
                                       side_effect=lambda *a: posted.append(a) or ("u", 1)):
                    with self.assertRaises(SystemExit):
                        P.main()
                self.assertEqual(posted, [])

    def test_require_green_ci_returns_true_when_the_override_fired(self):
        with mock.patch.object(P, "check_gate_result",
                               return_value=(True, "[GATE] no-pr-override: af35ca95 is the tip")):
            self.assertTrue(
                P.require_green_ci("gate-result", "o/r", self.PASS_BODY, "no PR for a migration"))

    def test_require_green_ci_returns_false_when_ci_was_genuinely_green(self):
        """The override flag can be passed defensively on every gate-result
        post; it must only be RECORDED when it was actually load-bearing."""
        with mock.patch.object(P, "check_gate_result",
                               return_value=(True, "[GATE] CI green for af35ca95")):
            self.assertFalse(
                P.require_green_ci("gate-result", "o/r", self.PASS_BODY, "should not matter"))

    def test_footer_records_the_reason_only_when_used(self):
        with_override = P.footer("gate-result", self.PASS_BODY, "LANE1",
                                  ack_no_pr_required="hrse#2095 production backfill, no PR")
        self.assertIn("ack-no-pr-required=hrse#2095 production backfill, no PR", with_override)
        without_override = P.footer("gate-result", self.PASS_BODY, "LANE1")
        self.assertNotIn("ack-no-pr-required", without_override)

    def test_end_to_end_records_the_override_in_the_posted_footer(self):
        """Replays hrse#2095 exactly: a main-tip SHA with no PR, `--kind
        gate-result`, `--ack-no-pr-required` given."""
        captured = {}

        def fake_comment_body(repo, issue, text):
            captured["text"] = text
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(self.PASS_BODY)
            argv = ["post_lane_discussion.py", "--issue", "2095", "--file", str(path),
                    "--kind", "gate-result", "--ack-no-pr-required",
                    "data-migration on main, no PR"] + _case_flags("gate-result", path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict("os.environ", {"LANE": "1"}), \
                 mock.patch.object(P, "check_gate_result",
                                   return_value=(True, "[GATE] no-pr-override: af35ca95 is the tip")), \
                 mock.patch.object(P, "comment_body", fake_comment_body):
                P.main()
        self.assertIn("ack-no-pr-required=data-migration on main, no PR", captured["text"])

    def test_end_to_end_omits_the_footer_when_a_real_pr_makes_the_override_moot(self):
        """AC1's absolute, at the posting layer: passing the flag when a PR
        genuinely exists (and check_gate_result therefore refuses/passes on
        its own merits, never via the override) must not stamp a footer
        claiming the override did the work."""
        captured = {}

        def fake_comment_body(repo, issue, text):
            captured["text"] = text
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-1", 1

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(self.PASS_BODY)
            argv = ["post_lane_discussion.py", "--issue", "1", "--file", str(path),
                    "--kind", "gate-result", "--ack-no-pr-required", "irrelevant"] + _case_flags("gate-result", path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(P, "check_gate_result",
                                   return_value=(True, "[GATE] CI green for af35ca95 (verify:success)")), \
                 mock.patch.object(P, "comment_body", fake_comment_body):
                P.main()
        self.assertNotIn("ack-no-pr-required", captured["text"])




# harmonic-forge#791: `main()` now also fetches the issue's comments for a
# genuine gate-report PASS (`require_round_approval`, scoped by
# `looks_like_a_gate_report` -- see RoundApprovalTests.test_a_non_gate_
# report_pass_is_never_gated below). Every posting-path test above stubs
# `check_gate_result` so it cannot reach GitHub; this does the same for the
# new check, module-wide, so none of them perform live GitHub I/O (a
# preclose refuter reproduced exactly that when this stub previously lived
# below an `if __name__ == "__main__": unittest.main()` guard and so never
# ran when the file was executed directly -- `unittest.main()` calls
# `sys.exit()`, so nothing after it in the file executes on that path.
# `setUpModule`/`tearDownModule` below MUST be the only such guard-adjacent
# code, and MUST come before the file's real, single `__main__` block at the
# true end of the file).
_REAL_REQUIRE_ROUND_APPROVAL = P.require_round_approval
_round_patcher = mock.patch.object(P, "require_round_approval")


def setUpModule():
    _round_patcher.start()


def tearDownModule():
    _round_patcher.stop()


class RoundApprovalTests(unittest.TestCase):
    """harmonic-forge#791: a PASS gate-result needs this round's approval,
    via `check_lane3_ready.resolve_gate_authority` -- the SAME function
    `lane3-begin` uses, not a second definition."""

    PASS = ("## Lane 3 Gate Results — H2101 — PASS\n\n**Verdict:** PASS\n"
            "**Head-SHA:** 21e587db\n**Finding:** none.\n**Next:** merge.\n")

    @staticmethod
    def _c(cid, kind, sha="21e587db", tier="W"):
        prefix = f"Write tier {tier} throughout.\n\n" if kind == "sweep" else ""
        return {"id": cid, "html_url": f"https://x/issues/1#issuecomment-{cid}",
                "body": f"{prefix}b\n\n<!-- l1-post v1; kind={kind}; sha={sha} -->"}

    def _check(self, comments, body=None):
        # Patch the module object `post_lane_discussion` actually holds, not
        # `import check_lane3_ready`: test_check_lane3_ready.py replaces the
        # sys.modules entry, so in a combined run the two differ and the stub
        # would miss, sending a real fetch to GitHub.
        with mock.patch.object(P.check_lane3_ready, "fetch_comments", return_value=comments):
            _REAL_REQUIRE_ROUND_APPROVAL("vitalharmony/hrse", 2101, body or self.PASS)

    def test_a_pass_after_an_unapproved_round_is_refused(self):
        """hrse#2101's 5857564292: round-1 AE, round-2 handoff and spec sit
        inside the carry window, no new AE."""
        comments = [self._c(1, "handoff"), self._c(2, "spec"), self._c(3, "ae", "829f8e54"),
                    self._c(4, "sweep", "829f8e54")]
        with self.assertRaises(SystemExit) as caught:
            self._check(comments)
        self.assertIn("21e587db", str(caught.exception))

    def test_a_pass_with_this_rounds_ae_posts(self):
        comments = [self._c(1, "handoff"), self._c(2, "ae"), self._c(3, "sweep")]
        self._check(comments)

    def test_a_pass_under_a_tampered_tier_r_sweep_is_refused(self):
        """Preclose finding, independently reported by 4 of 5 lenses: the
        tier-R branch must apply the same body-sha256 check the gate-start
        path always has, reached through the ONE shared function."""
        digest = __import__("hashlib").sha256(b"Write tier W throughout.\n\nb").hexdigest()
        tampered = {"id": 2, "html_url": "https://x/issues/1#issuecomment-2",
                    "body": f"Write tier R throughout.\n\nb\n\n<!-- l1-post v1; kind=sweep; "
                            f"sha=21e587db; body-sha256={digest} -->"}
        comments = [self._c(1, "spec"), tampered]
        with self.assertRaises(SystemExit):
            self._check(comments)

    def test_a_pass_under_a_tampered_tier_w_sweep_is_refused(self):
        """harmonic-forge#861: the body-sha256 check applies at every tier, in
        the one shared function, so an edited above-tier-R sweep refuses too."""
        digest = __import__("hashlib").sha256(b"Write tier W throughout.\n\nother cases").hexdigest()
        tampered = {"id": 3, "html_url": "https://x/issues/1#issuecomment-3",
                    "body": f"Write tier W throughout.\n\nb\n\n<!-- l1-post v1; kind=sweep; "
                            f"sha=21e587db; body-sha256={digest} -->"}
        comments = [self._c(1, "handoff"), self._c(2, "ae"), tampered]
        with self.assertRaises(SystemExit):
            self._check(comments)

    def test_a_pass_under_a_genuine_tier_r_sweep_posts(self):
        comments = [self._c(1, "spec"), self._c(2, "sweep", tier="R")]
        self._check(comments)

    def test_fail_and_blocked_are_never_gated(self):
        unapproved = [self._c(1, "handoff"), self._c(2, "spec")]
        for verdict in ("FAIL", "BLOCKED"):
            with self.subTest(verdict=verdict):
                self._check(unapproved, self.PASS.replace("PASS", verdict))

    def test_a_non_gate_report_pass_is_never_gated(self):
        """Preclose finding: `verdict_of` reads a PASS-shaped verdict out of
        ANY body whose lead block starts a line with 'Result:'/'Verdict:',
        including a `ready-for-l3`. Only a genuine `## Lane 3 Gate Results`
        body may be checked -- everything else must post regardless of
        thread state. `verdict_of` confirmed PASS on this exact body first,
        so the test cannot be vacuous the way the first draft's was."""
        no_protocol_at_all: list[dict] = []
        ready_for_l3_body = ("## Ready for Lane 3 — H2101\n\n**Target:** x\n"
                             "**Result:** PASS on every one.\n"
                             "**Next:** Lane 3 gates it.\n")
        import gate_ci
        self.assertEqual(gate_ci.verdict_of(ready_for_l3_body), "PASS")
        self._check(no_protocol_at_all, ready_for_l3_body)  # must not raise

    def test_main_calls_the_round_check(self):
        """Wiring: deleting the call from `main()` must fail a test, the lesson
        `GateCheckIsActuallyWiredTests` records for the CI check."""
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text(GATE_PASS, encoding="utf-8")
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/hrse", "--issue", "2101",
                    "--file", str(path), "--kind", "gate-result"] + _case_flags("gate-result", path)
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(P, "check_gate_result", return_value=(True, "[GATE] CI green")), \
                 mock.patch.object(P, "require_round_approval",
                                   side_effect=lambda repo, issue, body: seen.append((repo, issue))), \
                 mock.patch.object(P, "comment_body", return_value=("https://example/1", "")):
                P.main()
        self.assertEqual(seen, [("vitalharmony/hrse", 2101)])


if __name__ == "__main__":
    unittest.main()
