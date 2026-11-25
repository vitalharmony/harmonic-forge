#!/usr/bin/env python3
"""block_missing_preclose_inspection reads targets through gh_argv_adapters (harmonic-forge#1499).

Kept apart from test_block_missing_preclose_inspection.py, which is over the
300-line cap and may only shrink. The differential replays both modules.

Recognition is main's (`shell_parse.strip_invocation_prefix` plus a basename
test); the adapter owns target and flag grammar; `VERDICT["preclose"]` refuses
every close or merge the adapter cannot name or prove is not one.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_missing_preclose_inspection as hook  # noqa: E402

REPO = "vitalharmony/hrse"
UNDECIDED = [(None, "", "issue", False)]
MAIN_TABLE = Path(__file__).resolve().parent / "testdata" / "preclose_main_178ba5f.json"

#: Design amendment change 5: the parity grid. Every prefix and command word
#: main's predicate strips or recognizes, over a CLI close, a CLI merge and a
#: REST close; preclose_main_table.py freezes main's output for each.
GRID_PREFIXES = ("", "A=v ", "A=/usr/bin/gh ", "env ", "env env ", "env 1=x ", "env -u GH_REPO ",
                 "nohup ", "time ", "timeout 5 ", "exec ", "command -p ", "nice -n5 ",
                 "stdbuf -oL ", "then ", "! ", "gh-as vh ", "gha vh ")
GRID_WORDS = ("gh", "/usr/bin/gh", '"$X/gh"', "dash -c")
GRID_BODIES = ("issue close 849 -R o/r", "pr merge 7 -R o/r --match-head-commit abc123",
               "api -X PATCH repos/o/r/issues/849 -f state=closed")


def grid() -> list[str]:
    out = []
    for prefix in GRID_PREFIXES:
        for word in GRID_WORDS:
            for body in GRID_BODIES:
                out.append(prefix + (f"dash -c 'gh {body}'" if word == "dash -c" else f"{word} {body}"))
    return out


def _no_read(*_args, **_kwargs):
    raise AssertionError("an undecided target is denied before any repo or label read")


def _main(command: str) -> dict:
    with mock.patch.object(hook, "resolve_repo", side_effect=_no_read), \
         mock.patch.object(hook, "labels_for", side_effect=_no_read), \
         mock.patch.object(hook, "issues_closed_by_pr", side_effect=_no_read), \
         mock.patch("sys.stdin", mock.MagicMock()), \
         mock.patch("json.load", return_value={"tool_name": "Bash", "tool_input": {"command": command}}), \
         mock.patch("builtins.print") as printed:
        hook.main()
    return json.loads(printed.call_args[0][0])


def _denied(payload: dict) -> str | None:
    out = payload.get("hookSpecificOutput") or {}
    return out.get("permissionDecisionReason") if out.get("permissionDecision") == "deny" else None


class VerdictColumnTests(unittest.TestCase):
    """The issue's test cases and the handoff's step 9."""

    def test_a_bare_pr_merge_is_still_denied(self) -> None:
        self.assertEqual(hook.find_gated_targets_exempt("gh pr merge"), [(None, "", "pr", False)])
        self.assertIn("gh pr merge <N>", _denied(_main("gh pr merge --squash")))

    def test_a_rest_put_merge_is_a_gated_merge(self) -> None:
        self.assertEqual(hook.find_gated_targets("gh api -X PUT repos/o/r/pulls/7/merge"),
                         [("o/r", "7", "pr")])

    def test_an_unknown_flag_is_denied_never_not_a_close(self) -> None:
        for command in ("gh issue close 5 --repo o/r --bogus", "gh pr merge 5 --repo o/r --bogus"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), UNDECIDED)
                self.assertIn("harmonic-forge#1499", _denied(_main(command)))

    def test_an_unbound_issue_close_is_denied_before_any_read(self) -> None:
        """Differential rows `unbound_variable` and `unbound_issue_no_arg`."""
        for command in ('gh issue close "$N"', "gh issue close", 'gh issue close "$N" --repo o/r'):
            with self.subTest(command=command):
                self.assertTrue(all(t[1] == "" and t[2] == "issue"
                                    for t in hook.find_gated_targets_exempt(command)))
                self.assertIn("gh issue close <N> --repo <owner/repo>", _denied(_main(command)))

    def test_a_flag_before_the_command_is_denied(self) -> None:
        """Amendment change 3: main recognizes it; the adapter cannot prove it."""
        self.assertEqual(hook.find_gated_targets_exempt("gh -R o/r issue close 5"), UNDECIDED)
        self.assertTrue(_denied(_main("gh -R o/r issue close 5")))

    def test_one_undecided_segment_is_one_target(self) -> None:
        self.assertEqual(hook.find_gated_targets_exempt("gh api graphql --input body.json -X POST"),
                         UNDECIDED)

    def test_the_adapter_exemption_alone_never_exempts(self) -> None:
        for command in ("gh issue close 5 --repo o/r --reason='not planned'",
                        "gh api -X PATCH repos/o/r/issues/5 -F state=closed -F state_reason=not_planned"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), [("o/r", "5", "issue", False)])
        self.assertEqual(hook.find_gated_targets_exempt("gh issue close 5 --repo o/r -r 'not planned'"),
                         [("o/r", "5", "issue", True)])

    def test_an_inline_gh_repo_binds_and_an_expansion_is_undecided(self) -> None:
        """Differential row `bound_inline_repo` (old-defect): gh reads GH_REPO."""
        self.assertEqual(hook.find_gated_targets("GH_REPO=a/b gh issue close 12"), [("a/b", "12", "issue")])
        for command in ("GH_REPO=$O/r gh issue close 12", "GH_HOST=`h` gh pr merge 7 -R o/r"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), UNDECIDED)
                self.assertTrue(_denied(_main(command)))

    def test_only_leading_assignments_are_the_environment(self) -> None:
        """`timeout 5`, `-u GH_REPO`, `nohup` and wrappers contribute no env."""
        self.assertEqual(hook.find_gated_targets("nohup GH_REPO=a/b gh issue close 12"),
                         [(None, "12", "issue")])

    def test_an_adapter_fault_is_a_deny_never_a_raise(self) -> None:
        with mock.patch.object(hook.A, "classify_argv", side_effect=RuntimeError("boom")):
            self.assertEqual(hook.find_gated_targets_exempt("gh issue close 5 -R o/r"), UNDECIDED)
            self.assertTrue(_denied(_main("gh issue close 5 -R o/r")))

    def test_a_merge_on_another_host_is_the_preclose_hooks_deny(self) -> None:
        """Moved from test_block_route_tightened_merge (`GH_HOST=other.example`):
        gh_argv reads another host as unbound, so the route hook's numbered-PR
        filter (block_route_tightened_merge.py:400) skips it and this hook denies
        it as an unnumbered merge before any read; the merge stays denied."""
        command = "GH_HOST=other.example gh pr merge 9 --repo o/r --squash --match-head-commit " + "c" * 40
        self.assertEqual(hook.find_gated_targets_exempt(command), [(None, "", "pr", False)])
        self.assertIn("gh pr merge <N>", _denied(_main(command)))

    def test_not_gh_and_reads_stay_untouched(self) -> None:
        for command in ("git status", "gh issue view 5", "grep -rn 'gh issue close' tools/",
                        "gh api repos/o/r/issues/5", "echo gh pr merge 1"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), [])


class MergeHeadTests(unittest.TestCase):
    """Amendment change 6: the merge head reads the same recognition and argv."""

    def test_a_wrapped_bound_merge_gets_its_head(self) -> None:
        self.assertEqual(hook.merge_head_commits("nohup gh pr merge 7 -R o/r --match-head-commit abc123", "7"),
                         ["abc123"])

    def test_the_shim_argv_returns_the_match_head_commit(self) -> None:
        """Pitch change 4: the shim's argv is a whole segment, `gh` included."""
        argv = ["gh", "pr", "merge", "7", "--repo", "o/r", "--match-head-commit", "abc123"]
        self.assertEqual(hook.merge_head_commits_from_tokens(argv, "7"), ["abc123"])
        self.assertEqual(hook.merge_head_commits_from_tokens(argv, "8"), [None])

    def test_the_rest_merge_head_is_never_the_adapters(self) -> None:
        """harmonic-forge#1077: `_bound_sha`'s allow-list, never the adapter's heads."""
        self.assertEqual(hook.merge_head_commits(
            "gh api -X PUT repos/o/r/pulls/7/merge -f sha=abc123", "7"), [None])
        self.assertEqual(hook.merge_head_commits(
            "gh pr merge 7 -R o/r -b x -sd --match-head-commit abc123", "7"), [None])


class ParityGridTests(unittest.TestCase):
    def test_wherever_main_binds_a_target_the_hook_binds_the_same_one(self) -> None:
        """Kill checks 1 and 3: a narrower predicate, or a raising env harvest, fails here."""
        table = json.loads(MAIN_TABLE.read_text(encoding="utf-8"))
        bound = 0
        for command in grid():
            with self.subTest(command=command):
                self.assertIn(command, table, "regenerate: python3 tools/hooks/testdata/preclose_main_table.py")
                main = [tuple(t) for t in table[command]]
                got = hook.find_gated_targets_exempt(command)
                if main:  # a deny counts only where main had none
                    bound += 1
                    self.assertEqual(got, main, "main named a target; the hook must name the same one")
        self.assertGreater(bound, len(grid()) // 2)

    def test_both_readers_gate_on_mains_predicate(self) -> None:
        """Kill check 2 (the K7 analog): recognition is the shared helper's, and it
        is `strip_invocation_prefix` plus the basename test."""
        module = inspect.getsource(hook)  # the file, so a recording wrapper never hides a reader
        tree = {n.name: ast.get_source_segment(module, n) for n in ast.parse(module).body
                if isinstance(n, ast.FunctionDef)}
        self.assertIn("tokens = strip_invocation_prefix(raw_tokens)", tree["_gh_reading"])
        self.assertIn('os.path.basename(tokens[0]) != "gh"', tree["_gh_reading"])
        for reader in ("find_gated_targets_exempt", "_segment_heads"):
            with self.subTest(reader=reader):
                self.assertIn("_gh_reading(raw_tokens)", tree[reader])


if __name__ == "__main__":
    unittest.main()
