#!/usr/bin/env python3
"""Unit tests for deny_advisory_subagent_gh_writes.py (harmonic-forge#237).
Run: python3 tools/hooks/test_deny_advisory_subagent_gh_writes.py"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deny_advisory_subagent_gh_writes as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestDenyCases(unittest.TestCase):
    def _assert_denied(self, command: str) -> None:
        result = m.decision(command)
        self.assertTrue(_is_denied(result), f"expected denial for: {command}")

    def test_gh_issue_close(self):
        self._assert_denied("gh issue close 1")

    def test_gh_pr_merge(self):
        self._assert_denied("gh pr merge 1")

    def test_gh_api_explicit_post(self):
        self._assert_denied("gh api -X POST repos/o/r/issues/1/comments")

    def test_gh_api_flagless_parameterized_post(self):
        self._assert_denied('gh api repos/o/r/issues/1/comments -f body="hi"')

    def test_gh_api_input_flag(self):
        self._assert_denied("gh api repos/o/r/issues/1/comments --input -")

    def test_gh_api_concatenated_xpost(self):
        self._assert_denied("gh api -XPOST repos/o/r/issues/1/comments")

    def test_gh_api_graphql_mutation_document(self):
        self._assert_denied("gh api graphql -f query='mutation{addComment(input:{})}'")

    def test_gh_api_graphql_query_from_file(self):
        self._assert_denied("gh api graphql -F query=@mutation.graphql")

    def test_bash_c_smuggled_command(self):
        self._assert_denied('bash -c "gh issue close 1"')

    def test_post_comment_wrapper_direct(self):
        self._assert_denied("python3 tools/gh/post_comment.py --repo o/r --issue 1 --file x.md")

    def test_l1_comment_mise_task(self):
        self._assert_denied("mise run l1-comment --issue 1 --file x.md")

    def test_gh_as_wrapped_mutation(self):
        self._assert_denied("gh-as someaccount gh issue close 1")

    def test_gh_issue_comment(self):
        self._assert_denied('gh issue comment 1 --body "hi"')

    def test_gh_issue_create(self):
        self._assert_denied('gh issue create --title "x"')

    def test_gh_pr_create(self):
        self._assert_denied('gh pr create --title "x"')

    def test_gh_project_item_add(self):
        self._assert_denied("gh project item-add 1 --owner o --url http://x")

    def test_gh_release_create(self):
        self._assert_denied("gh release create v1.0.0")

    def test_gh_repo_delete(self):
        self._assert_denied("gh repo delete o/r")

    def test_sh_c_smuggled_command(self):
        self._assert_denied('sh -c "gh pr merge 1"')

    def test_eval_smuggled_command(self):
        self._assert_denied('eval "gh issue close 1"')

    def test_xargs_smuggled_command(self):
        self._assert_denied('echo "1" | xargs gh issue close')

    def test_gh_issue_edit(self):
        self._assert_denied('gh issue edit 1 --add-label bug')

    def test_gh_pr_close(self):
        self._assert_denied("gh pr close 1")

    def test_post_comment_mise_task(self):
        self._assert_denied("mise run post-comment --issue 1 --file x.md")

    def test_gh_new_issue_mise_task(self):
        self._assert_denied('mise run gh-new-issue --title "x"')

    def test_lane_comment_mise_task(self):
        self._assert_denied("mise run lane-comment --issue 1 --file x.md")

    def test_gh_issue_py_wrapper(self):
        self._assert_denied("python3 tools/gh/gh_issue.py --title x")

    def test_second_of_two_chained_commands_denied(self):
        """A benign first command followed by a mutating second must still
        be caught — command_segments() splits on `&&`/`;`/`|`, and every
        segment is classified independently."""
        self._assert_denied("gh issue view 1 && gh issue close 1")


class TestPermitCases(unittest.TestCase):
    def _assert_permitted(self, command: str) -> None:
        result = m.decision(command)
        self.assertFalse(_is_denied(result), f"expected permit for: {command}")

    def test_gh_issue_view(self):
        self._assert_permitted("gh issue view 1")

    def test_gh_issue_list(self):
        self._assert_permitted("gh issue list")

    def test_gh_pr_view(self):
        self._assert_permitted("gh pr view 1")

    def test_gh_api_explicit_get(self):
        self._assert_permitted("gh api -X GET repos/o/r/issues/1")

    def test_gh_api_bare_parameterless(self):
        self._assert_permitted("gh api repos/o/r/issues/1")

    def test_gh_api_graphql_query_document(self):
        self._assert_permitted("gh api graphql -f query='query{viewer{login}}'")

    def test_gh_pr_list(self):
        self._assert_permitted("gh pr list")

    def test_gh_pr_diff(self):
        self._assert_permitted("gh pr diff 1")

    def test_gh_pr_checks(self):
        self._assert_permitted("gh pr checks 1")

    def test_gh_search(self):
        self._assert_permitted("gh search issues --owner o repo:r foo")

    def test_gh_repo_view(self):
        self._assert_permitted("gh repo view o/r")

    def test_gh_label_list(self):
        self._assert_permitted("gh label list")

    def test_gh_release_view(self):
        self._assert_permitted("gh release view v1.0.0")

    def test_gh_release_list(self):
        self._assert_permitted("gh release list")

    def test_unrelated_command(self):
        self._assert_permitted("ls -la")

    def test_git_log_unaffected(self):
        self._assert_permitted("git log --oneline -5")

    def test_gh_as_wrapped_read(self):
        self._assert_permitted("gh-as someaccount gh issue view 1")

    def test_env_prefixed_read(self):
        self._assert_permitted("GH_REPO=o/r gh issue view 1")


class TestFailClosedOnMalformedPayload(unittest.TestCase):
    def test_non_string_command_fails_closed(self):
        result = m.decision(None)
        self.assertTrue(_is_denied(result))

    def test_malformed_stdin_fails_closed(self):
        import io
        from unittest.mock import patch

        with patch("sys.stdin", io.StringIO("not json")):
            with patch("builtins.print") as mock_print:
                m.main()
        printed = mock_print.call_args.args[0]
        import json as _json
        self.assertTrue(_is_denied(_json.loads(printed)))


class TestNonBashToolIsNoop(unittest.TestCase):
    def test_non_bash_tool_permits(self):
        import io
        import json as _json
        from unittest.mock import patch

        payload = _json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        with patch("sys.stdin", io.StringIO(payload)):
            with patch("builtins.print") as mock_print:
                m.main()
        printed = mock_print.call_args.args[0]
        self.assertFalse(_is_denied(_json.loads(printed)))


if __name__ == "__main__":
    unittest.main()
