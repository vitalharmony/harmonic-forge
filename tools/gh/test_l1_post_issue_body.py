#!/usr/bin/env python3
"""Focused tests for Lane 1 issue-body validation and discussion posting."""

import importlib.util
from pathlib import Path
import atexit
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


discussion = load("post_lane_discussion")
l1_post = load("l1_post")

_BODY_DIR = tempfile.TemporaryDirectory()
_BODY_PATH = Path(_BODY_DIR.name) / "body.md"
_BODY_PATH.write_text("Discussion.\n")
atexit.register(_BODY_DIR.cleanup)


def _body_file() -> str:
    """A real, absolute --file path.

    These tests patched `regular_body` and passed a relative "body.md", which
    worked until `post_lane_discussion.main` grew its own existence check
    (harmonic-forge#266). Nothing collects `scripts/test_*.py`, so the three
    tests that broke stayed broken and silent -- found while adding a private-repo incident's
    cases. The file is real so the check passes; contents are irrelevant
    because `regular_body` is still patched. Cleaned up at interpreter exit,
    since this task now runs on every `mise run check`.
    """
    return str(_BODY_PATH)


issue_post = load("post_lane1_issue")


class IssueBodyTests(unittest.TestCase):
    def valid_body(self) -> str:
        return '''## Tooling Exception

ADR-002 containment is satisfied:
1. The change is repo-local tooling.

## Problem

The existing guard misses an unsafe transport.

```text
unsafe command -> allowed
```

## Selected Approach

Update `scripts/post_lane1_issue.py` with structural validation.

## Test Cases

1. The validated body must be refetched unchanged after posting.
'''

    def test_rejects_missing_tooling_exception(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_body("## Selected Approach\nA plan.\n`scripts/tool.py`\n1. It must work.", {"tech-debt"})

    def test_rejects_h481_style_proposal_without_selected_approach(self) -> None:
        body = self.valid_body().replace("## Selected Approach", "## Proposal (not yet scoped as a full handoff)")
        with self.assertRaises(SystemExit):
            issue_post.validate_body(body, {"bug"})

    def test_rejects_bug_body_without_root_cause(self) -> None:
        body = self.valid_body().replace("## Problem", "## Context")
        with self.assertRaises(SystemExit):
            issue_post.validate_body(body, {"bug"})

    def test_accepts_complete_bug_body(self) -> None:
        issue_post.validate_body(self.valid_body(), {"bug", "tech-debt"})

    def test_accepts_ui_label(self) -> None:
        issue_post.validate_body(self.valid_body(), {"ui"})

    def test_accepts_feature_label(self) -> None:
        issue_post.validate_body(self.valid_body(), {"feature"})

    def test_rejects_epic_only_label(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_body(self.valid_body(), {"epic"})

    def test_accepts_correct_in_ordinary_prose(self) -> None:
        body = self.valid_body().replace(
            "The existing guard misses an unsafe transport.",
            "The styling is internally correct.",
        )
        issue_post.validate_body(body, {"bug", "tech-debt"})

    def test_correction_to_requires_an_issue_number(self) -> None:
        body = self.valid_body().replace(
            "The existing guard misses an unsafe transport.",
            "This is a correction to the prior description.",
        )
        with self.assertRaises(SystemExit):
            issue_post.validate_body(body, {"bug", "tech-debt"})

    def test_accepts_correction_to_with_issue_number(self) -> None:
        body = self.valid_body().replace(
            "The existing guard misses an unsafe transport.",
            "This is a correction to #481.",
        )
        issue_post.validate_body(body, {"bug", "tech-debt"})

    def test_accepts_inline_adr_conditions_used_by_real_issues(self) -> None:
        body = self.valid_body().replace(
            "ADR-002 containment is satisfied:\n1. The change is repo-local tooling.",
            "Meets all three ADR-002 conditions: (1) repo-local tooling; (2) bounded state; (3) explicit scope.",
        ).replace("## Selected Approach", "## Selected approach")
        issue_post.validate_body(body, {"bug", "tech-debt"})

    # a private-repo incident -- amend mode. The shape checks below are the ones that made
    # every pre-existing issue body unmaintainable by Lane 1.

    def amendable_body(self) -> str:
        """A real issue's shape: no approach heading, no 'full' before 3-lane."""
        return (
            "**Blocked on:** nothing\n"
            "**3-lane, not Tooling Exception.** Every session starts here.\n\n"
            "## Scope\n\nRework the launcher.\n\n"
            "## Acceptance criteria\n\n1. It works.\n"
        )

    def test_amend_accepts_a_body_the_new_issue_schema_rejects(self) -> None:
        body = self.amendable_body()
        with self.assertRaises(SystemExit):
            issue_post.validate_body(body, {"tech-debt"})
        issue_post.validate_amendment(body, "old body that is long enough here", True)

    def test_amend_rejects_the_placeholder(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_amendment(issue_post.PLACEHOLDER_BODY, "x" * 100, False)

    def test_amend_rejects_empty(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_amendment("   \n  ", "x" * 100, False)

    def test_amend_refuses_destructive_shrink(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_amendment("tiny", "x" * 100, False)

    def test_allow_shrink_overrides_the_floor(self) -> None:
        issue_post.validate_amendment("tiny", "x" * 100, True)

    def test_shrink_floor_permits_an_ordinary_trim(self) -> None:
        current = "x" * 100
        issue_post.validate_amendment("y" * 80, current, False)

    def test_shrink_guard_is_inert_on_an_empty_current_body(self) -> None:
        issue_post.validate_amendment("a real body", "", False)

    def test_routing_regex_accepts_3_lane_without_full(self) -> None:
        """The exact wording of harmonic-forge#322, which was rejected live."""
        body = self.valid_body().replace("## Tooling Exception", "## Scope")
        body = "**3-lane, not Tooling Exception.**\n\n" + body
        issue_post.validate_body(body, {"tech-debt"})

    def test_routing_regex_still_accepts_full_3_lane(self) -> None:
        body = self.valid_body().replace("## Tooling Exception", "## Scope")
        body = "Routed through the full 3-lane cycle.\n\n" + body
        issue_post.validate_body(body, {"tech-debt"})

    def test_fill_mode_still_enforces_the_full_schema(self) -> None:
        """The regression case: a stub must still get the new-issue schema."""
        with self.assertRaises(SystemExit) as caught:
            issue_post.validate_body(self.amendable_body(), {"tech-debt"})
        self.assertIn("Selected Approach", str(caught.exception))

    def test_routing_rejects_a_filename_mention(self) -> None:
        """`3-lane-protocol.md` cited as a path is not a routing declaration."""
        self.assertFalse(issue_post.declares_routing("See `3-lane-protocol.md:648`."))

    def test_routing_rejects_an_incidental_mention(self) -> None:
        self.assertFalse(issue_post.declares_routing("This is the 3-lane protocol and the board discipline."))

    def test_routing_accepts_real_declarations(self) -> None:
        for line in (
            "**3-lane, not Tooling Exception.** Every session starts here.",
            "Routed through the full 3-lane cycle.",
            "**Full 3-lane routing, not Tooling Exception.**",
        ):
            self.assertTrue(issue_post.declares_routing(line), line)

    def test_amend_rejects_unfilled_template_placeholder(self) -> None:
        with self.assertRaises(SystemExit):
            issue_post.validate_amendment("## Scope\n\n{labels}\n", "x" * 20, True)

    def test_amend_keeps_the_supersedes_rule(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            issue_post.validate_amendment("This supersedes the earlier description.", "x" * 20, True)
        self.assertIn("issue number", str(caught.exception))
        issue_post.validate_amendment("This supersedes #1209's earlier description.", "x" * 20, True)

    def _run_main(self, current_body: str, argv_extra: list[str]) -> str:
        """Drive main() with gh calls mocked; report which validator ran.

        Dispatches on the command rather than a fixed response list -- amend
        mode never calls issue_labels(), so a positional list silently shifts
        the PATCH response onto the refetch and fails for the wrong reason.
        """
        body_reads = []

        def fake_run(*args, **kwargs):
            if "PATCH" in args:
                return subprocess.CompletedProcess([], 0, "", "")
            if ".labels[].name" in args:
                return subprocess.CompletedProcess([], 0, "tech-debt\n", "")
            body_reads.append(1)
            first = len(body_reads) == 1
            return subprocess.CompletedProcess([], 0, current_body if first else "BODY", "")

        with patch.object(issue_post, "regular_body", return_value="BODY"), \
             patch.object(issue_post, "reject_reserved_marker"), \
             patch.object(issue_post, "validate_body") as fill, \
             patch.object(issue_post, "validate_amendment") as amend, \
             patch.object(issue_post, "run", side_effect=fake_run), \
             patch.object(sys, "argv", ["post_lane1_issue.py", "--issue", "1", "--body-file", _body_file()] + argv_extra):
            issue_post.main()
        return "amend" if amend.called else "fill" if fill.called else "neither"

    def test_stub_filed_with_a_scoping_note_still_gets_the_full_schema(self) -> None:
        """a private-repo incident review finding: --body-file is the preferred filing route,
        so a stub whose body is a short scoping note must NOT infer amend."""
        note = issue_post.PLACEHOLDER_BODY + "\n\nSee the epic for scope."
        self.assertEqual(self._run_main(note, []), "fill")

    def test_placeholder_body_infers_fill(self) -> None:
        self.assertEqual(self._run_main(issue_post.PLACEHOLDER_BODY, []), "fill")

    def test_empty_body_infers_fill(self) -> None:
        self.assertEqual(self._run_main("", []), "fill")

    def test_substantive_body_infers_amend(self) -> None:
        self.assertEqual(self._run_main("## Scope\n\nReal content.\n", []), "amend")

    def test_amend_flag_forces_amend_on_a_stub(self) -> None:
        self.assertEqual(self._run_main(issue_post.PLACEHOLDER_BODY, ["--amend"]), "amend")

    def test_edits_and_refetches_matching_body(self) -> None:
        body = self.valid_body()
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, body + "\n", ""),
        ]
        with patch.object(issue_post, "run", side_effect=responses) as mocked:
            issue_post.edit_issue_body("vitalharmony/hrse", 483, Path("body.md"), body)

        self.assertEqual(
            mocked.call_args_list[0].args[:4],
            ("gh", "api", "-X", "PATCH"),
        )
        self.assertIn("repos/vitalharmony/hrse/issues/483", mocked.call_args_list[0].args)

    def test_l1_comment_uses_explicit_repo(self) -> None:
        with patch.object(discussion, "regular_body", return_value="Discussion."), \
             patch.object(discussion, "comment_body", return_value=("https://example.test/comment", 1)) as comment, \
             patch.object(sys, "argv", [
                 "post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "123", "--file", _body_file(),
             ]):
            discussion.main()

        self.assertEqual(comment.call_args.args[:2], ("vitalharmony/harmonic-forge", 123))

    def test_marker_records_posting_lane(self) -> None:
        with patch.object(discussion, "regular_body", return_value="Discussion."), \
             patch.object(discussion, "comment_body", return_value=("https://example.test/comment", 1)) as comment, \
             patch.dict("os.environ", {"LANE": "3"}), \
             patch.object(sys, "argv", [
                 "post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "123", "--file", _body_file(),
             ]):
            discussion.main()

        self.assertIn("posted-by=LANE3", comment.call_args.args[2])

    def test_marker_records_unset_lane(self) -> None:
        with patch.object(discussion, "regular_body", return_value="Discussion."), \
             patch.object(discussion, "comment_body", return_value=("https://example.test/comment", 1)) as comment, \
             patch.dict("os.environ", {}, clear=True), \
             patch.object(sys, "argv", [
                 "post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "123", "--file", _body_file(),
             ]):
            discussion.main()

        self.assertIn("posted-by=LANE-unset", comment.call_args.args[2])


class SweepPreExecutionTests(unittest.TestCase):
    """a private-repo incident: a gate-readiness sweep is a PRE-execution artifact.

    It must validate without asserting any per-case outcome, because no case
    has run. The regression this guards: a `TC<n>`-numbered spec used to force
    `- TC<n>: pass|fail|blocked` lines, which could only be satisfied by
    fabricating gate results inside the artifact that attests the gate is ready
    to start. Both a private-repo incident and a private-repo incident were parked on it live.
    """

    SPEC = (
        "### Test cases\n\n"
        "**TC1 - first thing**\nBody.\n\n"
        "**TC2 - second thing**\nBody.\n\n"
        "**TC3 - third thing**\nBody.\n"
    )

    def test_readiness_prose_validates_without_any_outcome(self) -> None:
        sweep = (
            "## Gate-readiness sweep — H1273\n\nWrite tier: R\n\n"
            "### Per-case readiness\n\n"
            "1. **TC1** - ready. Confirmed at foo.py:10.\n"
            "2. **TC2** - ready. Confirmed at foo.py:20.\n"
            "3. **TC3** - ready to run, never executed by anyone.\n"
        )
        l1_post.validate_sweep(sweep, self.SPEC, "vitalharmony/hrse", 1273)  # must not raise

    def test_a_missing_case_still_fails(self) -> None:
        """The ID-match check is rule 3's real content and must survive."""
        sweep = (
            "### Per-case readiness\n\n"
            "1. **TC1** - ready.\n"
            "2. **TC2** - ready.\n"
        )
        with self.assertRaises(SystemExit):
            l1_post.validate_sweep(sweep, self.SPEC, "vitalharmony/hrse", 1273)

    def test_an_extra_case_still_fails(self) -> None:
        sweep = (
            "### Per-case readiness\n\n"
            "1. **TC1** - ready.\n2. **TC2** - ready.\n"
            "3. **TC3** - ready.\n4. **TC4** - ready.\n"
        )
        with self.assertRaises(SystemExit):
            l1_post.validate_sweep(sweep, self.SPEC, "vitalharmony/hrse", 1273)

    def test_a_spec_with_no_cases_still_fails(self) -> None:
        with self.assertRaises(SystemExit):
            l1_post.validate_sweep("### Per-case readiness\n\nnothing\n", "no cases here", "vitalharmony/hrse", 1273)

    def test_a_list_numbered_spec_is_unaffected(self) -> None:
        """The two numbering schemes must behave the same after this change."""
        spec = "### Test cases\n\n1. first thing\n2. second thing\n"
        sweep = (
            "## Gate-readiness sweep — H1273\n\nWrite tier: R\n\n"
            "### Per-case readiness\n\n1. ready, checked.\n2. ready, checked.\n"
        )
        l1_post.validate_sweep(sweep, spec, "vitalharmony/hrse", 1273)  # must not raise


if __name__ == "__main__":
    unittest.main()
