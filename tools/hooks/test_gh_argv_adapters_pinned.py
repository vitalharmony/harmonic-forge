#!/usr/bin/env python3
"""The differential's agreeing rows and corpus entries, pinned by id (harmonic-forge#1499).

Round 1 survivor 2: `test_gh_argv_adapters_differential` asserts only on the
rows and corpus entries where the old recognizer and the adapter disagree, so
deleting an agreeing one stayed green. This module pins the exact set of
agreeing ids per consumer, so removing (or adding) any one fails until it is
re-pinned on purpose. Kept apart from the differential, which is over the
300-line cap and may only shrink.

Re-pin after an intended change, from the repo root:
    python3 tools/hooks/test_gh_argv_adapters_pinned.py --pin
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
import test_gh_argv_adapters_differential as D  # noqa: E402

FIXTURE = HERE / "testdata" / "gh_argv_adapters_agreeing.json"
HINT = ("an agreeing entry was added or removed; if intended, re-pin: "
        "python3 tools/hooks/test_gh_argv_adapters_pinned.py --pin")
#: consumer -> (its test modules, the recognizers they reach): the differential's CorpusReplay runs.
CORPUS_RUNS = {
    "migration": ("test_block_data_migration_close", [(block_data_migration_close, "find_close_targets")]),
    "preclose": ("test_block_missing_preclose_inspection,test_block_missing_preclose_inspection_adapter",
                 [(block_missing_preclose_inspection, "find_gated_targets"),
                  (block_missing_preclose_inspection, "find_gated_targets_exempt")]),
    "batch": ("test_batch_auth", [(batch_auth, "classify_issue_close"), (batch_auth, "classify_pr_merge")]),
}


def agreeing_rows() -> dict[str, list[str]]:
    found = D.differences(D.ROWS)
    return {consumer: sorted(name for name in D.ROWS if (consumer, name) not in found)
            for consumer in D.CONSUMERS}


def agreeing_corpus(consumer: str) -> list[str]:
    """Every replayed entry's key (as `corpus_differences` keys it) that is not a difference."""
    entries = D.recorded(*CORPUS_RUNS[consumer])
    keys = {shlex.join(e) if isinstance(e, list) else e for e in entries if isinstance(e, (list, str))}
    return sorted(keys - set(D.corpus_differences(consumer, entries)))


def pinned() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class AgreeingPinned(unittest.TestCase):
    def test_every_agreeing_row_is_pinned(self):
        self.assertEqual(agreeing_rows(), pinned()["rows"], HINT)

    def test_every_agreeing_corpus_entry_is_pinned(self):
        self.assertEqual(set(CORPUS_RUNS), set(D.CORPUS_CLASSIFIED), "a consumer without a pinned corpus")
        for consumer in CORPUS_RUNS:
            with self.subTest(consumer=consumer):
                self.assertEqual(agreeing_corpus(consumer), pinned()["corpus"][consumer], HINT)


if __name__ == "__main__":
    if sys.argv[1:] == ["--pin"]:
        FIXTURE.write_text(json.dumps({"rows": agreeing_rows(), "corpus": {
            c: agreeing_corpus(c) for c in CORPUS_RUNS}}, indent=1) + "\n", encoding="utf-8")
    else:
        unittest.main()
