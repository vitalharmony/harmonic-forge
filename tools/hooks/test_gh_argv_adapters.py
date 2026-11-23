#!/usr/bin/env python3
"""The gh close/merge adapter's outcome table, flag-table drift and no-consumer-change tests (harmonic-forge#1496).

The differential run against the three old recognizers, and the replay of the
consumers' own test corpora, are in `test_gh_argv_adapters_differential.py`.
Nothing here changes a consumer: `ConsumersUnchanged` fails if one differs from
`origin/main`.
"""
from __future__ import annotations

import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gh_argv  # noqa: E402
import gh_argv_adapters as A  # noqa: E402

#: The shared synthetic argv table: one row per outcome-table row, plus the
#: shapes the consumers parse differently.
ROWS: dict[str, list[str]] = {
    "bound_issue": ["gh", "issue", "close", "12", "--repo", "o/r"],
    "bound_merge": ["gh", "pr", "merge", "--match-head-commit", "123456", "7"],
    "bound_api_close": ["gh", "api", "-X", "PATCH", "repos/o/r/issues/5", "-f", "state=closed"],
    "bound_api_merge": ["gh", "api", "-X", "PUT", "repos/o/r/pulls/5/merge"],
    "unbound_variable": ["gh", "issue", "close", "$N"],
    "unbound_issue_no_arg": ["gh", "issue", "close"],
    "unbound_merge_no_arg": ["gh", "pr", "merge"],
    "unparseable_flag": ["gh", "issue", "close", "12", "--bogus"],
    "unprovable_graphql_input": ["gh", "api", "graphql", "--input", "body.json", "-X", "POST"],
    "allowed_view": ["gh", "issue", "view", "12"],
    "not_gated_root": ["gh", "repo", "view"],
    "not_gh": ["git", "status"],
}

#: The adapter's outcome for each row (the outcome table's adapter column).
ADAPTER_OUTCOME = {
    "bound_issue": A.BOUND, "bound_merge": A.BOUND, "bound_api_close": A.BOUND,
    "bound_api_merge": A.BOUND, "unbound_variable": A.UNBOUND,
    "unbound_issue_no_arg": A.UNBOUND, "unbound_merge_no_arg": A.UNBOUND,
    "unparseable_flag": A.UNPARSEABLE, "unprovable_graphql_input": A.UNPROVABLE,
    "allowed_view": A.ALLOWED, "not_gated_root": A.NOT_GATED, "not_gh": A.NOT_GATED,
}


class OutcomeTable(unittest.TestCase):
    def test_every_row_has_the_named_adapter_outcome(self):
        for name, tokens in ROWS.items():
            with self.subTest(row=name):
                self.assertEqual(A.classify(tokens, {}).outcome, ADAPTER_OUTCOME[name])

    def test_the_table_covers_every_outcome(self):
        self.assertEqual(
            set(ADAPTER_OUTCOME.values()),
            {A.BOUND, A.UNBOUND, A.UNPARSEABLE, A.UNPROVABLE, A.ALLOWED, A.NOT_GATED})

    def test_unprovable_is_its_own_result_not_unbound_and_not_not_a_close(self):
        result = A.classify(ROWS["unprovable_graphql_input"], {})
        self.assertEqual(result.outcome, A.UNPROVABLE)
        self.assertFalse(result.gated)
        self.assertIn("cannot read", result.reason)

    def test_bound_merge_carries_the_head_commit(self):
        self.assertEqual(A.classify(ROWS["bound_merge"], {}).heads, ("123456",))

    def test_close_and_merge_wrappers_split_by_kind(self):
        self.assertIsNotNone(A.close_target(ROWS["bound_issue"], {}))
        self.assertIsNone(A.close_target(ROWS["bound_merge"], {}))
        self.assertIsNotNone(A.merge_target(ROWS["bound_merge"], {}))
        self.assertIsNone(A.merge_target(ROWS["bound_issue"], {}))

    def test_invocation_prefix_is_stripped(self):
        self.assertEqual(
            A.classify(["env", "FOO=1", "gh", "issue", "close", "12", "--repo", "o/r"], {}).outcome, A.BOUND)


class FlagTableDrift(unittest.TestCase):
    """A gh upgrade that outdates the pinned flag table must never turn a close
    into not-a-close. The hard case: a flag that takes a value, placed before the
    positional (`--comment x 12`), so a wrong table can leave `x` or `12` as the
    number."""

    ARGV = ["gh", "issue", "close", "--comment", "x", "12", "--repo", "o/r"]

    def _classify_with(self, table):
        with mock.patch.object(gh_argv, "load_table", return_value=table):
            return A.classify(self.ARGV, {})

    def test_the_pinned_table_binds_the_number_behind_a_value_flag(self):
        result = A.classify(self.ARGV, {})
        self.assertEqual((result.outcome, result.number), (A.BOUND, "12"))

    def test_a_flag_removed_from_the_table_is_unparseable_never_not_a_close(self):
        table = copy.deepcopy(gh_argv.load_table())
        del table["commands"]["issue close"]["flags"]["--comment"]
        result = self._classify_with(table)
        self.assertEqual(result.outcome, A.UNPARSEABLE)
        self.assertNotIn(result.outcome, (A.ALLOWED, A.NOT_GATED))

    def test_a_value_flag_the_table_calls_boolean_is_never_not_a_close(self):
        table = copy.deepcopy(gh_argv.load_table())
        table["commands"]["issue close"]["flags"]["--comment"]["value"] = False
        result = self._classify_with(table)
        self.assertNotIn(result.outcome, (A.ALLOWED, A.NOT_GATED))


class ConsumersUnchanged(unittest.TestCase):
    CONSUMERS = ("batch_auth.py", "block_data_migration_close.py", "block_missing_preclose_inspection.py",
                 "afk_defer.py", "block_undetermined_phase_close.py", "close_gate.py", "gh_argv.py")

    def test_no_consumer_file_differs_from_origin_main(self):
        root = HERE.parent.parent
        merge_base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=root,
                                    capture_output=True, text=True)
        if merge_base.returncode:
            self.skipTest("origin/main is not available here")
        diff = subprocess.run(
            ["git", "diff", "--name-only", merge_base.stdout.strip(), "--",
             *[f"tools/hooks/{name}" for name in self.CONSUMERS]],
            cwd=root, capture_output=True, text=True)
        self.assertEqual(diff.stdout.split(), [], "F1496 changes no consumer")


if __name__ == "__main__":
    unittest.main()
