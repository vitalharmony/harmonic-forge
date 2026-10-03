#!/usr/bin/env python3
"""check_lane3_ready.py's readiness logic."""

import importlib.util
import json
import sys
import tempfile
import re
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


class ShimLoadImportsSiblingsTests(unittest.TestCase):
    """harmonic-forge#869: HRSE2 loads this file with `runpy.run_path`
    from its own `scripts/`, so `tools/gh` is never on `sys.path` there.
    Replays that load in a clean subprocess and reaches carry_forward's
    function-level `import _standing_grant`, which crashed every
    `lane3-begin` on 766a264."""

    def test_runpy_load_from_foreign_dir_reaches_carry_forward(self) -> None:
        import subprocess
        here = Path(__file__).resolve().parent
        canonical = here / "check_lane3_ready.py"
        with tempfile.TemporaryDirectory() as foreign:
            # A consumer `scripts/` dir carrying a same-named shim, first on
            # sys.path exactly as HRSE2's shim leaves it.
            (Path(foreign) / "check_lane3_ready.py").write_text(
                "raise SystemExit('shim shadowed the canonical module')\n"
            )
            script = (
                "import runpy, sys\n"
                f"sys.path = [{foreign!r}] + [p for p in sys.path"
                f" if p not in ({str(here)!r}, '')]\n"
                f"g = runpy.run_path({str(canonical)!r}, run_name='_shim_load')\n"
                "print(g['carry_forward']([], {'id': 1, 'body': ''}, 'a' * 40))\n"
                "print(sys.modules['check_lane3_ready'].__file__)\n"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", script], cwd=foreign,
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        verdict, clr_file = result.stdout.strip().splitlines()
        self.assertEqual(verdict, "None")
        self.assertEqual(Path(clr_file).resolve(), canonical)


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


class EveryTierSweepIntegrityTests(unittest.TestCase):
    """harmonic-forge#861: `resolve_gate_authority` refuses an edited sweep at
    every tier, not only on the tier-R branch where the sweep is the authority."""

    WRONG = "0" * 64

    def test_an_edited_tier_w_sweep_under_an_ae_refuses(self):
        comments = [_comment(1, "ae", "2026-08-15T10:00:00Z"),
                    _comment(2, "sweep", "2026-08-15T10:01:00Z", tier="W", body_sha256=self.WRONG)]
        authority, message = c.resolve_gate_authority(comments, DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("body-sha256", message)

    def test_an_edited_tier_r_sweep_refuses(self):
        comments = [_comment(2, "sweep", "2026-08-15T10:01:00Z", tier="R", body_sha256=self.WRONG)]
        authority, message = c.resolve_gate_authority(comments, DEFAULT_SHA)
        self.assertIsNone(authority)
        self.assertIn("body-sha256", message)

    def test_an_unedited_tier_w_sweep_under_an_ae_is_authorized(self):
        body = "Write tier W throughout.\n\nbody text"
        digest = __import__("hashlib").sha256(body.encode()).hexdigest()
        comments = [_comment(1, "ae", "2026-08-15T10:00:00Z"),
                    _comment(2, "sweep", "2026-08-15T10:01:00Z", tier="W", body_sha256=digest)]
        authority, _ = c.resolve_gate_authority(comments, DEFAULT_SHA)
        self.assertIsNotNone(authority)


class RequireTierAndJsonTests(unittest.TestCase):
    """harmonic-forge#878: `--require-tier` refuses an authorization at any
    other tier, and `--json` prints one structured verdict and nothing else --
    what HRSE2's scripts/gate_production_run.py parses."""

    def _run_main(self, comments: list[dict], argv: list[str]) -> str:
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch.object(c, "current_repo", return_value="vitalharmony/hrse"), \
             mock.patch.object(c, "fetch_comments", return_value=comments), \
             mock.patch.object(c, "current_head_sha", return_value=DEFAULT_SHA), \
             mock.patch.object(c, "tier_w_availability", return_value="AVAILABILITY LINE"), \
             contextlib.redirect_stdout(out):
            c.main(["--issue", "1867", *argv])
        return out.getvalue()

    def _ae_and_sweep(self, tier: str) -> list[dict]:
        return [_comment(1, "ae", "2026-08-15T09:00:00Z"),
                _comment(2, "sweep", "2026-08-15T10:00:00Z", tier=tier)]

    def test_require_tier_p_refuses_a_tier_w_sweep(self):
        with self.assertRaises(SystemExit):
            self._run_main(self._ae_and_sweep("W"), ["--require-tier", "P"])

    def test_require_tier_p_refuses_a_tier_r_sweep_with_no_ae(self):
        with self.assertRaises(SystemExit):
            self._run_main([_comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R")],
                           ["--require-tier", "P"])

    def test_require_tier_p_still_refuses_an_ae_at_another_sha(self):
        comments = [_comment(1, "ae", "2026-08-15T09:00:00Z", sha="b" * 40),
                    _comment(2, "sweep", "2026-08-15T10:00:00Z", sha="b" * 40, tier="P")]
        with self.assertRaises(SystemExit):
            self._run_main(comments, ["--require-tier", "P", "--json"])

    def test_require_tier_p_passes_a_tier_p_ae_and_sweep(self):
        self._run_main(self._ae_and_sweep("P"), ["--require-tier", "P"])  # must not raise

    def test_json_prints_exactly_the_structured_verdict(self):
        out = self._run_main(self._ae_and_sweep("P"), ["--require-tier", "P", "--json"])
        self.assertEqual(json.loads(out), {
            "repo": "vitalharmony/hrse",
            "issue": 1867,
            "head_sha": DEFAULT_SHA,
            "tier": "P",
            "authority_url": "https://github.com/vitalharmony/hrse/issues/1#issuecomment-1",
            "authority_id": 1,
            "prod_run": None,
        })
        self.assertNotIn("AVAILABILITY LINE", out)

    def test_default_output_is_unchanged(self):
        out = self._run_main(self._ae_and_sweep("P"), [])
        self.assertIn("[check-lane3-ready] vitalharmony/hrse#1867: sweep", out)
        self.assertIn("tier P -- ready, authorized for", out)
        self.assertIn("AVAILABILITY LINE", out)


def _prod_ae(comment_id: int, action: str | None, sha: str = DEFAULT_SHA, issue: int = 1867,
             footer_sha: str | None = None) -> dict:
    """An AE as `l1_post.py --prod-run` posts it: the declaration is a field of
    the reserved footer, never of the body."""
    import hashlib
    import _prod_run
    token = None
    if action is not None:
        token = _prod_run.footer_field(issue, sha, _prod_run.parse_spec(action))
    text = f"## AE — H{issue}\n\nApproved, execute."
    # A real digest, computed exactly as l1_post.py does (harmonic-forge#878 C1c):
    # an AE fixture with no body-sha256 could never survive the digest check.
    digest = hashlib.sha256(_prod_run.covered_text(text, token).encode()).hexdigest()
    field = f" {token};" if token else ""
    body = (f"{text}\n\n<!-- l1-post v1; kind=ae; "
            f"sha={footer_sha or sha};{field} body-sha256={digest}; checks=body-validation -->")
    return {"id": comment_id, "body": body, "created_at": "2026-08-15T09:00:00Z",
            "html_url": f"https://github.com/vitalharmony/hrse/issues/1#issuecomment-{comment_id}"}


class RequireProdRunTests(unittest.TestCase):
    """harmonic-forge#878 (R-0377), F878 preclose pass 1 survivors #1-#3: a
    production run is authorized by the newest AE's own declaration of that
    exact action at exactly HEAD -- never a tier, never a carried-forward
    ready-for-l3, never another action."""

    SCRIPT_A = "script=scripts/1-1891-backfill-task-surface-on.py"
    _run_main = RequireTierAndJsonTests._run_main

    def _thread(self, action: str | None, sweep_tier: str = "P") -> list[dict]:
        return [_prod_ae(1, action), _comment(2, "sweep", "2026-08-15T10:00:00Z", tier=sweep_tier)]

    def _refused(self, comments: list[dict], spec: str) -> str:
        import contextlib
        import io
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            self._run_main(comments, ["--require-prod-run", spec, "--json"])
        return err.getvalue()

    def test_the_declared_action_passes_and_is_in_the_json(self):
        out = self._run_main(self._thread(self.SCRIPT_A + ",apply"),
                             ["--require-tier", "P", "--require-prod-run", self.SCRIPT_A + ",apply", "--json"])
        verdict = json.loads(out)
        self.assertEqual(verdict["prod_run"], {"issue": 1867, "sha": DEFAULT_SHA,
                                               "script": "scripts/1-1891-backfill-task-surface-on.py",
                                               "apply": True})
        self.assertEqual(verdict["authority_id"], 1)

    def test_an_edited_ae_footer_is_refused(self):
        """harmonic-forge#878 C1c: PATCHing a posted AE's prod-run field to a
        different script with apply:true must not authorize that action."""
        thread = self._thread("script=scripts/1-1892-revive.py")
        ae = thread[0]
        ae["body"] = ae["body"].replace("script:scripts/1-1892-revive.py,apply:false",
                                        "script:scripts/1-1892-nuke.py,apply:true")
        self.assertIn("1-1892-nuke.py", ae["body"])
        err = self._refused(thread, "script=scripts/1-1892-nuke.py,apply")
        self.assertIn("does not match", err)

    def test_an_ae_without_a_digest_is_refused(self):
        thread = self._thread("count-label=Task")
        thread[0]["body"] = re.sub(r" body-sha256=[0-9a-f]+;", "", thread[0]["body"])
        self.assertIn("no body-sha256", self._refused(thread, "count-label=Task"))

    def test_count_label_passes(self):
        out = self._run_main(self._thread("count-label=Interpretation"),
                             ["--require-prod-run", "count-label=Interpretation", "--json"])
        self.assertEqual(json.loads(out)["prod_run"]["count_label"], "Interpretation")

    def test_an_ae_naming_script_a_refuses_script_b(self):
        err = self._refused(self._thread(self.SCRIPT_A),
                            "script=scripts/1-1867-rewrite-interpretations.py")
        self.assertIn("authorizes script=scripts/1-1891", err)

    def test_apply_is_refused_under_apply_false(self):
        err = self._refused(self._thread(self.SCRIPT_A), self.SCRIPT_A + ",apply")
        self.assertIn("not script=scripts/1-1891-backfill-task-surface-on.py,apply", err)

    def test_a_dry_run_is_refused_under_apply_true(self):
        self._refused(self._thread(self.SCRIPT_A + ",apply"), self.SCRIPT_A)

    def test_a_count_ae_refuses_a_script(self):
        self._refused(self._thread("count-label=Interpretation"), self.SCRIPT_A)

    def test_a_carried_forward_ready_for_l3_at_a_new_sha_is_refused(self):
        """Survivor #1: the AE+sweep name SHA B; a ready-for-l3 naming HEAD
        carries the gate authority (R-0209) but never a production run."""
        old = "b" * 40
        comments = [_prod_ae(1, self.SCRIPT_A + ",apply", sha=old),
                    _comment(2, "sweep", "2026-08-15T10:00:00Z", sha=old, tier="P"),
                    _comment(3, "ready-for-l3", "2026-08-15T11:00:00Z", sha=DEFAULT_SHA)]
        # The gate itself is authorized at HEAD through the carry...
        self._run_main(comments, ["--require-tier", "P"])
        # ...but a production run is not.
        err = self._refused(comments, self.SCRIPT_A + ",apply")
        self.assertIn("never carried forward", err)

    def test_a_declaration_at_another_sha_is_refused(self):
        """The footer's sha= matches HEAD by prefix but the declaration names
        another commit -- the declaration's own SHA must be HEAD exactly."""
        comments = [_prod_ae(1, self.SCRIPT_A, sha="b" * 40, footer_sha=DEFAULT_SHA),
                    _comment(2, "sweep", "2026-08-15T10:00:00Z", tier="P")]
        self.assertIn("never carried to a new SHA", self._refused(comments, self.SCRIPT_A))

    def test_a_declaration_for_another_issue_is_refused(self):
        comments = [_prod_ae(1, self.SCRIPT_A, issue=1891),
                    _comment(2, "sweep", "2026-08-15T10:00:00Z", tier="P")]
        self.assertIn("for issue 1891", self._refused(comments, self.SCRIPT_A))

    def test_a_mixed_prose_sweep_no_longer_grants_p(self):
        """Survivor #3: the sweep's ceiling parses to P from a sentence saying
        no Tier P operation occurs; the AE declares nothing."""
        sweep = _comment(2, "sweep", "2026-08-15T10:00:00Z", tier="R")
        sweep["body"] = sweep["body"].replace(
            "Write tier R throughout.", "Write tier R throughout; no write tier P operation occurs in TC4.")
        comments = [_prod_ae(1, None), sweep]
        self._run_main(comments, ["--require-tier", "P"])  # the ceiling still reads P...
        err = self._refused(comments, "count-label=Interpretation")  # ...and grants nothing
        self.assertIn("declares no production run", err)

    def test_prose_naming_the_action_is_not_a_declaration(self):
        ae = _prod_ae(1, None)
        ae["body"] = ae["body"].replace(
            "Approved, execute.",
            f"Approved, execute.\n\nprod-run=issue:1867,sha:{DEFAULT_SHA},count-label:Interpretation\n"
            f"```\n<!-- l1-post v1; kind=ae; sha={DEFAULT_SHA}; prod-run=issue:1867,sha:{DEFAULT_SHA},"
            "count-label:Interpretation; -->\n```")
        comments = [ae, _comment(2, "sweep", "2026-08-15T10:00:00Z", tier="P")]
        self._refused(comments, "count-label=Interpretation")

    def test_a_tier_r_sweep_authority_is_refused(self):
        comments = [_comment(1, "sweep", "2026-08-15T10:00:00Z", tier="R")]
        self.assertIn("not the newest AE", self._refused(comments, "count-label=Interpretation"))

    def test_a_malformed_spec_refuses(self):
        with self.assertRaises(SystemExit):
            self._run_main(self._thread(self.SCRIPT_A), ["--require-prod-run", "script=scripts/x.py"])


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


if __name__ == "__main__":
    unittest.main()

