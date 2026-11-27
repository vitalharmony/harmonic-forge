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

    def test_ansi_c_quoting_and_comments_are_refused(self):
        """Post-verdict: forms the lexer does not model are refused, not read."""
        for line in ("echo $'it\\'s' # x' && ./s.sh", "echo $'a'", "gh pr view 1 # note",
                     '$"tools/x.sh"'):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                verdict, reason = self.decide(t.say(block(line)))
                self.assertEqual(verdict, "block")
                self.assertIn("cannot split it", reason)
        t = self.fresh()
        t.bash("gh pr view 1 --body 'a#b'")
        self.assertEqual(self.decide(t.say(block("gh pr view 1 --body 'a#b'")))[0], "allow")

    def test_substitution_is_named_as_such(self):
        for line in ('echo "$(x)"', "echo `id`", "echo $(id)"):
            t = self.fresh()
            t.bash(line)
            with self.subTest(line=line):
                self.assertIn("command substitution", self.decide(t.say(block(line)))[1])

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



SHA40 = "0123456789abcdef0123456789abcdef01234567"
#: TC1 (harmonic-forge#1639 AC1): well-formed directives, as the operator types them.
WELL_FORMED = (
    "FORCE F1566: two passes clean",
    "FORCE f1566: x",
    "FORCE F1531 reforge 2b5d58f0a1b2: patch",
    f"FORCE F1579 route {SHA40}: merge",
    "FORCE F1565 cluster: x",
    "FORCE F1291 gate 0123456789ab: x",
    "PARK H2621",
    "PARK H2621: moved to 3.2",
    "SCAN H open-issues: tune-up",
    "UI H2615: nav",
    "UI H2615 0123456789abcdef: merge",
    "close F1639",
)
#: TC2 (AC2): candidates the prompt hook refuses; the probe hook refuses them with its reason.
MALFORMED = (
    "FORCE F1 rebase abc: x",
    "FORCE F1 reforge xyz: x",
    "FORCE F1 route abc123abc123: x",
    "FORCE F1:",
    "FORCE F1 x",
    "force F1: x",
    "  FORCE F1: x",
    "> PARK H1",
    "FORCE K12: x",
    "UI P5: x",
    "PARK Z5",
    "PARK H02621",
    "PARK H1 extra",
    "SCAN K open-issues: x",
    "SCAN H everything: x",
    "UI H1 a b: x",
    "UI H1 zz: x",
    "UI H1:",
    "close F1 now",
)
#: TC3 (AC3): never exempt, whatever the parser reads.
SHELLISH = (
    "FORCE F1: x; rm -rf ~",
    "FORCE F1: fixes (D54)",
    "FORCE F1: Marc's call",
)
CHAINED = (
    "close F1 && ls",
    "FORCE F1: x && ls",
    "PARK H1: x && ls",
    "SCAN H open-issues: x && ls",
    "UI H1: x && ls",
)


class DirectiveLines(unittest.TestCase):
    """harmonic-forge#1639: directive lines are parsed by the prompt hook's own
    `parse_directive`, as written, never probed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def say(self, *lines: str, probes=None):
        path = Transcript(self.tmp).say(block(*lines))
        return hook.decide({"transcript_path": path}, probes=probes)

    def problem_line(self, reason: str, line: str) -> str:
        found = [ln for ln in reason.splitlines() if ln.startswith(f"  - `{line.strip()}`")]
        self.assertEqual(len(found), 1, reason)
        return found[0]

    def test_tc1_well_formed_directives_need_no_probe(self):
        self.assertEqual(self.say(*WELL_FORMED, probes=[]), ("allow", ""))
        # no Bash call at all in the transcript: the turn's own scan finds none
        self.assertEqual(self.say(*WELL_FORMED), ("allow", ""))

    def test_tc2_a_refused_directive_carries_the_parsers_reason(self):
        import expand_lane_shorthand as m
        for line in MALFORMED:
            with self.subTest(line=line):
                expected = m.parse_directive(line)
                self.assertIsInstance(expected, str)
                verdict, reason = self.say(line, probes=[line.strip()])
                self.assertEqual(verdict, "block")
                got = self.problem_line(reason, line)
                self.assertIn(f"the prompt hook would refuse it: {expected}", got)
                self.assertIn("reword the reason", got)
                self.assertNotIn("no Bash call", got)

    def test_tc2_placement_is_read_from_the_line_as_written(self):
        verdict, reason = self.say("  FORCE F1: x", "> PARK H1", probes=[])
        self.assertEqual(verdict, "block")
        self.assertIn("honored only as the prompt's first line",
                      self.problem_line(reason, "  FORCE F1: x"))
        self.assertIn("honored only as the prompt's first line",
                      self.problem_line(reason, "> PARK H1"))

    def test_tc3_shell_metacharacters_are_never_exempt(self):
        for line in SHELLISH:
            with self.subTest(line=line):
                verdict, reason = self.say(line, probes=[])
                self.assertEqual(verdict, "block")
                self.assertIn("reword the reason (no quotes or shell metacharacters)",
                              self.problem_line(reason, line))

    def test_tc3_an_and_chain_on_a_directive_is_refused(self):
        for line in CHAINED:
            with self.subTest(line=line):
                verdict, reason = self.say(line, probes=[line])
                self.assertEqual(verdict, "block")
                self.assertIn("a directive line is one line",
                              self.problem_line(reason, line))

    def test_tc3_non_candidates_keep_the_probe_rule(self):
        shell = "python3 tools/gh/force_grant.py status --key F1"
        for line in ("Close F1", shell):
            with self.subTest(line=line):
                verdict, reason = self.say(line, probes=[])
                self.assertEqual(verdict, "block")
                self.assertIn("no Bash call", self.problem_line(reason, line))
        self.assertEqual(self.say(shell, probes=[shell]), ("allow", ""))

    def test_tc3_executable_bit_still_checked_beside_directives(self):
        script = self.tmp / "reset.sh"
        script.write_text("#!/usr/bin/env bash\necho hi\n")
        os.chmod(script, 0o644)
        verdict, reason = self.say("FORCE F1566: x", str(script), probes=[str(script)])
        self.assertEqual(verdict, "block")
        self.assertIn("not executable", self.problem_line(reason, str(script)))

    def test_question_turn_still_refuses_a_malformed_directive(self):
        tail = "\n## DECISIONS NEEDED\n1. Should I merge it? Yes.\n"
        path = Transcript(self.tmp).say(block("FORCE F1 rebase abc: x") + tail)
        self.assertEqual(hook.decide({"transcript_path": path})[0], "block")

    def test_parser_import_failure_falls_back_to_the_probe_rule(self):
        line = "FORCE F1566: two passes clean"
        with mock.patch.object(hook, "directive_parser", return_value=None):
            verdict, reason = self.say(line, probes=[])
            self.assertEqual(verdict, "block")
            self.assertIn("no Bash call", reason)
            # the fallback is today's rule, not a refusal: an exactly probed line allows
            self.assertEqual(self.say(line, probes=[line]), ("allow", ""))

    def test_a_parser_that_raises_falls_back_to_the_probe_rule(self):
        def boom(_line):
            raise RuntimeError("boom")
        with mock.patch.object(hook, "directive_parser", return_value=boom):
            self.assertEqual(self.say("git status", probes=["git status"]), ("allow", ""))
            verdict, reason = self.say("git status", probes=[])
        self.assertEqual(verdict, "block")
        self.assertIn("no Bash call", reason)

    def test_a_broken_scan_grant_import_never_refuses_a_shell_line(self):
        with mock.patch.dict(sys.modules, {"scan_grant": None}):
            self.assertEqual(self.say("git status", probes=["git status"]), ("allow", ""))
            verdict, reason = self.say("SCAN H open-issues: tune-up", probes=[])
        self.assertEqual(verdict, "block")
        self.assertIn("SCAN H open-issues: tune-up", reason)

    def nested(self, *lines: str, probes=None):
        """The block as an indented fence under a numbered list item."""
        text = ("Done.\n\n## YOUR NEXT ACTIONS\n1. Run:\n   " + FENCE + "operator-commands\n"
                + "".join(f"   {ln}\n" for ln in lines) + "   " + FENCE + "\n")
        path = Transcript(self.tmp).say(text)
        return hook.decide({"transcript_path": path}, probes=probes)

    def test_a_fence_nested_under_a_list_item_reads_lines_relative_to_it(self):
        self.assertEqual(self.nested("FORCE F1566: two passes clean", "PARK H2621: moved to 3.2",
                                     "close F1639", probes=[]), ("allow", ""))

    def test_a_line_indented_beyond_its_nested_fence_is_refused(self):
        for line in ("  FORCE F1566: two passes clean", "> PARK H2621"):
            with self.subTest(line=line):
                verdict, reason = self.nested(line, probes=[line.strip()])
                self.assertEqual(verdict, "block")
                self.assertIn("honored only as the prompt's first line",
                              self.problem_line(reason, line))

    def test_directive_parser_returns_none_when_the_import_raises(self):
        with mock.patch.dict(sys.modules, {"expand_lane_shorthand": None}):
            self.assertIsNone(hook.directive_parser())

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
