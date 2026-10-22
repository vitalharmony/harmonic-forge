#!/usr/bin/env python3
"""Tests for require_probed_operator_commands.py (harmonic-forge#1151, epoch-2 reforge)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import require_probed_operator_commands as hook  # noqa: E402
import tier_model_stop_backstop as backstop  # noqa: E402

FENCE = "````"


class Transcript:
    """A synthetic transcript: one user turn, Bash calls, a final text."""

    def __init__(self, tmp: Path):
        self.path = tmp / "t.jsonl"
        self.entries = [{"type": "user", "message": {"content": "go"}}]
        self.n = 0

    def bash(self, command: str, error: bool = False, background: bool = False) -> "Transcript":
        self.n += 1
        tid = f"t{self.n}"
        call = {"command": command}
        if background:
            call["run_in_background"] = True
        self.entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": call}]}})
        self.entries.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": "ok"}]}})
        return self

    def say(self, text: str) -> str:
        self.entries.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}})
        self.path.write_text("\n".join(json.dumps(e) for e in self.entries) + "\n")
        return str(self.path)


def block(*cmds: str) -> str:
    return ("Done.\n\n## YOUR NEXT ACTIONS\n1. Run:\n" + FENCE + "operator-commands\n"
            + "\n".join(cmds) + "\n" + FENCE + "\n")


class Decide(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.t = Transcript(self.tmp)
        self.script = self.tmp / "reset.sh"
        self.script.write_text("#!/usr/bin/env bash\necho hi\n")
        os.chmod(self.script, 0o644)

    def tearDown(self):
        self._tmp.cleanup()

    def decide(self, path):
        return hook.decide({"transcript_path": path})

    def fresh(self) -> Transcript:
        return Transcript(self.tmp)

    def test_unprobed_line_blocks(self):
        verdict, reason = self.decide(self.t.say(block("gh pr merge 12 --squash")))
        self.assertEqual(verdict, "block")
        self.assertIn("gh pr merge 12 --squash", reason)

    def test_the_exact_line_probed_is_allowed(self):
        self.t.bash("  gh pr merge 12 --squash ")
        self.assertEqual(self.decide(self.t.say(block("gh pr merge 12 --squash")))[0], "allow")

    def test_help_or_dry_run_suffix_clears(self):
        for probe in ("python3 x.py --issue 1 --help", "python3 x.py --issue 1 --dry-run"):
            t = self.fresh()
            t.bash(probe)
            with self.subTest(probe=probe):
                self.assertEqual(self.decide(t.say(block("python3 x.py --issue 1")))[0], "allow")

    def test_no_suffix_on_an_and_chain(self):
        self.t.bash("cd /tmp && gh pr view 1 --help")
        self.assertEqual(self.decide(self.t.say(block("cd /tmp && gh pr view 1")))[0], "block")

    def test_any_other_string_does_not_clear(self):
        """No normalization of any kind: a different string is a different command."""
        for probe, line in (("gh pr view 12", "gh pr merge 12"),
                            ("python3 ./run.py --help", "python3 run.py"),
                            ("python3 x.py --issue 1", "python3 x.py --issue 1 --force"),
                            ("MODE=check cmd", "MODE=apply cmd"),
                            ("bash -n /x/reset.sh", "bash /x/reset.sh")):
            t = self.fresh()
            t.bash(probe)
            with self.subTest(line=line):
                self.assertEqual(self.decide(t.say(block(line)))[0], "block")

    def test_errored_probe_does_not_clear(self):
        self.t.bash("gh pr merge 12", error=True)
        self.assertEqual(self.decide(self.t.say(block("gh pr merge 12")))[0], "block")

    def test_backgrounded_probe_does_not_clear(self):
        """Reforge pass 2: a background launch's result says nothing of its exit."""
        self.t.bash("python3 x.py end --key F1", background=True)
        self.assertEqual(self.decide(self.t.say(block("python3 x.py end --key F1")))[0], "block")

    def test_joined_lines_are_refused_and_and_is_allowed(self):
        for line in ("python3 x.py | tail -5", "a || true", "a; b", "a & b", "a > out",
                     "echo $(gh pr merge 1)", "echo `id`"):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                verdict, reason = self.decide(t.say(block(line)))
                self.assertEqual(verdict, "block")
                self.assertIn(line, reason)
        t = self.fresh()
        t.bash("cd /tmp && gh pr view 1")
        self.assertEqual(self.decide(t.say(block("cd /tmp && gh pr view 1")))[0], "allow")

    def test_a_quoted_operator_is_not_an_operator(self):
        self.t.bash("gh issue comment 1 --body 'a; b'")
        self.assertEqual(self.decide(self.t.say(block("gh issue comment 1 --body 'a; b'")))[0],
                         "allow")

    def test_nonexecutable_script_blocks_even_when_probed(self):
        self.t.bash(str(self.script))
        verdict, reason = self.decide(self.t.say(block(str(self.script))))
        self.assertEqual(verdict, "block")
        self.assertIn("not executable as written", reason)

    def test_ac2_checks_every_joined_command_past_assignments(self):
        """Epoch 2 pass 1: not only the line's first token."""
        for line in (f"cd /tmp && {self.script}", f"FOO=1 {self.script}"):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                verdict, reason = self.decide(t.say(block(line)))
                self.assertEqual(verdict, "block")
                self.assertIn("not executable as written", reason)

    def test_quoted_operators_and_single_quoted_substitution_are_text(self):
        for line in ("gh issue comment 1 --body 'a > b'", "gh issue comment 1 --body '$(x)'"):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                self.assertEqual(self.decide(t.say(block(line)))[0], "allow")

    def test_inline_mention_of_the_info_string_is_not_a_second_block(self):
        self.t.bash("gh pr view 1")
        text = block("gh pr view 1") + "\n`operator-commands` above holds the step.\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")

    def test_a_probe_with_no_result_is_no_probe(self):
        """Epoch 2 pass 1: a call that never returned is no evidence."""
        self.t.bash("gh pr merge 12")
        self.t.entries.pop()  # drop its tool_result
        self.assertEqual(self.decide(self.t.say(block("gh pr merge 12")))[0], "block")

    def test_one_lexer_decides_quoting_for_every_check(self):
        """Epoch-2 pass 2 (sticky-wicket PATCH): quoting is read in one place."""
        allowed = [("gh issue comment 1 --body \"a;b\"", "gh issue comment 1 --body \"a;b\""),
                   ("gh x --body 'a && b' --help", "gh x --body 'a && b'"),
                   ("echo ';'", "echo ';'"),
                   ("echo '&&' --help", "echo '&&'"),
                   ("echo '$(x)'", "echo '$(x)'")]
        for probe, line in allowed:
            t = self.fresh()
            t.bash(probe)
            with self.subTest(line=line):
                self.assertEqual(self.decide(t.say(block(line)))[0], "allow")
        for line in ('echo "$(x)"', f"echo 'x && y' && {self.script}"):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                self.assertEqual(self.decide(t.say(block(line)))[0], "block")

    def test_ac2_reads_past_a_quoted_and_on_a_question_turn(self):
        line = f"echo 'x && y' && {self.script}"
        tail = "\n## DECISIONS NEEDED\n1. Should I run it? Yes.\n"
        verdict, reason = self.decide(self.t.say(block(line) + tail))
        self.assertEqual(verdict, "block")
        self.assertIn("not executable as written", reason)

    def test_actions_without_a_block_are_refused(self):
        for text in ("**YOUR NEXT ACTIONS**\n1. `gh pr merge 1`\n",
                     "## YOUR NEXT ACTIONS\n> ```\n> rm -rf build\n> ```\n",
                     "## your next actions\nNothing.\n"):
            with self.subTest(text=text):
                verdict, reason = self.decide(self.fresh().say(text))
                self.assertEqual(verdict, "block")
                self.assertIn("operator-commands", reason)

    def test_a_none_block_is_allowed(self):
        self.assertEqual(self.decide(self.t.say(block("# none")))[0], "allow")

    def test_malformed_or_second_declarations_are_refused(self):
        for text in ("## YOUR NEXT ACTIONS\n```operator-commands\ngh pr merge 12\n```\n",
                     block("# none") + "\n" + FENCE + "operator-commands\n# none\n" + FENCE + "\n"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(self.fresh().say(text))[0], "block")

    def test_inner_triple_backticks_do_not_close_the_block(self):
        verdict, reason = self.decide(self.t.say(block("```", "gh pr merge 12 --squash")))
        self.assertEqual(verdict, "block")
        self.assertIn("gh pr merge 12", reason)

    def test_truncated_scan_withholds_only_never_probed(self):
        path = self.t.say(block("gh pr merge 12 --squash"))
        with mock.patch.object(hook, "scan_turn", return_value=([], True)):
            self.assertEqual(self.decide(path)[0], "allow")
        path = self.fresh().say(block(str(self.script)))
        with mock.patch.object(hook, "scan_turn", return_value=([], True)):
            self.assertEqual(self.decide(path)[0], "block")

    def test_no_actions_text_is_allowed(self):
        self.assertEqual(self.decide(self.t.say("All done, nothing owed."))[0], "allow")

    def test_a_question_withholds_only_never_probed(self):
        """Operator-approved 2026-10-22: a question never bypasses the gate."""
        tail = "\n## DECISIONS NEEDED\n1. Should I merge it? Yes.\n"
        self.assertEqual(self.decide(self.fresh().say(block("gh pr merge 12") + tail))[0], "allow")
        for text in (block(str(self.script)) + tail, block("a | b") + tail,
                     "## YOUR NEXT ACTIONS\n`gh pr merge 1`\nProceed?\n"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(self.fresh().say(text))[0], "block")


class ScanTurn(unittest.TestCase):
    def test_drop_background_is_opt_in(self):
        with tempfile.TemporaryDirectory() as raw:
            t = Transcript(Path(raw))
            t.bash("a", background=True).bash("b")
            path = t.say("x")
            self.assertEqual([c[0] for c in backstop.scan_turn(path)[0]], ["a", "b"])
            self.assertEqual([c[0] for c in backstop.scan_turn(path, drop_background=True)[0]],
                             ["b"])


class Main(unittest.TestCase):
    def run_main(self, payload):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out):
            hook.main()
        return out.getvalue()

    def test_stop_hook_active_always_allows(self):
        with mock.patch.object(hook, "decide", return_value=("block", "x")) as d:
            self.assertEqual(self.run_main({"stop_hook_active": True}), "")
        d.assert_not_called()

    def test_block_prints_decision(self):
        with mock.patch.object(hook, "decide", return_value=("block", "why")):
            self.assertEqual(json.loads(self.run_main({}))["decision"], "block")


if __name__ == "__main__":
    unittest.main()
