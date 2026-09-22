#!/usr/bin/env python3
"""Tests for the Plan-First / Implementation-Spec cross-field rule (a private-repo incident).

`validate_handoff` never related two fields to each other, so a Plan-First
handoff carrying a complete step-by-step spec passed — the exact shape of
ADR-005's founding incident (a private-repo incident), invisible to the gate that exists to
prevent it.

The fixtures below are the real shapes of four live handoffs, not invented
ones. The rule was chosen by measuring against 333 real handoffs and hand-
reading these four; a fixture that paraphrased them would pass while the
rule was wrong about the corpus it has to run on.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402


def spec(body: str) -> str:
    return f"### Implementation Spec\n{body}\n\n### Test Cases (for Lane 3)\n1. x\n"


#: a private-repo incident — the genuine violation, and the founding incident's shape:
#: substantive Delegated Judgment Calls plus a complete seven-step spec, no
#: marker anywhere. Implemented straight from that spec with no plan round.
H1189 = spec(
    "1. Read `scripts/gate_scheduler_lease.py` in full, including the "
    "a private-repo incident docstring history at the top.\n"
    "2. Add the refcount property.\n"
    "3. Wire release.\n4. Tests.\n5. Gate.\n")

#: a private-repo incident — LEGITIMATE. A split handoff: one half specced, one withheld.
#: The naive "no numbered list" rule rejects this correct handoff.
H1577 = spec(
    "**Half 1 only. Half 2's spec is withheld pending the Plan-First "
    "decision above.**\n\n"
    "1. Add the company to the task card query.\n"
    "2. Render it.\n3. Tests.\n")

#: a private-repo incident — LEGITIMATE. Withheld, then enumerates what the spec will cover.
H964 = spec(
    "**Withheld — this handoff triggers Plan-First Implementation** "
    "(Delegated Judgment Calls is non-'none').\n\n"
    "Once ratified the spec covers:\n"
    "1. the pairing-manifest format\n2. the writer\n3. tests\n4. the gate\n")

#: a private-repo incident — no marker, straight into a list. Rejected, correctly.
H1514 = spec("Per DJC1's resolution. At minimum, per the ACs:\n"
             "1. Add the row-level repo signal.\n2. Keep the regex tolerant.\n")

#: A non-Plan-First handoff with a full spec — must stay accepted.
NORMAL = spec("1. Do the thing.\n2. Test it.\n3. Gate.\n")


class TheFourHandVerifiedShapes(unittest.TestCase):
    """Rule C classified all four correctly; the naive step-count rule got
    two of them wrong. These are that comparison, frozen."""

    def test_the_founding_incident_shape_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            L.validate_plan_first_spec(H1189, True)
        self.assertIn("no withheld marker", str(ctx.exception))
        self.assertIn("a private-repo incident", str(ctx.exception))

    def test_a_split_handoff_is_accepted(self) -> None:
        """a private-repo incident. Half 1's steps are correct work, not a leaked spec."""
        L.validate_plan_first_spec(H1577, True)

    def test_a_withheld_section_that_enumerates_scope_is_accepted(self) -> None:
        """a private-repo incident. Listing what the spec WILL cover is not delivering it."""
        L.validate_plan_first_spec(H964, True)

    def test_steps_with_no_marker_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            L.validate_plan_first_spec(H1514, True)


class TheRuleIsPositional(unittest.TestCase):
    def test_a_marker_after_the_first_step_is_rejected(self) -> None:
        """The whole point of 'before'. Marker-presence alone is trivially
        satisfiable while pasting the spec above it."""
        body = spec("1. Do the thing.\n2. Test it.\n\nSpec withheld, honest.\n")
        with self.assertRaises(SystemExit) as ctx:
            L.validate_plan_first_spec(body, True)
        self.assertIn("BEFORE its withheld marker", str(ctx.exception))

    def test_a_marker_with_no_steps_at_all_is_accepted(self) -> None:
        L.validate_plan_first_spec(
            spec("**WITHHELD — Plan-First.** Post the plan and stop."), True)

    def test_the_two_refusals_say_different_things(self) -> None:
        """A missing marker and a mispositioned one are different author
        errors and get different instructions."""
        missing = spec("1. Step one.\n2. Step two.\n")
        late = spec("1. Step one.\n2. Step two.\n\nwithheld\n")
        with self.assertRaises(SystemExit) as a:
            L.validate_plan_first_spec(missing, True)
        with self.assertRaises(SystemExit) as b:
            L.validate_plan_first_spec(late, True)
        self.assertNotEqual(str(a.exception), str(b.exception))


class TheTriggerIsDeclaredNotInferred(unittest.TestCase):
    """The scope correction Lane 1 confirmed: `DJC != none` is
    `plan_first_of`'s only guessing branch, and a private-repo incident is the recorded
    case where it read backwards."""

    def test_a_non_plan_first_handoff_with_a_full_spec_is_untouched(self) -> None:
        L.validate_plan_first_spec(NORMAL, False)
        L.validate_plan_first_spec(H1189, False)

    def test_the_rule_reads_nothing_but_the_declared_flag_and_the_spec(self) -> None:
        """A body whose Delegated Judgment Calls section says Plan-First
        loudly is still accepted when the author declared `false`. The
        section is not the trigger."""
        body = ("### Delegated Judgment Calls\nOne. **This is Plan-First.**\n\n"
                + NORMAL)
        L.validate_plan_first_spec(body, False)

    def test_hrse_1546s_declining_prose_cannot_cause_a_rejection(self) -> None:
        """The live regression a private-repo incident exists to fix, as a rejection this
        time: a handoff whose own sentence declines Plan-First must not have
        its spec refused because the section is non-empty."""
        body = ("### Delegated Judgment Calls\nGiven no design ambiguity and a "
                "single-file change, **this does not need Plan-First.** "
                "Implement directly.\n\n" + NORMAL)
        L.validate_plan_first_spec(body, False)


class MarkerVocabulary(unittest.TestCase):
    """Measured, not invented: the idioms authors actually use."""

    def test_every_live_idiom_is_accepted(self) -> None:
        for idiom in ("Withheld pending Lane 2's plan.",
                      "Omitted pending the plan round.",
                      "Deferred pending ratification.",
                      "**If this handoff triggers Plan-First Implementation** it does.",
                      "The spec follows once the plan is ratified.",
                      "Post the plan and stop there."):
            with self.subTest(idiom=idiom):
                L.validate_plan_first_spec(spec(idiom + "\n\n1. Later step.\n"), True)

    def test_the_match_is_case_insensitive(self) -> None:
        L.validate_plan_first_spec(spec("WITHHELD."), True)
        L.validate_plan_first_spec(spec("withheld."), True)

    def test_unrelated_prose_is_not_a_marker(self) -> None:
        with self.assertRaises(SystemExit):
            L.validate_plan_first_spec(
                spec("The plan is below.\n\n1. Step one.\n2. Step two.\n"), True)


if __name__ == "__main__":
    unittest.main()


class WiredIntoMain(unittest.TestCase):
    """A rule that is correct and unreachable is not a rule.

    Mutation testing found this: deleting the `validate_plan_first_spec`
    call from `main()` left every test above green. That is the same shape
    as harmonic-forge#472's own vacuous `ae-and-sweep` coverage, caught the
    same way, one issue later.
    """

    # a private-repo incident: the lead region needs Scope/Next now too.
    HANDOFF = "**Scope:** plan-first wiring test.\n**Next:** n/a.\n\n" + "\n".join(
        f"### {heading}\nreal content for {heading}\n" for heading in L.HANDOFF_HEADINGS
        if heading != "Implementation Spec"
    ) + "\n### Implementation Spec\n1. Do the thing.\n2. Then the other.\n"

    def _main(self, plan_first: str):
        import tempfile
        from unittest import mock
        posted = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "handoff.md"
            path.write_text(self.HANDOFF)
            argv = ["l1_post.py", "--kind", "handoff", "--issue", "1",
                    "--sha", "a" * 40, "--branch", "b", "--file", str(path),
                    "--plan-first", plan_first]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(L, "resolve_sha", return_value="a" * 40), \
                 mock.patch.object(L, "validate_tier_set"), \
                 mock.patch.object(L, "validate_milestone_set"), \
                 mock.patch.object(L, "post_kind",
                                   side_effect=lambda *a, **k: posted.append(a[2]) or ("u", 1)):
                L.main()
        return posted

    def test_a_plan_first_handoff_with_steps_never_reaches_the_transport(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._main("true")
        self.assertIn("a private-repo incident", str(ctx.exception))

    def test_the_same_body_posts_when_declared_not_plan_first(self) -> None:
        """The other direction, through `main()` — proving the refusal comes
        from the declared flag and not from the body."""
        self.assertEqual(self._main("false"), ["handoff"])
