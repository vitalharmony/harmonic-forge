"""harmonic-forge#659 AC3 -- the belt-arming guard, driven as a real hook process.

Each deny case below is a wrong arm from the 2026-09-14 incident or its
transcripts; each allow case is the canonical call, or something unrelated the
guard must not touch.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "enforce_belt_arming.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
import belt_plan  # noqa: E402
import enforce_belt_arming as guard  # noqa: E402


def _run(tool_name, tool_input, lane="3", raw=None):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    if lane is not None:
        env["LANE"] = lane
    stdin = raw if raw is not None else json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input})
    result = subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                            capture_output=True, text=True)
    return result


def _decision(result):
    out = json.loads(result.stdout or "{}")
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


INCIDENT_SWEEP = ("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --sweep-for l3 "
                  "--account-repos vitalharmony --interval 300")
INCIDENT_LOOP_ARGS = (
    "10m Lane 3 suspenders: run three checks every tick, none gates another -- "
    "(A) spec owed ... never call stop.")
INCIDENT_CRON = {"cron": "*/10 * * * *", "recurring": True,
                 "prompt": "Lane 3 suspenders: proactively find work to do -- run checks A, B, C"}


class DeniesTheIncidentArms(unittest.TestCase):
    def test_sweep_monitor_is_denied(self):
        result = _run("Monitor", {"command": INCIDENT_SWEEP, "description": "sweep",
                                  "timeout_ms": 1800000})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(_decision(result), "deny")

    def test_paraphrased_loop_is_denied(self):
        self.assertEqual(_decision(_run("Skill", {"skill": "loop",
                                                  "args": INCIDENT_LOOP_ARGS})), "deny")

    def test_extended_canonical_loop_is_denied(self):
        args = "10m proactively find work to do (Lane 1 suspenders -- see skill)"
        self.assertEqual(_decision(_run("Skill", {"skill": "loop", "args": args})), "deny")

    def test_plugin_qualified_loop_is_checked_too(self):
        self.assertEqual(_decision(_run("Skill", {"skill": "x:loop",
                                                  "args": INCIDENT_LOOP_ARGS})), "deny")

    def test_suspenders_cron_is_denied(self):
        self.assertEqual(_decision(_run("CronCreate", INCIDENT_CRON)), "deny")

    def test_cron_wrapping_the_loop_command_is_denied(self):
        cron = {"cron": "*/10 * * * *", "recurring": True,
                "prompt": "/loop 10m proactively find work to do"}
        self.assertEqual(_decision(_run("CronCreate", cron)), "deny")

    def test_wrong_lane_belt_is_denied(self):
        lane2 = belt_plan.canonical_calls("2")["monitor"]["command"]
        self.assertEqual(_decision(_run("Monitor", {"command": lane2}, lane="3")), "deny")

    def test_off_table_interval_is_denied(self):
        cmd = belt_plan.canonical_calls("3")["monitor"]["command"].replace("300", "60")
        self.assertEqual(_decision(_run("Monitor", {"command": cmd})), "deny")

    def test_every_deny_quotes_the_exact_calls(self):
        calls = belt_plan.canonical_calls("3")
        for tool, tool_input in (
            ("Monitor", {"command": INCIDENT_SWEEP}),
            ("Skill", {"skill": "loop", "args": INCIDENT_LOOP_ARGS}),
            ("CronCreate", INCIDENT_CRON),
        ):
            with self.subTest(tool=tool):
                out = json.loads(_run(tool, tool_input).stdout)
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn(json.dumps(calls["monitor"]), reason)
                self.assertIn(json.dumps(calls["loop"]), reason)
                self.assertIn("belt_plan.py", reason)


class AllowsTheCanonicalCalls(unittest.TestCase):
    def test_canonical_monitor_is_allowed_for_every_lane(self):
        for lane in ("1", "2", "3"):
            with self.subTest(lane=lane):
                calls = belt_plan.canonical_calls(lane)
                result = _run("Monitor", calls["monitor"], lane=lane)
                self.assertEqual(json.loads(result.stdout), {})

    def test_home_spellings_are_equivalent(self):
        cmd = belt_plan.canonical_calls("3")["monitor"]["command"]
        home = os.path.expanduser("~")
        for spelling in ("$HOME/", "${HOME}/", home + "/"):
            with self.subTest(spelling=spelling):
                variant = cmd.replace("~/", spelling).replace(" --", "   --", 1)
                self.assertIsNone(_decision(_run("Monitor", {"command": variant})))

    def test_canonical_loop_is_allowed(self):
        result = _run("Skill", belt_plan.canonical_calls("3")["loop"])
        self.assertEqual(json.loads(result.stdout), {})

    def test_the_cron_the_loop_skill_itself_creates_is_allowed(self):
        """`/loop 10m proactively find work to do` is implemented by the loop
        skill as this exact CronCreate (seen in real transcripts). Denying it
        would deny the canonical arm."""
        cron = {"cron": "*/10 * * * *", "prompt": "proactively find work to do",
                "recurring": True}
        self.assertIsNone(_decision(_run("CronCreate", cron)))


class LeavesEverythingElseAlone(unittest.TestCase):
    def test_unrelated_monitor(self):
        self.assertIsNone(_decision(_run("Monitor", {"command": "tail -f logs/backend.log"})))

    def test_unrelated_cron(self):
        cron = {"cron": "39 9 18 8 *", "prompt": "Remind Marc about hrse#999",
                "recurring": False}
        self.assertIsNone(_decision(_run("CronCreate", cron)))

    def test_unrelated_loop(self):
        self.assertIsNone(_decision(_run(
            "Skill", {"skill": "loop", "args": "30m check hrse#1166 for ready-for-l3"})))

    def test_other_skills(self):
        self.assertIsNone(_decision(_run(
            "Skill", {"skill": "belt-and-suspenders", "args": "the belt"})))

    def test_no_lane_means_no_enforcement(self):
        for lane in (None, "", "4"):
            with self.subTest(lane=lane):
                self.assertIsNone(_decision(_run("Monitor", {"command": INCIDENT_SWEEP},
                                                 lane=lane)))


class FailsOpen(unittest.TestCase):
    def test_malformed_payload_allows_with_exit_0(self):
        result = _run(None, None, raw="not json")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(_decision(result))
        self.assertIn("guard did not run", result.stdout)

    def test_internal_error_allows(self):
        original = guard._load_belt_plan
        try:
            guard._load_belt_plan = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            with self.assertRaises(RuntimeError):
                guard.decide({"tool_name": "Monitor",
                              "tool_input": {"command": INCIDENT_SWEEP}}, "3")
        finally:
            guard._load_belt_plan = original
        # The process-level wrapper turns that into an allow; a missing
        # belt_plan module is the realistic trigger.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, runpy; sys.modules['belt_plan'] = None; "
             f"runpy.run_path({str(HOOK)!r}, run_name='__main__')"],
            input=json.dumps({"tool_name": "Monitor",
                              "tool_input": {"command": INCIDENT_SWEEP}}),
            env={**os.environ, "LANE": "3"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(_decision(result))
        self.assertIn("guard did not run", result.stdout)


class Registration(unittest.TestCase):
    """AC5: registered in this repo, guarded, on the three tools."""

    def test_forge_settings_register_the_guarded_hook(self):
        settings = json.loads((HERE.parent.parent / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        entries = [e for e in settings["hooks"]["PreToolUse"]
                   if e.get("matcher") == "Monitor|CronCreate|Skill"]
        self.assertEqual(len(entries), 1)
        commands = [h["command"] for h in entries[0]["hooks"]]
        self.assertIn('f="${HOME}/harmonic-forge/tools/hooks/enforce_belt_arming.py"; '
                      '[ -f "$f" ] && python3 "$f" || true', commands)


if __name__ == "__main__":
    unittest.main()
