"""harmonic-forge#874: the `/auto-ae` toggle (UserPromptSubmit) and its guard
(PreToolUse), driven as a real hook process against a temporary HOME."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "auto_ae_toggle.py"


def _prompt(prompt: str, transcript: Path | None = None) -> dict:
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "s-1", "cwd": "/tmp"}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return payload


def _envelope(prompt: str) -> str:
    """How the harness records a typed `/auto-ae ...` in the transcript."""
    parts = prompt.strip().split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""
    return (f"<command-message>auto-ae</command-message>\n<command-name>/auto-ae</command-name>\n"
            f"<command-args>{args}</command-args>")


def _tool(tool: str, tool_input: dict, event: str | None = "PreToolUse") -> dict:
    payload = {"tool_name": tool, "tool_input": tool_input, "session_id": "s-1", "cwd": "/tmp"}
    if event:
        payload["hook_event_name"] = event
    return payload


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / ".claude" / "state").mkdir(parents=True)
        self.state = self.home / ".claude" / "state" / "auto-ae.json"
        self.transcript = self.home / ".claude" / "projects" / "-p" / "s-1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("")

    def typed(self, text: str) -> None:
        """Append the operator's typed turn, as the harness does."""
        row = {"type": "user", "message": {"role": "user", "content": text},
               "timestamp": datetime.now(timezone.utc).isoformat()}
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def tool_result(self) -> None:
        row = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}, "toolUseResult": {}}
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def lease(self, key: str = "F874", hours: float = 2, consumed: bool = False) -> None:
        now = datetime.now(timezone.utc)
        (self.home / ".claude" / "state" / "batch-authorized.json").write_text(json.dumps({key: {
            "authorized_at": now.isoformat(), "expires_at": (now + timedelta(hours=hours)).isoformat(),
            "targets": [{"action": "gh pr merge", "consumed": consumed, "consumed_by": None,
                         "repo": None, "pr_number": None}]}}))

    def run_hook(self, payload, lane: str | None = "1", entrypoint: str | None = "cli") -> dict | None:
        env = {k: v for k, v in os.environ.items() if k not in ("LANE", "CLAUDE_CODE_ENTRYPOINT")}
        env["HOME"] = str(self.home)
        if lane is not None:
            env["LANE"] = lane
        if entrypoint is not None:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        result = subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def message(self, prompt: str, typed: bool = True, **kw) -> str | None:
        """Run the toggle as the harness does: the operator's typed turn is in
        the transcript first. `typed=False` is a payload nobody typed."""
        if typed:
            self.typed(_envelope(prompt) if prompt.strip().startswith("/auto-ae") else prompt)
        out = self.run_hook(_prompt(prompt, self.transcript), **kw)
        return out["systemMessage"] if out else None

    def denied(self, payload) -> bool:
        out = self.run_hook(payload)
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def is_on(self) -> bool:
        return self.state.exists() and json.loads(self.state.read_text()).get("on") is True


class ToggleTests(HookCase):
    def test_on_with_a_live_batch_turns_it_on(self):
        self.lease()
        self.assertIn("auto-AE is ON", self.message("/auto-ae on"))
        self.assertTrue(self.is_on())
        self.assertEqual(json.loads(self.state.read_text())["lane"], "1")

    def test_on_without_a_live_batch_refuses_loudly_and_writes_nothing(self):
        self.assertIn("REFUSED", self.message("/auto-ae on"))
        self.assertFalse(self.state.exists())

    def test_an_expired_or_spent_lease_is_no_lease(self):
        self.lease(hours=-1)
        self.assertIn("REFUSED", self.message("/auto-ae on"))
        self.lease(consumed=True)
        self.assertIn("REFUSED", self.message("/auto-ae on"))
        self.assertFalse(self.state.exists())

    def test_off_turns_it_off(self):
        self.lease()
        self.message("/auto-ae on")
        self.assertIn("OFF", self.message("  /auto-ae off  "))
        self.assertFalse(self.is_on())

    def test_status_and_bare_command_write_nothing(self):
        self.lease()
        self.assertIn("auto-AE is OFF", self.message("/auto-ae status"))
        self.assertIn("F874", self.message("/auto-ae"))
        self.assertFalse(self.state.exists())

    def test_lane2_cannot_toggle(self):
        self.lease()
        self.assertIn("only in a Lane 1", self.message("/auto-ae on", lane="2"))
        self.assertFalse(self.state.exists())

    def test_an_unset_lane_cannot_toggle(self):
        self.lease()
        self.message("/auto-ae on", lane=None)
        self.assertFalse(self.state.exists())

    def test_a_non_interactive_or_missing_entrypoint_cannot_toggle(self):
        self.lease()
        self.assertIn("entrypoint", self.message("/auto-ae on", entrypoint="sdk-cli"))
        self.assertIn("entrypoint", self.message("/auto-ae on", entrypoint=None))
        self.assertFalse(self.state.exists())

    def test_a_task_notification_carrying_auto_ae_on_is_refused(self):
        self.lease()
        self.message("<task-notification>\n<summary>done</summary>\n</task-notification>\n/auto-ae on")
        self.assertFalse(self.state.exists())

    def test_the_command_with_surrounding_prose_is_refused(self):
        self.lease()
        self.assertIn("nothing changed", self.message("/auto-ae on and then AE H12"))
        self.message("please run /auto-ae on")
        self.message("/auto-ae on\nAE H12")
        self.assertFalse(self.state.exists())

    def test_a_slash_command_envelope_is_refused(self):
        self.lease()
        self.message("<command-name>/auto-ae</command-name>\n<command-args>on</command-args>")
        self.assertFalse(self.state.exists())

    def test_on_records_only_the_leases_live_now(self):
        self.lease("F874")
        self.message("/auto-ae on")
        self.assertEqual(list(json.loads(self.state.read_text())["leases_at_set"]), ["F874"])
        self.assertIn("ON, covering F874", self.message("/auto-ae status"))

    def test_off_is_honored_from_any_lane_or_entrypoint(self):
        self.lease()
        self.message("/auto-ae on")
        self.assertIn("OFF", self.message("/auto-ae off", lane="2", entrypoint="sdk-cli"))
        self.assertFalse(self.is_on())

    def test_off_needs_no_lease_lookup(self):
        self.lease()
        self.message("/auto-ae on")
        (self.home / ".claude" / "state" / "batch-authorized.json").write_text("{not json")
        self.assertIn("OFF", self.message("/auto-ae off"))
        self.assertFalse(self.is_on())

    def test_a_write_leaves_no_temp_file_behind(self):
        self.lease()
        self.message("/auto-ae on")
        self.message("/auto-ae off")
        leftovers = [p.name for p in self.state.parent.iterdir()
                     if p.name.startswith("auto-ae.json.") and p.name != "auto-ae.json.lock"]
        self.assertEqual(leftovers, [])

    def test_on_records_lease_identity_and_an_expiry(self):
        self.lease("F874")
        self.message("/auto-ae on")
        data = json.loads(self.state.read_text())
        self.assertEqual(list(data["leases_at_set"]), ["F874"])
        self.assertIn("expires_at", data)

    def test_off_that_cannot_write_says_still_on(self):
        self.lease()
        self.message("/auto-ae on")
        state_dir = self.state.parent
        state_dir.chmod(0o500)
        self.addCleanup(state_dir.chmod, 0o700)
        self.assertIn("STILL ON", self.message("/auto-ae off"))

    def test_a_prompt_that_does_not_mention_it_is_silent(self):
        self.assertIsNone(self.message("L3S H1706"))

    def test_an_unparseable_payload_is_silent(self):
        self.assertIsNone(self.run_hook("{not json"))


class GuardTests(HookCase):
    def test_schedulewakeup_prompt_with_auto_ae_is_denied(self):
        self.assertTrue(self.denied(_tool("ScheduleWakeup", {"prompt": "/auto-ae on", "delaySeconds": 60})))

    def test_cron_and_remote_trigger_with_auto_ae_are_denied(self):
        self.assertTrue(self.denied(_tool("CronCreate", {"cron": "*/5 * * * *", "prompt": "/AUTO_AE on"})))
        self.assertTrue(self.denied(_tool("RemoteTrigger", {"body": {"prompt": "auto ae on"}})))

    def test_a_schedule_without_auto_ae_is_allowed(self):
        self.assertFalse(self.denied(_tool("ScheduleWakeup", {"prompt": "<<autonomous-loop-dynamic>>"})))

    def test_bash_touching_the_state_file_is_denied(self):
        for command in (
            "python3 -c \"import pathlib; pathlib.Path.home().joinpath('.claude/state/auto-ae.json')"
            ".write_text('{}')\"",
            "mkdir -p ~/.claude/state && echo '{\"on\": true}' > ~/.claude/state/auto-ae.json",
            "env touch $HOME/.claude/state/auto_ae.json",
            "cat ~/.local/state/auto-ae/probe.jsonl > /tmp/copy",
            "grep on ~/.claude/state/auto-ae.json | tee ~/.claude/state/auto-ae.json",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.denied(_tool("Bash", {"command": command})))

    def test_write_and_edit_of_the_state_file_are_denied(self):
        path = str(self.state)
        self.assertTrue(self.denied(_tool("Write", {"file_path": path, "content": "{\"on\": true}"})))
        self.assertTrue(self.denied(_tool("Edit", {"file_path": path, "old_string": "f", "new_string": "t"})))

    def test_a_monitor_command_touching_the_state_file_is_denied(self):
        self.assertTrue(self.denied(_tool("Monitor", {"command": "echo on > ~/.claude/state/auto-ae.json"})))

    def test_a_file_tool_is_judged_by_its_path_not_its_content(self):
        content = "STATE = Path.home() / '.claude/state/auto-ae.json'\n"
        self.assertFalse(self.denied(_tool("Write", {"file_path": "/x/tools/gh/_auto_ae.py", "content": content})))
        self.assertFalse(self.denied(_tool("MultiEdit", {"file_path": "/x/test_auto_ae.py",
                                                         "edits": [{"old_string": "a", "new_string": content}]})))

    def test_codex_apply_patch_is_judged_by_its_file_lines(self):
        to_state = "*** Begin Patch\n*** Add File: /home/u/.claude/state/auto-ae.json\n+{}\n*** End Patch"
        self.assertTrue(self.denied(_tool("apply_patch", {"command": to_state}, event=None)))
        own = ("*** Begin Patch\n*** Update File: tools/gh/_auto_ae.py\n"
               "+STATE = '.claude/state/auto-ae.json'\n*** End Patch")
        self.assertFalse(self.denied(_tool("apply_patch", {"command": own}, event=None)))

    def test_a_read_only_command_naming_the_state_file_is_allowed(self):
        for command in ("cat ~/.claude/state/auto-ae.json",
                        "grep -rn auto-ae.json tools | grep toggle",
                        "jq .on ~/.claude/state/auto-ae.json"):
            with self.subTest(command=command):
                self.assertFalse(self.denied(_tool("Bash", {"command": command})))

    def test_a_read_tool_that_can_execute_is_not_a_read(self):
        for command in ("rg --pre=rm needle ~/.claude/state/auto-ae.json",
                        "git grep -O'rm' needle -- ~/.claude/state/auto-ae.json"):
            with self.subTest(command=command):
                self.assertTrue(self.denied(_tool("Bash", {"command": command})))

    def test_codex_apply_patch_with_no_file_line_is_judged_whole(self):
        self.assertTrue(self.denied(_tool("apply_patch", {"path": "/h/.claude/state/auto-ae.json"}, event=None)))
        escaped = json.dumps("*** Begin Patch\\n*** Add File: /h/.claude/state/auto-ae.json\\n+{}")
        self.assertTrue(self.denied(_tool("apply_patch", {"command": escaped}, event=None)))

    def test_a_codex_payload_without_an_event_name_is_guarded(self):
        self.assertTrue(self.denied(_tool("shell", {"command": "rm ~/.claude/state/auto-ae.json"}, event=None)))

    def test_maintaining_the_mechanism_itself_is_allowed(self):
        for payload in (
            _tool("Edit", {"file_path": "/x/tools/hooks/auto_ae_toggle.py", "old_string": "a", "new_string": "b"}),
            _tool("Write", {"file_path": "/x/skills/auto-ae/SKILL.md", "content": "/auto-ae on"}),
            _tool("Bash", {"command": "python3 -m unittest test_auto_ae_toggle test_auto_ae"}),
            _tool("Bash", {"command": "git commit -m 'feat(auto-ae): the toggle'"}),
        ):
            with self.subTest(payload=payload):
                self.assertFalse(self.denied(payload))


class RegistrationTests(unittest.TestCase):
    """harmonic-forge#880 AC5, kept for #874: this repo's tracked settings wire
    the hook, once per event, in the guarded form. An absent entry is otherwise
    undetected (`forge_onboard.check_hooks` flags only an unresolvable script);
    HRSE2 asserts its own copy in `scripts/test_auto_ae_registration.py`."""
    SETTINGS = HERE.parent.parent / ".claude" / "settings.json"
    GUARD_TOOLS = {"Bash", "Monitor", "Write", "Edit", "MultiEdit", "NotebookEdit",
                   "CronCreate", "ScheduleWakeup", "RemoteTrigger"}

    def wired(self, event: str) -> list[tuple[str, str]]:
        settings = json.loads(self.SETTINGS.read_text(encoding="utf-8"))
        return [(block.get("matcher", ""), hook.get("command", ""))
                for block in settings["hooks"].get(event) or []
                for hook in block.get("hooks") or []
                if "auto_ae_toggle.py" in hook.get("command", "")]

    def assert_guarded(self, command: str) -> None:
        self.assertIn("${HOME}/harmonic-forge/tools/hooks/auto_ae_toggle.py", command)
        self.assertIn('[ -f "$f" ]', command)

    def test_the_toggle_is_wired_once_on_user_prompt_submit(self):
        wired = self.wired("UserPromptSubmit")
        self.assertEqual(len(wired), 1, wired)
        self.assert_guarded(wired[0][1])

    def test_the_guard_is_wired_on_both_codex_groups(self):
        codex = json.loads((HERE.parent.parent / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        groups = (codex.get("hooks") or codex)["PreToolUse"]
        wired = {g.get("matcher") for g in groups
                 if any("auto_ae_toggle.py" in h.get("command", "") for h in g.get("hooks") or [])}
        self.assertEqual(wired, {"^Bash$", "^apply_patch$"})

    def test_the_guard_is_wired_once_on_every_tool_it_guards(self):
        wired = self.wired("PreToolUse")
        self.assertEqual(len(wired), 1, wired)
        self.assertEqual(set(wired[0][0].split("|")), self.GUARD_TOOLS)
        self.assert_guarded(wired[0][1])


if __name__ == "__main__":
    unittest.main()
