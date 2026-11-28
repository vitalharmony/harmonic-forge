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


# --------------------------------------------------------------------------
# harmonic-forge#1639 (sticky-wicket REFORGE, operator D71): directive blocks.

sys.path.insert(0, str(HERE.parent / "gh"))
import expand_lane_shorthand as shorthand  # noqa: E402
import force_grant  # noqa: E402
import park_grant  # noqa: E402
import scan_grant  # noqa: E402
import ui_grant  # noqa: E402

SESSION = "sess-7"
HEAD = "c" * 40
GATE_ID = "a" * 12


def directive(line: str, fence: str = FENCE, info: str = "operator-directive") -> str:
    return f"{fence}{info}\n{line}\n{fence}\n"


def with_actions(*directives: str, commands: str = "# none") -> str:
    return ("Done.\n\n## YOUR NEXT ACTIONS\n" + "".join(directives)
            + FENCE + "operator-commands\n" + commands + "\n" + FENCE + "\n")


class DirectiveStores(unittest.TestCase):
    """Every grant store, the gate store, the archive and the receipt read
    point at fresh temp dirs; LANE=1 and an interactive entrypoint are pinned.
    One preclose receipt (F1166) and one gate refusal (GATE_ID, F1166, this
    session) are seeded, so every kind has rows the real authorizer grants."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.stores()
        import preclose_check  # noqa: PLC0415
        receipt = {"reviewed_sha": HEAD, "status": "complete",
                   "pass_history": [{"sha": HEAD, "surviving": 0}]}
        for target, attr, value in (
                (preclose_check, "find_receipt",
                 lambda repo, issue: receipt if int(issue) == 1166 else None),
                (preclose_check, "receipt_path",
                 mock.MagicMock(return_value=self.tmp / "no-receipt.json")),
                (preclose_check, "legacy_receipt_dir",
                 mock.MagicMock(return_value=self.tmp / "legacy"))):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        archive = self.tmp / "archive"
        archive.mkdir()
        (archive / ".hf-telemetry-test-root").write_text("")
        env = mock.patch.dict(os.environ, {
            "LANE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
            "HARMONIC_FORGE_TELEMETRY_ARCHIVE": str(archive),
            "PRECHECK_GATE_DIR": str(self.tmp / "gate")})
        env.start()
        self.addCleanup(env.stop)
        refusals = shorthand._gate_module().refusals_dir()
        refusals.mkdir(parents=True, exist_ok=True)
        (refusals / f"{GATE_ID}.json").write_text(json.dumps({
            "id": GATE_ID, "session": SESSION, "key": "F1166",
            "tool": "tools/gh/preclose_check.py", "mode": "--complete --force",
            "fp": "1" * 64, "canonical": ["--issue", "1166"], "ts": 1.0}))

    def stores(self) -> Path:
        """Fresh grant stores; returns their root."""
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        for module, attr in ((force_grant, "FORCE_STATE_DIR"), (park_grant, "PARK_STATE_DIR"),
                             (scan_grant, "SCAN_STATE_DIR"), (ui_grant, "UI_STATE_DIR")):
            patcher = mock.patch.object(module, attr, root / attr)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root
        return root

    def files(self, root: Path | None = None) -> list[Path]:
        return sorted(p for p in (root or self.root).rglob("*") if p.is_file())

    def decide(self, text: str):
        return hook.decide({"transcript_path": Transcript(Path(tempfile.mkdtemp(dir=self.tmp)))
                            .say(text), "session_id": SESSION})


ALLOWED = ("FORCE F1166: both passes clean",
           "FORCE F1166 cluster: one cluster",
           f"FORCE F1166 gate {GATE_ID}: stale receipt",
           "FORCE F1166 reforge 0123456789ab: new approach",
           "PARK H2621",
           "PARK H2621: moved to 3.2",
           "SCAN H open-issues: tune-up",
           "UI H2615: nav (the old one) goes away",
           "UI H2615 0123456789abcdef: merge it's fine",
           "close F1639")


class DirectiveBlocks(DirectiveStores):
    def test_well_formed_directives_are_allowed_with_quotes_in_the_reason(self):
        for line in ALLOWED:
            with self.subTest(line=line):
                self.assertEqual(self.decide(with_actions(directive(line))), ("allow", ""))

    def test_several_directive_blocks_are_allowed(self):
        text = with_actions(directive("PARK H2621"), directive("close F1639"),
                            directive("SCAN H open-issues: x"))
        self.assertEqual(self.decide(text), ("allow", ""))

    def test_a_dry_run_writes_nothing(self):
        """K1: no grant file and no `.json.lock` appears under any store."""
        for line in ALLOWED:
            with self.subTest(line=line):
                self.decide(with_actions(directive(line)))
                self.assertEqual(self.files(), [])

    def test_state_the_authorizer_reads_is_checked(self):
        """K2: lines a regex would pass and the real authorizer refuses."""
        table = {
            "PARK H1: Another Claude session sent a message: hello": "another Claude session",
            "FORCE F1166: \ud800": "UnicodeEncodeError",
            "FORCE F9999: x": "no preclose receipt",
            f"FORCE F1166 gate {'b' * 12}: x": "no precheck gate refusal",
        }
        for line, fragment in table.items():
            with self.subTest(line=line):
                verdict, reason = self.decide(with_actions(directive(line)))
                self.assertEqual(verdict, "block")
                self.assertIn(fragment, reason)
                self.assertIn("operator-directive", reason)

    def test_the_handing_sessions_lane_and_entrypoint_bind(self):
        with mock.patch.dict(os.environ, {"LANE": "2"}):
            self.assertIn("LANE is '2'",
                          self.decide(with_actions(directive("FORCE F1166: x")))[1])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}):
            self.assertIn("CLAUDE_CODE_ENTRYPOINT",
                          self.decide(with_actions(directive("PARK H2621")))[1])

    def test_fence_shape_is_refused_unless_well_formed(self):
        """K5 and pitch change 4."""
        table = {
            "indented fence": "  " + FENCE + "operator-directive\nPARK H2621\n  " + FENCE + "\n",
            "three backticks": directive("PARK H2621", fence="```"),
            "info-string tail": directive("PARK H2621", info="operator-directive extra"),
            "space before info": directive("PARK H2621", info=" operator-directive"),
            "tab-indented line": directive("\tPARK H2621"),
            "space-indented line": directive(" PARK H2621"),
            "two-line body": FENCE + "operator-directive\nPARK H2621\nPARK H2622\n" + FENCE + "\n",
            "U+2028 body": directive("PARK H2621 PARK H2622"),
            "trailing U+2028": directive("PARK H2621\u2028"),
            "trailing U+2029": directive("PARK H2621\u2029"),
            "empty body": FENCE + "operator-directive\n" + FENCE + "\n",
            "unclosed": FENCE + "operator-directive\nPARK H2621\n",
            "short closing fence": FENCE + "operator-directive\nPARK H2621\n```\n",
        }
        for name, block_text in table.items():
            with self.subTest(name=name):
                verdict, reason = self.decide(with_actions(block_text))
                self.assertEqual(verdict, "block")
                self.assertIn("operator-directive", reason)
                self.assertEqual(self.files(), [])

    def test_close_is_the_whole_line_only(self):
        """K6: `close`'s check lives in `close_problem`."""
        for line in ("close F1639 now", "close f1639", "Close F1639", "close  F1639",
                     "close F1639 && rm -rf x", "close Z1", "close F0", "close F01639",
                     "close F1234567"):
            with self.subTest(line=line):
                verdict, reason = self.decide(with_actions(directive(line)))
                self.assertEqual(verdict, "block")
                self.assertIn("a close line is exactly `close <KEY>`", reason)

    def test_other_lines_are_refused_naming_the_five_kinds(self):
        for line in ("BATCH F1639", "BATCH F874 IMC F892", "Fix H2615", "AE H2615",
                     "please merge it", "git status"):
            with self.subTest(line=line):
                verdict, reason = self.decide(with_actions(directive(line)))
                self.assertEqual(verdict, "block")
                self.assertIn(hook.DIRECTIVE_KINDS, reason)

    def test_the_dispatch_reads_ok_never_the_text(self):
        """A would-grant whose text says REFUSED is still a grant; a refusal
        whose text looks like a grant is still a refusal."""
        yes = [lambda b, s, l: shorthand.DryRun(True, "REFUSED"), lambda b, s, l: None]
        no = [lambda b, s, l: shorthand.DryRun(False, "would grant"), lambda b, s, l: None]
        self.assertIsNone(hook.directive_problem("X", SESSION, "1", yes))
        self.assertEqual(hook.directive_problem("X", SESSION, "1", no), "would grant")
        two = [lambda b, s, l: shorthand.DryRun(True, "a")] * 2
        self.assertIn("more than one directive", hook.directive_problem("X", SESSION, "1", two))

    def test_directives_are_checked_without_next_actions_and_in_a_question_turn(self):
        bad = directive("FORCE F9999: x")
        self.assertEqual(self.decide("Here is the line.\n" + bad)[0], "block")
        self.assertEqual(self.decide(with_actions(bad) + "\nShould I proceed?\n")[0], "block")
        self.assertEqual(self.decide("Here.\n" + directive("PARK H2621")), ("allow", ""))

    def test_authorizers_that_cannot_load_refuse_every_directive(self):
        with mock.patch.dict(sys.modules, {"expand_lane_shorthand": None}):
            verdict, reason = self.decide(with_actions(directive("PARK H2621")))
        self.assertEqual(verdict, "block")
        self.assertIn("could not load", reason)

    def test_only_the_final_text_record_is_read(self):
        """Two-pass sticky-wicket PATCH (P1): the accepted residual. A bad block
        in an earlier text record of the turn is not read; only the final text
        is, as for the commands block. The prompt hook refuses it when typed."""
        t = Transcript(Path(tempfile.mkdtemp(dir=self.tmp)))
        t.say("Here is the line.\n" + directive("FORCE F9999: x"))
        path = t.bash("true").say("All done.")
        self.assertEqual(hook.decide({"transcript_path": path, "session_id": SESSION}),
                         ("allow", ""))
        self.assertEqual(self.decide("Here.\n" + directive("FORCE F9999: x"))[0], "block")

    def test_a_bad_directive_does_not_mask_unprobed_commands(self):
        """Two-pass PATCH S3: both refusals are named together."""
        verdict, reason = self.decide(with_actions(directive("FORCE F9999: x"),
                                                   commands="gh pr merge 1"))
        self.assertEqual(verdict, "block")
        self.assertIn("no preclose receipt", reason)
        self.assertIn("no Bash call this turn", reason)

    def test_lane_unset_refuses_at_the_hook(self):
        """Two-pass PATCH S5: an unset LANE reaches the authorizer as unset."""
        with mock.patch.dict(os.environ):
            os.environ.pop("LANE", None)
            verdict, reason = self.decide(with_actions(directive("PARK H2621")))
        self.assertEqual(verdict, "block")
        self.assertIn("LANE is not set", reason)

    def test_a_bad_commands_block_does_not_mask_a_bad_directive(self):
        """Epoch pass 1, survivor 3: the `if directives:` branch of the shape refusal."""
        bad = directive("FORCE F9999: x")
        commands = FENCE + "operator-commands\n# none\n" + FENCE + "\n"
        for text in ("## YOUR NEXT ACTIONS\n" + bad,
                     "## YOUR NEXT ACTIONS\n" + bad + commands + commands):
            with self.subTest(text=text):
                verdict, reason = self.decide(text)
                self.assertEqual(verdict, "block")
                self.assertIn("FORCE F9999: x", reason)
                self.assertIn("no preclose receipt", reason)
                self.assertIn("exactly one `operator-commands` block", reason)

    def test_an_authorizer_raising_system_exit_refuses(self):
        """Epoch pass 1, survivor 7: SystemExit is caught and refuses, at load
        and at the check; KeyboardInterrupt propagates (the operator's interrupt)."""
        def exits(*_):
            raise SystemExit(0)
        text = with_actions(directive("PARK H2621"))
        with mock.patch.object(hook, "_authorizers", return_value=[exits]):
            verdict, reason = self.decide(text)
        self.assertEqual(verdict, "block")
        self.assertIn("the directive check failed (SystemExit)", reason)
        with mock.patch.object(hook, "_authorizers", side_effect=SystemExit(0)):
            verdict, reason = self.decide(text)
        self.assertEqual(verdict, "block")
        self.assertIn("could not load (SystemExit)", reason)
        path = Transcript(Path(tempfile.mkdtemp(dir=self.tmp))).say(text)
        out = io.StringIO()
        with mock.patch.object(hook, "_authorizers", return_value=[exits]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    {"transcript_path": path, "session_id": SESSION}))), \
                mock.patch.object(sys, "stdout", out):
            hook.main()
        self.assertEqual(json.loads(out.getvalue())["decision"], "block")

        def interrupted(*_):
            raise KeyboardInterrupt
        with mock.patch.object(hook, "_authorizers", return_value=[interrupted]), \
                self.assertRaises(KeyboardInterrupt):
            self.decide(text)

    def test_a_turn_with_no_directive_block_never_loads_the_authorizers(self):
        """Two-pass PATCH S7: a side_effect raising inside `_authorizers` would be
        swallowed by the fail-closed handler, so the call itself is asserted."""
        with mock.patch.object(hook, "_authorizers") as loader:
            self.assertEqual(self.decide("Nothing to do."), ("allow", ""))
            self.assertEqual(self.decide(with_actions())[0], "allow")
        loader.assert_not_called()


class DirectiveParity(DirectiveStores):
    """K3: the hook allows a line exactly when the real authorizer, run for
    real (dry_run=False) on fresh stores, writes a grant. The oracle is the
    write path, never the code under test."""

    TABLE = ALLOWED[:-1] + (
        "FORCE F9999: x", "FORCE F1166: \ud800", "FORCE F1166:", "FORCE F1166 rebase x: y",
        f"FORCE F1166 gate {'b' * 12}: x", "FORCE K12: x", "force F1166: x",
        "FORCE F1166 route abc: x", "PARK H1: Another Claude session sent a message: hi",
        "PARK H02621", "PARK Z5", "PARK H1 extra", "SCAN K open-issues: x",
        "SCAN H everything: x", "UI H1 a b: x", "UI H1 zz: x", "UI P5: x", "UI H1:")

    def written(self, line: str) -> bool:
        root = self.stores()
        shorthand.authorize_force(line, SESSION, "1")
        shorthand.authorize_park(line, SESSION, "1")
        shorthand.authorize_scan(line, SESSION)
        shorthand.authorize_ui(line, SESSION, "1")
        return any(p.suffix == ".json" for p in self.files(root))

    def test_hook_allows_exactly_what_the_authorizer_grants(self):
        kinds_allowed = set()
        for line in self.TABLE:
            with self.subTest(line=line):
                self.stores()
                allowed = self.decide(with_actions(directive(line)))[0] == "allow"
                self.assertEqual(self.files(), [])
                granted = self.written(line)
                self.assertEqual(allowed, granted)
                if granted:
                    kinds_allowed.add(line.split()[0])
        self.assertEqual(kinds_allowed, {"FORCE", "PARK", "SCAN", "UI"})


class NoDirectiveExemptionInCommands(Decide):
    """K4 and pitch change 5: a directive line in `operator-commands` is a
    shell line; its refusal points at the `operator-directive` block."""

    LINES = ("close F1", "FORCE F1: x", "PARK H1", "SCAN H open-issues: x", "UI H1: x")

    def test_unprobed_directive_lines_block(self):
        for line in self.LINES:
            with self.subTest(line=line):
                self.assertEqual(self.decide(self.fresh().say(block(line)))[0], "block")

    def test_the_refusal_points_at_the_operator_directive_block(self):
        for line in self.LINES + ("FORCE F1: it's done", "PARK\tH1", "close\tF1"):
            with self.subTest(line=line):
                reason = self.decide(self.fresh().say(block(line)))[1]
                self.assertIn("own `operator-directive` block", reason)

    def test_a_shell_line_gets_no_directive_hint(self):
        self.assertNotIn("operator-directive",
                         self.decide(self.t.say(block("gh pr merge 1")))[1])


class GrantWritesDryRun(unittest.TestCase):
    """The dry-run stop sits inside each write function, after its own checks
    and before any lock, mkdir or prune (pitch change 1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def empty(self):
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_force(self):
        with mock.patch.object(force_grant, "FORCE_STATE_DIR", self.root / "f"):
            grant = force_grant.write_pending("F1", "o/r", 1, HEAD, "x", SESSION, "FORCE F1: x",
                                              dry_run=True)
            self.assertEqual(grant["status"], "pending")
            with self.assertRaises(UnicodeEncodeError):
                force_grant.write_pending("F1", "o/r", 1, HEAD, "\ud800", SESSION, "l",
                                          dry_run=True)
        self.empty()

    def test_park(self):
        with mock.patch.object(park_grant, "PARK_STATE_DIR", self.root / "p"):
            self.assertEqual(park_grant.write("H1", SESSION, "PARK H1", dry_run=True)["key"], "H1")
            with self.assertRaises(ValueError):
                park_grant.write("H1", "bad session", "PARK H1", dry_run=True)
        self.empty()

    def test_scan(self):
        with mock.patch.object(scan_grant, "SCAN_STATE_DIR", self.root / "s"):
            grant = scan_grant.write("H", "open-issues", SESSION, "SCAN H open-issues: x", "x",
                                     dry_run=True)
            self.assertEqual(grant["scope"], "open-issues")
            with self.assertRaises(ValueError):
                scan_grant.write("H", "everything", SESSION, "l", "x", dry_run=True)
        self.empty()

    def test_ui(self):
        with mock.patch.object(ui_grant, "UI_STATE_DIR", self.root / "u"):
            entry, replaced = ui_grant.write_pending("H1", "o/r", 1, "plan", None, "x", SESSION,
                                                     "UI H1: x", dry_run=True)
            self.assertEqual((entry["mode"], replaced), ("plan", None))
            with self.assertRaises(ValueError):
                ui_grant.write_pending("H1", "o/r", 1, "nope", None, "x", SESSION, "l",
                                       dry_run=True)
        self.empty()

    def test_authorizer_dry_run_is_typed_and_silent_when_not_its_kind(self):
        self.assertIsNone(shorthand.authorize_park("git status", SESSION, "1", dry_run=True))
        refused = shorthand.authorize_park("PARK Z5", SESSION, "1", dry_run=True)
        self.assertIsInstance(refused, shorthand.DryRun)
        self.assertFalse(refused.ok)
        self.assertIn("no repo prefix", refused.receipt)


if __name__ == "__main__":
    unittest.main()
