"""harmonic-forge#1443: the close/merge guards read compound commands.

Problem 1: a `gh` close or merge inside `if/while/for/{ }/!` (or after `exec`)
led its segment with a reserved word, so `strip_invocation_prefix` left it in
place and every gating hook skipped the command.

Problem 2 (an ordinary --comment denied as an ambiguous repo) is not fixed:
the operator kept harmonic-forge#1339's no-flag-grammar ruling on
2026-11-19, because `--comment --repo o/r` makes `--repo` the comment's value
and only a flag grammar could tell. That AMBIGUOUS answer is intended.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
sys.path.insert(0, str(HOOK_DIR.parent / "gh"))
import batch_auth  # noqa: E402
import block_missing_preclose_inspection as preclose  # noqa: E402
import block_undetermined_phase_close as phase  # noqa: E402
import record_tasklist_post  # noqa: E402
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
CLOSE = f"gh issue close 7 --repo {REPO}"
MERGE = f"gh pr merge 8 --repo {REPO} --squash"

#: AC1: every compound form, and the segment-leading word each one leaves.
COMPOUND_CLOSES = {
    "if": f"if true; then {CLOSE}; fi",
    "while": f"while true; do {CLOSE}; done",
    "until": f"until false; do {CLOSE}; done",
    "for": f"for i in 1; do {CLOSE}; done",
    "else": f"if false; then :; else {CLOSE}; fi",
    "elif": f"if false; then :; elif true; then {CLOSE}; fi",
    "brace": f"{{ {CLOSE}; }}",
    "subshell": f"( {CLOSE} )",
    "bang": f"! {CLOSE}",
    "exec": f"exec {CLOSE}",
    "interleaved": f"if true; then time ! {CLOSE}; fi",
}


def _gh_segments(command: str) -> list[list[str]]:
    out = []
    for segment in command_segments(command):
        tokens = strip_invocation_prefix(segment)
        if tokens and tokens[0] == "gh":
            out.append(tokens)
    return out


class StripReservedWords(unittest.TestCase):
    def test_each_reserved_word_and_exec_strips_to_gh(self):
        for word in ("then", "do", "else", "elif", "if", "while", "until", "!", "{", "exec"):
            with self.subTest(word=word):
                self.assertEqual(strip_invocation_prefix([word, "gh", "issue", "close", "1"]),
                                 ["gh", "issue", "close", "1"])

    def test_interleaved_prefixes_strip_in_one_loop(self):
        self.assertEqual(strip_invocation_prefix(["then", "time", "!", "env", "A=1", "gh", "pr", "merge", "2"]),
                         ["gh", "pr", "merge", "2"])


class Problem1PrecloseGateSeesTheClose(unittest.TestCase):
    def test_every_compound_form_is_a_gated_target(self):
        for name, command in COMPOUND_CLOSES.items():
            with self.subTest(form=name):
                self.assertEqual(preclose.find_gated_targets(command), [(REPO, "7", "issue")])

    def test_a_merge_inside_a_compound_is_a_gated_target(self):
        self.assertEqual(preclose.find_gated_targets(f"if true; then {MERGE}; fi"), [(REPO, "8", "pr")])


class Problem1BatchClassificationSeesTheMerge(unittest.TestCase):
    """AC2: a merge inside `if ... fi` reaches batch_auth's classifier."""

    def test_merge_inside_if_is_classified(self):
        [tokens] = _gh_segments(f"if true; then {MERGE}; fi")
        self.assertIsNotNone(batch_auth.classify_pr_merge(tokens))

    def test_close_inside_while_is_classified(self):
        [tokens] = _gh_segments(f"while true; do {CLOSE}; done")
        self.assertIsNotNone(batch_auth.classify_issue_close(tokens))


class AC4MentionsAreNotCommands(unittest.TestCase):
    def test_echo_and_grep_of_the_words_are_not_targets(self):
        for command in ('echo "then gh issue close 9"', 'grep -n "gh issue close" f',
                        "echo then gh issue close 9"):
            with self.subTest(command=command):
                self.assertEqual(preclose.find_gated_targets(command), [])


if __name__ == "__main__":
    unittest.main()
