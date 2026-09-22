#!/usr/bin/env python3
"""Focused tests for the Lane 1 transport hook and repo targeting."""

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


CANONICAL_HOOK_PATH = ROOT.parents[1] / "tools" / "hooks" / "block_lane1_status_claims.py"


def load_canonical_hook():
    """Load the canonical platform hook directly from this checkout."""
    spec = importlib.util.spec_from_file_location("block_lane1_status_claims", CANONICAL_HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")
hook = load_canonical_hook()


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        """decision() became LANE-aware in harmonic-forge#190/#191 -- most
        tests in this class assume the Lane-1-only deny path (LANE unset),
        which only held incidentally before that change (LANE was
        irrelevant to decision() then). Pin LANE absent by default so
        these tests are hermetic regardless of the ambient LANE of
        whatever session actually runs the suite (harmonic-forge#193 --
        discovered when Lane 2's own LANE=2 session ran this file and
        several denial-path tests broke, since gh issue comment is
        legitimately allowed under LANE=2 now). Tests that need a specific
        LANE value still patch it explicitly within their own body,
        overriding this default for their duration."""
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        os.environ.pop("LANE", None)
        self.addCleanup(patcher.stop)

    def test_blocks_raw_issue_comment_without_a_body(self) -> None:
        result = hook.decision("gh issue comment 457", Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_blocks_raw_issue_create_and_body_edits(self) -> None:
        for command in (
            'gh issue create --title "unsafe" --body "unvalidated"',
            'gh issue edit 483 --body "unvalidated"',
            "gh issue edit 483 --body-file issue.md",
        ):
            result = hook.decision(command, Path.cwd())
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_stub_creation_task(self) -> None:
        self.assertEqual(hook.decision("mise run gh-new-issue --title stub", Path.cwd()), {})

    def test_blocks_raw_posting_api_even_with_copied_marker(self) -> None:
        result = hook.decision('gh api repos/vitalharmony/hrse/issues/457/comments -X POST -f body="<!-- l1-post v1 -->"', Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_unrelated_api_mutation(self) -> None:
        self.assertEqual(hook.decision("gh api repos/vitalharmony/hrse/labels -X POST -f name=bug", Path.cwd()), {})

    def test_blocks_compound_raw_comment(self) -> None:
        result = hook.decision('cd backend && gh issue comment 457 --body "anything"', Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_only_capability_wrappers(self) -> None:
        self.assertEqual(hook.decision("mise run l1-post --issue 457", Path.cwd()), {})
        self.assertEqual(hook.decision("mise run lane-comment --issue 457 --file body.md", Path.cwd()), {})

    def test_blocks_shared_post_comment_for_lane_one(self) -> None:
        with patch.object(hook, "repo_from_cwd", return_value="vitalharmony/hrse"), \
             patch.dict("os.environ", {}, clear=True):
            result = hook.decision("mise run post-comment --issue 457 --file body.md", Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_harmonic_forge_post_comment_wrapper(self) -> None:
        command = (
            "mise run post-comment --repo vitalharmony/harmonic-forge "
            "--issue 125 --file body.md"
        )
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_allows_harmonic_forge_post_comment_from_environment(self) -> None:
        with patch.object(hook, "repo_from_cwd", return_value=None), \
             patch.dict("os.environ", {"GH_REPO": "vitalharmony/harmonic-forge"}):
            self.assertEqual(hook.decision("mise run post-comment --issue 125 --file body.md", Path.cwd()), {})

    def test_allows_harmonic_forge_post_comment_from_inline_environment(self) -> None:
        command = (
            "GH_REPO=vitalharmony/harmonic-forge mise run post-comment "
            "--issue 125 --file body.md"
        )
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_allows_harmonic_forge_wrapper_after_cd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            forge = Path(directory)
            (forge / "mise.toml").write_text(
                'GH_REPO = "vitalharmony/harmonic-forge"\n',
                encoding="utf-8",
            )
            command = (
                f"cd {forge} && mise run post-comment "
                "--issue 125 --file body.md"
            )
            with patch.dict("os.environ", {"GH_REPO": "vitalharmony/hrse"}):
                self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_allows_forge_wrapper_from_forge_cwd_with_stale_hrse_environment(self) -> None:
        with patch.object(hook, "repo_from_cwd", return_value="vitalharmony/harmonic-forge"), \
             patch.dict("os.environ", {"GH_REPO": "vitalharmony/hrse"}):
            self.assertEqual(
                hook.decision("mise run post-comment --issue 125 --file body.md", Path.cwd()),
                {},
            )

    def test_allows_harmonic_forge_post_comment_script(self) -> None:
        command = (
            "python3 tools/gh/post_comment.py --repo vitalharmony/harmonic-forge "
            "--issue 125 --file body.md"
        )
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_blocks_hrse_post_comment_script(self) -> None:
        command = (
            "python3 tools/gh/post_comment.py --repo vitalharmony/hrse "
            "--issue 509 --file body.md"
        )
        result = hook.decision(command, Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_raw_transports_remain_blocked_for_harmonic_forge(self) -> None:
        for command in (
            "gh issue comment 125 --repo vitalharmony/harmonic-forge --body unsafe",
            "gh issue create --repo vitalharmony/harmonic-forge --title unsafe",
        ):
            with self.subTest(command=command):
                result = hook.decision(command, Path.cwd())
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ignores_non_comment_commands(self) -> None:
        self.assertEqual(hook.decision('git status', Path.cwd()), {})

    def test_allows_direct_post_for_lane2(self) -> None:
        with patch.dict("os.environ", {"LANE": "2"}, clear=True):
            self.assertEqual(hook.decision('gh issue comment 230 --body "PASS"', Path.cwd()), {})

    def test_allows_direct_post_for_lane3(self) -> None:
        with patch.dict("os.environ", {"LANE": "3"}, clear=True):
            self.assertEqual(hook.decision('gh issue comment 230 --body "PASS"', Path.cwd()), {})

    def test_blocks_direct_post_for_lane1(self) -> None:
        with patch.dict("os.environ", {"LANE": "1"}, clear=True):
            result = hook.decision('gh issue comment 230 --body "PASS"', Path.cwd())
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_blocks_direct_post_when_lane_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = hook.decision('gh issue comment 230 --body "PASS"', Path.cwd())
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ignores_command_shaped_test_data(self) -> None:
        command = 'python3 scripts/test_l1_post.py --example "gh issue comment 457 --body ready for L3"'
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_rejects_reserved_marker_in_user_body(self) -> None:
        with self.assertRaises(SystemExit):
            post.reject_reserved_marker("<!-- l1-post copied -->")

    def test_malformed_payload_is_denied_without_crashing(self) -> None:
        result = hook.decision(123, Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allows_heredoc_commit_message_with_apostrophe(self) -> None:
        command = '''git commit -m "$(cat <<'EOF'
every Lane 3 runtime's standing instructions
EOF
)"'''
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_heredoc_prose_is_not_checked_as_a_transport(self) -> None:
        command = '''git commit -m "$(cat <<'EOF'
gh issue comment 478 --body-file body.md
EOF
)"'''
        self.assertEqual(hook.decision(command, Path.cwd()), {})

    def test_blocks_transport_after_a_heredoc(self) -> None:
        command = '''git commit -m "$(cat <<'EOF'
runtime's standing instructions
EOF
)" && gh issue comment 478 --body-file body.md'''
        result = hook.decision(command, Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unterminated_heredoc_still_fails_closed(self) -> None:
        command = '''git commit -m "$(cat <<'EOF'
runtime's standing instructions'''
        result = hook.decision(command, Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_blocks_transport_on_a_new_physical_line(self) -> None:
        result = hook.decision('echo hi\ngh issue comment 457 --body "ready for L3"', Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_blocks_transport_after_heredoc_on_a_new_line(self) -> None:
        command = '''cat <<'EOF'
ordinary heredoc prose
EOF
gh issue comment 457 --body "ready for L3"'''
        result = hook.decision(command, Path.cwd())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")


class AeValidationTests(unittest.TestCase):
    """a private-repo incident: AE gains its own l1-post kind, with a reserved-marker
    footer, so `check_lane3_ready.py` can find it by scanning comments."""

    def test_valid_ae_heading_passes(self) -> None:
        post.validate_ae("## AE — H929\n\nApproved, execute the released spec.", "vitalharmony/hrse", 929)

    def test_missing_ae_heading_fails(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_ae("Just approving this in prose, no heading.", "vitalharmony/hrse", 929)

    def test_empty_body_fails(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_ae("", "vitalharmony/hrse", 929)


class AeExactHeadingTests(unittest.TestCase):
    """a private-repo incident: R-0128 (harmonic-forge#496) fixed the READ side
    (check_lane3_ready.py/the lane3-gate skill refuse without the exact
    `<PREFIX><N>` form) but the WRITE side only ever checked the leading
    keyword. Real incident, 2026-09-18 (a private-repo incident): `## AE — Issue
    #1925` passed this validator, then Lane 3 reported L3B on the exact
    heading-format finding this test guards."""

    def test_loose_but_wrong_prefix_form_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_ae("## AE — Issue #1925\n\nApproved.", "vitalharmony/hrse", 1925)

    def test_exact_prefix_form_passes(self) -> None:
        post.validate_ae("## AE — H1925\n\nApproved.", "vitalharmony/hrse", 1925)

    def test_wrong_issue_number_is_refused(self) -> None:
        # A heading naming a DIFFERENT issue's number must not pass just
        # because it's shaped correctly -- the number is load-bearing, not
        # decorative.
        with self.assertRaises(SystemExit):
            post.validate_ae("## AE — H1924\n\nApproved.", "vitalharmony/hrse", 1925)

    def test_trailing_text_after_the_number_is_still_accepted(self) -> None:
        post.validate_ae(
            "## AE — H1925 (approved after correction)\n\nApproved.",
            "vitalharmony/hrse", 1925,
        )

    def test_unrecognized_repo_falls_back_to_the_loose_check_only(self) -> None:
        # `_repo_prefix` returns None for a repo the table doesn't know --
        # refusing every post from an unrecognized repo would be a worse
        # failure than accepting a loosely-shaped heading, matching
        # batch_auth's own fail-open posture this reuses.
        post.validate_ae("## AE — Issue #1", "vitalharmony/some-other-repo", 1)

    def test_correct_heading_quoted_later_does_not_satisfy_a_malformed_actual_heading(self) -> None:
        # a private-repo incident preclose finding: an unanchored search only proved the
        # canonical form appears SOMEWHERE, including inside a fenced
        # example of what the heading SHOULD look like -- exactly the shape
        # the real a private-repo incident correction comments used. The body's own
        # first line is still the malformed one and must still be refused.
        with self.assertRaises(SystemExit):
            post.validate_ae(
                "## AE — Issue #1925\n\n"
                "Note: the required heading form is\n\n"
                "```\n## AE — H1925\n```\n",
                "vitalharmony/hrse", 1925,
            )

    def test_a_sibling_ae_heading_quoted_in_a_details_block_does_not_satisfy_it_either(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_ae(
                "## AE — Issue #1925\n\n<details>\n## AE — H1925\n</details>",
                "vitalharmony/hrse", 1925,
            )


class SweepExactHeadingTests(unittest.TestCase):
    """a private-repo incident preclose finding: `AeExactHeadingTests` above exercised
    `validate_ae` only -- `validate_sweep`'s independent copy of the exact-
    heading check had zero negative coverage; deleting it left the full
    suite green. R-0128/testing-gate.md's own mandate is specifically the
    SWEEP heading (the AE form is only in R-0336), so this half matters at
    least as much as the AE half above, not less."""

    SPEC = "### Test cases\n- TC1: a\n"

    def _sweep(self, heading_line: str) -> str:
        return f"{heading_line}\n\nWrite tier: R\n\n- TC1 — ready\n"

    def test_loose_but_wrong_prefix_form_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_sweep(
                self._sweep("## Gate-Readiness Sweep — Issue #1925"),
                self.SPEC, "vitalharmony/hrse", 1925,
            )

    def test_exact_prefix_form_passes(self) -> None:
        post.validate_sweep(
            self._sweep("## Gate-readiness sweep — H1925"),
            self.SPEC, "vitalharmony/hrse", 1925,
        )

    def test_wrong_issue_number_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_sweep(
                self._sweep("## Gate-readiness sweep — H1924"),
                self.SPEC, "vitalharmony/hrse", 1925,
            )

    def test_trailing_text_after_the_number_is_still_accepted(self) -> None:
        post.validate_sweep(
            self._sweep("## Gate-readiness sweep — H1925 (respec, targets abc)"),
            self.SPEC, "vitalharmony/hrse", 1925,
        )

    def test_correct_heading_quoted_later_does_not_satisfy_a_malformed_actual_heading(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_sweep(
                "## Gate-Readiness Sweep — Issue #1925\n\nWrite tier: R\n\n"
                "The required form is:\n```\n## Gate-readiness sweep — H1925\n```\n\n"
                "- TC1 — ready\n",
                self.SPEC, "vitalharmony/hrse", 1925,
            )

    def test_a_larger_issue_number_is_not_absorbed_as_a_suffix(self) -> None:
        # a private-repo incident preclose finding: the `\b` boundary's job -- H1925 must
        # not accept when the real heading names H19255 -- was asserted by
        # no test. This drives the same failure the AE side's
        # `test_wrong_issue_number_is_refused` covers, on the sweep side.
        with self.assertRaises(SystemExit):
            post.validate_sweep(
                self._sweep("## Gate-readiness sweep — H19255"),
                self.SPEC, "vitalharmony/hrse", 1925,
            )


class RepoTargetTests(unittest.TestCase):
    def test_comment_target_accepts_the_requested_repo_and_issue(self) -> None:
        self.assertEqual(
            post.validate_comment_target(
                "https://github.com/vitalharmony/harmonic-forge/issues/123#issuecomment-456",
                "vitalharmony/harmonic-forge",
                123,
            ),
            456,
        )

    def test_comment_target_rejects_a_silent_wrong_repo(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_comment_target(
                "https://github.com/vitalharmony/hrse/issues/123#issuecomment-456",
                "vitalharmony/harmonic-forge",
                123,
            )

    def test_l1_post_uses_explicit_repo_instead_of_environment(self) -> None:
        # a private-repo incident: a handoff's lead region (before its first `###`) must
        # now carry Scope/Next — incidental to this test's subject (repo
        # targeting), same footnote as the --plan-first flag below.
        body = "**Scope:** repo targeting test.\n**Next:** n/a.\n\n" + "\n".join(
            f"### {heading}\nA documented value." for heading in post.HANDOFF_HEADINGS
        )
        with patch.object(post, "regular_body", return_value=body), \
             patch.object(post, "resolve_sha", return_value="a" * 40), \
             patch.object(post, "validate_tier_set"), \
             patch.object(post, "validate_milestone_set"), \
             patch.object(post, "world_checks", return_value=(["world"], [])), \
             patch.object(post, "comment_body", return_value=("https://example.test/comment", 1)) as comment, \
             patch.object(post, "write_receipt"), \
             patch.object(sys, "argv", [
                 "l1_post.py", "--repo", "vitalharmony/harmonic-forge", "--issue", "123",
                 # a private-repo incident: a handoff must now declare Plan-First. This
                 # test's subject is repo targeting, so the value is
                 # incidental — but omitting the flag makes `main()` refuse
                 # before it reaches the code under test, which would turn a
                 # repo-targeting regression into a silent pass.
                 "--kind", "handoff", "--plan-first", "false",
                 "--sha", "HEAD", "--branch", "test-branch", "--file", "body.md",
             ]):
            post.main()

        self.assertEqual(comment.call_args.args[:2], ("vitalharmony/harmonic-forge", 123))


class EstimateGateTests(unittest.TestCase):
    """a private-repo incident: handoff posting must hard-fail when the target issue's
    board Estimate is unset, and no-op cleanly on a repo with no board."""

    def test_resolve_project_board_keys_on_repo_not_cwd(self) -> None:
        """The real bug this guards against: a --cross-repo handoff run
        from an HRSE2 mise environment must resolve harmonic-forge's own
        board, not silently fall back to HRSE2's, even though cwd never
        changes for the duration of the process. Uses real temp git repos
        (not mocked Path internals) so the actual git plumbing is exercised."""
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            projects = home / "Harmonic_Projects"
            projects.mkdir()
            hrse_cwd = projects / "HRSE2"
            forge_sibling = home / "harmonic-forge"
            for root, remote in ((hrse_cwd, "vitalharmony/hrse"), (forge_sibling, "vitalharmony/harmonic-forge")):
                root.mkdir()
                self.assertEqual(post.run("git", "init", "-q", cwd=root).returncode, 0)
                self.assertEqual(
                    post.run("git", "remote", "add", "origin", f"https://github.com/{remote}.git", cwd=root).returncode,
                    0,
                )

            with patch.object(Path, "home", return_value=home):
                root = post._find_repo_root("vitalharmony/harmonic-forge", hrse_cwd)
            self.assertEqual(root, forge_sibling)

            with patch.object(Path, "home", return_value=home):
                same = post._find_repo_root("vitalharmony/hrse", hrse_cwd)
            self.assertEqual(same, hrse_cwd)

    # a private-repo incident: `create=True` on these patches is deliberate. `_item_list_cache`
    # resolves to the *installed* ~/harmonic-forge sibling, whose version is not
    # controlled by this repo's checkout — so asserting the attribute exists here
    # would make these tests fail purely on cross-repo merge ordering. What they
    # verify is l1_post's own delegation logic; the case where the sibling really
    # lacks the function has its own test (falls_back_on_stale_sibling), which is
    # where that behaviour belongs.

    def test_resolve_board_tier_finds_a_set_value(self) -> None:
        """a private-repo incident: delegates to the targeted per-issue query, not a board scan."""
        with patch.object(post._item_list_cache, "fetch_issue_tier", create=True) as targeted:
            targeted.return_value = "deep"
            self.assertEqual(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 627), "deep")
            targeted.return_value = None
            self.assertIsNone(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 46))
        # repo and project number both reach the targeted query -- an issue on
        # two boards must be read against the right one.
        self.assertEqual(targeted.call_args[0][0], "vitalharmony/hrse")
        self.assertEqual(targeted.call_args[0][2], "1")

    def test_resolve_board_tier_does_not_fetch_the_board(self) -> None:
        """The #802 regression guard: no `project item-list` shell-out."""
        with patch.object(post._item_list_cache, "fetch_issue_tier", return_value="standard", create=True), \
             patch.object(post, "run") as run_call:
            self.assertEqual(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 627), "standard")
        run_call.assert_not_called()

    def test_resolve_board_tier_falls_back_on_stale_sibling(self) -> None:
        """A harmonic-forge checkout predating #802 must still gate correctly."""
        payload = json.dumps({"items": [
            {"content": {"number": 627}, "estimate": 8},
            {"content": {"number": 46}, "estimate": None},
        ]})
        stale = SimpleNamespace()  # no fetch_issue_tier attribute
        with patch.object(post, "_item_list_cache", stale), \
             patch.object(post, "run", return_value=SimpleNamespace(returncode=0, stdout=payload, stderr="")):
            self.assertEqual(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 627), "deep")
            self.assertIsNone(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 46))
            self.assertIsNone(post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 9999))

    def test_resolve_board_tier_fails_loud_on_query_error(self) -> None:
        """A quota error must abort the post, never read as 'estimate unset'."""
        err = post._item_list_cache.GhItemListError("rate limited")
        with patch.object(post._item_list_cache, "fetch_issue_tier", side_effect=err, create=True):
            with self.assertRaises(SystemExit):
                post.resolve_board_tier("vitalharmony/hrse", "vitalharmony", "1", 627)

    def test_validate_tier_set_noops_without_a_board(self) -> None:
        with patch.object(post, "resolve_project_board", return_value=None), \
             patch.object(post, "resolve_board_tier") as estimate_call:
            post.validate_tier_set("vitalharmony/openclaw-projects", 2)
        estimate_call.assert_not_called()

    def test_validate_tier_set_passes_when_estimate_present(self) -> None:
        with patch.object(post, "resolve_project_board", return_value=("vitalharmony", "1")), \
             patch.object(post, "resolve_board_tier", return_value="deep"):
            post.validate_tier_set("vitalharmony/hrse", 627)  # must not raise

    def test_validate_tier_set_fails_when_unset(self) -> None:
        with patch.object(post, "resolve_project_board", return_value=("vitalharmony", "1")), \
             patch.object(post, "resolve_board_tier", return_value=None):
            with self.assertRaises(SystemExit):
                post.validate_tier_set("vitalharmony/hrse", 671)


class RestMigrationTests(unittest.TestCase):
    """harmonic-forge#220: issue_is_open()/comment_body() must use REST,
    and issue_is_open() must compare against REST's lowercase state value
    (GraphQL's `gh issue view` returned uppercase "OPEN"/"CLOSED"; REST's
    `gh api .../issues/{n}` returns lowercase "open"/"closed" -- confirmed
    live, 2026-08-11 -- a same-string comparison against the old constant
    would silently always fail)."""

    def test_issue_is_open_uses_rest_and_lowercase_state(self) -> None:
        with patch.object(post, "run", return_value=SimpleNamespace(returncode=0, stdout="open\n", stderr="")) as mocked:
            post.issue_is_open("vitalharmony/hrse", 730)  # must not raise
        args = mocked.call_args.args
        self.assertEqual(args[:2], ("gh", "api"))
        self.assertIn("repos/vitalharmony/hrse/issues/730", args)

    def test_issue_is_open_rejects_uppercase_open(self) -> None:
        # The comparison is deliberately case-sensitive against REST's real
        # lowercase value. If `gh api` ever returned the old GraphQL-style
        # uppercase "OPEN" (or a future edit reverted the comparison), this
        # must fail loudly, not silently accept it.
        with patch.object(post, "run", return_value=SimpleNamespace(returncode=0, stdout="OPEN\n", stderr="")):
            with self.assertRaises(SystemExit):
                post.issue_is_open("vitalharmony/hrse", 730)

    def test_comment_body_uses_rest_endpoint(self) -> None:
        responses = [
            SimpleNamespace(returncode=0, stdout="https://github.com/vitalharmony/hrse/issues/730#issuecomment-1\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="hello\n", stderr=""),
        ]
        with patch.object(post, "run", side_effect=responses) as mocked:
            url, comment_id = post.comment_body("vitalharmony/hrse", 730, "hello")
        first_call_args = mocked.call_args_list[0].args
        self.assertEqual(first_call_args[:2], ("gh", "api"))
        self.assertIn("repos/vitalharmony/hrse/issues/730/comments", first_call_args)
        self.assertEqual(comment_id, 1)


class SiblingOverlapTests(unittest.TestCase):
    """a private-repo incident: the sibling-overlap guard fired on mechanically-generated
    files and on worktrees parked on already-merged branches.

    These run real git against a real temp repo with a bare `file://` origin
    -- not mocks. `world_checks()` calls live GitHub state (`issue_is_open`,
    `git ls-remote`) before reaching the overlap logic, so the comparison is
    only reachable through the extracted `sibling_overlaps()`.
    """

    def _git(self, repo: Path, *args: str) -> str:
        result = post.run("git", *args, cwd=repo)
        self.assertEqual(result.returncode, 0, f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def _write(self, repo: Path, path: str, content: str) -> None:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _commit(self, repo: Path, message: str, files: dict[str, str]) -> str:
        for path, content in files.items():
            self._write(repo, path, content)
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", message)
        return self._git(repo, "rev-parse", "HEAD")

    def _repo(self, stack: contextlib.ExitStack) -> Path:
        """A work repo whose `origin/main` genuinely resolves, via a local bare
        origin. The existing temp-repo tests in this file never fetch, but the
        merged-sibling predicate compares against a real `origin/main` ref."""
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        origin, work = tmp / "origin.git", tmp / "work"
        origin.mkdir()
        work.mkdir()
        self._git(origin, "init", "-q", "--bare", "--initial-branch=main", ".")
        self._git(work, "init", "-q", "--initial-branch=main", ".")
        self._git(work, "config", "user.email", "test@example.invalid")
        self._git(work, "config", "user.name", "Test")
        self._git(work, "remote", "add", "origin", f"file://{origin}")
        self._commit(work, "base", {"src/app.py": "print('base')\n", "transaction-log.md": "# log\n"})
        self._git(work, "push", "-q", "origin", "main")
        self._git(work, "fetch", "-q", "origin")
        return work

    def _branch(self, repo: Path, name: str, files: dict[str, str], *, base: str = "main") -> None:
        self._git(repo, "checkout", "-q", base)
        self._git(repo, "checkout", "-q", "-b", name)
        self._commit(repo, f"work on {name}", files)
        self._git(repo, "checkout", "-q", "main")

    def _target_files(self, repo: Path, branch: str) -> set[str]:
        base = self._git(repo, "merge-base", "origin/main", branch)
        return post.changed_files(base, self._git(repo, "rev-parse", branch), cwd=repo)

    def test_mechanical_paths_alone_do_not_conflict(self):
        """AC1: the guaranteed-collision class. Every lane branch touches both
        of these on every commit, so any two branches always collided."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            for name in ("feat/one", "feat/two"):
                self._branch(repo, name, {
                    "transaction-log.md": f"# log\n\n## {name}\n",
                    "frontend/package.json": '{"version": "2.6.%d"}\n' % len(name),
                })
            conflicts = post.sibling_overlaps(
                self._target_files(repo, "feat/one"), ["feat/two"], cwd=repo
            )
        self.assertEqual(conflicts, [])

    def test_genuine_source_overlap_still_conflicts(self):
        """AC4: the regression guard. Do not overcorrect into never blocking."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            for name, body in (("feat/one", "one"), ("feat/two", "two")):
                self._branch(repo, name, {
                    "src/app.py": f"print('{body}')\n",
                    "transaction-log.md": f"# log\n\n## {name}\n",
                })
            conflicts = post.sibling_overlaps(
                self._target_files(repo, "feat/one"), ["feat/two"], cwd=repo
            )
        self.assertEqual(conflicts, [("feat/two", {"src/app.py"})])
        self.assertNotIn("transaction-log.md", conflicts[0][1])

    def test_package_lock_is_never_excluded(self):
        """AC5: `package-lock.json` is the residual guard that makes excluding
        `package.json` safe -- `bump_version.py` never writes the lock file, so
        a real dependency conflict still co-signals there."""
        self.assertEqual(post._MECHANICAL_PATHS, {"transaction-log.md", "frontend/package.json"})
        self.assertNotIn("frontend/package-lock.json", post._MECHANICAL_PATHS)

    def test_sibling_merged_by_merge_commit_is_skipped(self):
        """AC2, the shape that `merge-base --is-ancestor` does handle."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/merged", {"src/app.py": "print('merged')\n"})
            self._git(repo, "merge", "--no-ff", "-q", "-m", "merge feat/merged", "feat/merged")
            self._git(repo, "push", "-q", "origin", "main")
            self._git(repo, "fetch", "-q", "origin")
            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            merged_sha = self._git(repo, "rev-parse", "feat/merged")
            merged_base = self._git(repo, "merge-base", "origin/main", merged_sha)
            # Assert the predicate itself, not just the aggregate: a true
            # ancestor has an empty `changed_files()`, so this case would pass
            # vacuously even with a predicate that always returned False.
            predicate = post._already_upstream(merged_sha, merged_base, "origin/main", cwd=repo)
            conflicts = post.sibling_overlaps(
                self._target_files(repo, "feat/target"), ["feat/merged"], cwd=repo
            )
        self.assertTrue(predicate, "_already_upstream must recognise a merge-commit-merged sibling")
        self.assertEqual(conflicts, [])

    def test_sibling_merged_by_squash_is_skipped(self):
        """AC2, the shape this repo actually produces -- and the one the
        original `merge-base --is-ancestor` design silently missed.

        A squash merge writes a new single-parent commit with no ancestry link
        to the branch, so `--is-ancestor` is false for every branch merged
        under the standing PR flow. Required passing assertion, not an
        expected failure: detecting this case is the entire reason the
        predicate is `commit-tree` + `git cherry`.
        """
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/squashed", {
                "src/app.py": "print('squashed')\n",
                "src/extra.py": "print('extra')\n",
            })
            self._git(repo, "merge", "--squash", "-q", "feat/squashed")
            self._git(repo, "commit", "-q", "-m", "squashed feat/squashed (#1)")
            self._git(repo, "push", "-q", "origin", "main")
            self._git(repo, "fetch", "-q", "origin")

            squashed_sha = self._git(repo, "rev-parse", "feat/squashed")
            self.assertNotEqual(
                post.run("git", "merge-base", "--is-ancestor", squashed_sha, "origin/main",
                         cwd=repo).returncode,
                0,
                "fixture invalid: the squashed branch must NOT be an ancestor of origin/main",
            )

            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            squashed_base = self._git(repo, "merge-base", "origin/main", squashed_sha)
            predicate = post._already_upstream(squashed_sha, squashed_base, "origin/main", cwd=repo)
            conflicts = post.sibling_overlaps(
                self._target_files(repo, "feat/target"), ["feat/squashed"], cwd=repo
            )
        self.assertTrue(predicate, "_already_upstream must recognise a squash-merged sibling")
        self.assertEqual(conflicts, [])

    def test_unmerged_divergent_sibling_still_conflicts(self):
        """AC4 negative control for the merged-sibling skip: a branch whose
        work is genuinely not upstream must still block."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/divergent", {"src/app.py": "print('divergent')\n"})
            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            conflicts = post.sibling_overlaps(
                self._target_files(repo, "feat/target"), ["feat/divergent"], cwd=repo
            )
        self.assertEqual(conflicts, [("feat/divergent", {"src/app.py"})])

    def test_recorded_incident_overlap_sets_no_longer_block(self):
        """AC6: replay the four incidents' recorded overlap sets structurally.
        Every incident's error message named only these paths -- no real
        source file -- so the exclusion alone clears all four."""
        for incident, overlap in (
            (1, {"transaction-log.md"}),
            (2, {"transaction-log.md"}),
            (3, {"transaction-log.md"}),
            (4, {"transaction-log.md", "frontend/package.json"}),
        ):
            with self.subTest(incident=incident):
                self.assertEqual(overlap - post._MECHANICAL_PATHS, set())



class AckOverlapTests(SiblingOverlapTests):
    """a private-repo incident: an explicit, human-supplied override for a genuine sibling
    overlap -- downgrades world_checks()'s hard fail to a durable warning
    appended into the posted comment, without weakening the default (no
    ack) path. Reuses SiblingOverlapTests' git fixture helpers; world_checks()
    itself has no `cwd` parameter (it runs against the process's real repo),
    so these tests chdir into the fixture for the call's duration."""

    def _world_checks_in(
        self, repo: Path, branch: str, *, ack_overlap: str | None, sibling_branches: tuple[str, ...] = ()
    ):
        """world_checks() finds siblings via `git worktree list`, not an
        explicit argument -- unlike sibling_overlaps() -- so any sibling this
        test needs detected must be a real worktree, not just a local branch."""
        sha = self._git(repo, "rev-parse", branch)
        self._git(repo, "push", "-q", "origin", f"{branch}:{branch}")
        worktree_dirs = []
        for sibling in sibling_branches:
            wt_dir = repo.parent / f"wt-{sibling.replace('/', '-')}"
            self._git(repo, "worktree", "add", "-q", str(wt_dir), sibling)
            worktree_dirs.append(wt_dir)
        previous = Path.cwd()
        os.chdir(repo)
        try:
            with patch.object(post, "issue_is_open"):
                return post.world_checks(
                    "vitalharmony/hrse", 1108, sha, branch, ack_overlap=ack_overlap
                )
        finally:
            os.chdir(previous)
            for wt_dir in worktree_dirs:
                self._git(repo, "worktree", "remove", "--force", str(wt_dir))

    def test_no_ack_still_hard_fails_on_genuine_overlap(self):
        """Existing behaviour, unchanged: overlap with no override still fails."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/divergent", {"src/app.py": "print('divergent')\n"})
            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            with self.assertRaises(SystemExit):
                self._world_checks_in(repo, "feat/target", ack_overlap=None, sibling_branches=("feat/divergent",))

    def test_ack_overlap_downgrades_to_a_warning_and_still_posts(self):
        """A genuine overlap with an override must post successfully, and the
        override reason must be present in the returned warning text."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/divergent", {"src/app.py": "print('divergent')\n"})
            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            checks, warnings = self._world_checks_in(
                repo, "feat/target", ack_overlap="reviewed live, safe to proceed (H1108 test)",
                sibling_branches=("feat/divergent",),
            )
        self.assertIn("active-worktree-overlap", checks)
        self.assertEqual(len(warnings), 1)
        self.assertIn("feat/divergent", warnings[0])
        self.assertIn("reviewed live, safe to proceed (H1108 test)", warnings[0])

    def test_ack_overlap_is_a_no_op_when_there_is_no_overlap(self):
        """An override supplied with nothing to override produces no warning --
        this is additive, not a general-purpose bypass."""
        with contextlib.ExitStack() as stack:
            repo = self._repo(stack)
            self._branch(repo, "feat/target", {"src/app.py": "print('target')\n"})
            checks, warnings = self._world_checks_in(
                repo, "feat/target", ack_overlap="not needed here"
            )
        self.assertIn("active-worktree-overlap", checks)
        self.assertEqual(warnings, [])

    def test_empty_ack_overlap_reason_is_rejected_at_the_cli(self):
        """--ack-overlap requires a non-empty human justification -- never a
        bare boolean escape hatch (per the issue's selected approach)."""
        with patch.object(sys, "argv", [
            "l1_post.py", "--repo", "vitalharmony/hrse", "--issue", "1", "--kind", "ae",
            "--sha", "HEAD", "--branch", "x", "--file", "body.md", "--ack-overlap", "   ",
        ]):
            with self.assertRaises(SystemExit):
                post.main()


class MilestoneGateTests(unittest.TestCase):
    """harmonic-forge#283: a handoff must not post without release membership."""

    def test_no_ops_on_a_repo_that_uses_no_milestones(self):
        # AC5: harmonic-forge/openclaw-projects are never gated — there is
        # nothing to enforce against, and requiredness is derived live.
        with patch.object(post, "repo_milestone_titles", return_value=[]), \
             patch.object(post, "issue_milestone_title") as issue_read:
            post.validate_milestone_set("vitalharmony/harmonic-forge", 283)
        issue_read.assert_not_called()

    def test_passes_when_the_issue_carries_a_milestone(self):
        with patch.object(post, "repo_milestone_titles", return_value=["2.7", "Later"]), \
             patch.object(post, "issue_milestone_title", return_value="2.7"):
            post.validate_milestone_set("vitalharmony/hrse", 950)

    def test_passes_on_the_Later_sentinel(self):
        with patch.object(post, "repo_milestone_titles", return_value=["2.7", "Later"]), \
             patch.object(post, "issue_milestone_title", return_value="Later"):
            post.validate_milestone_set("vitalharmony/hrse", 327)

    def test_refuses_and_names_the_fix_command_when_unset(self):
        with patch.object(post, "repo_milestone_titles", return_value=["2.7", "Later"]), \
             patch.object(post, "issue_milestone_title", return_value=None):
            with self.assertRaises(SystemExit) as caught:
                post.validate_milestone_set("vitalharmony/hrse", 999)
        message = str(caught.exception)
        # NC4: the flip-on moment must be a one-line unblock, not a puzzle.
        self.assertIn("has no milestone set", message)
        self.assertIn("-X PATCH", message)
        self.assertIn("Later", message)

    def test_repo_milestone_titles_fails_loud_on_query_error(self):
        """NC1: a failed query must not read as "this repo has none"."""
        with patch.object(post, "run", return_value=MagicMock(returncode=1, stdout="", stderr="bad creds")):
            with self.assertRaises(SystemExit) as caught:
                post.repo_milestone_titles("vitalharmony/hrse")
        self.assertIn("cannot read milestones", str(caught.exception))

    def test_repo_milestone_titles_parses_titles(self):
        with patch.object(post, "run", return_value=MagicMock(returncode=0, stdout="2.7\nLater\n", stderr="")):
            self.assertEqual(post.repo_milestone_titles("vitalharmony/hrse"), ["2.7", "Later"])


if __name__ == "__main__":
    unittest.main()
