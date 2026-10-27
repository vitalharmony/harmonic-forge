"""harmonic-forge#1051: the runner's verdict reducer is deterministic, ordered
and fails closed."""
from __future__ import annotations

import copy
import itertools
import random
import unittest

import verdict



def green_facts() -> dict:
    return {
        "eligible": True,
        "deny_hits": [],
        "permission_denials": 0,
        "budget_exceeded": False,
        "self_flag": "",
        "check_rc": 0,
        "kill": [{"ac": "AC1", "base_rc": 1, "head_rc": 0}],
        "kill_receipt": True,
        "live": {"pr_exists": True, "head_sha_matches": True},
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
            "budget", "tool-refusal", "self-flag"))

    def test_precedence_is_a_permutation_of_the_enum(self):
        self.assertEqual(sorted(verdict.PRECEDENCE), sorted(verdict.SET_ASIDE_REASONS))
        self.assertEqual(verdict.PRECEDENCE, (
            "ineligible", "deny-path", "tool-refusal", "budget", "self-flag",
            "check-fail", "kill-check-fail"))

    def test_all_facts_passing_is_green(self):
        self.assertEqual(verdict.reduce(green_facts()),
                         {"state": "green", "reason": None, "detail": "all clauses passed",
                      "reasons": []})

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
            with self.subTest(reason=reason):
                facts = green_facts()
                facts[key] = None
                self.assertEqual(verdict.reduce(facts)["reason"], reason)

    def test_none_self_flag_is_a_gap_not_no_flag(self):
        facts = green_facts()
        facts["self_flag"] = None
        result = verdict.reduce(facts)
        self.assertEqual((result["state"], result["reason"]), ("set-aside", "self-flag"))
        self.assertIn("fact gap", result["detail"])

    def test_empty_self_flag_means_no_flag(self):
        self.assertEqual(verdict.reduce(green_facts())["state"], "green")

    def test_wrong_typed_fact_sets_aside_per_clause(self):
        wrong = {"eligible": "yes", "deny_hits": "tools/hooks", "permission_denials": True,
                 "budget_exceeded": 0, "self_flag": 7, "check_rc": "0",
                 "kill": {"ac": "AC1"}}
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
        cases = [("live", "pr_exists", "check-fail"), ("live", "head_sha_matches", "check-fail")]
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

    def test_ci_is_not_read_by_reduce(self):
        """harmonic-forge#1195 AC1: CI is merge_authorization's input only."""
        for ci in ("red", "pending", "success", None):
            with self.subTest(ci=ci):
                facts = green_facts()
                facts["live"]["ci"] = ci
                self.assertEqual(verdict.reduce(facts)["state"], "green")


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

    def test_failed_receipt_with_passing_facts_is_never_green(self):
        """harmonic-forge#1147 AC6: every kill fact fail-on-base/pass-on-head,
        but kill_check.py's receipt failed -- set aside, never green."""
        facts = green_facts()
        facts["kill_receipt"] = False
        result = verdict.reduce(facts)
        self.assertEqual(result["state"], "set-aside")
        self.assertEqual(result["reason"], "kill-check-fail")
        self.assertIn("receipt failed", result["detail"])

    def test_missing_kill_receipt_is_a_fact_gap(self):
        facts = green_facts()
        del facts["kill_receipt"]
        result = verdict.reduce(facts)
        self.assertEqual(result["reason"], "kill-check-fail")
        self.assertIn("kill_receipt", result["detail"])

    def test_budget_detail_names_the_bound(self):
        facts = hit("budget")
        facts["budget_detail"] = "agent wall timeout"
        self.assertEqual(verdict.reduce(facts)["detail"], "agent wall timeout")
        facts["budget_detail"] = "  "
        self.assertEqual(verdict.reduce(facts)["detail"], "budget exceeded")

    def test_stale_pr_head_is_check_fail(self):
        facts = green_facts()
        facts["live"]["head_sha_matches"] = False
        self.assertEqual(verdict.reduce(facts)["reason"], "check-fail")

    def test_no_pr_is_check_fail(self):
        facts = green_facts()
        facts["live"]["pr_exists"] = False
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
              {"status": "complete", "survivors": 0},
              {"pr_exists": True, "head_sha_matches": True}]

    def test_no_input_yields_a_reason_outside_the_enum(self):
        rng = random.Random(1051)
        keys = list(FACT_OF.values()) + ["live", "unrelated", "preclose", "kill_receipt"]
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


class AllReasons(unittest.TestCase):
    """harmonic-forge#1112 AC8."""

    def test_every_failing_clause_is_listed(self):
        out = verdict.reduce(hit("check-fail", "tool-refusal", "kill-check-fail"))
        self.assertEqual(out["reason"], "tool-refusal")
        self.assertEqual([r["reason"] for r in out["reasons"]],
                         ["tool-refusal", "check-fail", "kill-check-fail"])
        self.assertEqual(out["detail"], out["reasons"][0]["detail"])

    def test_a_fact_gap_still_stops_at_its_clause(self):
        facts = hit("tool-refusal")
        del facts["check_rc"]
        out = verdict.reduce(facts)
        self.assertEqual([r["reason"] for r in out["reasons"]], ["tool-refusal", "check-fail"])
        self.assertIn("fact gap", out["reasons"][-1]["detail"])


class MachineChecksOnly(unittest.TestCase):
    """harmonic-forge#1195 AC1/AC2: no model verdict is in the accept path."""

    def test_no_preclose_key_reduces_to_green(self):
        facts = green_facts()
        self.assertNotIn("preclose", facts)
        self.assertEqual(verdict.reduce(facts)["state"], "green")

    def test_a_failing_preclose_fact_is_ignored(self):
        facts = green_facts()
        facts["preclose"] = {"status": "planned", "survivors": 14, "provenance": "in-family"}
        self.assertEqual(verdict.reduce(facts)["state"], "green")

    def test_preclose_survivor_is_gone(self):
        self.assertNotIn("preclose-survivor", verdict.SET_ASIDE_REASONS)
        self.assertNotIn("preclose-survivor", verdict.PRECEDENCE)
        self.assertFalse(hasattr(verdict, "CROSS" "_FAMILY_PREFIX"))  # split: AC6 greps for it

    def test_self_flag_only_ever_sets_aside(self):
        """For every combination of the other clauses, a non-blank flag is
        set-aside, and the flag never turns a set-aside into green."""
        others = [r for r in verdict.PRECEDENCE if r != "self-flag"]
        for size in range(len(others) + 1):
            for combo in itertools.combinations(others, size):
                with self.subTest(combo=combo):
                    flagged = verdict.reduce(hit("self-flag", *combo))
                    self.assertEqual(flagged["state"], "set-aside")
                    self.assertIn("self-flag", [r["reason"] for r in flagged["reasons"]])
                    unflagged = verdict.reduce(hit(*combo))
                    if unflagged["state"] == "set-aside":
                        self.assertEqual(flagged["state"], "set-aside")
                    if not combo:
                        self.assertEqual(flagged["reason"], "self-flag")

    def test_flag_alone_never_produces_green(self):
        for flag in ("x", "cannot do AC1", "\tno\n"):
            facts = green_facts()
            facts["self_flag"] = flag
            self.assertEqual(verdict.reduce(facts), {
                "state": "set-aside", "reason": "self-flag",
                "detail": verdict.one_line(f"agent self-flag: {flag}"),
                "reasons": [{"reason": "self-flag",
                             "detail": verdict.one_line(f"agent self-flag: {flag}")}]})


class MergeAuthorization(unittest.TestCase):
    """harmonic-forge#1195 AC3."""

    def test_only_green_authorizes(self):
        self.assertEqual(verdict.merge_authorization("green", "verify:success"),
                         {"authorized": True, "ci": "green", "detail": "verify:success",
                          "set_aside": False})

    def test_pending_absent_unknown_leave_the_item_open(self):
        for state in ("pending", "absent", "unknown"):
            with self.subTest(state=state):
                merge = verdict.merge_authorization(state, "waiting")
                self.assertFalse(merge["authorized"])
                self.assertFalse(merge["set_aside"])
                self.assertEqual(merge["ci"], state)

    def test_red_is_not_authorized_and_sets_aside_as_check_fail(self):
        merge = verdict.merge_authorization("red", "verify:failure")
        self.assertEqual((merge["authorized"], merge["set_aside"]), (False, True))
        out = verdict.with_merge(verdict.reduce(green_facts()), merge)
        self.assertEqual((out["state"], out["reason"]), ("set-aside", "check-fail"))
        self.assertIn("CI is red", out["detail"])

    def test_red_after_a_set_aside_keeps_the_first_reason(self):
        out = verdict.with_merge(verdict.reduce(hit("tool-refusal")),
                                 verdict.merge_authorization("red", "x"))
        self.assertEqual(out["reason"], "tool-refusal")
        self.assertEqual([r["reason"] for r in out["reasons"]], ["tool-refusal", "check-fail"])

    def test_non_red_leaves_the_outcome_unchanged(self):
        green = verdict.reduce(green_facts())
        for state in ("green", "pending", "absent", "unknown"):
            self.assertIs(verdict.with_merge(green, verdict.merge_authorization(state, "")), green)

    def test_a_state_outside_gate_ci_is_a_gap(self):
        for state in ("success", None, 3, "GREEN"):
            with self.subTest(state=state):
                merge = verdict.merge_authorization(state, "d")
                self.assertEqual((merge["authorized"], merge["ci"]), (False, "unknown"))

    def test_detail_is_one_line(self):
        merge = verdict.merge_authorization("pending", "a\nb" + "x" * 400)
        self.assertNotIn("\n", merge["detail"])
        self.assertLessEqual(len(merge["detail"]), 200)


if __name__ == "__main__":
    unittest.main()
