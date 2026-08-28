#!/usr/bin/env python3
"""Unit tests for deny_lane3_ae_self_post.py (harmonic-forge#216, extended
by harmonic-forge#407 for gate-readiness sweeps).
Run: python3 tools/hooks/test_deny_lane3_ae_self_post.py"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import deny_lane3_ae_self_post as m

AE_BODY = "## AE H706 — approved, execute\n\nOperator approved.\n"
NON_AE_BODY = "## Lane 3 gate status — H706\n\nBLOCKED — no test case executed.\n"
SWEEP_HEADING_BODY = "## Gate-readiness sweep — H1359\n\nWrite tier: R throughout.\n"
SWEEP_FOOTER_ONLY_BODY = (
    "Gate readiness confirmed for H1359, all checks green.\n\n"
    "<!-- l1-post v1; kind=sweep; sha=abc123; body-sha256=deadbeef; checks=lint,build -->\n"
)


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestLaneGating(unittest.TestCase):
    """Only LANE == '3' is checked — everything else passes through
    untouched, matching every other LANE-keyed guard's precedent."""

    def test_lane_unset_allows_ae_shaped_body(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LANE", None)
            result = m.decision(f'gh issue comment 706 --body "{AE_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))

    def test_lane_1_allows_ae_shaped_body(self):
        with patch.dict(os.environ, {"LANE": "1"}):
            result = m.decision(f'gh issue comment 706 --body "{AE_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))

    def test_lane_2_allows_ae_shaped_body(self):
        with patch.dict(os.environ, {"LANE": "2"}):
            result = m.decision(f'gh issue comment 706 --body "{AE_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))

    def test_lane_unset_allows_sweep_shaped_body(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LANE", None)
            result = m.decision(f'gh issue comment 1359 --body "{SWEEP_HEADING_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))

    def test_lane_1_allows_sweep_shaped_body(self):
        with patch.dict(os.environ, {"LANE": "1"}):
            result = m.decision(f'gh issue comment 1359 --body "{SWEEP_HEADING_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))

    def test_lane_2_allows_sweep_shaped_body(self):
        with patch.dict(os.environ, {"LANE": "2"}):
            result = m.decision(f'gh issue comment 1359 --body "{SWEEP_HEADING_BODY}"', Path.cwd())
        self.assertFalse(_is_denied(result))


class TestTransportCoverage(unittest.TestCase):
    """All 4 in-scope transport shapes, under LANE=3."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_raw_gh_inline_body_denied(self):
        result = m.decision(f'gh issue comment 706 --body "{AE_BODY}"', Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_raw_gh_body_file_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"gh issue comment 706 --body-file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_lane_comment_wrapper_file_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 706 --file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_post_comment_wrapper_file_denied(self):
        """harmonic-forge's own post-comment task — no lane-marking
        convention, but this check needs none (keys off LANE, not
        attribution)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run post-comment --issue 216 --file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_post_lane_discussion_script_direct_invocation_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"python3 scripts/post_lane_discussion.py --issue 706 --file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_l1_post_mise_task_ae_denied(self):
        """harmonic-forge#407 / C2: `l1-post` was previously an uncovered
        transport -- an AE posted this way under LANE=3 passed through
        unexamined. Now denied, closing the gap."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(
                f"mise run l1-post --repo vitalharmony/hrse --issue 706 --kind ae "
                f"--sha abc123 --branch fix/706 --file {body_path}",
                Path.cwd(),
            )
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_l1_post_script_direct_ae_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(
                f"python3 scripts/l1_post.py --issue 706 --kind ae --sha abc123 "
                f"--branch fix/706 --file {body_path}",
                Path.cwd(),
            )
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_non_ae_body_allowed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(NON_AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 706 --file {body_path}", Path.cwd())
            self.assertFalse(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_unrelated_command_allowed(self):
        result = m.decision("ls -la", Path.cwd())
        self.assertFalse(_is_denied(result))


class TestSweepDenial(unittest.TestCase):
    """harmonic-forge#407: mirrors TestTransportCoverage's shape for the
    gate-readiness sweep, the door deny_lane3_ae_self_post.py didn't cover."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_heading_style_sweep_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SWEEP_HEADING_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 1359 --file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_footer_style_sweep_denied_no_heading(self):
        """No heading at all -- raw `gh issue comment`, footer only."""
        result = m.decision(f'gh issue comment 1359 --body "{SWEEP_FOOTER_ONLY_BODY}"', Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_post_comment_wrapper_footer_sweep_denied(self):
        """harmonic-forge's own `post-comment` task, footer-only sweep."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SWEEP_FOOTER_ONLY_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run post-comment --issue 1359 --file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_l1_post_mise_task_sweep_denied(self):
        """The D3 gap this issue exists to close: `l1-post` is the only
        transport that produces a valid `kind=sweep` footer at all."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SWEEP_HEADING_BODY)
            body_path = f.name
        try:
            result = m.decision(
                f"mise run l1-post --repo vitalharmony/hrse --issue 1359 --kind sweep "
                f"--sha abc123 --branch fix/1359 --spec-comment 42 --file {body_path}",
                Path.cwd(),
            )
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()

    def test_l1_post_script_direct_sweep_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SWEEP_HEADING_BODY)
            body_path = f.name
        try:
            result = m.decision(
                f"python3 scripts/l1_post.py --issue 1359 --kind sweep --sha abc123 "
                f"--branch fix/1359 --spec-comment 42 --file {body_path}",
                Path.cwd(),
            )
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()


class TestSweepFencedAndInlineSafety(unittest.TestCase):
    """The exact self-referential case F407's own issue body creates:
    prose that quotes the sweep heading/footer format -- fenced or
    inline -- must not itself trigger a deny."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sweep_heading_fenced_quote_not_denied(self):
        quoting_body = (
            "A gate-readiness sweep looks like this:\n\n"
            "```\n## Gate-readiness sweep — H1359\n...\n```\n\n"
            "and must never be posted by Lane 3 itself."
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(quoting_body)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 407 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        self.assertFalse(_is_denied(result))

    def test_sweep_footer_inline_quote_not_denied(self):
        """The real false positive this issue's own body produced: the
        footer literal quoted mid-sentence in backticks, not a fence.
        `FENCED_BLOCK` stripping alone still denies this -- verified live
        during the Lane 2 plan for F407 -- `INLINE_CODE` closes it."""
        quoting_body = (
            "`check_lane3_ready.py`'s `latest_by_kind()` keys purely on the "
            "free-text `<!-- l1-post v1; kind=sweep; ... -->` footer, "
            "forgeable by hand-typing it into any comment."
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(quoting_body)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 407 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        self.assertFalse(_is_denied(result))

    def test_sweep_heading_outside_fence_still_denied(self):
        """Sanity check mirroring the AE class's own: normalization
        doesn't over-strip and suppress a genuine (non-fenced) sweep."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(SWEEP_HEADING_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 1359 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        self.assertTrue(_is_denied(result))

    def test_ae_inline_quote_not_denied(self):
        """Uniform D2 applied to the AE check too (harmonic-forge#407 Q2):
        an AE heading quoted inline in backticks must not be denied,
        symmetric with the fenced-quote case already covered above."""
        quoting_body = (
            "This guard exists because a Lane 3 session once posted "
            "`## AE H706 — approved, execute` itself, before proceeding."
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(quoting_body)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 216 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        self.assertFalse(_is_denied(result))


class TestHeadingAnchorAvoidsFalsePositive(unittest.TestCase):
    """The exact self-referential case this issue's own body creates:
    prose that quotes the AE heading format verbatim must not itself
    trigger a deny."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_body_merely_quoting_ae_format_not_denied(self):
        quoting_body = (
            "This issue exists because a Lane 3 session posted:\n\n"
            "```\n## AE H706 — approved, execute\n...\n```\n\n"
            "which defeats the rule's purpose."
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(quoting_body)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 216 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        # A fenced code block's content still starts at column 0 -- the
        # heading anchor ALONE does not distinguish "quoting the format"
        # from "using it." An earlier version of this hook (and this
        # test) got this wrong: the test asserted the false positive was
        # correct, matching a real gap between the plan's claim and the
        # implementation. Fenced-block stripping (FENCED_BLOCK) closes
        # that gap -- this now correctly asserts NOT denied.
        self.assertFalse(_is_denied(result))

    def test_ae_body_outside_any_fence_still_denied(self):
        """Sanity check that FENCED_BLOCK.sub doesn't over-strip and
        accidentally suppress a genuine (non-fenced) AE posting."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(AE_BODY)
            body_path = f.name
        try:
            result = m.decision(f"mise run lane-comment --issue 706 --file {body_path}", Path.cwd())
        finally:
            Path(body_path).unlink()
        self.assertTrue(_is_denied(result))


class TestFailOpenOnUnreadableFile(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_arg_path_allows(self):
        result = m.decision(
            "mise run lane-comment --issue 706 --file /nonexistent/path/does-not-exist.md",
            Path.cwd(),
        )
        self.assertFalse(_is_denied(result))

    def test_missing_body_file_path_allows(self):
        result = m.decision(
            "gh issue comment 706 --body-file /nonexistent/path/does-not-exist.md",
            Path.cwd(),
        )
        self.assertFalse(_is_denied(result))


class TestFailClosedOnMalformedPayload(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LANE": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_string_command_fails_closed(self):
        result = m.decision(None, Path.cwd())
        self.assertTrue(_is_denied(result))


if __name__ == "__main__":
    unittest.main()
