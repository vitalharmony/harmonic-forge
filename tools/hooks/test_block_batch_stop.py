#!/usr/bin/env python3
"""Tests for `block_batch_stop.py` (harmonic-forge#502).

Two properties matter more than the rest and are tested hardest:

1. **A genuine question is never blocked.** Stopping on an unanswered question
   is correct — work started under one runs in the wrong configuration and gets
   thrown away. A hook that blocked those would be worse than no hook.
2. **The hook can never wedge a session.** Every failure path allows.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_batch_stop as bbs  # noqa: E402

NOW = datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)


def entry(hours: float = 6, consumed: bool = False,
          merges: int = 1, linked: bool = True) -> dict:
    """harmonic-forge#612: merge-only targets. `linked` controls whether
    each target carries a `pr_number` -- an unlinked target is exactly the
    "spare capacity for a repo that may never need it" case, and is never
    pending regardless of `consumed`."""
    return {"expires_at": (NOW + timedelta(hours=hours)).isoformat(),
            "targets": [{"action": "gh pr merge", "consumed": consumed,
                         "pr_number": (100 + i) if linked else None}
                        for i in range(merges)]}


class PendingTests(unittest.TestCase):
    def test_a_live_entry_with_an_unconsumed_target_is_pending(self) -> None:
        self.assertEqual(bbs.live_pending({"F502": entry()}, NOW), ["F502"])

    def test_a_fully_consumed_entry_is_not_pending(self) -> None:
        """The batch is done; stopping is correct."""
        self.assertEqual(bbs.live_pending({"F502": entry(consumed=True)}, NOW), [])

    def test_an_expired_entry_is_not_pending(self) -> None:
        self.assertEqual(bbs.live_pending({"F502": entry(hours=-1)}, NOW), [])

    def test_one_of_two_linked_merges_still_unconsumed_is_pending(self) -> None:
        """harmonic-forge#612: replaces the old "merged but unclosed" case --
        closing no longer exists as a separate tracked action, but a
        genuinely cross-repo issue with one repo's merge landed and the
        other's still outstanding is still real pending work."""
        e = entry(merges=2, consumed=False, linked=True)
        e["targets"][0]["consumed"] = True   # the first repo's merge landed
        self.assertEqual(bbs.live_pending({"F502": e}, NOW), ["F502"])

    def test_a_spare_unlinked_merge_target_does_not_keep_a_finished_batch_pending(self) -> None:
        """THE defect this predicate was rewritten for. A grant carries two
        merge targets so a cross-repo issue does not prompt on its second
        merge; a single-repo issue consumes (links, then confirms merged)
        only one and leaves the spare forever UNLINKED. Asking "any
        unconsumed target" therefore reported every FINISHED batch as
        pending for the rest of its 12h TTL, and the Stop hook refused to
        end any turn with no action available that could clear it. Measured
        live on F495 and F498, both merged (and, pre-#612, closed)."""
        e = entry(merges=2, consumed=False, linked=False)
        e["targets"][0]["pr_number"] = 100
        e["targets"][0]["consumed"] = True   # the one merge that happened
        # targets[1] stays unlinked (pr_number=None) -- the spare
        self.assertEqual(bbs.live_pending({"F502": e}, NOW), [],
                         "a spare, never-linked merge target is not outstanding work")

    def test_an_entry_with_no_merge_targets_is_never_pending(self) -> None:
        """Nothing here can tell when a targetless grant is finished, and
        guessing in the blocking direction is the whole failure above."""
        e = {"expires_at": (NOW + timedelta(hours=6)).isoformat(), "targets": []}
        self.assertEqual(bbs.live_pending({"F502": e}, NOW), [])

    def test_legacy_single_action_merge_entries_are_understood(self) -> None:
        """Entries predating the `targets` list are one flat dict. harmonic-
        forge#612: converted from a close entry (no longer a real shape --
        BATCH never authorizes close) to a merge entry, the only legacy
        single-action shape that can still occur."""
        legacy = {"expires_at": (NOW + timedelta(hours=2)).isoformat(),
                  "action": "gh pr merge", "consumed": False, "pr_number": 100}
        self.assertEqual(bbs.live_pending({"F316": legacy}, NOW), ["F316"])

    def test_malformed_entries_are_skipped_not_raised(self) -> None:
        for bad in ({"F1": None}, {"F1": {}}, {"F1": {"expires_at": "nope"}},
                    {"F1": {"expires_at": NOW.isoformat(), "targets": "x"}}):
            with self.subTest(state=bad):
                self.assertEqual(bbs.live_pending(bad, NOW), [])

    def test_results_are_sorted_so_the_message_is_stable(self) -> None:
        state = {"F502": entry(), "F495": entry(), "H1631": entry()}
        self.assertEqual(bbs.live_pending(state, NOW), ["F495", "F502", "H1631"])


class QuestionDetectionTests(unittest.TestCase):
    """Generous on purpose. A false negative costs one 'keep going'; a false
    positive means the operator's real question goes unasked."""

    def test_question_syntax_is_recognized(self) -> None:
        """Syntax, not vocabulary. The keyword branch that used to live here
        matched a bare "which" or "confirm" ANYWHERE in the message, and let
        through 5 of 5 real turns whose DECISIONS NEEDED said "None"."""
        for text in (
            "Which approach do you want?",
            "1. Should I rebase first?\n2. ...",
            "## DECISIONS NEEDED\n1. Pick one.",
            "Ready to proceed?\n\nMore text below.",
        ):
            with self.subTest(text=text[:30]):
                self.assertTrue(bbs.asks_a_question(text), text)

    def test_question_vocabulary_alone_is_not_a_question(self) -> None:
        """These are the shapes that produced the 100% false-positive rate:
        the word appears, no question is asked."""
        for text in (
            "Your call on the TTL.",
            "Let me know how you want this sequenced.",
            "Confirm before I merge.",
            "## DONE\n| item | which repo |\n|---|---|\n| F498 | forge |",
        ):
            with self.subTest(text=text[:30]):
                self.assertFalse(bbs.asks_a_question(text), text)

    def test_a_realistic_bluf_report_with_no_decision_is_blocked(self) -> None:
        """The ONLY input this hook sees in production is a multi-paragraph
        BLUF report. The old tests used one-line strings, which is why a
        detector that failed on 100% of real cases looked healthy."""
        text = (
            "## DECISIONS NEEDED\nNone.\n\n"
            "## YOUR NEXT ACTIONS\nNone — the batch is running unattended.\n\n"
            "## DONE\n"
            "| item | state |\n|---|---|\n"
            "| **F497** | Closed. Both PRs merged, which took two targets |\n\n"
            "## Detail — reference\n\n"
            "Every prompt traced to one of three deny-hooks. Confirm nothing "
            "else is outstanding; your call on sequencing the rest.\n")
        self.assertFalse(bbs.asks_a_question(text),
                         "a status report containing 'which', 'Confirm' and "
                         "'your call' must still not read as asking")

    def test_none_is_recognized_in_every_form_it_is_actually_written(self) -> None:
        """`**None**` flipped the wrong way — the lookahead saw `*`, not `N` —
        and 25 real messages in this project use exactly that bolded form."""
        for spelling in ("None", "None.", "none.", "**None**", "*None*",
                         "No decisions needed.", "Nothing to decide.",
                         "N/A", "None — batch running.", "None (nothing blocking)."):
            with self.subTest(spelling=spelling):
                self.assertFalse(bbs.asks_a_question(
                    f"## DECISIONS NEEDED\n{spelling}\n\n## DONE\nx"), spelling)

    def test_a_decisions_section_with_content_is_a_question(self) -> None:
        for content in ("1. Pick one.", "**1. Pick the TTL**", "- Which repo?"):
            with self.subTest(content=content):
                self.assertTrue(bbs.asks_a_question(
                    f"## DECISIONS NEEDED\n{content}\n"), content)

    def test_empty_text_is_not_a_question(self) -> None:
        self.assertFalse(bbs.asks_a_question(""))


class DecideTests(unittest.TestCase):
    def _transcript(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.write(json.dumps(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        handle.close()
        return handle.name

    def test_a_live_batch_and_no_question_blocks(self) -> None:
        payload = {"transcript_path": self._transcript("## DONE\nF498 closed.")}
        verdict, reason = bbs.decide(payload, {"F502": entry()}, NOW)
        self.assertEqual(verdict, "block")
        self.assertIn("F502", reason)
        self.assertIn("Continue the batch", reason)

    def test_a_live_batch_with_a_question_allows(self) -> None:
        payload = {"transcript_path": self._transcript("Which TTL do you want?")}
        self.assertEqual(bbs.decide(payload, {"F502": entry()}, NOW)[0], "allow")

    def test_no_live_batch_allows(self) -> None:
        payload = {"transcript_path": self._transcript("## DONE\nAll finished.")}
        self.assertEqual(bbs.decide(payload, {}, NOW)[0], "allow")
        self.assertEqual(
            bbs.decide(payload, {"F502": entry(consumed=True)}, NOW)[0], "allow")

    def test_an_unreadable_transcript_allows_rather_than_wedges(self) -> None:
        """The old behavior blocked here, contradicting this module's own
        "cannot wedge" contract: with no text the question escape hatch can
        never fire, so the block would stand for the full TTL. Unreadable
        means unknown, and unknown allows."""
        verdict, _ = bbs.decide({"transcript_path": "/nonexistent"},
                                {"F502": entry()}, NOW)
        self.assertEqual(verdict, "allow")
        self.assertEqual(bbs.decide({}, {"F502": entry()}, NOW)[0], "allow")

    def test_the_reason_names_the_escape_hatch(self) -> None:
        payload = {"transcript_path": self._transcript("done")}
        _v, reason = bbs.decide(payload, {"F502": entry()}, NOW)
        self.assertIn("ASK IT", reason)
        self.assertIn("expire", reason)


class NeverWedgeTests(unittest.TestCase):
    """`main()` must exit 0 and emit nothing on every failure path. A hook that
    can stop a session from ever ending its turn is worse than a missed stop."""

    def _run(self, stdin_text: str, **patches) -> tuple[str, int]:
        out = io.StringIO()
        ctx = mock.patch.object(sys, "stdin", io.StringIO(stdin_text))
        with ctx, mock.patch.object(sys, "stdout", out):
            for target, side_effect in patches.items():
                mock.patch.object(bbs, target, side_effect=side_effect).start()
            try:
                code = bbs.main()
            finally:
                mock.patch.stopall()
        return out.getvalue(), code

    def test_malformed_stdin_allows_silently(self) -> None:
        for text in ("", "not json", "[]"):
            with self.subTest(stdin=text):
                output, code = self._run(text)
                self.assertEqual(output, "")
                self.assertEqual(code, 0)

    def test_a_raising_decide_allows_silently(self) -> None:
        output, code = self._run(json.dumps({"transcript_path": "/x"}),
                                 decide=RuntimeError("boom"))
        self.assertEqual(output, "")
        self.assertEqual(code, 0)

    def test_an_unreadable_state_file_allows_silently(self) -> None:
        with mock.patch.dict(sys.modules):
            sys.modules.pop("batch_auth", None)
            with mock.patch.object(sys, "stdin",
                                   io.StringIO(json.dumps({"transcript_path": "/x"}))), \
                    mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                    mock.patch("builtins.__import__", side_effect=ImportError):
                self.assertEqual(bbs.main(), 0)
                self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


class ProductionEntryPointTests(unittest.TestCase):
    """`main()`'s emission was untested: `if verdict == "block":` → `if False:`
    left the whole suite green, so the hook could ship as a no-op."""

    def _run(self, payload: dict, state: dict) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch("batch_auth._load", return_value=state), \
                mock.patch("batch_auth.STATE_PATH", Path("/x")):
            # harmonic-forge#515: PIN THE CLOCK. The fixtures are built as
            # `NOW + hours`, and `main()` used to resolve the clock itself, so
            # these assertions silently became "is the wall clock still before
            # 2026-09-07T12:00Z" — false since that instant, permanently, with
            # no code change on either side.
            self.assertEqual(bbs.main(now=NOW), 0)
        return out.getvalue()

    def _transcript(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.write(json.dumps(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        handle.close()
        return handle.name

    def test_main_emits_the_block_decision_in_the_documented_shape(self) -> None:
        payload = {"transcript_path": self._transcript("## DONE\nclosed.")}
        output = self._run(payload, {"F502": entry()})
        self.assertNotEqual(output, "", "main() emitted nothing on a blockable turn")
        parsed = json.loads(output)
        self.assertEqual(parsed["decision"], "block")
        self.assertIn("F502", parsed["reason"])

    def test_main_emits_nothing_when_the_batch_is_finished(self) -> None:
        payload = {"transcript_path": self._transcript("## DONE\nclosed.")}
        self.assertEqual(self._run(payload, {"F502": entry(consumed=True)}), "")

    def test_the_recursion_guard_is_honoured_before_anything_else(self) -> None:
        """This hook's block condition is not clearable within the turn, so
        without the guard it re-fires until the TTL elapses."""
        payload = {"transcript_path": self._transcript("## DONE\nclosed."),
                   "stop_hook_active": True}
        self.assertEqual(self._run(payload, {"F502": entry()}), "")
