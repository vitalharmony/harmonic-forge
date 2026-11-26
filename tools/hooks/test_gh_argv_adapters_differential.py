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

import json
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
from test_gh_argv_adapters import ASK, CONSUMERS, NOT_A_CLOSE, PASS, REFUSE, ROWS, SCOPE, verdict  # noqa: E402

CLASSES = {"old-defect", "adapter-defect", "intended"}
MENTION_DENY, UNDECIDED_SET = ("tightening against main to undecided (harmonic-forge#1499 sticky-wicket rule 2): argv names no repo and the command names GH_REPO/GH_HOST, so the target is denied before any read where main bound the checkout"), frozenset({('issue', None, None, False)})

#: harmonic-forge#1497 sticky-wicket PATCH (issuecomment-7182907859): BATCH against main's frozen
#: readers. The classifiers name a target only for a gated call of their own kind; an
#: undecided literal gh call gets None from every reader and asks in `decide()` alone.
BATCH_UNDECIDED = ("verdict unchanged (ask) against main; the adapter cannot decide this literal gh call, "
                   "so no reader names a target and decide() asks on it as undecided")
BATCH_TIGHTER = ("tightening against main: a close or merge main's reader missed, or a literal gh call "
                 "that cannot be proved not to be one, now asks")
BATCH_UNBOUND = ("verdict unchanged (ask) against main; gh_argv's grammar binds no number here, "
                 "where main's private reader picked a digit word")
#: (consumer, row) -> (class, reason, observation): (old verdict, new verdict, old targets, new targets), as observed.
DISAGREEMENTS: dict[tuple[str, str], tuple[str, str, tuple]] = {
    ('batch', 'unparseable_flag'): ('intended', BATCH_UNDECIDED,
        (ASK, ASK, frozenset({('issue', None, '12')}), frozenset())),
    ('batch', 'unprovable_alias'): ('intended', BATCH_TIGHTER, (NOT_A_CLOSE, ASK, frozenset(), frozenset())),
    ('batch', 'unprovable_flag_before_root'): ('intended', BATCH_UNDECIDED,
        (ASK, ASK, frozenset({('issue', 'o/r', '12')}), frozenset())),
    ('batch', 'unprovable_placeholder_close'): ('intended', BATCH_UNDECIDED,
        (ASK, ASK, frozenset({('issue', '{owner}/{repo}', '5')}), frozenset())),
    ('batch', 'unprovable_placeholder_merge'): ('intended', BATCH_UNDECIDED,
        (ASK, ASK, frozenset({('pr', '{owner}/{repo}', '5')}), frozenset())),
    ('migration', 'bound_inline_repo'): ('old-defect', 'main ignored the inline GH_REPO and named no repo; gh reads GH_REPO, and the hook binds it',
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset({('issue', 'a/b', '12')}))),
    ('migration', 'unbound_issue_no_arg'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unbound_variable'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unparseable_flag'): ('intended', 'verdict unchanged (refuse) against main; the hook reports the undecided call as the empty-number target it denies before any read, where main named the number',
        (REFUSE, REFUSE, frozenset({('issue', None, '12')}), frozenset({('issue', None, None)}))),
    ('migration', 'unprovable_alias'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_flag_before_root'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_graphql_input'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_placeholder_close'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('migration', 'unprovable_placeholder_merge'): ('intended', 'tightening against main: main read this literal gh call as not-a-close; the hook denies an undecided literal gh call before any read (harmonic-forge#1498 reforge)',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'bound_inline_repo'): ('intended', MENTION_DENY, (REFUSE, REFUSE, frozenset({('issue', None, '12', False)}), UNDECIDED_SET)),
    ('preclose', 'unbound_issue_no_arg'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unbound_variable'): ('old-defect', 'an unbound issue close passes (fail-open) while BATCH asks on it',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unparseable_flag'): ('intended', 'verdict unchanged (refuse) against main; the hook reports the undecided call as the empty-number target it denies before any read, where main named a target (harmonic-forge#1499)',
        (REFUSE, REFUSE, frozenset({('issue', None, '12', False)}), frozenset({('issue', None, None, False)}))),
    ('preclose', 'unprovable_alias'): ('intended', 'tightening: a call that is or may be a close or merge (gh behind an unmodeled prefix included) is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_env_unset'): ('intended', MENTION_DENY, (REFUSE, REFUSE, frozenset({('issue', None, '12', False)}), UNDECIDED_SET)),
    ('preclose', 'unprovable_flag_before_root'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_graphql_input'): ('intended', 'tightening: a call that is or may be a close or merge (gh behind an unmodeled prefix included) is no longer passed',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_placeholder_close'): ('old-defect', 'a real close the old reader does not recognize passes',
        (NOT_A_CLOSE, REFUSE, frozenset(), frozenset())),
    ('preclose', 'unprovable_placeholder_merge'): ('intended', 'verdict unchanged (refuse) against main; the hook reports the undecided call as the empty-number target it denies before any read, where main named a target (harmonic-forge#1499)',
        (REFUSE, REFUSE, frozenset({('pr', '{owner}/{repo}', '5', False)}), frozenset({('issue', None, None, False)}))),
}


#: Preclose `unprovable_sudo`/`unprovable_xargs` agree with main ([]): stated
#: residuals of F1499's amendment change 3, tracked in harmonic-forge#1577.
PLANTED = ["gh", "issue", "close", "99", "--repo", "x/y"]
#: Main's frozen recognizers (migration: F1498 survivor 3; preclose: F1499 amendment change 4;
#: batch: F1497 sticky-wicket PATCH step 6): never the hook under test.
MAIN_FIXTURES = {c: HERE / "testdata" / f"{c}_main_178ba5f.json" for c in ("migration", "preclose", "batch")}
MAIN = {c: json.loads(f.read_text(encoding="utf-8")) if f.exists() else {} for c, f in MAIN_FIXTURES.items()}
#: BATCH's `decide()` is read with no grant, so it never reads CI (harmonic-forge#1497).
NO_BATCH_STATE = HERE / "testdata" / "no-such-batch-state.json"
_DECIDE = {None: NOT_A_CLOSE, "ask": ASK, "allow": PASS}


def _norm(kind: str, repo, number) -> tuple:
    return (kind, repo or None, str(number) if number not in (None, "") else None)


def _old_targets(consumer: str, tokens: list[str] | None, command: str | None = None) -> frozenset | None:
    """What the old recognizer reports, as (kind, repo, number) targets; None when
    a text hook cannot tokenize the command."""
    command = command if command is not None else shlex.join(tokens)
    if command not in MAIN[consumer]:
        raise AssertionError(f"{command!r} is not in {MAIN_FIXTURES[consumer].name}; "
                             f"regenerate it: python3 tools/hooks/testdata/{consumer}_main_table.py")
    found = MAIN[consumer][command]
    if consumer == "batch":
        return frozenset(_norm(kind, *found[kind]) for kind in ("issue", "pr") if found[kind] is not None)
    if consumer == "migration":
        return None if found is None else frozenset(_norm("issue", r, n) for r, n in found)
    # The preclose hook decides on find_gated_targets_exempt: the exemption bit is
    # part of what it acts on, so it is part of the observation.
    return None if found is None else frozenset(_norm(k, r, n) + (bool(e),) for r, n, k, e in found)


def _hook_targets(consumer: str, command: str) -> frozenset:
    """The hook's own reading, measured against main (F1498 survivor 3, F1499)."""
    if consumer == "migration":
        return frozenset(_norm("issue", r, n) for r, n in block_data_migration_close.find_close_targets(command) or [])
    found = block_missing_preclose_inspection.find_gated_targets_exempt(command) or []
    return frozenset(_norm(k, r, n) + (bool(e),) for r, n, k, e in found)


def _batch_new(command: str) -> tuple[str, frozenset]:
    """BATCH under test: `decide()`'s verdict with no grant, and the two
    classifiers' targets over the command's segments (harmonic-forge#1497)."""
    found = set()
    for tokens in command_segments(command):
        if (close := batch_auth.classify_issue_close(tokens)) is not None:
            found.add(_norm("issue", *close))
        if (merge := batch_auth.classify_pr_merge(tokens)) is not None:
            found.add(_norm("pr", *merge))
    result = batch_auth.decide(command, state_path=NO_BATCH_STATE)
    return _DECIDE[result[0] if result else None], frozenset(found)


def _old_verdict(consumer: str, targets: frozenset) -> str:
    if not targets:
        return NOT_A_CLOSE
    if consumer == "preclose" and all(target[3] for target in targets):
        return PASS
    return ASK if consumer == "batch" else REFUSE


def _strictest(verdicts: list[str]) -> str:
    for candidate in (REFUSE, ASK, PASS):
        if candidate in verdicts:
            return candidate
    return NOT_A_CLOSE


def observe(consumer: str, segments: list[list[str]], old: frozenset, command: str | None = None) -> tuple:
    if consumer == "batch" and command is not None:
        new_verdict, new = _batch_new(command)
        old_verdict = _DECIDE[MAIN["batch"][command]["decide"]]
        seen = old_verdict != NOT_A_CLOSE and new_verdict != NOT_A_CLOSE
        return (old_verdict, new_verdict, old if seen else frozenset(), new if seen else frozenset())
    if consumer in MAIN and command is not None:
        new = _hook_targets(consumer, command)
        new_verdict = _old_verdict(consumer, new)
    else:
        results = [A.classify(tokens, {}) for tokens in segments]
        new_verdict = _strictest([verdict(consumer, r) for r in results])
        new = frozenset(_norm(r.kind, r.repo, r.number) + ((r.exempt,) if consumer == "preclose" else ())
                        for r in results if r.gated and r.kind in SCOPE[consumer])
    old_verdict = _old_verdict(consumer, old)
    seen = old_verdict != NOT_A_CLOSE and new_verdict != NOT_A_CLOSE
    return (old_verdict, new_verdict, old if seen else frozenset(), new if seen else frozenset())


def differences(rows: dict[str, list[str]]) -> dict[tuple[str, str], tuple]:
    found = {}
    for name, tokens in rows.items():
        for consumer in CONSUMERS:
            seen = observe(consumer, [tokens], _old_targets(consumer, tokens), shlex.join(tokens))
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
        planted = {"planted": PLANTED}
        original = batch_auth.classify_issue_close
        batch_auth.classify_issue_close = lambda tokens: None
        try:
            found = differences(planted)
        finally:
            batch_auth.classify_issue_close = original
        self.assertIn(("batch", "planted"), found)


def recorded(test_module: str, hooks: list[tuple[object, str]]) -> list:
    """Every argument the old recognizers receive while the consumer's own test
    module runs: the corpus as executed, so f-string and helper-built commands
    count. The recognizers are wrapped, not replaced, so the tests see the same
    behavior; the run's own pass or fail does not matter here."""
    import io  # noqa: PLC0415
    seen: list = []
    saved = [(module, name, getattr(module, name)) for module, name in hooks]
    for module, name, original in saved:
        def wrapper(arg, *args, _original=original, **kwargs):
            seen.append(list(arg) if isinstance(arg, list) else arg)
            return _original(arg, *args, **kwargs)
        setattr(module, name, wrapper)
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(test_module.split(","))
        outcome = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        for module, name, original in saved:
            setattr(module, name, original)
    if not outcome.wasSuccessful():
        # A failing consumer test may stop before reaching a recognizer, leaving
        # corpus entries unrecorded: that is unmeasured, never agreement.
        raise AssertionError(f"{test_module}: its own tests failed while recording "
                             f"({len(outcome.failures)} failures, {len(outcome.errors)} errors)")
    unique = []
    for arg in seen:
        if arg not in unique:
            unique.append(arg)
    return unique


CORPUS_FIXTURE = HERE / "testdata" / "gh_argv_adapters_corpus.json"


def _load_corpus_classified() -> dict:
    """Corpus entries whose observation differs, consumer -> command -> (class,
    reason, observation); the fixture stores each target set as a sorted list."""
    raw = json.loads(CORPUS_FIXTURE.read_text(encoding="utf-8"))
    return {consumer: {cmd: (klass, reason, (obs[0], obs[1], frozenset(map(tuple, obs[2])),
                                             frozenset(map(tuple, obs[3]))))
                       for cmd, (klass, reason, obs) in entries.items()}
            for consumer, entries in raw.items()}


CORPUS_CLASSIFIED = _load_corpus_classified()


def corpus_differences(consumer: str, entries: list) -> dict:
    mismatched = {}
    for entry in entries:
        if isinstance(entry, list):
            seen = observe(consumer, [entry], _old_targets(consumer, entry), shlex.join(entry))
            key = shlex.join(entry)
        elif isinstance(entry, str):
            old = _old_targets(consumer, None, entry)
            try:
                segments = command_segments(entry)
            except ValueError:
                segments = None
            if old is None or segments is None:
                if (old is None) != (segments is None):  # both must agree it cannot be tokenized
                    mismatched[entry] = ("untokenizable", old is None, segments is None)
                continue
            seen, key = observe(consumer, segments, old, entry), entry
        else:
            mismatched[repr(entry)] = ("unmeasured", type(entry).__name__)
            continue
        if seen[0] != seen[1] or seen[2] != seen[3]:
            mismatched[key] = seen
    return mismatched


class CorpusReplay(unittest.TestCase):
    """Each consumer's existing tests are run with its recognizers wrapped; every
    argument they received is replayed against the adapter. An argument that
    cannot be replayed is listed as unmeasured, which fails like a difference."""

    def _check(self, consumer, test_module, hooks):
        entries = recorded(test_module, hooks)
        self.assertGreater(len(entries), 0, f"{test_module}: its tests reached no recognizer")
        mismatched = corpus_differences(consumer, entries)
        classified = CORPUS_CLASSIFIED[consumer]
        wrong = {c: v for c, v in mismatched.items() if c not in classified or classified[c][2] != v}
        self.assertEqual(wrong, {}, "unclassified, unmeasured, or changed since classified")
        self.assertEqual(set(classified), set(mismatched), "a stale classification")
        for klass, reason, _ in classified.values():
            self.assertIn(klass, CLASSES)
            self.assertTrue(reason.strip())

    def test_migration_hook_corpus(self):
        self._check("migration", "test_block_data_migration_close",
                    [(block_data_migration_close, "find_close_targets")])

    def test_preclose_hook_corpus(self):
        self._check("preclose", "test_block_missing_preclose_inspection,test_block_missing_preclose_inspection_adapter",
                    [(block_missing_preclose_inspection, "find_gated_targets"),
                     (block_missing_preclose_inspection, "find_gated_targets_exempt")])

    def test_batch_corpus(self):
        self._check("batch", "test_batch_auth",
                    [(batch_auth, "classify_issue_close"), (batch_auth, "classify_pr_merge")])


if __name__ == "__main__":
    unittest.main()
