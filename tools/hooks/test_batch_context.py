#!/usr/bin/env python3
"""Tests for `batch_context.py` (harmonic-forge#509).

Two properties dominate:

1. **AC4 — nothing here can soften a verdict.** The module has no return path a
   caller could branch on, and the deny-surface grep test below asserts no hook
   returns `allow` on the basis of a live batch.
2. **AC5 — a hook mid-denial must never lose its denial.** Every failure path
   returns the original message unchanged.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import batch_context as bc  # noqa: E402

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)


def state(*keys: str, hours: float = 6) -> dict:
    return {k: {"expires_at": (NOW + timedelta(hours=hours)).isoformat(),
                "targets": [{"action": "gh issue close", "consumed": False}]}
            for k in keys}


class LiveKeysTests(unittest.TestCase):
    def _keys(self, st: dict) -> list[str]:
        with mock.patch("batch_auth._load", return_value=st), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            return bc.live_batch_keys(NOW)

    def test_unexpired_keys_are_returned_sorted(self) -> None:
        self.assertEqual(self._keys(state("F509", "H1631", "F495")),
                         ["F495", "F509", "H1631"])

    def test_expired_keys_are_excluded(self) -> None:
        self.assertEqual(self._keys(state("F509", hours=-1)), [])

    def test_malformed_entries_are_skipped_not_raised(self) -> None:
        for bad in ({"F1": None}, {"F1": {}}, {"F1": {"expires_at": "nope"}},
                    {"F1": 5}):
            with self.subTest(state=bad):
                self.assertEqual(self._keys(bad), [])

    def test_a_non_dict_state_is_empty_not_an_error(self) -> None:
        self.assertEqual(self._keys([]), [])

    def test_an_unreadable_state_is_empty_not_an_error(self) -> None:
        with mock.patch("batch_auth._load", side_effect=OSError("boom")), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            self.assertEqual(bc.live_batch_keys(NOW), [])


class AnnotateTests(unittest.TestCase):
    def _annotate(self, message: str, st: dict, **kw) -> str:
        with mock.patch("batch_auth._load", return_value=st), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            return bc.annotate(message, now=NOW, **kw)

    def test_no_live_batch_returns_the_message_untouched(self) -> None:
        self.assertEqual(self._annotate("denied.", {}), "denied.")

    def test_the_original_message_is_always_preserved_verbatim(self) -> None:
        """The annotation is additive. A hook's own reason is the product."""
        out = self._annotate("denied: use --body-file.", state("F509"))
        self.assertTrue(out.startswith("denied: use --body-file."))

    def test_a_target_key_in_the_batch_is_named(self) -> None:
        out = self._annotate("denied.", state("F509"), target_key="f509")
        self.assertIn("on F509", out)
        self.assertIn("Live keys: F509", out)

    def test_a_target_key_outside_the_batch_is_flagged_as_such(self) -> None:
        """Acting on an unauthorized issue during a batch is worth saying —
        it is a different situation from being stopped on an authorized one."""
        out = self._annotate("denied.", state("F509"), target_key="H999")
        self.assertIn("NOT one of the authorized keys", out)

    def test_without_a_target_key_it_still_names_the_batch(self) -> None:
        out = self._annotate("denied.", state("F509", "F495"))
        self.assertIn("interrupted an authorized batch", out)
        self.assertIn("F495, F509", out)

    def test_a_long_key_list_is_elided(self) -> None:
        """Asserts the LIST is short, not merely that a suffix appears.

        The old version checked only `assertIn("more", ...)`, and the suffix
        was derived from the full key count rather than the shown slice — so
        deleting the slice printed all forty keys AND `+34 more`, and passed.
        """
        keys = [f"F{n}" for n in range(500, 540)]
        out = self._annotate("denied.", state(*keys))
        listed = [k for k in keys if k in out]
        self.assertLessEqual(len(listed), bc._MAX_KEYS + 1, listed)
        self.assertIn(f"+{len(keys) - bc._MAX_KEYS} more", out)

    def test_the_acting_key_is_never_elided_away(self) -> None:
        """With more live grants than the cap, sorting alone can push the key
        actually being acted on out of the list — `H1636` behind six `F` keys.
        Naming an unrelated batch is worse than naming none."""
        keys = [f"F{n}" for n in range(500, 520)] + ["H1636"]
        out = self._annotate("denied.", state(*keys), target_key="H1636")
        self.assertIn("H1636", out)

    def test_it_says_the_batch_does_not_authorize_past_the_guard(self) -> None:
        """The most important sentence: a reader must not conclude the batch
        entitles them to work around the guard."""
        out = self._annotate("denied.", state("F509"))
        self.assertIn("NOT authorized past this guard", out)
        self.assertIn("do not look for a way", out)

    def test_it_points_at_the_preflight(self) -> None:
        self.assertIn("batch-preflight", self._annotate("denied.", state("F509")))

    def test_an_exception_anywhere_returns_the_message_unchanged(self) -> None:
        """A hook is mid-denial. Losing the annotation is survivable; losing
        the denial is not."""
        with mock.patch.object(bc, "live_batch_keys", side_effect=RuntimeError):
            self.assertEqual(bc.annotate("denied."), "denied.")


class NoHookSoftensAVerdictTests(unittest.TestCase):
    """AC4 — assert the `#336` composition failure is not reintroduced.

    > a static `permissions.ask` rule was found to always beat a hook's
    > `allow` regardless of hook order or content ... one hook now owns each
    > command class end to end.

    So a guard consulting `batch_auth` to return `allow` would be undefined
    behavior, not a feature. This greps for that shape rather than trusting it.
    """

    #: The hooks that legitimately decide the merge/close permission class on
    #: authorization state. `#336` requires exactly one owner per class, and
    #: these are it.
    AUTHORIZATION_PATH = {"batch_gate.py", "block_irreversible_ops.py",
                          "batch_auth.py"}

    #: Keyed on `permissionDecision` specifically, not on the bare word
    #: "allow". The failure shape `#336` names is a hook emitting a TOOL-CALL
    #: permission verdict it does not own. `block_batch_stop.py` reads batch
    #: state and returns an allow — but it is a **Stop** hook emitting
    #: `{"decision": ...}`, deciding whether a turn ends, which no other hook
    #: decides. A name allowlist would have hidden that distinction and grown
    #: by one entry every time someone wanted past this test.
    #: Matches the permissionDecision KEY, not an `"allow"` literal.
    #:
    #: The first version matched only the literal, and this repo's one
    #: legitimate example — `batch_gate.py:32` — emits `"permissionDecision":
    #: decision`, a variable. So the test caught the naive shape and missed the
    #: shape a future author gets by copying the working example next door. A
    #: deny-surface hook has no business emitting this key at all, whatever
    #: follows it.
    def test_no_batch_aware_hook_decides_a_tool_call_permission(self) -> None:
        """A hook that reads batch state may refuse, and may decide nothing else.

        `#336`: one hook owns each command class end to end; anything else is
        undefined behavior under "strongest decision wins".
        """
        from _ac4_check import offending_decisions, reads_batch_state

        offenders: dict[str, list[str]] = {}
        for path in sorted(HERE.glob("*.py")):
            # `_ac4_check.py` is the checker; its docstring necessarily quotes
            # the very shapes it hunts for. Excluded by name with that reason,
            # not to get a hook past the test.
            if (path.name.startswith("test_") or path.name == "_ac4_check.py"
                    or path.name in self.AUTHORIZATION_PATH):
                continue
            source = path.read_text(encoding="utf-8")
            if not reads_batch_state(source):
                continue
            bad = offending_decisions(source)
            if bad:
                offenders[path.name] = bad
        self.assertEqual(offenders, {},
                         f"{offenders} read batch state and emit a "
                         "permissionDecision other than a literal deny — the "
                         "#336 composition failure")

    def test_the_check_catches_every_shape_that_matters(self) -> None:
        """Three regex attempts were wrong in three different ways; these are
        the cases each of them missed."""
        from _ac4_check import offending_decisions

        # The #336 failure, literal and variable-valued.
        self.assertTrue(offending_decisions('"permissionDecision": "allow"'))
        self.assertTrue(offending_decisions('"permissionDecision": decision'))
        self.assertTrue(offending_decisions('"permissionDecision": verdict,'))
        # A guard doing its job.
        self.assertEqual(offending_decisions('"permissionDecision": "deny"'), [])
        # Not the same key — a prefix match here flagged every deny hook.
        self.assertEqual(offending_decisions('"permissionDecisionReason": msg'), [])
        # A Stop verdict is a different decision entirely.
        self.assertEqual(offending_decisions('"decision": "block"'), [])

    def test_a_stop_hook_reading_batch_state_is_not_an_offender(self) -> None:
        """`block_batch_stop.py` does exactly that and is correct: it decides
        turn-end, not a tool-call permission. Pinned so the test above is not
        later "fixed" by adding it to the allowlist."""
        source = (HERE / "block_batch_stop.py").read_text(encoding="utf-8")
        self.assertIn("batch_auth", source)
        from _ac4_check import offending_decisions

        self.assertEqual(offending_decisions(source), [])

    def test_batch_context_itself_has_no_allow_path(self) -> None:
        source = (HERE / "batch_context.py").read_text(encoding="utf-8")
        self.assertNotIn("permissionDecision", source)
        self.assertNotIn('"allow"', source)

    def test_annotate_returns_a_string_not_a_decision(self) -> None:
        """No caller can branch on it to soften a verdict."""
        with mock.patch("batch_auth._load", return_value=state("F509")), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            self.assertIsInstance(bc.annotate("denied.", now=NOW), str)


class GuardsStillDenyTests(unittest.TestCase):
    """AC5/AC6 — the wired guards' behavior is unchanged when no batch is live,
    and `block_missing_preclose_inspection` still fires on an unrun preclose."""

    def test_the_wired_guards_import_and_expose_their_helper(self) -> None:
        for name in ("block_inline_prose", "block_lane1_status_claims",
                     "block_missing_preclose_inspection"):
            with self.subTest(hook=name):
                module = __import__(name)
                self.assertTrue(hasattr(module, "_batch_note"),
                                f"{name} lost its annotation helper")

    def test_the_helper_is_a_passthrough_with_no_batch(self) -> None:
        for name in ("block_inline_prose", "block_lane1_status_claims",
                     "block_missing_preclose_inspection"):
            with self.subTest(hook=name):
                module = __import__(name)
                with mock.patch("batch_auth._load", return_value={}), \
                        mock.patch("batch_auth.STATE_PATH", Path("/x")):
                    self.assertEqual(module._batch_note("denied."), "denied.")

    def test_the_helper_never_raises_into_a_denial(self) -> None:
        import block_inline_prose as bip

        with mock.patch.object(bc, "annotate", side_effect=RuntimeError):
            self.assertEqual(bip._batch_note("denied."), "denied.")


if __name__ == "__main__":
    unittest.main()


class AC3WiringTests(unittest.TestCase):
    """AC3 asserted end to end, not by `hasattr`.

    The first version checked that each hook *had* a `_batch_note` helper and
    that the helper was a passthrough with no batch live. Neither asserts the
    helper is ever CALLED: reverting all three hooks to
    `"permissionDecisionReason": message` left 826 tests green. AC3 was
    protected by a test that the annotation exists, not that it runs.
    """

    HOOKS = ("block_inline_prose", "block_lane1_status_claims",
             "block_missing_preclose_inspection")

    def _emitted(self, hook: str, live: bool) -> str:
        """The reason string a hook emits, with and without a live batch."""
        module = __import__(hook)
        st = state("F509") if live else {}
        with mock.patch("batch_auth._load", return_value=st), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            return module._batch_note("DENIED: the original reason.")

    def test_every_wired_hook_annotates_when_a_batch_is_live(self) -> None:
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                out = self._emitted(hook, live=True)
                self.assertIn("[BATCH]", out, f"{hook} did not annotate")
                self.assertIn("F509", out)

    def test_every_wired_hook_is_a_passthrough_with_no_batch(self) -> None:
        """AC5: unchanged behavior when no batch is live."""
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                self.assertEqual(self._emitted(hook, live=False),
                                 "DENIED: the original reason.")

    def test_the_original_reason_survives_annotation(self) -> None:
        """The hook's own message is the product; the batch line is additive."""
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                self.assertTrue(
                    self._emitted(hook, live=True).startswith(
                        "DENIED: the original reason."))

    def test_both_operator_and_model_facing_fields_are_annotated(self) -> None:
        """`permissionDecisionReason` is what the model sees; `systemMessage`
        is what the OPERATOR sees. Annotating only the first left the person
        who walked away and came back to a stalled lane — the issue's whole
        audience — reading the unchanged text."""
        import re as _re

        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                source = (HERE / f"{hook}.py").read_text(encoding="utf-8")
                for field in ("permissionDecisionReason", "systemMessage"):
                    pattern = _re.compile(
                        rf'"{field}":\s*_batch_note\(')
                    self.assertRegex(source, pattern,
                                     f"{hook}'s {field} is not annotated")

    def test_the_preclose_hook_passes_the_acting_issue_key(self) -> None:
        """AC3 says "and the issue key it interrupted". Without a target key
        only the generic branch can fire, and with more live grants than the
        elision cap the acting key may not even appear in the list."""
        source = (HERE / "block_missing_preclose_inspection.py").read_text(
            encoding="utf-8")
        self.assertIn("issue_key(repo, issue)", source)
        self.assertIn("target_key=_acting", source)
