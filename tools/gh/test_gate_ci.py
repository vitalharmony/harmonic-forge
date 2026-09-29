#!/usr/bin/env python3
"""Tests for gate_ci.py (harmonic-forge#504).

AC1 says a prose instruction does not satisfy this issue, so the check is
mechanical — and a mechanical check is only worth what its tests are. The one
that matters most is `test_replaying_the_live_incident_does_not_pass`: the
issue's AC5 names a real SHA whose CI is still red on GitHub today, and that
test drives the real code path against a recorded copy of that real payload.
"""
from __future__ import annotations

import json
import re
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_ci  # noqa: E402


def fake_gh(checks=(), pulls=(), required=None, fail=(), main_tip=None, bare_runs=()):
    """A `run` stand-in that ROUTES BY ENDPOINT.

    The first version returned one payload for every command, which worked only
    while there was one call. Once the check consulted branch protection and
    the commit's PRs as well, every test errored — the stub was answering
    "here are some check runs" to "what are the required checks?".

    `main_tip` (harmonic-forge#788): the SHA `/branches/main` reports as
    `.commit.sha`, for `sha_is_current_main_tip`'s own call. `None` means the
    endpoint fails, matching every other unset fixture's fail-closed default.
    """
    def run(cmd):
        url = next((a for a in cmd if a.startswith("repos/")), "")
        for marker in fail:
            if marker in url:
                return 1, "gh: simulated failure"
        if url.endswith("/branches/main"):
            # Exact suffix, not a substring: `/branches/main/protection` (the
            # required-checks lookup, checked next) contains "/branches/main"
            # too, and a bare `in` check swallowed it -- a real regression
            # this test class introduced and its own suite caught.
            if main_tip is None:
                return 1, "gh: simulated failure"
            # `--jq ".commit.sha"` returns the bare string, not JSON-quoted.
            return 0, main_tip
        if "/protection" in url:
            return 0, json.dumps(sorted(required)) if required else (0, "null")[1]
        if "/pulls" in url:
            listed = list(pulls)
            # `stale_against_pr` asks gh to filter to open PRs with `--jq`;
            # the real CLI drops closed ones before returning. Honour that, or
            # a closed merge PR (harmonic-forge#788's main-tip case) reads as
            # a live one. Entries without `state` are pre-filtered fixtures
            # from older tests and pass through unchanged.
            if any('select(.state == "open")' in a for a in cmd):
                listed = [p for p in listed if p.get("state", "open") == "open"]
            return 0, json.dumps(listed)
        if re.fullmatch(r"repos/[^/]+/[^/]+/commits/[0-9a-fA-F]+", url):
            # Short-SHA resolution (`--jq .sha`): the fixture's SHAs are hex, padded to 40.
            return 0, url.rsplit("/", 1)[1].ljust(40, "0")
        if "/actions/runs?" in url:
            # `--jq '.workflow_runs[] | {id, name, ...}'` streams one object per line. The
            # fixture models every check as a job of ONE workflow run (harmonic-forge#805);
            # `bare_runs` are workflow runs that have no jobs at all.
            lines = ([json.dumps({"id": 1, "name": "ci", "status": "completed",
                                  "conclusion": "success"})] if checks else [])
            lines += [json.dumps(dict(r, id=100 + i)) for i, r in enumerate(bare_runs)]
            return 0, "\n".join(lines)
        if "/actions/runs/" in url and "/jobs" in url:
            # `--jq '.jobs[]'` streams ONE OBJECT PER LINE, not an array.
            # Only run 1 carries the fixture's jobs; a bare run (id 100+) has none.
            return 0, "\n".join(json.dumps(c) for c in checks) if "/runs/1/" in url else ""
        return 0, "null"
    return run


def check(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "completed_at": "2026-09-09T00:00:00Z"}


def fake_runs(*runs):
    """Back-compat shim: only check-runs matter, nothing required, no PRs."""
    return fake_gh(checks=runs)


def failing_run(code=1, out="gh: not found"):
    def run(cmd):
        return code, out
    return run


PASS_BODY = """## Lane 3 Gate Results — harmonic-forge#494

**Verdict:** PASS
**Head-SHA:** 0359854f1234567890abcdef1234567890abcdef

All nine cases executed.
"""


class VerdictParsingTests(unittest.TestCase):
    def test_a_verdict_line_is_read(self):
        self.assertEqual(gate_ci.verdict_of(PASS_BODY), "PASS")

    def test_fail_and_blocked_are_read(self):
        for word in ("FAIL", "BLOCKED"):
            with self.subTest(word=word):
                self.assertEqual(
                    gate_ci.verdict_of(f"**Verdict:** {word}\n"), word)

    def test_a_verdict_in_the_heading_is_read(self):
        """`post_lane_discussion` deliberately does not require PASS/FAIL in the
        heading, so both shapes exist in the corpus."""
        self.assertEqual(
            gate_ci.verdict_of("### Lane 3 Gate Results — H395 — PASS\n"), "PASS")

    def test_no_verdict_reads_as_none(self):
        self.assertIsNone(gate_ci.verdict_of("## Lane 3 Gate Results\n\nnotes\n"))

    def test_the_sha_is_read(self):
        self.assertTrue(gate_ci.gated_sha(PASS_BODY).startswith("0359854f"))

    def test_an_abbreviated_sha_is_read(self):
        self.assertEqual(gate_ci.gated_sha("Head-SHA: `0359854`\n"), "0359854")

    def test_a_body_with_no_sha_reads_as_none(self):
        self.assertIsNone(gate_ci.gated_sha("**Verdict:** PASS\n"))


class OnlyPassIsGatedTests(unittest.TestCase):
    """A verification layer that can silence a failure report is worse than
    none. Only PASS authorises a merge, so only PASS has to clear the bar."""

    def test_fail_posts_without_touching_ci(self):
        def explode(cmd):
            raise AssertionError("CI must not be consulted for a FAIL")
        ok, _ = gate_ci.check_gate_result("o/r", "**Verdict:** FAIL\n", run=explode)
        self.assertTrue(ok)

    def test_blocked_posts_without_touching_ci(self):
        def explode(cmd):
            raise AssertionError("CI must not be consulted for a BLOCKED")
        ok, _ = gate_ci.check_gate_result("o/r", "**Verdict:** BLOCKED\n", run=explode)
        self.assertTrue(ok)

    def test_a_body_that_is_not_a_gate_report_is_not_gated(self):
        ok, _ = gate_ci.check_gate_result("o/r", "no verdict here\n",
                                          run=failing_run())
        self.assertTrue(ok)


class PassRequiresGreenCiTests(unittest.TestCase):
    def test_green_ci_lets_a_pass_through(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs(check("verify")))
        self.assertTrue(ok, msg)

    def test_red_ci_refuses_a_pass(self):
        """AC1, and the incident itself."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs(check("verify", "failure")))
        self.assertFalse(ok)
        self.assertIn("RED", msg)
        self.assertIn("verify:failure", msg)

    def test_a_pending_check_refuses_a_pass(self):
        """AC3: waiting is the correct behaviour, not a judgment call."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs(check("verify", None, "in_progress")))
        self.assertFalse(ok)
        self.assertIn("BLOCKED", msg)

    def test_no_checks_at_all_refuses_a_pass(self):
        """'No checks yet' is not 'nothing failing'.

        I made exactly this mistake in a wait-loop while implementing this
        issue: it tested "every check completed", an empty list satisfied that
        vacuously, and it reported done on a PR whose CI had not registered.
        """
        ok, msg = gate_ci.check_gate_result("o/r", PASS_BODY, run=fake_runs())
        self.assertFalse(ok)
        self.assertIn("not completed", msg)

    def test_an_unreadable_api_refuses_a_pass(self):
        """Fail closed. An unreachable API is not evidence of health."""
        ok, msg = gate_ci.check_gate_result("o/r", PASS_BODY, run=failing_run())
        self.assertFalse(ok)

    def test_a_pass_with_no_sha_is_refused(self):
        """AC2. Without the SHA there is nothing to check, and a reader cannot
        tell what was gated."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", "**Verdict:** PASS\n", run=failing_run())
        self.assertFalse(ok)
        self.assertIn("head SHA", msg)

    def test_nonrequired_skipped_and_neutral_do_not_poison_required_green(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(checks=[check("verify"), check("build-and-push", "skipped"),
                                check("lint", "neutral")], required={"verify"}))
        self.assertTrue(ok, msg)

    def test_required_skipped_check_refuses_a_pass(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(checks=[check("verify", "skipped")], required={"verify"}))
        self.assertFalse(ok)
        self.assertIn("verify:skipped", msg)

    def test_one_red_among_many_greens_still_refuses(self):
        ok, _ = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs(check("a"), check("b", "failure"), check("c")))
        self.assertFalse(ok)

    def test_an_unrecognised_conclusion_refuses(self):
        """Fail closed on a vocabulary this module does not know, rather than
        guessing it is benign."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs(check("verify", "something_new")))
        self.assertFalse(ok)


class NoPrOverrideTests(unittest.TestCase):
    """harmonic-forge#788: hrse#2095's live incident replayed. `verify` is
    gated to `pull_request` events (HRSE2 `.github/workflows/ci.yml:82`), so
    a `main`-branch data-migration action's own reference SHA never gets a
    run at all -- reproducing exactly `test_no_checks_at_all_refuses_a_pass`
    above, but for a shape that can never resolve by waiting."""

    SHA = PASS_BODY.split("Head-SHA:** ")[1].split("\n")[0].strip()

    def test_no_override_behaves_exactly_as_before(self):
        """Passing no override at all must not change any existing
        behaviour -- this is the same call `test_no_checks_at_all_refuses_a_pass`
        makes, with the new keyword simply omitted."""
        ok, msg = gate_ci.check_gate_result("o/r", PASS_BODY, run=fake_runs())
        self.assertFalse(ok)
        self.assertIn("not completed", msg)

    def test_override_lets_a_main_tip_sha_through(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY, run=fake_gh(main_tip=self.SHA),
            ack_no_pr_required="hrse#2095 production backfill, no PR")
        self.assertTrue(ok, msg)
        self.assertIn("no-pr-override", msg)
        self.assertIn("hrse#2095 production backfill, no PR", msg)

    def test_override_does_not_fire_when_main_has_moved_past_the_sha(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY, run=fake_gh(main_tip="a" * 40),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertIn("not completed", msg)
        self.assertNotIn("no-pr-override", msg)

    def test_override_does_not_fire_when_an_open_pr_contains_the_sha(self):
        """AC3, pinned for real (preclose finding: the first version of this
        test built no PR at all and passed on the main-tip mismatch alone).
        The SHA IS main's tip here, so only the open PR can refuse it."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(main_tip=self.SHA,
                        pulls=[{"number": 7, "state": "open", "head": self.SHA}]),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertNotIn("no-pr-override", msg)

    def test_a_closed_merge_pr_does_not_block_the_override(self):
        """A main-tip commit always has its own merge PR, closed. That is the
        hrse#2095 shape (PR #2106) and must not refuse."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(main_tip=self.SHA,
                        pulls=[{"number": 2106, "state": "closed", "head": "b" * 40}]),
            ack_no_pr_required="migration on main")
        self.assertTrue(ok, msg)

    def test_override_does_not_fire_while_ci_is_pending(self):
        """Preclose finding: a run in flight is not 'CI cannot run'."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(main_tip=self.SHA,
                        checks=[check("verify", None, "in_progress")]),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertNotIn("no-pr-override", msg)

    def test_override_does_not_fire_when_check_runs_are_unreadable(self):
        """Preclose finding: a failed check-runs read is `unknown`, and an
        unread CI must never be published as one that cannot exist."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(main_tip=self.SHA, fail=("/actions/runs",)),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertNotIn("no-pr-override", msg)

    def test_override_fires_when_the_required_check_never_ran(self):
        """The literal hrse#2095 state: other checks ran on main's tip, the
        required `verify` never did."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_gh(main_tip=self.SHA, checks=[check("render-and-deploy")],
                        required={"verify"}),
            ack_no_pr_required="migration on main")
        self.assertTrue(ok, msg)
        self.assertIn("no-pr-override", msg)

    def test_override_does_not_fire_on_red_ci_even_with_a_pr_less_sha(self):
        """The override is consulted ONLY at the final no-checks-at-all
        branch -- a SHA that DOES have CI, and it's red, is refused on that
        basis regardless of the flag."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY, run=fake_gh(checks=[check("verify", "failure")],
                                          main_tip=self.SHA),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertIn("RED", msg)

    def test_override_does_not_fire_when_the_lookup_itself_fails(self):
        """A failed lookup must not be silently treated as confirmed-no-PR."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY, run=fake_gh(main_tip=None),
            ack_no_pr_required="should not matter")
        self.assertFalse(ok)
        self.assertNotIn("no-pr-override", msg)

    def test_sha_is_current_main_tip_directly(self):
        self.assertTrue(gate_ci.sha_is_current_main_tip("o/r", self.SHA, run=fake_gh(main_tip=self.SHA)))
        self.assertFalse(gate_ci.sha_is_current_main_tip("o/r", self.SHA, run=fake_gh(main_tip="a" * 40)))
        self.assertIsNone(gate_ci.sha_is_current_main_tip("o/r", self.SHA, run=fake_gh(main_tip=None)))


class Ac5LiveIncidentReplayTests(unittest.TestCase):
    """AC5: replaying the gate on harmonic-forge#494's merge SHA must not PASS.

    `0359854` is the real merge commit of PR #501 (which closed #494). Its
    `verify` check is still `failure` on GitHub today — confirmed live while
    writing this. The payload below is that real response, recorded so the test
    is deterministic and offline; the SHA and conclusion are not invented.
    """

    RECORDED = check("verify", "failure")

    def test_the_recorded_payload_still_matches_github(self):
        """The offline replay below is only as honest as this.

        The recorded conclusion is a hand-written dict, so the replay would
        keep passing if GitHub's record changed — it proves the CODE behaves,
        not that the incident is still what the issue says. Set
        `GATE_CI_LIVE=1` to check the record itself. Verified by hand at
        merge time: `0359854` -> `verify: failure`.
        """
        if not os.environ.get("GATE_CI_LIVE"):
            self.skipTest("set GATE_CI_LIVE=1 to verify the record against GitHub")
        state, detail = gate_ci.ci_conclusion(
            "vitalharmony/harmonic-forge", "0359854")
        self.assertEqual(state, "red", detail)

    def test_replaying_the_live_incident_does_not_pass(self):
        body = ("## Lane 3 Gate Results — harmonic-forge#494\n\n"
                "**Verdict:** PASS\n**Head-SHA:** 0359854\n")
        ok, msg = gate_ci.check_gate_result(
            "vitalharmony/harmonic-forge", body, run=fake_runs(self.RECORDED))
        self.assertFalse(ok, "the incident's own PASS must not be postable")
        self.assertIn("0359854", msg)
        self.assertIn("verify:failure", msg)

    def test_the_same_report_as_fail_is_postable(self):
        """The corrective verdict must never be blocked."""
        body = ("## Lane 3 Gate Results — harmonic-forge#494\n\n"
                "**Verdict:** FAIL\n**Head-SHA:** 0359854\n")
        ok, _ = gate_ci.check_gate_result(
            "vitalharmony/harmonic-forge", body, run=fake_runs(self.RECORDED))
        self.assertTrue(ok)


class CiConclusionStatesTests(unittest.TestCase):
    def test_absent_is_its_own_state_not_green(self):
        state, _ = gate_ci.ci_conclusion("o/r", "abc1234", run=fake_runs())
        self.assertEqual(state, "absent")

    def test_unparseable_payload_is_unknown(self):
        state, _ = gate_ci.ci_conclusion("o/r", "abc1234",
                                         run=lambda cmd: (0, "{not json"))
        self.assertEqual(state, "unknown")

    def test_pending_wins_over_a_red_sibling(self):
        """Report the weaker fact. 'Still running' tells the operator to wait;
        'red' tells them to fix — and while anything is pending, which one is
        true is not yet known."""
        state, _ = gate_ci.ci_conclusion(
            "o/r", "abc1234",
            run=fake_runs(check("a", "failure"), check("b", None, "queued")))
        self.assertEqual(state, "pending")


class CliTests(unittest.TestCase):
    """`return 0 if ok else 1` — mutating it to `return 0` left the old test
    green, because it only ever exercised the accepting path."""

    def _run_cli(self, body, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text(body, encoding="utf-8")
            # harmonic-forge#804: `main` applies the repo's registered identity; the
            # fake repo has none, and that step has its own tests.
            with mock.patch.object(gate_ci, "check_gate_result", checker), \
                 mock.patch.dict(sys.modules, {"manifest_identity": mock.Mock()}):
                return gate_ci.main(["--repo", "o/r", "--file", str(path)])

    def test_an_accepted_report_exits_zero(self):
        self.assertEqual(
            self._run_cli("**Verdict:** FAIL\n", lambda r, b: (True, "ok")), 0)

    def test_a_REFUSED_report_exits_nonzero(self):
        """The half that matters: a shell caller doing `gate_ci.py … || abort`
        would have shipped the regression undetected."""
        self.assertEqual(
            self._run_cli(PASS_BODY, lambda r, b: (False, "[GATE] REFUSED")), 1)


class HookPathRunsAsTheReposSlot(unittest.TestCase):
    """harmonic-forge#804 preclose: `enforce_gate_ci_on_raw_post.py` calls
    `check_gate_result` in-process, so `main()`'s identity call is never reached;
    the lookups must still run under the repo's slot."""

    def tearDown(self) -> None:
        gate_ci._SLOT_ENV = None

    def test_check_gate_result_sets_the_repos_slot_env_for_its_gh_calls(self) -> None:
        slot_env = {"GH_CONFIG_DIR": "/slots/harmonicarchitect"}
        with mock.patch.object(gate_ci, "_slot_env_for", return_value=slot_env) as resolver:
            gate_ci.check_gate_result("harmonicarchitect/kenekted-platform", "no verdict here")
        resolver.assert_called_once_with("harmonicarchitect/kenekted-platform")
        self.assertEqual(gate_ci._SLOT_ENV, slot_env)

    def test_run_passes_the_slot_env_to_the_subprocess(self) -> None:
        gate_ci._SLOT_ENV = {"GH_CONFIG_DIR": "/slots/x"}
        ok = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", return_value=ok) as run:
            gate_ci._run(["gh", "api", "x"])
        self.assertEqual(run.call_args.kwargs["env"], {"GH_CONFIG_DIR": "/slots/x"})

    def test_an_unregistered_repo_inherits_the_callers_environment(self) -> None:
        self.assertIsNone(gate_ci._slot_env_for("someone/else"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PrecloseRegressionTests(unittest.TestCase):
    """The twelve findings the harmonic-forge#504 panel raised against this
    diff, each with the test whose absence let it through."""

    def _report(self, heading_extra="", lead="**Verdict:** PASS\n",
                sha="**Head-SHA:** 0359854\n"):
        return f"## Lane 3 Gate Results — H999{heading_extra}\n\n{lead}{sha}"

    # --- keyed on the body, never on the author's stamp -------------------
    def test_a_gate_report_is_recognised_by_its_heading(self):
        """74 of 98 real gate comments carry no `kind=gate-result` footer, and
        `lane_state.py` scores them from the heading regardless. Keying on the
        flag meant omitting it skipped the check while the state model still
        recorded `gate.pass`."""
        self.assertTrue(gate_ci.looks_like_a_gate_report(self._report()))
        self.assertFalse(gate_ci.looks_like_a_gate_report("## Handoff — H1\n"))

    # --- the verdict comes from the lead, not from anywhere ---------------
    def test_a_per_case_result_line_is_not_the_verdict(self):
        """`**Result:** FAIL on TC4's optional half only; overall PASS.` sat in
        the lead of a PASS report and made the whole thing read FAIL — which
        skipped the CI check entirely."""
        body = ("## Lane 3 Gate Results — H999 — PASS\n\n"
                "**Verdict:** PASS\n**Head-SHA:** 0359854\n\n"
                "### Per-case\n**Result:** FAIL on TC4.\n")
        self.assertEqual(gate_ci.verdict_of(body), "PASS")

    def test_a_real_fail_report_with_per_case_passes_reads_fail(self):
        """hrse#373's genuine FAIL report has per-TC `**Result:** PASS` lines.
        Reading them routed a FAILURE report into the refusal path — the
        'worse than none' outcome this module warns about."""
        body = ("# Lane 3 Gate Results — H373\n\n**Verdict:** FAIL\n\n"
                "### TC1\n**Result:** PASS\n### TC2\n**Result:** PASS\n")
        self.assertEqual(gate_ci.verdict_of(body), "FAIL")

    def test_a_heading_and_lead_that_disagree_are_refused(self):
        body = self._report(heading_extra=" — PASS", lead="**Verdict:** FAIL\n")
        self.assertEqual(gate_ci.verdict_of(body), "CONFLICT")
        ok, msg = gate_ci.check_gate_result("o/r", body, run=failing_run())
        self.assertFalse(ok)
        self.assertIn("DIFFERENT", msg)

    def test_a_hedged_verdict_on_a_gate_report_is_refused(self):
        """`L3P`, `Conditional PASS — 8/9 cases` and `✅ PASS` all satisfy
        `validate_lead`, which requires the LINE and never its content. Each
        derived to None and posted with the CI check silently skipped —
        indistinguishable from a genuine green."""
        for lead in ("**Verdict:** L3P\n",
                     "**Verdict:** Conditional PASS — 8/9 cases\n",
                     "**Verdict:** \u2705 PASS\n"):
            with self.subTest(lead=lead):
                ok, msg = gate_ci.check_gate_result(
                    "o/r", self._report(lead=lead), run=failing_run())
                self.assertFalse(ok, f"{lead!r} slipped through")
                self.assertIn("could not be read", msg)

    # --- required checks only, deduped, paginated -------------------------
    def test_only_required_checks_are_consulted(self):
        """hrse `main`'s tip carries THIRTY runs of a re-dispatched
        `render-and-deploy` dashboard workflow, one `cancelled`. Its only
        required check, `verify`, is green. Reading every run poisoned that SHA
        permanently, and 'fix the failure and re-gate' is unactionable — you
        cannot un-cancel a historical run of an unrelated workflow."""
        state, detail = gate_ci.ci_conclusion(
            "o/r", "abc1234",
            run=fake_gh(checks=[check("verify"),
                                check("render-and-deploy", "cancelled")]),
            required={"verify"})
        self.assertEqual(state, "green", detail)

    def test_without_required_checks_every_check_counts(self):
        """The fallback is the STRICTER direction: unable to establish what is
        required means treat everything as required."""
        state, _ = gate_ci.ci_conclusion(
            "o/r", "abc1234",
            run=fake_gh(checks=[check("verify"),
                                check("render-and-deploy", "cancelled")]))
        self.assertEqual(state, "red")

    def test_a_missing_required_check_is_absent_not_green(self):
        state, msg = gate_ci.ci_conclusion(
            "o/r", "abc1234", run=fake_gh(checks=[check("lint")]),
            required={"verify"})
        self.assertEqual(state, "absent")
        self.assertIn("verify", msg)

    def test_only_the_latest_run_of_each_name_counts(self):
        """`filter=latest` does NOT dedupe — thirty same-named runs came back
        from one request. A historical failure of a workflow that has since
        succeeded is not evidence about this commit."""
        old = check("verify", "failure")
        old["completed_at"] = "2026-09-01T00:00:00Z"
        new = check("verify", "success")
        new["completed_at"] = "2026-09-09T00:00:00Z"
        state, _ = gate_ci.ci_conclusion("o/r", "abc1234",
                                         run=fake_gh(checks=[old, new]))
        self.assertEqual(state, "green")

    def test_newer_queued_rerun_beats_older_completed_success(self):
        old = check("verify", "success")
        old["completed_at"] = "2026-09-01T00:00:00Z"
        queued = {"name": "verify", "status": "queued", "conclusion": None,
                  "created_at": "2026-09-02T00:00:00Z"}
        state, detail = gate_ci.ci_conclusion(
            "o/r", "abc1234", run=fake_gh(checks=[old, queued]), required={"verify"})
        self.assertEqual(state, "pending")
        self.assertIn("verify", detail)

    def test_the_check_run_request_paginates(self):
        """The default page is 30 and hrse's `main` tip already carries exactly
        thirty. `--jq .check_runs` also discarded `total_count`, so truncation
        could not even be detected — the same 'a partial list read as nothing
        failing' error, one level up."""
        seen = []

        def spy(cmd):
            seen.append(cmd)
            return 0, ""
        gate_ci.ci_conclusion("o/r", "abc1234" + "0" * 33, run=spy)  # a full SHA: no resolution call first
        joined = " ".join(seen[0])
        self.assertIn("--paginate", joined)
        self.assertIn("per_page=100", joined)

    # --- the SHA must be current ------------------------------------------
    def test_a_stale_sha_is_refused_even_when_green(self):
        """The original incident plus one push: Lane 3 truthfully gates A which
        is green, a correction pushes B which is red, the report names A."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", self._report(),
            run=fake_gh(checks=[check("verify")],
                        pulls=[{"number": 42, "head": "bbbbbbb"}]))
        self.assertFalse(ok)
        self.assertIn("not the current head", msg)

    def test_a_current_sha_passes(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", self._report(),
            run=fake_gh(checks=[check("verify")],
                        pulls=[{"number": 42, "head": "0359854"}]))
        self.assertTrue(ok, msg)

    # --- the SHA is where real reports put it -----------------------------
    def test_a_sha_stated_in_the_heading_is_read(self):
        """Real reports write `... at \u0060f09eeeff\u0060` and `... @ \u00603b8c55ec\u0060`
        in the heading. The label-anchored regex alone read a SHA from 0 of 23
        real gate comments, while its docstring claimed it read real reports."""
        for heading in ("## Lane 3 Gate Results — H1739 (resolver, at `f09eeeff`)",
                        "## Lane 3 Gate Results — H1725 (tie-safety, PR #1727 @ `3b8c55ec`)"):
            with self.subTest(heading=heading):
                self.assertIsNotNone(gate_ci.gated_sha(heading + "\n"))


class StackedProbePrTests(unittest.TestCase):
    """harmonic-forge#816: a probe PR stacked on the gated PR must not block a valid PASS, and a
    real correction pushed to the gated PR must still refuse, whatever branch it targets."""

    SHA = "86c52894ea0000000000000000000000000000ff"
    PR_A = {"number": 390, "head": SHA, "ref": "feat/389-x", "base": "main"}
    PROBE = {"number": 391, "head": "58047cb0de1111", "ref": "i389-probe", "base": "feat/389-x"}

    def stale(self, *pulls, branch=None):
        return gate_ci.stale_against_pr("o/r", self.SHA, run=fake_gh(pulls=pulls),
                                        branch=branch)[0]

    def test_a_stacked_probe_is_not_stale(self):
        self.assertFalse(self.stale(self.PR_A, self.PROBE))

    def test_the_gated_pr_itself_heading_past_the_sha_is_still_stale(self):
        moved = dict(self.PR_A, head="99999999aaaa")
        self.assertTrue(self.stale(moved))
        self.assertTrue(self.stale(moved, self.PROBE))

    def test_another_pr_against_main_that_moved_on_is_still_stale(self):
        other = {"number": 7, "head": "12345678bbbb", "ref": "other", "base": "main"}
        self.assertTrue(self.stale(self.PR_A, other))

    def test_a_gated_pr_targeting_a_non_default_branch_still_refuses_its_correction(self):
        """The fail-open the review found: a base-is-not-default filter dropped this PR."""
        moved = {"number": 42, "head": "99999999aaaa", "ref": "feat/2-b", "base": "feat/1-a"}
        self.assertTrue(self.stale(moved))

    def test_a_probe_stacked_on_a_non_default_base_gated_pr_is_ignored(self):
        gated = {"number": 42, "head": self.SHA, "ref": "feat/2-b", "base": "feat/1-a"}
        probe = {"number": 43, "head": "58047cb0de1111", "ref": "probe", "base": "feat/2-b"}
        self.assertFalse(self.stale(gated, probe))

    def test_a_known_gated_branch_considers_only_that_pr(self):
        other = {"number": 7, "head": "12345678bbbb", "ref": "other", "base": "main"}
        self.assertFalse(self.stale(self.PR_A, other, self.PROBE, branch="feat/389-x"))
        moved = dict(self.PR_A, head="99999999aaaa")
        self.assertTrue(self.stale(moved, self.PROBE, branch="feat/389-x"))

    def test_the_gated_pr_stacked_on_another_candidate_is_not_dropped(self):
        """It heads AT the SHA, so it is the gated PR; the probe stacked on it is what is ignored."""
        base_pr = {"number": 41, "head": "0000000000aa", "ref": "feat/1-a", "base": "main"}
        gated = {"number": 42, "head": self.SHA, "ref": "feat/2-b", "base": "feat/1-a"}
        probe = {"number": 43, "head": "58047cb0de1111", "ref": "probe", "base": "feat/2-b"}
        self.assertTrue(self.stale(base_pr, gated, probe))  # the base PR heads elsewhere: strict
        self.assertFalse(self.stale(gated, probe))

    def test_a_mutual_base_cycle_does_not_empty_the_candidates(self):
        a = {"number": 1, "head": "aaaaaaaaaaaa", "ref": "a", "base": "b"}
        b = {"number": 2, "head": "bbbbbbbbbbbb", "ref": "b", "base": "a"}
        self.assertTrue(self.stale(a, b))

    def test_a_pr_is_never_stacked_on_itself(self):
        odd = {"number": 3, "head": "cccccccccccc", "ref": "x", "base": "x"}
        self.assertTrue(self.stale(odd))

    def test_a_branch_that_matches_no_pr_falls_back_to_strict_not_to_passing(self):
        moved = dict(self.PR_A, head="99999999aaaa")
        self.assertTrue(self.stale(moved, branch="feat/renamed"))

class ReadsActionsNotCheckRuns(unittest.TestCase):
    """harmonic-forge#805: a fine-grained PAT cannot read `commits/<sha>/check-runs` (403, no
    permission exists for it), so the gate reads Actions runs and their jobs instead."""

    SHA = "0359854f1234567890abcdef1234567890abcdef"

    def test_no_check_runs_endpoint_is_ever_called(self) -> None:
        seen = []
        inner = fake_gh(checks=[check("verify")])

        def spy(cmd):
            seen.extend(a for a in cmd if a.startswith("repos/"))
            return inner(cmd)

        state, _ = gate_ci.ci_conclusion("o/r", self.SHA, run=spy)
        self.assertEqual(state, "green")
        self.assertFalse([u for u in seen if "check-runs" in u], seen)
        self.assertTrue(any("actions/runs?head_sha=" + self.SHA in u for u in seen), seen)
        self.assertTrue(any("/jobs" in u and "filter=latest" in u for u in seen), seen)

    def test_an_unreadable_jobs_list_is_unknown_not_green(self) -> None:
        state, detail = gate_ci.ci_conclusion(
            "o/r", self.SHA, run=fake_gh(checks=[check("verify")], fail=("/jobs",)))
        self.assertEqual(state, "unknown")
        self.assertIn("jobs", detail)

    def test_a_failed_job_is_red(self) -> None:
        state, detail = gate_ci.ci_conclusion(
            "o/r", self.SHA, run=fake_gh(checks=[check("verify", conclusion="failure")]))
        self.assertEqual(state, "red")
        self.assertIn("verify", detail)

    def test_a_workflow_run_with_no_jobs_is_read_as_a_check_not_ignored(self) -> None:
        """A startup_failure run has no jobs; it must not read as green."""
        broken = {"name": "docs", "status": "completed", "conclusion": "startup_failure"}
        state, detail = gate_ci.ci_conclusion(
            "o/r", self.SHA, run=fake_gh(checks=[check("verify")], bare_runs=[broken]))
        self.assertEqual(state, "red")
        self.assertIn("docs", detail)

    def test_a_queued_run_with_no_jobs_is_pending(self) -> None:
        queued = {"name": "docs", "status": "queued", "conclusion": None}
        state, _ = gate_ci.ci_conclusion(
            "o/r", self.SHA, run=fake_gh(checks=[check("verify")], bare_runs=[queued]))
        self.assertEqual(state, "pending")

    def test_an_abbreviated_sha_is_resolved_to_the_full_sha_before_the_runs_lookup(self) -> None:
        full = "0359854f1234567890abcdef1234567890abcdef"
        calls = []
        inner = fake_gh(checks=[check("verify")])

        def run(cmd):
            url = next((a for a in cmd if a.startswith("repos/")), "")
            calls.append(url)
            if url.endswith("/commits/0359854"):
                return 0, full
            return inner(cmd)

        state, _ = gate_ci.ci_conclusion("o/r", "0359854", run=run)
        self.assertEqual(state, "green")
        self.assertTrue(any(f"head_sha={full}" in u for u in calls), calls)

    def test_an_unresolvable_abbreviated_sha_is_unknown_not_absent(self) -> None:
        state, _ = gate_ci.ci_conclusion("o/r", "0359854", run=lambda cmd: (1, "gh: not found"))
        self.assertEqual(state, "unknown")

    def test_a_sha_with_no_workflow_runs_is_absent(self) -> None:
        state, _ = gate_ci.ci_conclusion("o/r", self.SHA, run=fake_gh(checks=[]))
        self.assertEqual(state, "absent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
