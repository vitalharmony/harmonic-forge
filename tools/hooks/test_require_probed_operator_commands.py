#!/usr/bin/env python3
"""Tests for require_probed_operator_commands.py (harmonic-forge#1151)."""
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

ROOT = HERE.parent.parent


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


def actions(*cmds: str) -> str:
    return "Done.\n\n## YOUR NEXT ACTIONS\n" + "\n".join(
        f"{i}. Run:\n   `{c}`" for i, c in enumerate(cmds, 1)) + "\n"


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

    def test_unprobed_command_blocks(self):
        verdict, reason = self.decide(self.t.say(actions("python3 tools/gh/l1_post.py --issue 1")))
        self.assertEqual(verdict, "block")
        self.assertIn("python3 tools/gh/l1_post.py", reason)

    def test_probed_command_is_allowed(self):
        self.t.bash("python3 tools/gh/l1_post.py --help")
        self.assertEqual(self.decide(self.t.say(actions("python3 tools/gh/l1_post.py --issue 1")))[0],
                         "allow")

    def test_probe_of_other_script_does_not_clear(self):
        self.t.bash("python3 tools/gh/other.py --help")
        self.assertEqual(self.decide(self.t.say(actions("python3 tools/gh/l1_post.py --issue 1")))[0],
                         "block")

    def test_other_mise_task_does_not_clear(self):
        self.t.bash("mise run check")
        self.assertEqual(self.decide(self.t.say(actions("mise run restart")))[0], "block")

    def test_errored_probe_does_not_clear(self):
        self.t.bash("python3 tools/gh/l1_post.py --help", error=True)
        self.assertEqual(self.decide(self.t.say(actions("python3 tools/gh/l1_post.py --issue 1")))[0],
                         "block")

    def test_tilde_and_absolute_spellings_match(self):
        home = Path.home()
        self.t.bash(f"bash -n {home}/Harmonic_Projects/research/x.sh")
        self.assertEqual(self.decide(self.t.say(actions("bash ~/Harmonic_Projects/research/x.sh")))[0],
                         "allow")

    def test_relative_probe_clears_absolute_offer(self):
        self.t.bash("python3 tools/gh/l1_post.py --help")
        self.assertEqual(self.decide(self.t.say(actions(
            "python3 ~/harmonic-forge/tools/gh/l1_post.py --issue 1")))[0], "allow")

    def test_bare_nonexecutable_tilde_script_blocks_even_when_probed(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            self.t.bash(f"bash -n {self.script}")
            verdict, reason = self.decide(self.t.say(actions("~/reset.sh && systemctl --user start x")))
        self.assertEqual(verdict, "block")
        self.assertIn("not executable as written", reason)

    def test_plain_words_and_trigger_phrases_are_not_commands(self):
        doc = self.tmp / "notes.md"
        doc.write_text("# notes\n")
        text = actions("forced F1145", "BATCH F1151 IMC F1151", "test", "close H2484", str(doc))
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")

    def test_flag_value_is_not_the_subcommand(self):
        self.t.bash("git -C /r status")
        self.assertEqual(self.decide(self.t.say(actions("git -C /r push --force")))[0], "block")

    def test_gh_read_does_not_clear_gh_write(self):
        self.t.bash("gh pr view 12 --repo o/r")
        self.assertEqual(self.decide(self.t.say(actions("gh pr merge 12 --repo o/r")))[0], "block")

    def test_comment_inside_fence_does_not_end_section(self):
        text = "## YOUR NEXT ACTIONS\n1. Run:\n```bash\n# merge forge first\ngh pr merge 12 --squash\n```\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "block")

    def test_tilde_fence_is_read(self):
        text = "## YOUR NEXT ACTIONS\n~~~\ngh pr merge 12 --squash\n~~~\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "block")

    def test_malformed_span_does_not_hide_other_commands(self):
        text = actions("echo 'oops", "gh pr merge 12 --squash")
        self.assertEqual(self.decide(self.t.say(text))[0], "block")

    def test_truncated_scan_allows(self):
        path = self.t.say(actions("gh pr merge 12 --squash"))
        with mock.patch.object(hook, "scan_turn", return_value=([], True)):
            self.assertEqual(self.decide(path)[0], "allow")

    def test_no_section_or_no_commands_is_allowed(self):
        self.assertEqual(self.decide(self.t.say("All done, nothing owed."))[0], "allow")

    def test_turn_ending_in_question_is_allowed(self):
        text = actions("python3 tools/gh/l1_post.py --issue 1") + \
            "\n## DECISIONS NEEDED\n1. Should I post it? I recommend yes.\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")

    def test_section_ends_at_next_heading(self):
        text = "## YOUR NEXT ACTIONS\nNone.\n\n## Notes\n`python3 tools/gh/l1_post.py`\n"
        self.assertEqual(self.decide(self.t.say(text))[0], "allow")


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
