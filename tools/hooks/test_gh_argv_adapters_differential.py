#!/usr/bin/env python3
"""The differential run: each old gh close/merge recognizer against the adapter (harmonic-forge#1496).

Each old recognizer (BATCH, the data-migration hook, the preclose hook) runs
against `gh_argv_adapters` over the shared argv table in
`test_gh_argv_adapters.py` plus every argv or command string the consumers' own
tests pass to them. Every disagreement is listed with a classification, and the
test fails on an unclassified disagreement and on a classification nothing
produces any more.

String corpora (the two text hooks pass whole command strings to `find_*`) are
replayed through `shell_parse.command_segments` and compared at the hooks' own
`find_*` level; an entry the AST scan cannot read as a constant is counted
unmeasured, never as agreeing.
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
from test_gh_argv_adapters import ROWS  # noqa: E402

#: Each consumer's scope: which kinds it reads. The migration hook reads issue
#: closes only; BATCH and the preclose hook read closes and merges.
SCOPE = {"batch": ("issue", "pr"), "migration": ("issue",), "preclose": ("issue", "pr")}

#: Every disagreement the differential run may show, keyed (consumer, case) with
#: its classification: old-defect (the old reader is wrong or inconsistent),
#: adapter-defect (the adapter is wrong), intended (a difference an adopter maps
#: back, named). The test fails on any disagreement not listed here and on any
#: entry here that the run no longer produces.
DISAGREEMENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("migration", "unbound_variable"): (
        "old-defect", "an unbound issue close is dropped (fail-open) while the preclose hook and BATCH keep it"),
    ("migration", "unbound_issue_no_arg"): (
        "old-defect", "same: `_parse_issue_close` returns None when no number is found"),
    ("preclose", "unbound_variable"): (
        "old-defect", "an unbound issue close is dropped, while an unbound merge is denied: inconsistent"),
    ("preclose", "unbound_issue_no_arg"): (
        "old-defect", "same inconsistency"),
    ("batch", "unparseable_flag"): (
        "intended", "the old reader reads through an unknown flag and still finds the close; the adopter maps"
        " unparseable to its conservative answer (ask) so that stays true"),
    ("migration", "unparseable_flag"): (
        "intended", "same; the adopter maps unparseable to a close it cannot bind, never to not-a-close"),
    ("preclose", "unparseable_flag"): (
        "intended", "same"),
}


def _sig_from_result(consumer: str, result: A.Result) -> set[tuple]:
    if not result.gated or result.kind not in SCOPE[consumer]:
        return set()
    return {(result.kind, result.repo, result.number)}


def _norm(kind: str, repo, number) -> tuple:
    return (kind, repo or None, str(number) if number not in (None, "") else None)


def old_sig(consumer: str, tokens: list[str]) -> set[tuple]:
    """What the old recognizer reports for one argv, as a set of (kind, repo, number)."""
    command = shlex.join(tokens)
    if consumer == "batch":
        found = set()
        close = batch_auth.classify_issue_close(tokens)
        if close is not None:
            found.add(_norm("issue", *close))
        merge = batch_auth.classify_pr_merge(tokens)
        if merge is not None:
            found.add(_norm("pr", *merge))
        return found
    if consumer == "migration":
        return {_norm("issue", repo, number)
                for repo, number in (block_data_migration_close.find_close_targets(command) or [])}
    return {_norm(kind, repo, number)
            for repo, number, kind in (block_missing_preclose_inspection.find_gated_targets(command) or [])}


def new_sig(consumer: str, tokens: list[str]) -> set[tuple]:
    result = A.classify(tokens, {})
    sig = _sig_from_result(consumer, result)
    return {_norm(*item) for item in sig}


def disagreements(rows: dict[str, list[str]]) -> dict[tuple[str, str], tuple[set, set]]:
    found = {}
    for name, tokens in rows.items():
        for consumer in SCOPE:
            old, new = old_sig(consumer, tokens), new_sig(consumer, tokens)
            if old != new:
                found[(consumer, name)] = (old, new)
    return found


class DifferentialRun(unittest.TestCase):
    def test_every_disagreement_on_the_shared_table_is_classified(self):
        found = disagreements(ROWS)
        unclassified = sorted(set(found) - set(DISAGREEMENTS))
        self.assertEqual(unclassified, [], f"unclassified disagreements: {[(k, found[k]) for k in unclassified]}")

    def test_no_classification_outlives_its_disagreement(self):
        found = disagreements(ROWS)
        stale = sorted(set(DISAGREEMENTS) - set(found))
        self.assertEqual(stale, [], f"classified disagreements the run no longer produces: {stale}")

    def test_every_classification_names_a_known_class_and_a_reason(self):
        for key, (klass, reason) in DISAGREEMENTS.items():
            with self.subTest(key=key):
                self.assertIn(klass, {"old-defect", "adapter-defect", "intended"})
                self.assertTrue(reason.strip())

    def test_a_planted_disagreement_is_reported(self):
        planted = dict(ROWS)
        planted["planted_bound_close_old_reader_misses"] = ["gh", "issue", "close", "99", "--repo", "x/y"]
        original = batch_auth.classify_issue_close
        batch_auth.classify_issue_close = lambda tokens: None
        try:
            found = disagreements(planted)
        finally:
            batch_auth.classify_issue_close = original
        self.assertIn(("batch", "planted_bound_close_old_reader_misses"), found)
        self.assertNotIn(("batch", "planted_bound_close_old_reader_misses"), DISAGREEMENTS)


def _constants(node: ast.AST):
    """A call argument that is a constant string or a list of constant strings, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.List) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


def corpus(test_file: str, functions: tuple[str, ...]) -> tuple[list, int]:
    """(entries, unmeasured): the constant first argument of every call to one of
    `functions` in a consumer's own test file; unmeasured counts the calls whose
    argument is not a constant, which the AST scan cannot replay."""
    tree = ast.parse((HERE / test_file).read_text(encoding="utf-8"))
    entries, unmeasured = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in functions:
            continue
        value = _constants(node.args[0])
        if value is None:
            unmeasured += 1
        else:
            entries.append(value)
    return entries, unmeasured


def argv_lists(test_file: str) -> list[list[str]]:
    """Every list literal of constant strings in a test file that holds a `gh`
    token: the argv fixtures BATCH's tests build and pass by name."""
    tree = ast.parse((HERE / test_file).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            values = [e.value for e in node.elts]
            if "gh" in values[:4]:
                found.append(values)
    return found


class StringCorpusReplay(unittest.TestCase):
    """The two text hooks' tests pass whole command strings to `find_*`; replay
    them through `command_segments` and compare at the hook's own level."""

    def _replay(self, test_file, functions, find, consumer):
        entries, unmeasured = corpus(test_file, functions)
        self.assertGreater(len(entries), 0, f"{test_file}: the scan found no replayable call (a moved or empty corpus)")
        mismatched = []
        for command in entries:
            old_found = find(command)
            try:
                segments = command_segments(command)
            except ValueError:
                segments = None
            if old_found is None or segments is None:
                # An untokenizable command: both sides must agree it is untokenizable
                # (the hooks fail open there), never one reading it and the other not.
                if (old_found is None) != (segments is None):
                    mismatched.append((command, old_found, segments))
                continue
            if consumer == "migration":
                old = {_norm("issue", repo, number) for repo, number in old_found}
            else:
                old = {_norm(kind, repo, number) for repo, number, kind in old_found}
            new = set()
            for tokens in segments:
                new |= {_norm(*item) for item in _sig_from_result(consumer, A.classify(tokens, {}))}
            if old != new:
                mismatched.append((command, old, new))
        return entries, unmeasured, mismatched

    def test_migration_hook_corpus(self):
        entries, unmeasured, mismatched = self._replay(
            "test_block_data_migration_close.py", ("find_close_targets",),
            block_data_migration_close.find_close_targets, "migration")
        self.assertEqual([m for m in mismatched if m[0] not in CORPUS_CLASSIFIED["migration"]], [], mismatched)
        self.assertEqual(set(CORPUS_CLASSIFIED["migration"]), {m[0] for m in mismatched}, "a stale classification")
        self.assertGreaterEqual(len(entries) + unmeasured, 1)

    def test_preclose_hook_corpus(self):
        entries, unmeasured, mismatched = self._replay(
            "test_block_missing_preclose_inspection.py", ("find_gated_targets",),
            block_missing_preclose_inspection.find_gated_targets, "preclose")
        self.assertEqual([m for m in mismatched if m[0] not in CORPUS_CLASSIFIED["preclose"]], [], mismatched)
        self.assertEqual(set(CORPUS_CLASSIFIED["preclose"]), {m[0] for m in mismatched}, "a stale classification")
        self.assertGreaterEqual(len(entries) + unmeasured, 1)

    def test_batch_argv_corpus(self):
        entries = argv_lists("test_batch_auth.py")
        self.assertGreater(len(entries), 0, "test_batch_auth.py: the scan found no replayable argv list")
        mismatched = []
        for tokens in entries:
            old, new = old_sig("batch", tokens), new_sig("batch", tokens)
            if old != new:
                mismatched.append((" ".join(tokens), old, new))
        self.assertEqual(
            [m for m in mismatched if m[0] not in CORPUS_CLASSIFIED["batch"]], [], mismatched)
        self.assertEqual(set(CORPUS_CLASSIFIED["batch"]), {m[0] for m in mismatched}, "a stale classification")


#: Corpus entries whose old and new answers differ, classified like DISAGREEMENTS.
#: Filled from the first run; empty means the corpora agree with the adapter.
CORPUS_CLASSIFIED: dict[str, dict[str, str]] = {
    "batch": {},
    "migration": {},
    "preclose": {
        "gh api -X PUT repos/o/r/pulls/7": (
            "intended: gh_argv gates any PUT on a pull request as a merge (fail-closed shim); the preclose test"
            " pins it as not a merge. GitHub's PUT on /pulls/N is not a documented method, so the adopter (F1499)"
            " either keeps the old answer with a path-suffix check or accepts the stricter one, and says which"),
    },
}


if __name__ == "__main__":
    unittest.main()
