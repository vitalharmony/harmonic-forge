#!/usr/bin/env python3
"""The gh close/merge adapter's outcome table, per-consumer verdicts and flag-table drift (harmonic-forge#1496).

`ROWS` is the shared argv table. `ADAPTER_OUTCOME` is the adapter's column and
`VERDICT` the per-consumer columns: what each consumer (BATCH, the data-migration
hook, the preclose hook) must answer, ask / refuse / pass / not-a-close, for each
adapter outcome. `test_gh_argv_adapters_differential.py` checks that `VERDICT`
applied to the adapter reproduces what each old recognizer answers today, row by
row, and lists every place it does not.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gh_argv  # noqa: E402
import gh_argv_adapters as A  # noqa: E402

ROWS: dict[str, list[str]] = {
    "bound_issue": ["gh", "issue", "close", "12", "--repo", "o/r"],
    "bound_merge": ["gh", "pr", "merge", "--match-head-commit", "123456", "7"],
    "bound_api_close": ["gh", "api", "-X", "PATCH", "repos/o/r/issues/5", "-f", "state=closed"],
    "bound_api_merge": ["gh", "api", "-X", "PUT", "repos/o/r/pulls/5/merge"],
    "bound_inline_repo": ["GH_REPO=a/b", "gh", "issue", "close", "12"],
    "unbound_variable": ["gh", "issue", "close", "$N"],
    "unbound_issue_no_arg": ["gh", "issue", "close"],
    "unbound_merge_no_arg": ["gh", "pr", "merge"],
    "unparseable_flag": ["gh", "issue", "close", "12", "--bogus"],
    "unprovable_graphql_input": ["gh", "api", "graphql", "--input", "body.json", "-X", "POST"],
    "unprovable_placeholder_merge": ["gh", "api", "-X", "PUT", "repos/{owner}/{repo}/pulls/5/merge"],
    "unprovable_placeholder_close": ["gh", "api", "-X", "PATCH", "repos/{owner}/{repo}/issues/5", "-f", "state=closed"],
    "unprovable_flag_before_root": ["gh", "-R", "o/r", "issue", "close", "12"],
    "unprovable_alias": ["gh", "triage-done", "12", "--repo", "o/r"],
    "unprovable_env_unset": ["env", "-u", "GH_REPO", "gh", "issue", "close", "12"],
    "unprovable_sudo": ["sudo", "gh", "pr", "merge", "5"],
    "unprovable_xargs": ["xargs", "-n1", "gh", "issue", "close"],
    "account_wrapper_merge": ["gh-as", "vitalharmony", "gh", "pr", "merge", "5", "--repo", "o/r"],
    "allowed_view": ["gh", "issue", "view", "12"],
    "not_gated_root": ["gh", "repo", "view"],
    "not_gh": ["git", "status"],
}

ADAPTER_OUTCOME = {
    "bound_issue": A.BOUND, "bound_merge": A.BOUND, "bound_api_close": A.BOUND,
    "bound_api_merge": A.BOUND, "bound_inline_repo": A.BOUND, "unbound_variable": A.UNBOUND,
    "unbound_issue_no_arg": A.UNBOUND, "unbound_merge_no_arg": A.UNBOUND,
    "unparseable_flag": A.UNPARSEABLE, "unprovable_graphql_input": A.UNPROVABLE,
    "unprovable_placeholder_merge": A.UNPROVABLE, "unprovable_placeholder_close": A.UNPROVABLE,
    "unprovable_flag_before_root": A.UNPROVABLE,
    "unprovable_alias": A.UNPROVABLE, "unprovable_env_unset": A.UNPROVABLE, "unprovable_sudo": A.UNPROVABLE,
    "unprovable_xargs": A.UNPROVABLE, "account_wrapper_merge": A.BOUND, "allowed_view": A.ALLOWED, "not_gated_root": A.NOT_GATED,
    "not_gh": A.NOT_GATED,
}

ASK, REFUSE, PASS, NOT_A_CLOSE = "ask", "refuse", "pass", "not-a-close"
CONSUMERS = ("batch", "migration", "preclose")
#: Each consumer's scope: the migration hook reads issue closes only.
SCOPE = {"batch": ("issue", "pr"), "migration": ("issue",), "preclose": ("issue", "pr")}

#: The per-consumer columns: the verdict each consumer gives, by adapter outcome
#: and kind (None: any kind, or no kind), before its own authorization lookup.
#: `pass` is that lookup's answer for a granted target and appears in no cell:
#: no outcome passes unconditionally. BATCH asks on anything it cannot bind
#: (its conservative ask); the text hooks refuse anything that is or may be a close
#: or merge, and stay fail-open only on a command they cannot tokenize at all.
VERDICT: dict[str, dict[tuple[str, str | None], str]] = {
    "batch": {
        (A.BOUND, None): ASK, (A.UNBOUND, None): ASK, (A.UNPARSEABLE, None): ASK,
        (A.UNPROVABLE, None): ASK, (A.ALLOWED, None): NOT_A_CLOSE, (A.NOT_GATED, None): NOT_A_CLOSE,
    },
    "migration": {
        (A.BOUND, None): REFUSE, (A.UNBOUND, None): REFUSE, (A.UNPARSEABLE, None): REFUSE,
        (A.UNPROVABLE, None): REFUSE, (A.ALLOWED, None): NOT_A_CLOSE, (A.NOT_GATED, None): NOT_A_CLOSE,
    },
    "preclose": {
        (A.BOUND, None): REFUSE, (A.UNBOUND, None): REFUSE, (A.UNPARSEABLE, None): REFUSE,
        (A.UNPROVABLE, None): REFUSE, (A.ALLOWED, None): NOT_A_CLOSE, (A.NOT_GATED, None): NOT_A_CLOSE,
    },
}


def verdict(consumer: str, result: A.Result) -> str:
    """The consumer's verdict for one adapter result, from `VERDICT`."""
    cells = VERDICT[consumer]
    if result.kind is not None and result.kind not in SCOPE[consumer]:
        return NOT_A_CLOSE
    key = (result.outcome, result.kind)
    return cells[key] if key in cells else cells[(result.outcome, None)]


class OutcomeTable(unittest.TestCase):
    def test_every_row_has_the_named_adapter_outcome(self):
        for name, tokens in ROWS.items():
            with self.subTest(row=name):
                self.assertEqual(A.classify(tokens, {}).outcome, ADAPTER_OUTCOME[name])

    def test_the_table_covers_every_outcome(self):
        self.assertEqual(
            set(ADAPTER_OUTCOME.values()),
            {A.BOUND, A.UNBOUND, A.UNPARSEABLE, A.UNPROVABLE, A.ALLOWED, A.NOT_GATED})

    def test_every_consumer_has_a_verdict_for_every_row(self):
        for consumer in CONSUMERS:
            for name, tokens in ROWS.items():
                with self.subTest(consumer=consumer, row=name):
                    self.assertIn(verdict(consumer, A.classify(tokens, {})), (ASK, REFUSE, PASS, NOT_A_CLOSE))

    def test_batch_asks_on_every_close_or_merge_it_cannot_bind(self):
        for name in ("unbound_variable", "unbound_issue_no_arg", "unparseable_flag", "unprovable_placeholder_merge",
                     "unprovable_flag_before_root", "unprovable_alias"):
            with self.subTest(row=name):
                self.assertEqual(verdict("batch", A.classify(ROWS[name], {})), ASK)

    def test_no_outcome_passes_unconditionally(self):
        self.assertNotIn(PASS, {cell for cells in VERDICT.values() for cell in cells.values()})

    def test_the_adapter_reports_gh_argv_exemption_but_the_preclose_column_never_trusts_it(self):
        # gh_argv's not-planned exemption is wider than the preclose hook's
        # canonical shape (`--reason='not planned'`, a REST `-F state_reason`),
        # so the adopter keeps the hook's own exemption check (F1499).
        exempt = A.classify(["gh", "issue", "close", "5", "--repo", "o/r", "--reason=not planned"], {})
        self.assertTrue(exempt.exempt)
        self.assertEqual(verdict("preclose", exempt), REFUSE)

    def test_bound_merge_carries_the_head_commit(self):
        self.assertEqual(A.classify(ROWS["bound_merge"], {}).heads, ("123456",))

    def test_wrappers_return_every_call_that_cannot_be_proved_not_a_close(self):
        self.assertIsNotNone(A.close_target(ROWS["bound_issue"], {}))
        self.assertIsNone(A.close_target(ROWS["bound_merge"], {}))
        self.assertIsNotNone(A.merge_target(ROWS["bound_merge"], {}))
        self.assertIsNone(A.merge_target(ROWS["bound_issue"], {}))
        for name in ("unparseable_flag", "unprovable_placeholder_merge", "unprovable_flag_before_root"):
            with self.subTest(row=name):
                self.assertIsNotNone(A.close_target(ROWS[name], {}))
                self.assertIsNotNone(A.merge_target(ROWS[name], {}))
        self.assertIsNone(A.close_target(ROWS["allowed_view"], {}))

    def test_an_inline_repo_assignment_binds_and_overrides_the_inherited_one(self):
        result = A.classify(ROWS["bound_inline_repo"], {"GH_REPO": "old/repo"})
        self.assertEqual((result.repo, result.number), ("a/b", "12"))

    def test_a_non_repo_assignment_does_not_change_the_target(self):
        result = A.classify(["env", "FOO=1", "gh", "issue", "close", "12", "--repo", "o/r"], {})
        self.assertEqual((result.outcome, result.repo, result.number), (A.BOUND, "o/r", "12"))


class UnmodeledPrefixes(unittest.TestCase):
    """The default is the safe answer: gh behind any prefix this module does not
    model is never not-a-close and never bound to a repo the prefix may change."""

    PREFIXES = (["sudo"], ["xargs", "-n1"], ["nohup"], ["timeout", "5"], ["<f"], ["env", "-u", "X"],
                ["env", "-i"], ["env", "--"], ["command"], ["exec"])

    def test_gh_behind_an_unmodeled_prefix_is_never_not_a_close_or_bound(self):
        for prefix in self.PREFIXES:
            for tail in (["gh", "issue", "close", "12"], ["gh", "pr", "merge", "5"]):
                with self.subTest(argv=prefix + tail):
                    result = A.classify(prefix + tail, {"GH_REPO": "x/y"})
                    self.assertNotIn(result.outcome, (A.NOT_GATED, A.ALLOWED, A.BOUND), result)

    def test_gh_inside_a_shell_c_script_is_unprovable_never_parsed(self):
        for script in ("echo ok && gh issue close 12 --repo o/r", "GH_REPO=a/b gh issue close 12",
                       "gh-as vitalharmony gh pr merge 5", "x; gha vh issue close 5"):
            for shell in (["bash", "-lc"], ["nohup", "bash", "-c"], ["env", "-u", "X", "sh", "-c"]):
                with self.subTest(argv=shell + [script]):
                    self.assertEqual(A.classify(shell + [script], {}).outcome, A.UNPROVABLE)
        self.assertEqual(A.classify(["bash", "-c", "ls -la"], {}).outcome, A.NOT_GATED)

    def test_a_shell_word_as_a_flag_value_does_not_hide_a_close(self):
        result = A.classify(["gh", "issue", "close", "12", "--repo", "o/r", "--comment", "bash"], {})
        self.assertEqual((result.outcome, result.number), (A.BOUND, "12"))

    def test_an_account_wrapper_behind_an_unmodeled_prefix_is_unprovable(self):
        for argv in (["sudo", "gha", "vh", "issue", "close", "5"],
                     ["env", "-u", "GH_REPO", "gha", "vh", "issue", "close", "5"]):
            with self.subTest(argv=argv):
                self.assertEqual(A.classify(argv, {"GH_REPO": "x/y"}).outcome, A.UNPROVABLE)

    def test_a_segment_with_no_gh_at_all_is_not_gated(self):
        self.assertEqual(A.classify(["sudo", "ls"], {}).outcome, A.NOT_GATED)


class FlagTableDrift(unittest.TestCase):
    """A gh upgrade that outdates the pinned flag table must never turn a close
    into not-a-close, nor bind it to another number. The hard case: a value flag
    before the positional (`--comment x 12`)."""

    ARGV = ["gh", "issue", "close", "--comment", "x", "12", "--repo", "o/r"]

    def _classify_with(self, table, argv=None):
        with mock.patch.object(gh_argv, "load_table", return_value=table):
            return A.classify(argv or self.ARGV, {})

    def _drifted(self, change):
        table = copy.deepcopy(gh_argv.load_table())
        change(table["commands"]["issue close"]["flags"])
        return table

    def _assert_never_permissive_or_misbound(self, result):
        self.assertNotIn(result.outcome, (A.ALLOWED, A.NOT_GATED), result)
        if result.outcome == A.BOUND:
            self.assertEqual(result.number, "12", result)

    def test_the_pinned_table_binds_the_number_behind_a_value_flag(self):
        result = A.classify(self.ARGV, {})
        self.assertEqual((result.outcome, result.number), (A.BOUND, "12"))

    def test_a_flag_removed_from_the_table_is_unparseable(self):
        result = self._classify_with(self._drifted(lambda flags: flags.pop("--comment")))
        self.assertEqual(result.outcome, A.UNPARSEABLE)

    def test_a_value_flag_the_table_calls_boolean_never_binds_another_number(self):
        table = self._drifted(lambda flags: flags["--comment"].update(value=False))
        self._assert_never_permissive_or_misbound(self._classify_with(table))

    def test_a_drifted_value_that_reads_as_help_is_never_allowed(self):
        table = self._drifted(lambda flags: flags["--comment"].update(value=False))
        argv = ["gh", "issue", "close", "--comment", "--help", "12", "--repo", "o/r"]
        self._assert_never_permissive_or_misbound(self._classify_with(table, argv))

    def test_a_drifted_api_value_that_reads_as_help_is_never_not_a_merge(self):
        """harmonic-forge#1496 reforge pass 1 (cross-family): the help safeguard
        covers `gh api`, which can merge, not only issue close and pr merge."""
        table = copy.deepcopy(gh_argv.load_table())
        table["commands"]["api"]["flags"]["--jq"].update(value=False)
        argv = ["gh", "api", "-X", "PUT", "repos/o/r/pulls/9/merge", "--jq", "--help"]
        with mock.patch.object(gh_argv, "load_table", return_value=table):
            self.assertIsNotNone(A.merge_target(argv, {}))
            self.assertNotIn(A.classify(argv, {}).outcome, (A.ALLOWED, A.NOT_GATED))

    def test_a_method_flag_swallowed_by_a_drifted_api_flag_is_unprovable(self):
        """harmonic-forge#1496 re-scope: a drifted table that makes `--jq` take a
        value swallows `-X PUT`; the merge must not read as a GET."""
        table = copy.deepcopy(gh_argv.load_table())
        self.assertFalse(table["commands"]["api"]["flags"]["--include"].get("value"))
        table["commands"]["api"]["flags"]["--include"].update(value=True)  # the drift
        # Attached value: nothing is left over for the one-endpoint rule to refuse,
        # so only the swallowed-method guard stands between drift and a GET.
        argv = ["gh", "api", "--include", "--method=PUT", "repos/o/r/pulls/9/merge"]
        self.assertEqual(A.classify(argv, {}).outcome, A.BOUND)  # the pinned table binds it
        with mock.patch.object(gh_argv, "load_table", return_value=table):
            self.assertEqual(A.classify(argv, {}).outcome, A.UNPROVABLE)
            self.assertIsNotNone(A.merge_target(argv, {}))
        self.assertEqual(A.classify(["gh", "api", "-X", "GET", "repos/o/r/pulls/9"], {}).outcome, A.ALLOWED)

    def test_a_swallowed_method_after_an_earlier_one_or_in_a_cluster_is_unprovable(self):
        table = copy.deepcopy(gh_argv.load_table())
        table["commands"]["api"]["flags"]["--include"].update(value=True)
        with mock.patch.object(gh_argv, "load_table", return_value=table):
            for argv in (["gh", "api", "-X", "GET", "--include", "--method=PUT", "repos/o/r/pulls/9/merge"],
                         ["gh", "api", "--include", "-XPUT", "repos/o/r/pulls/9/merge"]):
                self.assertNotIn(A.classify(argv, {}).outcome, (A.ALLOWED, A.NOT_GATED), argv)


if __name__ == "__main__":
    unittest.main()
