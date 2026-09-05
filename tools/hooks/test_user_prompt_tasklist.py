#!/usr/bin/env python3
"""Tests for the task-list injection hook's versioned source (hrse#1546).

The hook itself lives in the untracked `~/.claude/settings.json`, so this
file is the only thing standing between it and silent drift. These assert
the three properties the ACs turn on: the text is the operator's sentence
byte-for-byte, the command cannot block a prompt, and what it emits is what
the harness can actually parse.
"""
import json
import subprocess
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "user_prompt_tasklist.json"

#: The operator's own sentence. Hard-coded here rather than read from the
#: JSON, so the two have to agree — reading it from the file under test
#: would make this assertion true by construction and prove nothing.
SENTENCE = "use the todolist widget so I can track your progress"


def command() -> str:
    entry = json.loads(SOURCE.read_text())["hooks"]["UserPromptSubmit"][0]
    return entry["hooks"][0]["command"]


class SourceShape(unittest.TestCase):
    def test_the_file_is_valid_json(self) -> None:
        json.loads(SOURCE.read_text())

    def test_it_declares_exactly_one_user_prompt_submit_hook(self) -> None:
        hooks = json.loads(SOURCE.read_text())["hooks"]
        self.assertEqual(list(hooks), ["UserPromptSubmit"])
        self.assertEqual(len(hooks["UserPromptSubmit"]), 1)

    def test_it_touches_no_other_hook_event(self) -> None:
        """It merges into a file that already wires `PreToolUse`. A second
        event here would silently replace that on install."""
        self.assertNotIn("PreToolUse", json.loads(SOURCE.read_text())["hooks"])


class TextIsVerbatim(unittest.TestCase):
    """AC2 — asserted by string equality, not by intent."""

    def test_the_emitted_context_is_the_operators_sentence(self) -> None:
        raw = subprocess.run(["bash", "-c", command()], capture_output=True, text=True)
        payload = json.loads(raw.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["additionalContext"], SENTENCE)

    def test_it_is_not_capitalised_politened_or_expanded(self) -> None:
        raw = subprocess.run(["bash", "-c", command()], capture_output=True, text=True)
        text = json.loads(raw.stdout)["hookSpecificOutput"]["additionalContext"]
        # Not `text == text.lower()`: the sentence contains the pronoun "I"
        # and always did. The property is that nobody sentence-capitalised
        # it or dressed it up, not that it is uniformly lowercase.
        self.assertTrue(text[0].islower(), "leading capital = someone tidied it")
        self.assertNotIn("please", text.lower())
        self.assertFalse(text.endswith("."), "trailing period = someone tidied it")
        self.assertEqual(len(text), 52)


class CannotBlockAPrompt(unittest.TestCase):
    """AC3 — a UserPromptSubmit hook blocks the prompt on exit code 2, so
    the requirement is that no reachable path returns one."""

    def test_it_exits_zero(self) -> None:
        self.assertEqual(subprocess.run(["bash", "-c", command()],
                                        capture_output=True).returncode, 0)

    def test_it_exits_zero_with_stdin_closed(self) -> None:
        """The sibling hooks on this event both READ stdin — a bare `grep`
        and `expand_lane_shorthand.py`. This one must not, or it competes
        for the same payload (AC4)."""
        result = subprocess.run(["bash", "-c", command()],
                                stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout)

    def test_it_exits_zero_from_a_nonexistent_directory_and_empty_env(self) -> None:
        """No file read, no interpreter start, no PATH lookup beyond the
        shell — so there is no environment that makes it fail."""
        result = subprocess.run(["bash", "-c", command()], cwd="/",
                                env={"PATH": "/usr/bin:/bin"}, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_it_starts_no_interpreter(self) -> None:
        """A python3 script would be the natural symmetry with
        `expand_lane_shorthand.py` and is strictly worse: it can fail to
        import, and it costs an interpreter start on every prompt."""
        self.assertNotIn("python", command())
        self.assertTrue(command().startswith("echo "))


class EmitsParseableOutput(unittest.TestCase):
    def test_stdout_is_the_hook_output_envelope_the_harness_expects(self) -> None:
        raw = subprocess.run(["bash", "-c", command()], capture_output=True, text=True)
        payload = json.loads(raw.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")
        self.assertIn("additionalContext", payload["hookSpecificOutput"])

    def test_it_is_unconditional(self) -> None:
        """The sibling reminder hook is guarded by a grep over the prompt.
        This one fires on every prompt in every session, which is AC1 and
        the whole reason it is at user level rather than in a project."""
        self.assertNotIn("grep", command())
        self.assertNotIn("&&", command())


if __name__ == "__main__":
    unittest.main()
