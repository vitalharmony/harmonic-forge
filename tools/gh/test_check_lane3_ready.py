#!/usr/bin/env python3
"""check_lane3_ready.py's readiness logic."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "check_lane3_ready", Path(__file__).parent / "check_lane3_ready.py"
)
c = importlib.util.module_from_spec(SPEC)
sys.modules["check_lane3_ready"] = c
SPEC.loader.exec_module(c)


DEFAULT_SHA = "a" * 40


def _comment(
    comment_id: int,
    kind: str | None,
    created_at: str,
    sha: str = DEFAULT_SHA,
    tier: str | None = "W",
    body_sha256: str | None = None,
) -> dict:
    """`tier` only matters for `kind="sweep"` -- defaults to "W"
    (any non-R tier) so every pre-existing call site keeps requiring an AE
    exactly as before, unmodified. Pass `tier=None` for a sweep that
    declares no tier at all (AC4); pass an explicit `tier="R"`/`"P"`/etc.
    for the new tier-R and ceiling-resolution cases. `body_sha256`, when
    given, is embedded in the footer for AC6's tamper-detection cases --
    omitted (the common case) means no body-sha256 marker at all, which
    `verify_body_sha256()` treats as "nothing to verify," not a mismatch."""
    if not kind:
        return {
            "id": comment_id, "body": "plain discussion", "created_at": created_at,
            "html_url": f"https://github.com/vitalharmony/hrse/issues/1#issuecomment-{comment_id}",
        }
    prefix = f"Write tier {tier} throughout.\n\n" if kind == "sweep" and tier else ""
    body_sha256_field = f"; body-sha256={body_sha256}" if body_sha256 else ""
    body = f"{prefix}body text\n\n<!-- l1-post v1; kind={kind}; sha={sha}{body_sha256_field} -->"
    return {
        "id": comment_id,
        "body": body,
        "created_at": created_at,
        "html_url": f"https://github.com/vitalharmony/hrse/issues/1#issuecomment-{comment_id}",
    }


class LatestByKindTests(unittest.TestCase):
    def test_finds_most_recent_matching_kind(self):
        comments = [
            _comment(1, "ae", "2026-08-15T10:00:00Z"),
            _comment(2, "sweep", "2026-08-15T09:00:00Z"),
            _comment(3, "ae", "2026-08-15T11:00:00Z"),
        ]
        latest = c.latest_by_kind(comments, "ae")
        self.assertEqual(latest["id"], 3)

    def test_ignores_unmarked_and_other_kinds(self):
        comments = [_comment(1, None, "2026-08-15T10:00:00Z"), _comment(2, "handoff", "2026-08-15T10:01:00Z")]
        self.assertIsNone(c.latest_by_kind(comments, "ae"))

    def test_no_match_returns_none(self):
        self.assertIsNone(c.latest_by_kind([], "sweep"))


class IssueForBranchTests(unittest.TestCase):
    def test_picks_most_recent_receipt_for_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1-100.json").write_text(json.dumps(
                {"branch": "fix/1-x", "issue": 1, "created_at": "2026-08-15T09:00:00Z"}))
            (root / "2-200.json").write_text(json.dumps(
                {"branch": "fix/2-y", "issue": 2, "created_at": "2026-08-15T09:00:00Z"}))
            (root / "1-101.json").write_text(json.dumps(
                {"branch": "fix/1-x", "issue": 1, "created_at": "2026-08-15T10:00:00Z"}))
            with mock.patch.object(c, "RECEIPT_ROOT", root):
                self.assertEqual(c.issue_for_branch("fix/1-x"), 1)

    def test_no_matching_receipt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1-100.json").write_text(json.dumps(
                {"branch": "fix/1-x", "issue": 1, "created_at": "2026-08-15T09:00:00Z"}))
            with mock.patch.object(c, "RECEIPT_ROOT", root):
                with self.assertRaises(SystemExit):
                    c.issue_for_branch("fix/999-nope")

    def test_no_receipt_root_fails(self):
        with mock.patch.object(c, "RECEIPT_ROOT", Path("/nonexistent/does/not/exist")):
            with self.assertRaises(SystemExit):
                c.issue_for_branch("fix/1-x")


class MainReadinessTests(unittest.TestCase):
    """End-to-end main() against mocked branch/repo/comments -- the actual
    refusal logic this check exists to add."""

    def _run_main(self, comments: list[dict], argv: list[str] | None = None, head_sha: str = DEFAULT_SHA) -> None:
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=head_sha):
            c.main(argv if argv is not None else [])

    def test_refuses_with_no_ae(self):
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z")])

    def test_refuses_with_ae_but_no_sweep(self):
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "ae", "2026-08-15T10:00:00Z")])

    def test_refuses_with_sweep_before_ae(self):
        """AE was re-triggered (e.g. rescoped spec) without a fresh sweep."""
        with self.assertRaises(SystemExit):
            self._run_main([
                _comment(1, "sweep", "2026-08-15T09:00:00Z"),
                _comment(2, "ae", "2026-08-15T10:00:00Z"),
            ])

    def test_passes_with_ae_then_sweep(self):
        self._run_main([
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T10:00:00Z"),
        ])

    def test_passes_with_ae_then_sweep_in_the_same_wall_clock_second(self):
        """harmonic-forge#381: the atomic ae-and-sweep path can post both
        comments inside one GitHub REST created_at second (one-second
        resolution). The ordering must still be decided by comment id
        (strictly monotonic), not by a created_at value that can tie."""
        self._run_main([
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T09:00:00Z"),
        ])

    def test_refuses_with_sweep_before_ae_in_the_same_wall_clock_second(self):
        """The id-based comparison must still correctly REJECT a same-second
        sweep-then-ae -- confirms the fix didn't just make same-second pairs
        always pass regardless of true order. Sweep's id (1) is lower than
        AE's (2), i.e. the sweep was actually posted first."""
        with self.assertRaises(SystemExit):
            self._run_main([
                _comment(1, "sweep", "2026-08-15T09:00:00Z"),
                _comment(2, "ae", "2026-08-15T09:00:00Z"),
            ])


class ExplicitIssueFlagTests(unittest.TestCase):
    """--issue bypasses branch-derived resolution entirely, so a
    worktree on a stale/unrelated branch (a prior gate's) or already
    detached can still validate the intended issue."""

    def test_explicit_issue_skips_branch_derivation_entirely(self):
        comments = [
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T10:00:00Z"),
        ]
        with mock.patch.object(c, "current_branch") as branch, \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch") as issue_for_branch, \
             mock.patch.object(c, "fetch_comments", return_value=comments) as fetch, \
             mock.patch.object(c, "current_head_sha", return_value=DEFAULT_SHA):
            c.main(["--issue", "1145"])
        branch.assert_not_called()
        issue_for_branch.assert_not_called()
        fetch.assert_called_once_with("vitalharmony/hrse", 1145)

    def test_explicit_issue_works_from_a_detached_head(self):
        """The exact failure mode: current_branch() would raise
        SystemExit on a detached HEAD, but --issue never calls it."""
        comments = [
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T10:00:00Z"),
        ]
        with mock.patch.object(c, "current_branch", side_effect=SystemExit(1)), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=DEFAULT_SHA):
            c.main(["--issue", "1145"])  # must not raise

    def test_no_issue_flag_still_derives_from_branch(self):
        """Regression guard: the default (no --issue) path is unchanged."""
        comments = [
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T10:00:00Z"),
        ]
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1) as issue_for_branch, \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=DEFAULT_SHA):
            c.main([])
        issue_for_branch.assert_called_once_with("fix/1-x")


class ShaCarryForwardTests(unittest.TestCase):
    """The AE's own sha= marker must actually authorize the
    checked-out commit, not just exist. Fixtures replay two real issues'
    recorded shapes -- both a pre-fix AE followed by a re-push with no
    fresh authorization at all."""

    NEW_SHA = "2" * 40

    def _run_main(self, comments: list[dict]) -> None:
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=self.NEW_SHA):
            c.main([])

    def test_matching_sha_passes_as_today(self):
        self._run_main([
            _comment(1, "ae", "2026-08-15T09:00:00Z", sha=self.NEW_SHA),
            _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=self.NEW_SHA),
        ])

    def test_mismatched_sha_with_no_carry_forward_fails(self):
        """Those issues' shape before Lane 3 flagged the gap: a fix-and-
        repush cycle with only the pre-fix AE on record, no ready-for-l3
        naming the new commit."""
        with self.assertRaises(SystemExit):
            self._run_main([
                _comment(1, "ae", "2026-08-15T09:00:00Z", sha=DEFAULT_SHA),
                _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA),
            ])

    def test_mismatched_sha_with_postdating_ready_for_l3_passes(self):
        """The carry-forward path: a Lane 1 ready-for-l3 posted after the AE,
        naming the actual checked-out commit, extends the AE's authorization
        without requiring a fresh AE for the routine repush cycle."""
        self._run_main([
            _comment(1, "ae", "2026-08-15T09:00:00Z", sha=DEFAULT_SHA),
            _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA),
            _comment(3, "ready-for-l3", "2026-08-15T11:00:00Z", sha=self.NEW_SHA),
        ])

    def test_ready_for_l3_predating_ae_does_not_count_as_carry_forward(self):
        """Only a ready-for-l3 that POSTDATES the AE counts -- one from
        before the AE was even posted proves nothing about this commit."""
        with self.assertRaises(SystemExit):
            self._run_main([
                _comment(1, "ready-for-l3", "2026-08-15T08:00:00Z", sha=self.NEW_SHA),
                _comment(2, "ae", "2026-08-15T09:00:00Z", sha=DEFAULT_SHA),
                _comment(3, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA),
            ])

    def test_ready_for_l3_naming_a_different_sha_does_not_count(self):
        """A postdating ready-for-l3 that names yet a THIRD sha (neither the
        AE's nor HEAD's) is not a valid carry-forward for this commit."""
        with self.assertRaises(SystemExit):
            self._run_main([
                _comment(1, "ae", "2026-08-15T09:00:00Z", sha=DEFAULT_SHA),
                _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA),
                _comment(3, "ready-for-l3", "2026-08-15T11:00:00Z", sha="3" * 40),
            ])

    def test_ae_with_no_parseable_sha_fails(self):
        """An AE footer that is missing its sha= marker entirely -- must fail
        closed, not treat "no sha" as "any commit is fine"."""
        with self.assertRaises(SystemExit):
            self._run_main([
                {"id": 1, "body": "## AE\nApproved.\n\n<!-- l1-post v1; kind=ae; body-sha256=x -->",
                 "created_at": "2026-08-15T09:00:00Z",
                 "html_url": "https://github.com/vitalharmony/hrse/issues/1#issuecomment-1"},
                _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA),
            ])


class WriteTierParsingTests(unittest.TestCase):
    """Ceiling resolution, not first-match."""

    def test_ceiling_not_first_match(self):
        """The literal bug Lane 1's pitch-inspection caught live: a naive
        first-match regex resolves "R" here and skips the AE requirement --
        must resolve to P instead."""
        body = (
            "Write tier R throughout.\n\n"
            "### Test cases\n"
            "- TC7 -- if this needs escalation, write tier P is required.\n"
        )
        self.assertEqual(c.parse_write_tier(body), "P")

    def test_single_tier_resolves_to_itself(self):
        self.assertEqual(c.parse_write_tier("Write tier R throughout."), "R")

    def test_no_tier_mention_returns_none(self):
        self.assertIsNone(c.parse_write_tier("No tier declaration here."))

    def test_real_hrse1324_sweep_text_resolves_to_r(self):
        """Replay a live precedent's actual wording, not a
        paraphrase."""
        body = (
            "**All eleven cases are read-only.** Write tier R throughout -- "
            "**no AE is issued and none is needed.**"
        )
        self.assertEqual(c.parse_write_tier(body), "R")

    def test_real_harmonic_forge401_sweep_text_resolves_to_r(self):
        body = "**All eight cases are read-only.** Write tier R throughout -- **no AE is issued and none is needed.**"
        self.assertEqual(c.parse_write_tier(body), "R")


class TierRAuthorityTests(unittest.TestCase):
    """The sweep itself is a valid authorization
    anchor when it declares tier R and no AE exists."""

    NEW_SHA = "2" * 40

    def _run_main(self, comments: list[dict], head_sha: str = DEFAULT_SHA) -> None:
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=head_sha):
            c.main(["--issue", "1145"])

    def test_tier_r_no_ae_matching_sha_passes(self):
        """AC2: the core new path."""
        self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R")])  # must not raise

    def test_tier_w_no_ae_fails_naming_the_tier(self):
        """AC3/AC5: above tier R still requires an AE; message names the tier."""
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z", tier="W")])

    def test_tier_p_no_ae_fails_naming_the_tier(self):
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z", tier="P")])

    def test_no_tier_declared_fails_distinctly_from_ae_missing(self):
        """AC4: silence must never be read as tier R, and must be a
        different failure than 'no AE' -- both are checked, but a missing
        tier declaration is caught first (it's checked before the AE
        lookup even happens)."""
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z", tier=None)])

    def test_tier_r_stale_sha_no_ready_for_l3_fails(self):
        """AC7: a tier-R sweep whose sha doesn't match HEAD, with nothing
        carrying it forward, is stale and must be rejected -- the
        regression the AE-anchor removal could otherwise introduce."""
        with self.assertRaises(SystemExit):
            self._run_main(
                [_comment(1, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA, tier="R")],
                head_sha=self.NEW_SHA,
            )

    def test_tier_r_stale_sha_carried_forward_by_ready_for_l3_passes(self):
        """AC7: the same carry_forward() mechanism the AE path already uses
        for a stale AE, reused for a stale tier-R sweep."""
        self._run_main(
            [
                _comment(1, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA, tier="R"),
                _comment(2, "ready-for-l3", "2026-08-15T11:00:00Z", sha=self.NEW_SHA),
            ],
            head_sha=self.NEW_SHA,
        )  # must not raise

    def test_tier_w_with_valid_ae_passes_exactly_as_today(self):
        """AC3 non-regression, exercised through the renamed `authority`
        param: an AE-present path is completely untouched by this issue."""
        self._run_main([
            _comment(1, "ae", "2026-08-15T09:00:00Z"),
            _comment(2, "sweep", "2026-08-15T10:00:00Z", tier="W"),
        ])  # must not raise

    def test_ae_present_governs_even_when_sweep_declares_tier_r(self):
        """An AE, when present, is checked first regardless of tier -- not
        a special carve-out, just 'the AE path runs first' (see main()'s
        structure). A stale/mismatched AE here still fails on ITS own
        terms, it does not silently fall back to the tier-R path."""
        with self.assertRaises(SystemExit):
            self._run_main(
                [
                    _comment(1, "ae", "2026-08-15T09:00:00Z", sha=DEFAULT_SHA),
                    _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=DEFAULT_SHA, tier="R"),
                ],
                head_sha=self.NEW_SHA,
            )


class BodySha256VerificationTests(unittest.TestCase):
    """When the sweep itself is the authorization anchor,
    its fetched body must match its own recorded body-sha256 -- comments
    are editable in place."""

    def _digest_for(self, prefix_body: str) -> str:
        import hashlib
        return hashlib.sha256(prefix_body.rstrip("\n").encode()).hexdigest()

    def test_no_recorded_digest_is_not_a_mismatch(self):
        """Absence of a body-sha256 marker (every pre-#1359 comment, and
        every fixture in this file that doesn't pass body_sha256=) is
        'nothing to verify', not a failure -- that's AC4/no-tier's job,
        not this check's."""
        comment = _comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R")
        self.assertTrue(c.verify_body_sha256(comment))

    def test_matching_digest_verifies(self):
        prefix = "Write tier R throughout.\n\nbody text"
        digest = self._digest_for(prefix)
        comment = _comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R", body_sha256=digest)
        self.assertTrue(c.verify_body_sha256(comment))

    def test_edited_body_fails_verification(self):
        """The actual tamper case: the recorded digest was computed
        against different text than what's fetched now."""
        wrong_digest = self._digest_for("Write tier R throughout.\n\noriginal text")
        comment = _comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R", body_sha256=wrong_digest)
        self.assertFalse(c.verify_body_sha256(comment))

    def test_mismatch_fails_the_full_readiness_check(self):
        """End-to-end: a tier-R gate must not start on an unverifiable
        sweep."""
        wrong_digest = "0" * 64
        comment = _comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R", body_sha256=wrong_digest)
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1), \
             mock.patch.object(c, "fetch_comments", return_value=[comment]), \
             mock.patch.object(c, "current_head_sha", return_value=DEFAULT_SHA):
            with self.assertRaises(SystemExit):
                c.main(["--issue", "1145"])


class L1PostDigestRoundTripTests(unittest.TestCase):
    """Regression guard for the l1_post.py fix Lane 1 required
    -- the recorded body-sha256 must match what verify_body_sha256() (i.e.
    footer-stripped, rstripped) computes, independent of how many trailing
    newlines the source body happened to have."""

    def _round_trip(self, source_body: str) -> bool:
        import hashlib
        digest = hashlib.sha256(source_body.rstrip("\n").encode()).hexdigest()
        posted_body = source_body.rstrip("\n") + (
            f"\n\n<!-- l1-post v1; kind=sweep; sha={DEFAULT_SHA}; body-sha256={digest} -->\n"
        )
        return c.verify_body_sha256({"body": posted_body})

    def test_zero_trailing_newlines(self):
        self.assertTrue(self._round_trip("Write tier R throughout."))

    def test_one_trailing_newline(self):
        self.assertTrue(self._round_trip("Write tier R throughout.\n"))

    def test_two_trailing_newlines(self):
        self.assertTrue(self._round_trip("Write tier R throughout.\n\n"))


class RoundWindowScopingTests(unittest.TestCase):
    """harmonic-forge#791: hrse#2101's real protocol-post sequence, replayed
    by comment id, kind and sha. Round 1's AE carried forward through a
    round-2 handoff and then a round-2 spec, and Lane 3 gated round 2 twice
    with no AE for it.

    `carry_forward` is fixed WINDOW-scoped (only a handoff/spec strictly
    between the authority and the candidate `ready-for-l3` breaks the carry),
    not thread-globally -- a preclose refuter reproduced live, against
    hrse#2095's real thread, that a thread-global "any newer spec/handoff
    anywhere" check false-refuses a legitimate carry-forward whenever an
    UNRELATED spec for separate work sits on the same issue after the
    candidate `ready-for-l3`. `InterleavedWorkstreamTests` below is that
    replay.
    """

    T = "2026-09-27T00:00:00Z"
    ROUND_1 = [
        _comment(5851011782, "handoff", T, sha="cf097489"),
        _comment(5851858894, "handoff", T, sha="cf097489"),
        _comment(5852328795, "ready-for-l3", T, sha="85564917"),
        _comment(5852444586, "rework", T, sha="85564917"),
        _comment(5852649394, "ready-for-l3", T, sha="829f8e54"),
        _comment(5852721664, "spec", T, sha="-"),
        _comment(5852764884, "ae", T, sha="829f8e54"),
        _comment(5852765265, "sweep", T, sha="829f8e54"),
    ]
    REBASE = [_comment(5853299906, "ready-for-l3", T, sha="524ef3c7")]
    ROUND_2 = [
        _comment(5853949813, "handoff", T, sha="17e7630b"),
        _comment(5854113886, "rework", T, sha="1aca6444"),
        _comment(5854339981, "ready-for-l3", T, sha="21e587db"),
    ]
    ROUND_2_SPEC = [_comment(5857537661, "spec", T, sha="-")]
    ROUND_2_AE = [
        _comment(5857586993, "ae", T, sha="21e587db"),
        _comment(5857587454, "sweep", T, sha="21e587db"),
    ]

    def _run(self, comments, head_sha):
        with mock.patch.object(c, "current_branch", return_value="fix/2101-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=2101), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=head_sha), \
             mock.patch.object(c, "tier_w_availability", return_value=None):
            c.main([])

    def _refused(self, comments, head_sha):
        err = __import__("io").StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            self._run(comments, head_sha)
        return err.getvalue()

    def test_the_rebase_carry_forward_still_passes(self):
        """5853299906: a rebase-only ready-for-l3 under round 1's AE, with
        nothing between the AE and it. The case carry-forward exists for --
        it must keep working."""
        self._run(self.ROUND_1 + self.REBASE, "524ef3c7")

    def test_round_2_gate_1_is_refused_a_handoff_sits_in_the_carry_window(self):
        """The state Lane 3 gated at 5854445232: the round-2 handoff sits
        strictly between round 1's AE and the ready-for-l3 that would carry
        it to 21e587db."""
        msg = self._refused(self.ROUND_1 + self.REBASE + self.ROUND_2, "21e587db")
        self.assertIn("authorizes sha=829f8e54", msg)
        self.assertIn("21e587db", msg)

    def test_round_2_gate_2_is_also_refused_a_spec_now_sits_in_the_window_too(self):
        """The state Lane 3 gated at 5857564292, three minutes after its
        spec. The spec's id is still less than the ready-for-l3's, so it is
        still inside the (AE, ready-for-l3) window."""
        self._refused(self.ROUND_1 + self.REBASE + self.ROUND_2 + self.ROUND_2_SPEC, "21e587db")

    def test_a_handoff_is_recognized_by_heading_even_with_the_wrong_kind_footer(self):
        """Preclose finding: a spec/handoff posted via `--kind discussion`
        (the sanctioned-transport default) must still count -- 74 of 98 real
        gate reports in this house carry no matching kind footer either."""
        mis_kinded_handoff = _comment(5853949813, "discussion", self.T, sha="17e7630b")
        mis_kinded_handoff["body"] = "## Handoff: something new\n\n" + mis_kinded_handoff["body"]
        comments = self.ROUND_1 + self.REBASE + [mis_kinded_handoff] + [
            _comment(5854113886, "rework", self.T, sha="1aca6444"),
            _comment(5854339981, "ready-for-l3", self.T, sha="21e587db"),
        ]
        self._refused(comments, "21e587db")

    def test_round_2_passes_once_its_own_ae_and_sweep_exist(self):
        self._run(self.ROUND_1 + self.REBASE + self.ROUND_2 + self.ROUND_2_SPEC + self.ROUND_2_AE,
                  "21e587db")

    #: No `ae` comment at all -- isolates the no-AE tier-R fallback path.
    #: AE always takes precedence when present (main()'s original,
    #: unmodified behaviour: an AE that exists but does not cover head_sha
    #: is a hard failure, it never falls through to tier-R), so a fixture
    #: containing an AE would pass or fail these tests for the wrong
    #: reason -- exactly what made the first draft of these three vacuous.
    NO_AE = [_comment(5853949813, "handoff", "2026-09-27T00:00:00Z", sha="17e7630b")]

    def test_a_tier_r_sweep_authorizes_without_an_ae(self):
        """The existing no-AE tier-R path still works."""
        comments = self.NO_AE + [_comment(5857600000, "sweep", self.T, sha="21e587db", tier="R")]
        self._run(comments, "21e587db")

    def test_a_tampered_tier_r_sweep_is_refused(self):
        """Preclose finding (4 of 5 lenses, independently): the tier-R branch
        must apply the SAME body-sha256 tamper check the gate-start path
        always has -- not a second, weaker copy. The recorded digest is for
        the tier-W body this sweep originally announced; the body was then
        edited to say tier R without updating the digest."""
        digest = c.hashlib.sha256(b"Write tier W throughout.\n\nbody text").hexdigest()
        tampered = _comment(5857600001, "sweep", self.T, sha="21e587db", tier="R",
                            body_sha256=digest)
        self._refused(self.NO_AE + [tampered], "21e587db")

    def test_a_tier_w_sweep_never_authorizes(self):
        """Preclose finding: the tier check must actually discriminate --
        mutating it to accept any tier must be caught."""
        comments = self.NO_AE + [_comment(5857600002, "sweep", self.T, sha="21e587db", tier="W")]
        self._refused(comments, "21e587db")


class InterleavedWorkstreamTests(unittest.TestCase):
    """harmonic-forge#791 preclose finding, reproduced against hrse#2095's
    REAL thread (fetched live): a carry-forward for one piece of work must
    not be defeated by an unrelated spec, posted for SEPARATE work on the
    same issue, that has no AE yet simply because its own approval hasn't
    arrived. Window-scoping (RoundWindowScopingTests above) is immune to
    this by construction -- this is the regression test proving it."""

    T = "2026-09-27T00:00:00Z"
    # hrse#2095, comment ids and kinds exactly as fetched live; sha values
    # abbreviated consistently with this file's other fixtures.
    THREAD = [
        _comment(5851088617, "ae", T, sha="4e24ef11"),
        _comment(5852171416, "sweep", T, sha="4e24ef11"),
        _comment(5852951693, "ready-for-l3", T, sha="7b201aad"),
        # Unrelated work: the production-backfill spec, posted on the same
        # issue while the embedding-fallback fix above is still awaiting its
        # own gate. No AE for it yet -- its AE (5853318698) arrives only
        # after the PASS below.
        _comment(5852952053, "spec", T, sha="-"),
        _comment(5852978732, "spec", T, sha="-"),
    ]

    def test_an_unrelated_later_spec_does_not_block_this_carry_forward(self):
        with mock.patch.object(c, "current_branch", return_value="fix/2095-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=2095), \
             mock.patch.object(c, "fetch_comments", return_value=self.THREAD), \
             mock.patch.object(c, "current_head_sha", return_value="7b201aad"), \
             mock.patch.object(c, "tier_w_availability", return_value=None):
            c.main([])  # must not raise


class ReworkRoundBoundaryTests(unittest.TestCase):
    """harmonic-forge#792 AC3. A rework between the AE and the `ready-for-l3`
    breaks the carry unless it declares `**Test cases:** unchanged`."""

    T = "2026-09-27T00:00:00Z"
    APPROVED = [
        _comment(10, "spec", T, sha="-"),
        _comment(11, "ae", T, sha="1111111"),
        _comment(12, "sweep", T, sha="1111111"),
    ]

    def _rework(self, lead: str) -> dict:
        rework = _comment(13, "rework", self.T, sha="1111111")
        rework["body"] = f"## Rework\n\n**Finding:** TC2 fails.\n{lead}**Next:** fix.\n\n" + rework["body"]
        return rework

    def _main(self, comments):
        with mock.patch.object(c, "current_branch", return_value="fix/1-x"), \
             mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "issue_for_branch", return_value=1), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value="2222222"), \
             mock.patch.object(c, "tier_w_availability", return_value=None):
            c.main([])

    def _thread(self, lead: str) -> list[dict]:
        return self.APPROVED + [self._rework(lead), _comment(14, "ready-for-l3", self.T, sha="2222222")]

    def test_a_rework_that_does_not_declare_its_test_cases_breaks_the_carry(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._main(self._thread(""))

    def test_a_rework_that_changes_test_cases_breaks_the_carry(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._main(self._thread("**Test cases:** TC3 added.\n"))

    def test_a_qualified_unchanged_still_breaks_the_carry(self):
        """Preclose finding: "unchanged except TC3" changes a test case."""
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._main(self._thread("**Test cases:** unchanged except TC3 is new\n"))

    def test_a_rework_with_test_cases_unchanged_keeps_the_carry(self):
        self._main(self._thread("**Test cases:** unchanged\n"))

    def test_the_declaration_is_read_in_its_plain_spelling_too(self):
        self._main(self._thread("Test cases: unchanged\n"))

    def _same_sha_thread(self, lead: str) -> list[dict]:
        """Preclose finding: the rework lands after the AE with no new push."""
        rework = self._rework(lead)
        rework["body"] = rework["body"].replace("sha=1111111", "sha=2222222")
        ae = _comment(11, "ae", self.T, sha="2222222")
        return [self.APPROVED[0], ae, _comment(12, "sweep", self.T, sha="2222222"), rework]

    def test_a_tc_rework_after_an_ae_at_the_same_sha_is_refused(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._main(self._same_sha_thread(""))

    def test_a_new_spec_after_an_ae_at_the_same_sha_is_refused(self):
        """Cross-family finding: a new spec at the AE's own SHA is a new round."""
        thread = self._same_sha_thread("**Test cases:** unchanged\n")[:3] + [_comment(13, "spec", self.T, sha="-")]
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._main(thread)

    def test_an_unchanged_rework_after_an_ae_at_the_same_sha_is_fine(self):
        self._main(self._same_sha_thread("**Test cases:** unchanged\n"))

    def test_a_rework_before_the_ae_is_outside_the_window(self):
        rework = self._rework("")
        rework["id"] = 9
        self._main([rework] + self.APPROVED + [_comment(14, "ready-for-l3", self.T, sha="2222222")])


class AutoAeAuthorityTests(unittest.TestCase):
    """harmonic-forge#851 AC3.6: the consumer half of the fixed carve-out."""

    NEW_SHA = "b" * 40

    @staticmethod
    def _url(cid: int) -> str:
        return f"https://github.com/vitalharmony/hrse/issues/1#issuecomment-{cid}"

    def _handoff(self, cid: int, mutates_live: str | None) -> dict:
        field = f" mutates-live={mutates_live};" if mutates_live else ""
        return {"id": cid, "created_at": "2026-10-02T01:00:00Z", "html_url": self._url(cid),
                "body": f"## Handoff\n\n<!-- l1-post v1; kind=handoff; plan-first=false;{field} "
                        f"sha={DEFAULT_SHA} -->"}

    def _auto_ae(self, cid: int) -> dict:
        return {"id": cid, "created_at": "2026-10-02T02:00:00Z", "html_url": self._url(cid),
                "body": "**Authorized under:** the standing auto-AE toggle\n\n"
                        f"<!-- l1-post v1; kind=ae; authorized-by=auto-ae; sha={DEFAULT_SHA} -->"}

    def _spec(self, cid: int, tier: str | None) -> dict:
        line = f"Write tier: {tier}\n\n" if tier else ""
        return {"id": cid, "created_at": "2026-10-02T01:30:00Z", "html_url": self._url(cid),
                "body": f"## Lane 3 Test Spec — H1\n\n{line}1. TC1\n\n"
                        f"<!-- l1-post v1; kind=spec; posted-by=LANE3; body-sha256={'0' * 64} -->"}

    def _thread(self, mutates_live="false", tier="W", spec_tier="W"):
        return [self._handoff(1, mutates_live), self._spec(5, spec_tier) if spec_tier != "none"
                else self._spec(5, None), self._auto_ae(6),
                _comment(7, "sweep", "2026-10-02T02:00:01Z", tier=tier)]

    def test_accepts_auto_ae_over_tier_w_on_a_safe_handoff(self):
        authority, message = c.resolve_gate_authority(self._thread(), DEFAULT_SHA)
        self.assertIsNotNone(authority, message)

    def test_refuses_auto_ae_over_tier_p_sweep(self):
        authority, message = c.resolve_gate_authority(self._thread(tier="P"), DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("auto-AE authorizes Tier R or W only", message)

    def test_refuses_auto_ae_when_only_the_spec_is_tier_p(self):
        """Preclose pass 1 survivor 2: the consumer's ceiling includes the spec."""
        authority, message = c.resolve_gate_authority(self._thread(spec_tier="P"), DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("ceiling over the sweep and the newest spec is P", message)

    def test_refuses_auto_ae_when_the_spec_declares_no_tier(self):
        authority, message = c.resolve_gate_authority(self._thread(spec_tier="none"), DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("declares no write tier", message)

    def test_refuses_auto_ae_on_mutates_live_handoff(self):
        authority, message = c.resolve_gate_authority(self._thread("true"), DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("mutates-live=true", message)

    def test_refuses_auto_ae_on_legacy_handoff(self):
        authority, message = c.resolve_gate_authority(self._thread(None), DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("carries no mutates-live field", message)

    def test_ready_for_l3_does_not_carry_refused_auto_ae(self):
        thread = self._thread("true") + [
            _comment(8, "ready-for-l3", "2026-10-02T03:00:00Z", sha=self.NEW_SHA)]
        authority, message = c.resolve_gate_authority(thread, self.NEW_SHA)
        self.assertIsNone(authority)
        self.assertIn("auto-AE toggle", message)

    def test_manual_ae_over_tier_p_unchanged(self):
        thread = [self._handoff(1, None), _comment(2, "ae", "2026-10-02T02:00:00Z"),
                  _comment(3, "sweep", "2026-10-02T02:00:01Z", tier="P")]
        authority, message = c.resolve_gate_authority(thread, DEFAULT_SHA)
        self.assertIsNotNone(authority, message)

    QUOTED = f"<!-- l1-post v1; kind=ae; authorized-by=auto-ae; sha={'a' * 40} -->"

    def _manual_ae_quoting(self, wrapper: str, trailing: bool = True) -> dict:
        ae = _comment(6, "ae", "2026-10-02T02:00:00Z")
        footer = ae["body"][ae["body"].index("<!--"):]
        quoted = wrapper.format(self.QUOTED)
        ae["body"] = f"**Authorized:** x\n\n{quoted}\n\nmore prose" + (f"\n\n{footer}" if trailing else "")
        return ae

    def test_a_quoted_auto_ae_marker_never_counts(self):
        """Sticky-wicket PATCH (survivor 4): only the body's own trailing footer
        is read. Each wrapper quotes a REAL marker this time."""
        wrappers = {"fence": "```\n{}\n```", "blockquote": "> {}", "inline": "`{}`",
                    "prose": "see {} above"}
        for name, wrapper in wrappers.items():
            with self.subTest(wrapper=name):
                self.assertFalse(c.is_auto_ae(self._manual_ae_quoting(wrapper)))

    def test_a_quoted_marker_with_no_trailing_footer_reads_as_absent(self):
        self.assertFalse(c.is_auto_ae(self._manual_ae_quoting("> {}", trailing=False)))

    def test_manual_ae_quoting_auto_ae_over_tier_p_still_authorizes(self):
        thread = [self._handoff(1, "true"), self._manual_ae_quoting("```\n{}\n```"),
                  _comment(7, "sweep", "2026-10-02T02:00:01Z", tier="P")]
        authority, message = c.resolve_gate_authority(thread, DEFAULT_SHA)
        self.assertIsNotNone(authority, message)

    def test_body_sha256_ignores_a_quoted_marker(self):
        """The third site the sticky-wicket ruling named: verify_body_sha256."""
        import hashlib
        prefix = f"body\n\n> {self.QUOTED}\n\nmore"
        digest = hashlib.sha256(prefix.encode()).hexdigest()
        comment = {"body": f"{prefix}\n\n<!-- l1-post v1; kind=sweep; sha={'a' * 40}; body-sha256={digest} -->\n"}
        self.assertTrue(c.verify_body_sha256(comment))


if __name__ == "__main__":
    unittest.main()

