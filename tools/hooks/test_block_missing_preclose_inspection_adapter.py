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
import re
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
                 "stdbuf -oL ", "then ", "! ", "gh-as vh ", "gha vh ",
                 # Round 1 survivor 1: a decoy GH_REPO a later wrapper unsets or clears.
                 "GH_REPO=d/x env -u GH_REPO ", "GH_REPO=d/x env -i ",
                 # Sticky-wicket rule 5: the spellings are coverage, not mechanics.
                 "env -uGH_REPO ", "env --unset=GH_REPO ", "env --unset GH_REPO ", "export GH_REPO=x/y; ")
GRID_WORDS = ("gh", "/usr/bin/gh", '"$X/gh"', "dash -c", "bash -c export")
GRID_BODIES = ("issue close 849 -R o/r", "pr merge 7 -R o/r --match-head-commit abc123",
               "api -X PATCH repos/o/r/issues/849 -f state=closed",
               # Unbound: the environment alone names the repo (main: cwd).
               "issue close 849", "pr merge 7 --match-head-commit abc123")
#: Pitch change 1: gh's `:owner/:repo` placeholder, beside its explicit twin.
PLACEHOLDER_PREFIXES = ("", "A=v ", "env -u GH_REPO ", "nohup ", "export GH_REPO=x/y; ")
PLACEHOLDER_BODIES = ("api -X PATCH repos/:owner/:repo/issues/849 -f state=closed",
                      "api -X PUT repos/:owner/:repo/pulls/7/merge", "api -X PUT repos/o/r/pulls/7/merge")


def _cell(prefix: str, word: str, body: str) -> str:
    if word == "dash -c":
        return prefix + f"dash -c 'gh {body}'"
    if word == "bash -c export":
        return prefix + f"bash -c 'export GH_RE\"\"PO=x/y; gh {body}'"
    return prefix + f"{word} {body}"


def grid() -> list[str]:
    return ([_cell(p, w, b) for p in GRID_PREFIXES for w in GRID_WORDS for b in GRID_BODIES]
            + [_cell(p, w, b) for p in PLACEHOLDER_PREFIXES for w in GRID_WORDS for b in PLACEHOLDER_BODIES])


def grid_expected(command: str, table: dict) -> list | None:
    """Main's row, except: a cell naming GH_REPO/GH_HOST where main bound a target
    with no repo expects UNDECIDED (rule 5); a placeholder cell expects UNDECIDED
    where main bound its explicit twin. None: main bound nothing, so no claim."""
    if ":owner/:repo" in command:
        return UNDECIDED if table[command.replace(":owner/:repo", "o/r")] else None
    main = [tuple(t) for t in table[command] or []]
    if main and any(t[0] is None for t in main) and re.search(r"GH_(REPO|HOST)", re.sub("[\"']", "", command)):
        return UNDECIDED
    return main or None


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

    def test_a_gh_env_mention_with_no_argv_repo_is_undecided(self) -> None:
        """Sticky-wicket rules 1-2: the adapter's env is always empty, and a gated
        call whose argv names no repo, in a command naming GH_REPO/GH_HOST at any
        depth, is undecided: denied before any read. Round 1's decoys included."""
        sha = "c" * 40
        for command in (
                "GH_REPO=a/b gh issue close 12", "GH_REPO=d/x env -u GH_REPO gh issue close 5",
                "export GH_REPO=x/y; gh issue close 5", 'gh(){ GH_REPO=x command gh "$@"; }; gh issue close 5',
                "bash -c 'export GH_RE\"\"PO=x; gh issue close 5'", "env -uGH_REPO gh issue close 5",
                f"env --unset=GH_REPO gh pr merge 7 --match-head-commit {sha}", "GH_REPO=$O/r gh issue close 12",
                "GH_REPO=x/y env GH_REPO=o/r gh issue close 5", "GH_REPO=x/y env -i HOME=/h gh issue close 5",
                "GH_REPO=x/y exec -c gh issue close 5", "GH_REPO=x/y ksh -c 'gh issue close 5'",
                "GH_REPO=x/y bash -c 'gh issue close 5'", "GH_REPO=x/y dash -c 'GH_REPO=o/r; gh issue close 5'",
                "GH_REPO=x/y env --chdir=/t gh issue close 5", "exec -a GH_REPO=decoy/x gh issue close 5",
                "nohup GH_REPO=a/b gh issue close 5", "GH_HOST=h gh issue close 5 -r 'not planned'",
                "GH_REPO=x/y time GH_REPO=o/r gh issue close 5", "GH_REPO=x/y dash -c 'GH_REPO=o/r gh issue close 5'",
                "GH_REPO=x/y bash -c 'unset GH_REPO; gh issue close 5'", "GH_REPO=x/y exec -lx gh issue close 5",
                "unset GH_REPO; gh issue close 5", "timeout -s GH_REPO=decoy/x 5 gh issue close 5",
                "GH_REPO=x/y exec -cl gh issue close 5", "GH_REPO=decoy/x env --unset GH_REPO gh issue close 5",
                f"GH_REPO=x/y time GH_REPO=o/r gh pr merge 7 --match-head-commit {sha}"):
            with self.subTest(command=command):
                self.assertEqual(set(hook.find_gated_targets_exempt(command)), set(UNDECIDED))
                self.assertIn("harmonic-forge#1499", _denied(_main(command)))

    def test_an_argv_repo_binds_as_main_whatever_the_environment(self) -> None:
        """Rule 3: gh gives --repo, a URL and an explicit api path precedence over
        GH_REPO, so argv binds as on main; no mention binds the checkout as main."""
        sha = "c" * 40
        for command, want in (
                ("env -u GH_REPO gh issue close 5 --repo o/r", ("o/r", "5", "issue", False)),
                ("GH_REPO=x/y gh issue close https://github.com/o/r/issues/5", ("o/r", "5", "issue", False)),
                ("GH_REPO=x/y gh api -X PATCH repos/o/r/issues/5 -f state=closed", ("o/r", "5", "issue", False)),
                ("GH_HOST=`h` gh pr merge 7 -R o/r", ("o/r", "7", "pr", False)),
                ("gh issue close 5", (None, "5", "issue", False))):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), [want])
        merge = f"GH_REPO=d/x env -u GH_REPO gh pr merge 7 -R o/r --match-head-commit {sha}"
        self.assertEqual(hook.find_gated_targets(merge), [("o/r", "7", "pr")])
        self.assertEqual(hook.merge_head_commits(merge, "7"), [sha])

    def test_a_gh_placeholder_api_path_names_no_repo(self) -> None:
        """Pitch change 1: gh fills `:owner`/`:repo`/`{x}` from GH_REPO or the
        checkout, so a gated api call holding one is undecided."""
        for command in ("gh api -X PATCH repos/:owner/:repo/issues/5 -f state=closed",
                        "GH_REPO=x/y gh api -X PATCH repos/:owner/:repo/issues/5 -f state=closed",
                        "gh api -X PUT repos/:owner/:repo/pulls/7/merge", "gh api -X PUT repos/o/:repo/pulls/7/merge",
                        "gh api -X PATCH repos/{owner}/{repo}/issues/5 -f state=closed"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets_exempt(command), UNDECIDED)
                self.assertTrue(_denied(_main(command)))
        self.assertEqual(hook.find_gated_targets_exempt("gh api repos/:owner/:repo/issues/5"), [])

    def test_a_shell_word_pair_in_a_gh_argv_reads_only_that_payload(self) -> None:
        """The docstring's stated residual, unchanged from main."""
        self.assertEqual(hook.find_gated_targets_exempt("gh issue close 5 -c sh -c x"), [])

    def test_an_adapter_fault_is_a_deny_never_a_raise(self) -> None:
        with mock.patch.object(hook.A, "classify_argv", side_effect=RuntimeError("boom")):
            self.assertEqual(hook.find_gated_targets_exempt("gh issue close 5 -R o/r"), UNDECIDED)
            self.assertTrue(_denied(_main("gh issue close 5 -R o/r")))

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
        """Kill check 1 and rules 2, 3, 5: a narrower predicate, a dropped or
        unconditional mention-deny, or a dropped placeholder check fails here."""
        table = json.loads(MAIN_TABLE.read_text(encoding="utf-8"))
        bound = 0
        for command in grid():
            with self.subTest(command=command):
                self.assertIn(command, table, "regenerate: python3 tools/hooks/testdata/preclose_main_table.py")
                want, got = grid_expected(command, table), hook.find_gated_targets_exempt(command)
                if want is not None:  # a deny counts only where main had none, or rule 5 says so
                    bound += 1
                    self.assertEqual(got, want)
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
