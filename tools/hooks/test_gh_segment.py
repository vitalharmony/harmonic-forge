#!/usr/bin/env python3
"""The one shell gate: is a command segment exactly a gh call? (harmonic-forge#1496 reforge).

Every case here is a shell form an earlier preclose pass raised against the
adapter when it tried to model shell grammar itself. The allowlist answers each
one without parsing a script.
"""
from __future__ import annotations

import shlex
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gh_segment as S  # noqa: E402


class AllowedShapes(unittest.TestCase):
    def test_plain_gh_with_inline_assignments_is_a_gh_call(self):
        seg = S.gh_segment(["GH_REPO=a/b", "gh", "issue", "close", "12"])
        self.assertEqual((seg.kind, seg.argv, seg.env), (S.GH, ("issue", "close", "12"), {"GH_REPO": "a/b"}))

    def test_a_bare_env_prefix_is_allowed(self):
        self.assertEqual(S.gh_segment(["env", "FOO=1", "gh", "pr", "merge", "5"]).kind, S.GH)

    def test_the_account_wrappers_are_allowed(self):
        self.assertEqual(S.gh_segment(["gh-as", "vitalharmony", "gh", "pr", "merge", "5"]).argv, ("pr", "merge", "5"))
        self.assertEqual(S.gh_segment(["gha", "vh", "issue", "close", "5"]).argv, ("issue", "close", "5"))

    def test_a_path_qualified_gh_is_allowed(self):
        self.assertEqual(S.gh_segment(["/usr/bin/gh", "issue", "close", "5"]).kind, S.GH)


class UnprovableShapes(unittest.TestCase):
    CASES = {
        "env option": ["env", "-u", "GH_REPO", "gh", "issue", "close", "12"],
        "env -i": ["env", "-i", "GH_REPO=a/b", "gh", "issue", "close", "12"],
        "sudo": ["sudo", "gh", "pr", "merge", "5"],
        "xargs": ["xargs", "-n1", "gh", "issue", "close"],
        "nohup": ["nohup", "gh", "issue", "close", "5"],
        "timeout": ["timeout", "5", "gh", "issue", "close", "5"],
        "command": ["command", "gh", "issue", "close", "5"],
        "redirection": ["<f", "gh", "issue", "close", "5"],
        "gh-as with its own env": ["gh-as", "vh", "env", "GH_REPO=x/y", "gh", "issue", "close", "5"],
        "gha behind sudo": ["sudo", "gha", "vh", "issue", "close", "5"],
        "script": ["bash", "-lc", "echo ok && gh issue close 12"],
        "quoted gh in a script": ["bash", "-c", "'gh' issue close 5"],
        "script setting GH_REPO": ["sh", "-c", "GH_REPO=a/b gh issue close 12"],
        "eval": ["eval", "gh issue close 5"],
        "gh word after gh": ["gh", "alias", "set", "x", "gh"],
    }

    def test_every_unmodeled_shape_is_unprovable(self):
        for name, tokens in self.CASES.items():
            with self.subTest(name=name):
                self.assertEqual(S.gh_segment(tokens).kind, S.UNPROVABLE)



class DynamicShapes(unittest.TestCase):
    """harmonic-forge#1496 reforge pass 1: the gate fails closed on a dynamic
    command word and on a substitution that runs gh, and still lets a plain
    expansion or a non-gh substitution through."""

    UNPROVABLE = (
        "$(echo gh) issue close 5",
        "`echo gh` issue close 5",
        "$GH issue close 5",
        "g${x}h issue close 5",
        'gh issue comment 5 --body "$(gh issue close 7)"',
        'echo "$(gh issue close 7)"',
        'gh issue view 3 --comments "`gh pr merge 9`"',
    )

    def test_each_reproduced_shape_is_unprovable(self):
        for command in self.UNPROVABLE:
            self.assertEqual(S.gh_segment(shlex.split(command)).kind, S.UNPROVABLE, command)

    def test_a_non_gh_substitution_or_plain_expansion_is_not_gh(self):
        for command in ("echo $(date)", "echo $HOME", 'printf "%s" "$(uname)"'):
            self.assertEqual(S.gh_segment(shlex.split(command)).kind, S.NOT_GH, command)


class Containment(unittest.TestCase):
    """harmonic-forge#1496 re-scope: no runner is modelled; a gh word anywhere is
    contained, and a gh call hidden from every token is the F1447 shim's."""

    def test_string_runners_and_assignments_are_unprovable(self):
        for command in ("fish -c 'gh issue close 5'", "ssh host 'gh issue close 5'",
                        "env -S 'gh issue close 5'", 'X="$(gh issue close 5)" gh issue view 1',
                        "X=gh sudo $X pr merge 5", 'echo "$($c pr merge 5)"',
                        'gh-as "$(gh issue close 5)" gh issue view 1', "gha $(gh pr merge 2) issue view 1",
                        "gh=1 true"):
            self.assertEqual(S.gh_segment(shlex.split(command)).kind, S.UNPROVABLE, command)

    def test_the_bounding_keeps_ordinary_words_out(self):
        for command in ("[ -f x ]", "[[ -n y ]]", "ls /usr/bin/ghostscript", "echo ghost",
                        "git checkout gh-pages", "curl https://x.org/gh/y/z"):
            self.assertEqual(S.gh_segment(shlex.split(command)).kind, S.NOT_GH, command)

    def test_a_path_ending_in_gh_is_contained(self):
        # Intended: `tools/gh` ends in a bounded gh word, so it is unprovable.
        self.assertEqual(S.gh_segment(["ls", "tools/gh"]).kind, S.UNPROVABLE)

class NotGh(unittest.TestCase):
    def test_a_segment_with_no_gh_word_is_not_gh(self):
        for tokens in (["ls", "-la"], ["sudo", "ls"], ["bash", "-c", "ls -la"], ["git", "status"]):
            with self.subTest(tokens=tokens):
                self.assertEqual(S.gh_segment(tokens).kind, S.NOT_GH)

    def test_prose_that_names_a_gh_word_is_unprovable(self):
        # harmonic-forge#1496 re-scope, intended: containment, not recognition. A
        # token naming gh is unprovable outside the allowed shapes, commit
        # messages included; the adopting gate decides ask or refuse.
        self.assertEqual(S.gh_segment(["git", "commit", "-m", "close via gh issue close 5"]).kind, S.UNPROVABLE)

    def test_a_gh_named_file_or_word_inside_another_word_is_not_gh(self):
        for tokens in (["cat", "ghost.txt"], ["echo", "high"], ["bash", "-c", "echo ghost"]):
            with self.subTest(tokens=tokens):
                self.assertEqual(S.gh_segment(tokens).kind, S.NOT_GH)


if __name__ == "__main__":
    unittest.main()
