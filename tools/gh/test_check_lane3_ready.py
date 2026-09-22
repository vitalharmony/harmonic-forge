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


if __name__ == "__main__":
    unittest.main()
