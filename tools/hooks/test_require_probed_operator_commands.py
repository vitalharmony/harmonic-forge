#!/usr/bin/env python3
"""Tests for require_probed_operator_commands.py (harmonic-forge#1151, reforged)."""
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

FENCE = "````"


class Transcript:
    """A synthetic transcript: one user turn, Bash calls, a final text."""

    def __init__(self, tmp: Path):
        self.path = tmp / "t.jsonl"
        self.entries = [{"type": "user", "message": {"content": "go"}}]
        self.n = 0

    def bash(self, command: str, error: bool = False) -> "Transcript":
        self.n += 1
        tid = f"t{self.n}"
        self.entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}})
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
        self.cwd = str(self.tmp)
        self.script = self.tmp / "reset.sh"
        self.script.write_text("#!/usr/bin/env bash\necho hi\n")
        os.chmod(self.script, 0o644)

    def tearDown(self):
        self._tmp.cleanup()

    def decide(self, path):
        return hook.decide({"transcript_path": path, "cwd": self.cwd})

    def test_unprobed_command_blocks(self):
        verdict, reason = self.decide(self.t.say(block("gh pr merge 12 --squash")))
        self.assertEqual(verdict, "block")
        self.assertIn("gh pr merge 12 --squash", reason)

    def test_same_command_probed_is_allowed(self):
        self.t.bash("gh pr merge 12 --squash")
        self.assertEqual(self.decide(self.t.say(block("gh pr merge 12 --squash")))[0], "allow")

    def test_help_or_dry_run_probe_clears(self):
        self.t.bash("python3 tools/x.py --issue 1 --dry-run")
        self.assertEqual(self.decide(self.t.say(block("python3 tools/x.py --issue 1")))[0], "allow")

    def test_removed_mutating_flag_probe_clears(self):
        self.t.bash("python3 tools/x.py --issue 1")
        self.assertEqual(self.decide(self.t.say(block("python3 tools/x.py --issue 1 --force")))[0],
                         "allow")

    def test_other_arguments_do_not_clear(self):
        """Pass 1 #1 / pass 2 #1: a coarse key let a read clear a write."""
        for probe, offer in (("gh pr view 12", "gh pr merge 12"),
                             ("git -C /r status", "git -C /r push --force"),
                             ('python3 -c "print(1)"', 'python3 -c "print(2)"')):
            t = Transcript(self.tmp)
            t.bash(probe)
            with self.subTest(offer=offer):
                self.assertEqual(self.decide(t.say(block(offer)))[0], "block")

    def test_paths_compare_resolved_never_by_suffix(self):
        """Pass 2 #4: a probe of ./run.py never clears another run.py."""
        self.t.bash("python3 ./run.py --help")
        self.assertEqual(self.decide(self.t.say(block("python3 /x/y/run.py")))[0], "block")
        self.t.bash(f"python3 {self.tmp}/run.py --help")
        self.assertEqual(self.decide(self.t.say(block("python3 ./run.py")))[0], "allow")

    def test_cd_in_the_probe_moves_its_base(self):
        sub = self.tmp / "repo"
        sub.mkdir()
        self.t.bash(f"cd {sub} && python3 tools/x.py --help")
        self.assertEqual(self.decide(self.t.say(block(f"python3 {sub}/tools/x.py")))[0], "allow")

    def test_tilde_and_absolute_spellings_match(self):
        home = Path.home()
        self.t.bash(f"bash -n {home}/Harmonic_Projects/research/x.sh")
        self.assertEqual(self.decide(self.t.say(block("bash ~/Harmonic_Projects/research/x.sh")))[0],
                         "allow")

    def test_bash_c_is_split_and_each_command_matched(self):
        """Pass 2 #7."""
        self.t.bash("gh pr view 12")
        verdict, reason = self.decide(self.t.say(block('bash -c "gh pr view 12; gh pr merge 12"')))
        self.assertEqual(verdict, "block")
        self.assertIn("gh pr merge 12", reason)
        t = Transcript(self.tmp)
        t.bash('bash -c "echo ready; gh pr view 12"')
        self.assertEqual(self.decide(t.say(block("gh pr view 12")))[0], "allow")

    def test_errored_probe_does_not_clear(self):
        self.t.bash("gh pr merge 12", error=True)
        self.assertEqual(self.decide(self.t.say(block("gh pr merge 12")))[0], "block")

    def test_nonexecutable_script_blocks_even_when_probed(self):
        self.t.bash(f"bash -n {self.script}")
        verdict, reason = self.decide(self.t.say(block(str(self.script))))
        self.assertEqual(verdict, "block")
        self.assertIn("not executable as written", reason)
        t = Transcript(self.tmp)
        t.bash(f"bash -n {self.script}")
        self.assertEqual(self.decide(t.say(block(f"bash {self.script}")))[0], "allow")

    def test_cd_to_missing_directory_blocks(self):
        """Pass 2 #2: `cd` is checked."""
        verdict, reason = self.decide(self.t.say(block(f"cd {self.tmp}/nope")))
        self.assertEqual(verdict, "block")
        self.assertIn("does not exist", reason)
        self.assertEqual(self.decide(Transcript(self.tmp).say(block(f"cd {self.tmp}")))[0], "allow")

    def test_inner_triple_backticks_do_not_close_the_block(self):
        """Pass 2 #5."""
        text = block("echo '```'", "```", "# merge forge first", "gh pr merge 12 --squash")
        verdict, reason = self.decide(self.t.say(text))
        self.assertEqual(verdict, "block")
        self.assertIn("gh pr merge 12", reason)

    def test_commands_outside_a_block_are_asked_into_one(self):
        """Pass 2 #6: any backticked command shape, double backticks included."""
        for text in ("## YOUR NEXT ACTIONS\nRun ``gh pr merge 12 --squash``.\n",
                     "## YOUR NEXT ACTIONS\n1. `bash ~/x.sh`\n"):
            with self.subTest(text=text):
                verdict, reason = self.decide(Transcript(self.tmp).say(text))
                self.assertEqual(verdict, "block")
                self.assertIn("operator-commands", reason)

    def test_prose_and_trigger_phrases_are_allowed(self):
        """Pass 2 #3: nothing outside the block is classified."""
        text = ("## YOUR NEXT ACTIONS\n1. Type `BATCH F1151 IMC F1151`.\n"
                "2. Read `notes.csv`.\n")
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")

    def test_truncated_scan_withholds_only_never_probed(self):
        path = self.t.say(block("gh pr merge 12 --squash"))
        with mock.patch.object(hook, "scan_turn", return_value=([], True)):
            self.assertEqual(self.decide(path)[0], "allow")
        t = Transcript(self.tmp)
        path = t.say(block(str(self.script)))
        with mock.patch.object(hook, "scan_turn", return_value=([], True)):
            self.assertEqual(self.decide(path)[0], "block")  # AC2 still runs

    def test_any_label_form_and_plain_fence_fail_closed(self):
        """Reforge pass 1: the presence check fails closed on every form."""
        for text in ("**YOUR NEXT ACTIONS**\n1. `gh pr merge 1`\n",
                     "## 4. YOUR NEXT ACTIONS (2)\n1. `gh pr merge 1`\n",
                     "## YOUR NEXT ACTIONS\n```\nmise run restart --push\n```\n",
                     "## YOUR NEXT ACTIONS\n1. `sh ./x.sh`\n"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(Transcript(self.tmp).say(text))[0], "block")

    def test_a_stray_command_beside_a_block_blocks(self):
        self.t.bash("gh pr view 1")
        text = block("gh pr view 1") + "\nThen `gh pr merge 1`.\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "block")

    def test_malformed_or_empty_declarations_are_refused(self):
        for text in ("## YOUR NEXT ACTIONS\n```operator-commands\ngh pr merge 12\n```\n",
                     "## YOUR NEXT ACTIONS\n" + FENCE + "operator-commands\n# nothing\n"
                     + FENCE + "\n"):
            with self.subTest(text=text):
                self.assertEqual(self.decide(Transcript(self.tmp).say(text))[0], "block")

    def test_a_failure_masked_by_a_pipe_is_not_a_probe(self):
        """Reforge pass 1: the F1126 incident, `cmd | tail` exits 0."""
        for probe in ("python3 x.py end --key F1 | tail -5", "python3 x.py end --key F1 || true",
                      "python3 x.py end --key F1; echo done"):
            t = Transcript(self.tmp)
            t.bash(probe)
            with self.subTest(probe=probe):
                self.assertEqual(self.decide(t.say(block("python3 x.py end --key F1")))[0],
                                 "block")
        t = Transcript(self.tmp)
        t.bash("cd /tmp && python3 x.py end --key F1 2>&1")
        self.assertEqual(self.decide(t.say(block("cd /tmp && python3 x.py end --key F1")))[0],
                         "allow")

    def test_bash_c_with_positional_arguments_matches_whole(self):
        self.t.bash("bash -c 'gh pr merge \"$1\"' _ 12")
        self.assertEqual(self.decide(self.t.say(block("bash -c 'gh pr merge \"$1\"' _ 13")))[0],
                         "block")

    def test_a_bare_script_name_resolves_by_position(self):
        self.t.bash("python3 run.py --help")
        self.assertEqual(self.decide(self.t.say(block(f"python3 {self.tmp}/run.py")))[0], "allow")

    def test_turn_ending_in_question_is_allowed(self):
        text = block("gh pr merge 12") + "\n## DECISIONS NEEDED\n1. Should I merge it? Yes.\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")

    def test_no_section_is_allowed(self):
        self.assertEqual(self.decide(self.t.say("All done, nothing owed."))[0], "allow")


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
