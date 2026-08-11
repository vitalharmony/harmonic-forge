#!/usr/bin/env python3
"""Unit tests for deny_lane3_ae_self_post.py (harmonic-forge#216).
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
