"""harmonic-forge#1051: the runner's verdict reducer is deterministic, ordered
and fails closed."""
from __future__ import annotations

import copy
import itertools
import random
import unittest

import verdict

CROSS = "Red-team provenance: cross-family (codex / gpt-6.1-sol) — 3 of 3 assumption(s) checked"
FALLBACK = "Red-team provenance: in-family fallback (claude-opus-5-5) — cross-family call failed"


def green_facts() -> dict:
    return {
        "eligible": True,
        "deny_hits": [],
        "permission_denials": 0,
        "budget_exceeded": False,
        "self_flag": None,
        "check_rc": 0,
        "kill": [{"ac": "AC1", "base_rc": 1, "head_rc": 0}],
        "preclose": {"status": "complete", "survivors": 0, "provenance": CROSS},
        "live": {"pr_exists": True, "head_sha_matches": True, "ci": "green"},
    }


#: One fact edit per clause that makes exactly that clause hit.
HITS = {
    "ineligible": lambda f: f.update(eligible=False),
    "deny-path": lambda f: f.update(deny_hits=["tools/hooks/batch_gate.py"]),
    "tool-refusal": lambda f: f.update(permission_denials=2),
    "budget": lambda f: f.update(budget_exceeded=True),
    "self-flag": lambda f: f.update(self_flag="cannot meet AC2 honestly"),
    "check-fail": lambda f: f.update(check_rc=1),
    "kill-check-fail": lambda f: f["kill"].append({"ac": "AC2", "base_rc": 0, "head_rc": 0}),
    "preclose-survivor": lambda f: f["preclose"].update(survivors=1),
}

#: The top-level fact each clause reads, and the reason its absence yields.
FACT_OF = {
    "ineligible": "eligible",
    "deny-path": "deny_hits",
    "tool-refusal": "permission_denials",
    "budget": "budget_exceeded",
    "self-flag": "self_flag",
    "check-fail": "check_rc",
    "kill-check-fail": "kill",
    "preclose-survivor": "preclose",
}


def hit(*reasons: str) -> dict:
    facts = green_facts()
    for reason in reasons:
        HITS[reason](facts)
    return facts


class EnumAndShape(unittest.TestCase):
    def test_enum_is_exactly_the_g7_list(self):
        self.assertEqual(verdict.SET_ASIDE_REASONS, (
            "ineligible", "deny-path", "check-fail", "kill-check-fail",
            "preclose-survivor", "budget", "tool-refusal", "self-flag"))

    def test_precedence_is_a_permutation_of_the_enum(self):
        self.assertEqual(sorted(verdict.PRECEDENCE), sorted(verdict.SET_ASIDE_REASONS))
        self.assertEqual(verdict.PRECEDENCE, (
            "ineligible", "deny-path", "tool-refusal", "budget", "self-flag",
            "check-fail", "kill-check-fail", "preclose-survivor"))

    def test_all_facts_passing_is_green(self):
        self.assertEqual(verdict.reduce(green_facts()),
                         {"state": "green", "reason": None, "detail": "all clauses passed"})

    def test_reduce_does_not_mutate_its_input(self):
        facts = hit("check-fail")
        before = copy.deepcopy(facts)
        verdict.reduce(facts)
        self.assertEqual(facts, before)

    def test_non_dict_facts_set_aside(self):
        for value in (None, [], "facts", 3):
            self.assertEqual(verdict.reduce(value)["state"], "set-aside")


class OneClauseEach(unittest.TestCase):
    def test_each_clause_sets_aside_with_its_own_reason(self):
        for reason in verdict.PRECEDENCE:
            with self.subTest(reason=reason):
                result = verdict.reduce(hit(reason))
                self.assertEqual((result["state"], result["reason"]), ("set-aside", reason))

    def test_deny_path_sets_aside(self):
        result = verdict.reduce(hit("deny-path"))
        self.assertEqual(result["reason"], "deny-path")
        self.assertIn("tools/hooks/batch_gate.py", result["detail"])

    def test_blank_self_flag_is_not_a_flag(self):
        facts = green_facts()
        facts["self_flag"] = "   "
        self.assertEqual(verdict.reduce(facts)["state"], "green")


class Precedence(unittest.TestCase):
    def test_every_adjacent_pair_resolves_to_the_earlier(self):
        for earlier, later in zip(verdict.PRECEDENCE, verdict.PRECEDENCE[1:]):
            with self.subTest(pair=(earlier, later)):
                self.assertEqual(verdict.reduce(hit(earlier, later))["reason"], earlier)

    def test_precedence_tool_refusal_before_budget(self):
        self.assertEqual(verdict.reduce(hit("budget", "tool-refusal"))["reason"], "tool-refusal")

    def test_every_pair_resolves_to_the_earlier(self):
        for a, b in itertools.combinations(verdict.PRECEDENCE, 2):
            with self.subTest(pair=(a, b)):
                self.assertEqual(verdict.reduce(hit(b, a))["reason"], a)


class FailsClosed(unittest.TestCase):
    def test_missing_fact_sets_aside_per_clause(self):
        for reason, key in FACT_OF.items():
            with self.subTest(reason=reason):
                facts = green_facts()
                del facts[key]
                result = verdict.reduce(facts)
                self.assertEqual((result["state"], result["reason"]), ("set-aside", reason))
                self.assertIn("fact gap", result["detail"])

    def test_none_fact_sets_aside_per_clause(self):
        for reason, key in FACT_OF.items():
            if key == "self_flag":
                continue  # None is the no-flag value, covered below
            with self.subTest(reason=reason):
                facts = green_facts()
                facts[key] = None
                self.assertEqual(verdict.reduce(facts)["reason"], reason)

    def test_none_self_flag_means_no_flag(self):
        self.assertEqual(verdict.reduce(green_facts())["state"], "green")

    def test_wrong_typed_fact_sets_aside_per_clause(self):
        wrong = {"eligible": "yes", "deny_hits": "tools/hooks", "permission_denials": True,
                 "budget_exceeded": 0, "self_flag": 7, "check_rc": "0",
                 "kill": {"ac": "AC1"}, "preclose": "complete"}
        for reason, key in FACT_OF.items():
            with self.subTest(reason=reason):
                facts = green_facts()
                facts[key] = wrong[key]
                self.assertEqual(verdict.reduce(facts)["reason"], reason)

    def test_missing_live_is_check_fail(self):
        facts = green_facts()
        del facts["live"]
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")

    def test_missing_nested_fields_set_aside(self):
        cases = [("live", "pr_exists", "check-fail"), ("live", "head_sha_matches", "check-fail"),
                 ("live", "ci", "check-fail"), ("preclose", "status", "preclose-survivor"),
                 ("preclose", "survivors", "preclose-survivor"),
                 ("preclose", "provenance", "preclose-survivor")]
        for outer, inner, reason in cases:
            with self.subTest(field=f"{outer}.{inner}"):
                facts = green_facts()
                del facts[outer][inner]
                self.assertEqual(verdict.reduce(facts)["reason"], reason)

    def test_malformed_kill_entry_is_kill_check_fail(self):
        for entry in ({"ac": "AC1", "base_rc": 1}, {"base_rc": 1, "head_rc": 0},
                      {"ac": "AC1", "base_rc": True, "head_rc": 0}, "AC1"):
            with self.subTest(entry=entry):
                facts = green_facts()
                facts["kill"] = [entry]
                self.assertEqual(verdict.reduce(facts)["reason"], "kill-check-fail")

    def test_unknown_ci_state_is_check_fail(self):
        facts = green_facts()
        facts["live"]["ci"] = "success"
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")


class ClauseSemantics(unittest.TestCase):
    def test_empty_kill_list_is_kill_check_fail(self):
        facts = green_facts()
        facts["kill"] = []
        self.assertEqual(verdict.reduce(facts)["reason"], "kill-check-fail")

    def test_kill_check_must_fail_on_base(self):
        facts = green_facts()
        facts["kill"] = [{"ac": "AC1", "base_rc": 0, "head_rc": 0}]
        self.assertEqual(verdict.reduce(facts)["reason"], "kill-check-fail")

    def test_kill_check_must_pass_on_head(self):
        facts = green_facts()
        facts["kill"] = [{"ac": "AC1", "base_rc": 1, "head_rc": 1}]
        self.assertEqual(verdict.reduce(facts)["reason"], "kill-check-fail")

    def test_in_family_fallback_is_preclose_survivor(self):
        facts = green_facts()
        facts["preclose"]["provenance"] = FALLBACK
        result = verdict.reduce(facts)
        self.assertEqual(result["reason"], "preclose-survivor")
        self.assertIn("not cross-family", result["detail"])

    def test_incomplete_preclose_is_preclose_survivor(self):
        facts = green_facts()
        facts["preclose"]["status"] = "planned"
        self.assertEqual(verdict.reduce(facts)["reason"], "preclose-survivor")

    def test_stale_pr_head_is_check_fail(self):
        facts = green_facts()
        facts["live"]["head_sha_matches"] = False
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")

    def test_no_pr_is_check_fail(self):
        facts = green_facts()
        facts["live"]["pr_exists"] = False
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")

    def test_red_ci_is_check_fail_and_pending_is_allowed(self):
        facts = green_facts()
        facts["live"]["ci"] = "pending"
        self.assertEqual(verdict.reduce(facts)["state"], "green")
        facts["live"]["ci"] = "red"
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")


class Detail(unittest.TestCase):
    def test_detail_is_one_line_at_most_200_chars(self):
        facts = green_facts()
        facts["self_flag"] = "line one\nline two\r\n" + "x" * 500
        detail = verdict.reduce(facts)["detail"]
        self.assertNotIn("\n", detail)
        self.assertNotIn("\r", detail)
        self.assertLessEqual(len(detail), 200)


class NeverOutsideTheEnum(unittest.TestCase):
    """Property test: random, mostly-broken facts never escape the enum."""

    VALUES = [None, True, False, 0, 1, -1, "", "x", [], ["a"], {}, {"ac": "AC1"},
              [{"ac": "AC1", "base_rc": 1, "head_rc": 0}], [{"ac": 1}],
              {"status": "complete", "survivors": 0, "provenance": CROSS},
              {"pr_exists": True, "head_sha_matches": True, "ci": "pending"}]

    def test_no_input_yields_a_reason_outside_the_enum(self):
        rng = random.Random(1051)
        keys = list(FACT_OF.values()) + ["live", "unrelated"]
        for _ in range(3000):
            facts = green_facts() if rng.random() < 0.5 else {}
            for key in rng.sample(keys, rng.randint(0, len(keys))):
                facts[key] = rng.choice(self.VALUES)
            result = verdict.reduce(facts)
            self.assertIn(result["state"], ("green", "set-aside"))
            if result["state"] == "green":
                self.assertIsNone(result["reason"])
            else:
                self.assertIn(result["reason"], verdict.SET_ASIDE_REASONS)
            self.assertLessEqual(len(result["detail"]), 200)


if __name__ == "__main__":
    unittest.main()
