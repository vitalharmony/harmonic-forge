#!/usr/bin/env python3
"""The differential run: each old gh close/merge recognizer against the adapter (harmonic-forge#1496).

For every row of the shared argv table, and every argv or command string the
consumers' own tests pass to them, each old recognizer (BATCH, the data-migration
hook, the preclose hook) is compared with the adapter read through that
consumer's `VERDICT` column. An observation is the old and new verdict plus, when
both see a close or merge, its targets. Every difference is listed with a
classification AND the exact observation it was classified against, so a listed
difference that changes (another target, another verdict) fails as unclassified.

String corpora (the two text hooks pass whole command strings to `find_*`) are
replayed through `shell_parse.command_segments`. The AST scan reads constant
arguments of `find_*` and the tests' `assert_target` helpers; a call whose
argument is not a constant is counted unmeasured, never as agreeing.
"""
from __future__ import annotations

import ast
import shlex
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import batch_auth  # noqa: E402
import block_data_migration_close  # noqa: E402
import block_missing_preclose_inspection  # noqa: E402
import gh_argv_adapters as A  # noqa: E402
from shell_parse import command_segments  # noqa: E402
from test_gh_argv_adapters import ASK, CONSUMERS, NOT_A_CLOSE, REFUSE, ROWS, SCOPE, verdict  # noqa: E402

CLASSES = {"old-defect", "adapter-defect", "intended"}

#: (consumer, row) -> (class, reason, observation). The observation is
#: (old verdict, new verdict, old targets, new targets), exactly as observed.
DISAGREEMENTS: dict[tuple[str, str], tuple[str, str, tuple]] = {
    ('batch', 'bound_inline_repo'): ('old-defect', "the old reader drops the inline GH_REPO and binds the checkout's repo",
        (ASK, ASK, frozenset({('issue', None, '12')}), frozenset({('issue', 'a/b', '12')}))),
    ('batch', 'unparseable_flag'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (ASK, ASK, frozenset({('issue', None, '12')}), frozenset())),
    ('batch', 'unprovable_alias'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, ASK, frozenset(), frozenset())),
    ('batch', 'unprovable_flag_before_root'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (ASK, ASK, frozenset({('issue', 'o/r', '12')}), frozenset())),
    ('batch', 'unprovable_graphql_input'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, ASK, frozenset(), frozenset())),
    ('batch', 'unprovable_placeholder_close'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (ASK, ASK, frozenset({('issue', '{owner}/{repo}', '5')}), frozenset())),
    ('batch', 'unprovable_placeholder_merge'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (ASK, ASK, frozenset({('pr', '{owner}/{repo}', '5')}), frozenset())),
    ('migration', 'bound_inline_repo'): ('old-defect', "the old reader drops the inline GH_REPO and binds the checkout's repo",
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset({('issue', 'a/b', '12')}))),
    ('migration', 'unbound_issue_no_arg'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unbound_variable'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unparseable_flag'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset())),
    ('migration', 'unprovable_alias'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_flag_before_root'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_graphql_input'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_placeholder_close'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_placeholder_merge'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'bound_inline_repo'): ('old-defect', "the old reader drops the inline GH_REPO and binds the checkout's repo",
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset({('issue', 'a/b', '12')}))),
    ('preclose', 'unbound_issue_no_arg'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unbound_variable'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unparseable_flag'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset())),
    ('preclose', 'unprovable_alias'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_flag_before_root'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_graphql_input'): ('intended', 'tightening: a call that may close or merge is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_placeholder_close'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_placeholder_merge'): ('intended', 'verdict unchanged; the adapter withholds a target it cannot prove',
        (REFUSE, REFUSE, frozenset({('pr', '{owner}/{repo}', '5')}), frozenset())),
}


def _norm(kind: str, repo, number) -> tuple:
    return (kind, repo or None, str(number) if number not in (None, "") else None)


def _old_targets(consumer: str, tokens: list[str] | None, command: str | None = None) -> frozenset | None:
    """What the old recognizer reports, as (kind, repo, number) targets; None when
    a text hook cannot tokenize the command."""
    command = command if command is not None else shlex.join(tokens)
    if consumer == "batch":
        found = set()
        if (close := batch_auth.classify_issue_close(tokens)) is not None:
            found.add(_norm("issue", *close))
        if (merge := batch_auth.classify_pr_merge(tokens)) is not None:
            found.add(_norm("pr", *merge))
        return frozenset(found)
    if consumer == "migration":
        found = block_data_migration_close.find_close_targets(command)
        return None if found is None else frozenset(_norm("issue", r, n) for r, n in found)
    found = block_missing_preclose_inspection.find_gated_targets(command)
    return None if found is None else frozenset(_norm(k, r, n) for r, n, k in found)


def _old_verdict(consumer: str, targets: frozenset) -> str:
    if not targets:
        return NOT_A_CLOSE
    return ASK if consumer == "batch" else REFUSE


def _strictest(verdicts: list[str]) -> str:
    for candidate in (REFUSE, ASK):
        if candidate in verdicts:
            return candidate
    return NOT_A_CLOSE


def observe(consumer: str, segments: list[list[str]], old: frozenset) -> tuple:
    results = [A.classify(tokens, {}) for tokens in segments]
    new_verdict = _strictest([verdict(consumer, r) for r in results])
    new = frozenset(_norm(r.kind, r.repo, r.number) for r in results
                    if r.gated and r.kind in SCOPE[consumer])
    old_verdict = _old_verdict(consumer, old)
    seen = old_verdict != NOT_A_CLOSE and new_verdict != NOT_A_CLOSE
    return (old_verdict, new_verdict, old if seen else frozenset(), new if seen else frozenset())


def differences(rows: dict[str, list[str]]) -> dict[tuple[str, str], tuple]:
    found = {}
    for name, tokens in rows.items():
        for consumer in CONSUMERS:
            seen = observe(consumer, [tokens], _old_targets(consumer, tokens))
            if seen[0] != seen[1] or seen[2] != seen[3]:
                found[(consumer, name)] = seen
    return found


class DifferentialRun(unittest.TestCase):
    def test_every_difference_matches_its_classified_observation(self):
        found = differences(ROWS)
        wrong = {k: v for k, v in found.items() if k not in DISAGREEMENTS or DISAGREEMENTS[k][2] != v}
        self.assertEqual(wrong, {}, "unclassified, or changed since classified")

    def test_no_classification_outlives_its_difference(self):
        self.assertEqual(sorted(set(DISAGREEMENTS) - set(differences(ROWS))), [])

    def test_every_classification_names_a_known_class_and_a_reason(self):
        for key, (klass, reason, _) in DISAGREEMENTS.items():
            with self.subTest(key=key):
                self.assertIn(klass, CLASSES)
                self.assertTrue(reason.strip())

    def test_a_planted_difference_is_reported(self):
        planted = {"planted": ["gh", "issue", "close", "99", "--repo", "x/y"]}
        original = batch_auth.classify_issue_close
        batch_auth.classify_issue_close = lambda tokens: None
        try:
            found = differences(planted)
        finally:
            batch_auth.classify_issue_close = original
        self.assertIn(("batch", "planted"), found)


def _constant(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.List) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


def corpus(test_file: str, functions: tuple[str, ...]) -> tuple[list, int]:
    """(entries, unmeasured) for the first argument of every call to `functions`."""
    tree = ast.parse((HERE / test_file).read_text(encoding="utf-8"))
    entries, unmeasured = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in functions:
            value = _constant(node.args[0])
            if value is None:
                unmeasured += 1
            else:
                entries.append(value)
    return entries, unmeasured


def argv_lists(test_file: str) -> list[list[str]]:
    """Every list literal of constant strings holding a `gh` token (BATCH's argv fixtures)."""
    tree = ast.parse((HERE / test_file).read_text(encoding="utf-8"))
    return [[e.value for e in node.elts] for node in ast.walk(tree)
            if isinstance(node, ast.List) and node.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)
            and "gh" in [e.value for e in node.elts][:4]]


#: Corpus entries whose observation differs: command -> (class, reason, observation).
CORPUS_CLASSIFIED: dict[str, dict[str, tuple[str, str, tuple]]] = {"batch": {}, "migration": {}, "preclose": {
    "gh api -X PUT repos/o/r/pulls/7": (
        "intended", "gh_argv gates any PUT on a pull request (fail-closed); the preclose test pins it as not a"
        " merge. The adopter (F1499) keeps the old answer with a path-suffix check or accepts the stricter one",
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
}}


class CorpusReplay(unittest.TestCase):
    def _check(self, consumer, mismatched):
        classified = CORPUS_CLASSIFIED[consumer]
        wrong = {c: v for c, v in mismatched.items() if c not in classified or classified[c][2] != v}
        self.assertEqual(wrong, {}, "unclassified, or changed since classified")
        self.assertEqual(set(classified), set(mismatched), "a stale classification")
        for klass, reason, _ in classified.values():
            self.assertIn(klass, CLASSES)
            self.assertTrue(reason.strip())

    def _text_hook(self, consumer, test_file, functions):
        entries, unmeasured = corpus(test_file, functions)
        self.assertGreater(len(entries), 0, f"{test_file}: no replayable call (a moved or empty corpus)")
        mismatched = {}
        for command in entries:
            old = _old_targets(consumer, None, command)
            try:
                segments = command_segments(command)
            except ValueError:
                segments = None
            if old is None or segments is None:
                if (old is None) != (segments is None):  # both must agree it cannot be tokenized
                    mismatched[command] = ("untokenizable", old is None, segments is None)
                continue
            seen = observe(consumer, segments, old)
            if seen[0] != seen[1] or seen[2] != seen[3]:
                mismatched[command] = seen
        self._check(consumer, mismatched)
        return unmeasured

    def test_migration_hook_corpus(self):
        self._text_hook("migration", "test_block_data_migration_close.py", ("find_close_targets", "assert_target"))

    def test_preclose_hook_corpus(self):
        self._text_hook("preclose", "test_block_missing_preclose_inspection.py", ("find_gated_targets", "assert_target"))

    def test_batch_argv_corpus(self):
        entries = argv_lists("test_batch_auth.py")
        self.assertGreater(len(entries), 0, "test_batch_auth.py: no replayable argv list")
        mismatched = {}
        for tokens in entries:
            seen = observe("batch", [tokens], _old_targets("batch", tokens))
            if seen[0] != seen[1] or seen[2] != seen[3]:
                mismatched[" ".join(tokens)] = seen
        self._check("batch", mismatched)


if __name__ == "__main__":
    unittest.main()
