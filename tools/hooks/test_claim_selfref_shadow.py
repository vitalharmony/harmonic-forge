#!/usr/bin/env python3
"""harmonic-forge#902: the self-referential claim class is recorded, never
acted on. One class per handoff test case."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claim_selfref_shadow as hook  # noqa: E402

INCIDENT = ("Slice 3a is implemented and all its tests pass. The verification gate (`mise run check`) "
            "is running in the background. When it passes, I'll commit through `mise run restart`.")
RUNNING = [{"type": "shell", "status": "running",
            "command": "cd /home/m/.worktrees/hrse2-2056-impl && mise run check > /tmp/check.log 2>&1"}]
LANE = {"LANE": "2"}


class StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "claim-stop"

    def decide(self, text: object, tasks: object = RUNNING, env: dict | None = None, **extra) -> object:
        payload = {"last_assistant_message": text, "background_tasks": tasks, "prompt_id": "p-1", **extra}
        return hook.decide(payload, env=LANE if env is None else env, directory=self.dir)

    def shadow(self) -> list[dict]:
        path = self.dir / hook.SHADOW
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def counts(self) -> dict:
        path = self.dir / hook.HEARTBEAT
        return json.loads(path.read_text()) if path.exists() else {}


class AC1OneLinePerSelfReferentialClaim(StateDir):
    def test_the_incident_appends_exactly_one_line(self) -> None:
        self.decide(INCIDENT)
        (line,) = self.shadow()
        self.assertEqual((line["claim"], line["command"], line["status"]), ("tests pass", "mise run check", "running"))

    def test_any_other_stop_appends_nothing(self) -> None:
        cases = [
            ("all its tests pass. Nothing else is running.", RUNNING),                        # no span
            (INCIDENT, [{**RUNNING[0], "status": "completed"}]),                             # task ended
            (INCIDENT, [{**RUNNING[0], "command": "npm run build"}]),                        # other command
            ("The gate (`mise run check`) is running in the background.", RUNNING),          # no claim
            ("All tests pass; run `check` again later.", [{**RUNNING[0], "command": "check"}]),  # 1-token span
            (INCIDENT, []),
            (INCIDENT, None),
        ]
        for text, tasks in cases:
            with self.subTest(text=text[:40], tasks=str(tasks)[:40]):
                self.decide(text, tasks)
        self.assertEqual(self.shadow(), [])

    def test_a_double_quoted_span_and_a_contiguous_token_run_both_match(self) -> None:
        self.assertIsNotNone(hook.selfref_match('Tests pass; "mise   run\tcheck" still going.', RUNNING))
        self.assertIsNone(hook.selfref_match("Tests pass; `mise check` still going.", RUNNING))  # not contiguous


class AC2NeverActsAndNeverFails(StateDir):
    def test_decide_is_always_none_and_main_prints_nothing(self) -> None:
        for text in (INCIDENT, "", None, 5, "Tests pass."):
            with self.subTest(text=str(text)[:20]):
                self.assertIsNone(self.decide(text))
        for raw in (json.dumps({"last_assistant_message": INCIDENT, "background_tasks": RUNNING}), "not json", "[1]"):
            out = io.StringIO()
            with self.subTest(raw=raw[:20]), patch("sys.stdin", io.StringIO(raw)), patch.dict(os.environ, LANE), \
                    patch.object(hook, "STATE_DIR", self.dir), contextlib.redirect_stdout(out):
                self.assertEqual(hook.main([]), 0)
            self.assertEqual(out.getvalue(), "")

    def test_an_unwritable_state_directory_never_raises(self) -> None:
        self.dir.mkdir(parents=True)
        self.dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.dir.chmod, stat.S_IRWXU)
        self.assertIsNone(self.decide(INCIDENT))

    def test_no_lane_records_nothing(self) -> None:
        self.decide(INCIDENT, env={})
        self.assertEqual((self.shadow(), self.counts()), ([], {}))


class Heartbeat(StateDir):
    def test_every_lane_stop_counts_and_a_match_counts_as_fired(self) -> None:
        self.decide("Working on it.", [])
        self.decide("Tests pass.", None)
        self.decide(INCIDENT)
        self.assertEqual(self.counts(), {"stops_seen": 3, "background_tasks_present": 2,
                                         "background_tasks_nonempty": 1, "tests_pass_matched": 2, "fired": 1})


class NonShellTasks(StateDir):
    def test_a_task_without_a_command_or_a_dict_shaped_list_never_raises_or_matches(self) -> None:
        tasks = {"agents": [{"status": "running", "description": "mise run check"}],
                 "bash": [{"status": "running", "command": None}, {"status": "running", "command": ""}, "junk"]}
        self.assertIsNone(hook.selfref_match(INCIDENT, tasks))
        self.assertEqual(len(hook.running_tasks({"bash": RUNNING})), 1)
        self.decide(INCIDENT, tasks)
        self.assertEqual(self.shadow(), [])


class AC3OnlySixFields(StateDir):
    def test_the_line_has_exactly_the_six_keys_and_no_other_message_text(self) -> None:
        self.decide(INCIDENT)
        (line,) = self.shadow()
        self.assertEqual(set(line), {"ts", "lane", "prompt_id", "claim", "command", "status"})
        raw = (self.dir / hook.SHADOW).read_text()
        for fragment in ("Slice 3a", "implemented", "background", "mise run restart"):
            self.assertNotIn(fragment, raw)


class AC4Report(StateDir):
    def test_report_prints_counts_pointers_and_sample_and_writes_nothing(self) -> None:
        self.decide(INCIDENT)
        (self.dir / "s1.json").write_text("not json, and never read")
        projects = Path(self.tmp.name) / "projects" / "-x"
        projects.mkdir(parents=True)
        (projects / "t.jsonl").write_text('{"type":"user"}\n{"promptId":"p-1","type":"user"}\n')
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(hook.report(self.dir, [projects.parent]), 0)
        text = out.getvalue()
        self.assertIn("1 fired, 1 distinct prompt(s)", text)
        self.assertIn("stops_seen=1", text)
        self.assertIn(f"{projects / 't.jsonl'}:2", text)
        self.assertIn("sample: n=1 (source: live)", text)
        self.assertEqual({p.name: p.read_bytes() for p in self.dir.iterdir()}, before)

    def test_report_on_an_empty_state_directory_says_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hook.report(self.dir, [])
        self.assertIn("0 fired", out.getvalue())
        self.assertFalse(self.dir.exists())


class Replay(unittest.TestCase):
    def test_the_incident_shape_fires_once_and_an_ended_run_does_not(self) -> None:
        launch = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": RUNNING[0]["command"], "run_in_background": True}}]}}
        ack = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "Command running in background with ID: bk1."}]}}
        final = {"type": "assistant", "message": {"content": [{"type": "text", "text": INCIDENT}]}}
        done = {"type": "user", "message": {"content": "<task-notification>\n<task-id>bk1</task-id>\n"
                                                       "<status>completed</status>"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in (launch, ack, final, done, final)) + "\n")
            fires = hook.replay_file(path)
            self.assertEqual([n for n, _ in fires], [3])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hook.replay(str(Path(tmp) / "*.jsonl"))
            self.assertIn("replay: 1 fire(s) over 1 transcript(s); sample: n=1 (source: replay)", out.getvalue())


class AC5Registration(unittest.TestCase):
    def test_registered_on_stop_in_the_guarded_form(self) -> None:
        settings = json.loads((Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text())
        commands = [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]
                    if "claim_selfref_shadow.py" in h["command"]]
        self.assertEqual(commands, ['f="${HOME}/harmonic-forge/tools/hooks/claim_selfref_shadow.py"; '
                                    '[ -f "$f" ] && python3 "$f" || true'])


if __name__ == "__main__":
    unittest.main()
