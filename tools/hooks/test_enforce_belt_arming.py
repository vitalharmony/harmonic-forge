"""harmonic-forge#659 AC3 -- the belt-arming guard, driven as a real hook process.

Each deny case below is a wrong arm from the 2026-09-14 incident or its
transcripts; each allow case is the canonical call, or something unrelated the
guard must not touch.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "enforce_belt_arming.py"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
import belt_plan  # noqa: E402
import enforce_belt_arming as guard  # noqa: E402


#: Every hook process in this file records arming here, never in the real cache.
_ARMING_ROOT = tempfile.TemporaryDirectory(prefix="belt_arming_test_")


def _run(tool_name, tool_input, lane="3", raw=None, session_id=None, arming_dir=None):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    if lane is not None:
        env["LANE"] = lane
    env[guard.ARMING_DIR_ENV] = arming_dir or tempfile.mkdtemp(dir=_ARMING_ROOT.name)
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if session_id is not None:
        payload["session_id"] = session_id
    stdin = raw if raw is not None else json.dumps(payload)
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
                self.assertIn(json.dumps(guard.LOOP_CRON), reason)
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

    def test_other_skills(self):
        self.assertIsNone(_decision(_run(
            "Skill", {"skill": "belt-and-suspenders", "args": "the belt"})))

    def test_no_lane_means_no_enforcement(self):
        for lane in (None, "", "4"):
            with self.subTest(lane=lane):
                self.assertIsNone(_decision(_run("Monitor", {"command": INCIDENT_SWEEP},
                                                 lane=lane)))
                self.assertIsNone(_decision(_run(
                    "CronCreate", {"cron": "* * * * *", "prompt": "anything"}, lane=lane)))


CANONICAL_CRON = {"cron": "*/10 * * * *", "prompt": "proactively find work to do",
                  "recurring": True}


class PrecloseA_RewordedLoopPromptsAreDenied(unittest.TestCase):
    """#659 preclose A: the keyword regex let these through."""

    REWORDED = (
        "10m look for work queued to lane 3 and act",
        "1m proactively find any queued work",
        "10m check my queue for handoffs and act on them",
        "5m find any open work to do",
    )

    def test_each_reworded_loop_is_denied(self):
        for args in self.REWORDED:
            with self.subTest(args=args):
                self.assertEqual(_decision(_run("Skill", {"skill": "loop", "args": args})),
                                 "deny")

    def test_each_reworded_cron_is_denied(self):
        for args in self.REWORDED:
            prompt = args.split(" ", 1)[1]
            with self.subTest(prompt=prompt):
                cron = {"cron": "*/10 * * * *", "prompt": prompt, "recurring": True}
                self.assertEqual(_decision(_run("CronCreate", cron)), "deny")


class PrecloseB_CanonicalPromptOnAnotherScheduleIsDenied(unittest.TestCase):
    """#659 preclose B: only `prompt` was compared, never `cron` or `recurring`."""

    def test_every_minute_is_denied(self):
        cron = dict(CANONICAL_CRON, cron="* * * * *")
        self.assertEqual(_decision(_run("CronCreate", cron)), "deny")

    def test_one_shot_is_denied(self):
        cron = dict(CANONICAL_CRON, recurring=False)
        self.assertEqual(_decision(_run("CronCreate", cron)), "deny")

    def test_missing_recurring_is_the_tool_default_true(self):
        cron = {"cron": "*/10 * * * *", "prompt": "proactively find work to do"}
        self.assertIsNone(_decision(_run("CronCreate", cron)))


class PrecloseC_SecondArmInOneSessionIsDenied(unittest.TestCase):
    """#659 preclose C: re-arming stacked a duplicate cron job."""

    def test_second_canonical_cron_in_the_same_session_is_denied(self):
        with tempfile.TemporaryDirectory() as arming:
            first = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(first))
            self.assertTrue((Path(arming) / "sess-a").exists())
            second = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertEqual(_decision(second), "deny")
            reason = json.loads(second.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("already armed", reason)

    def test_another_session_arms_independently(self):
        with tempfile.TemporaryDirectory() as arming:
            _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            other = _run("CronCreate", CANONICAL_CRON, session_id="sess-b", arming_dir=arming)
            self.assertIsNone(_decision(other))

    def test_the_skill_call_does_not_arm(self):
        """The loop skill issues its CronCreate AFTER the Skill call, so the
        record is written at the cron, not the skill -- or the skill's own
        cron would be denied as a re-arm."""
        with tempfile.TemporaryDirectory() as arming:
            skill = _run("Skill", belt_plan.canonical_calls("3")["loop"],
                         session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(skill))
            cron = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(cron))


class PrecloseD_NoKeywordMatching(unittest.TestCase):
    """#659 preclose D: a word-boundary-free regex denied unrelated crons as
    mis-armed suspenders, and let other unrelated ones through. Now every
    non-canonical cron or loop in a lane session is denied the same way, and
    the reason sends the work to Monitor or run_in_background."""

    CRONS = ("every hour find failing workflow runs on main",
             "check CI and find broken workflows",
             "remind me to buckle my seatbelt",
             "Remind Marc about hrse#999")
    LOOPS = ("30m find failing workflows", "30m check hrse#1166 for ready-for-l3")

    def _assert_redirected(self, result):
        self.assertEqual(_decision(result), "deny")
        reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Monitor", reason)
        self.assertIn("run_in_background", reason)

    def test_unrelated_crons_are_redirected(self):
        for prompt in self.CRONS:
            with self.subTest(prompt=prompt):
                self._assert_redirected(_run("CronCreate", {"cron": "0 * * * *",
                                                            "prompt": prompt}, lane="2"))

    def test_unrelated_loops_are_redirected(self):
        for args in self.LOOPS:
            with self.subTest(args=args):
                self._assert_redirected(_run("Skill", {"skill": "loop", "args": args},
                                             lane="2"))


class PrecloseE_MonitorsThatOnlyMentionTheWatcher(unittest.TestCase):
    """#659 preclose E: a substring match denied read-only commands."""

    def test_mentions_are_allowed(self):
        for command in ("pgrep -af watch_lane_posts.py",
                        "tail -F logs/x.log | grep watch_lane_posts.py",
                        "grep -n CANONICAL ~/harmonic-forge/tools/gh/watch_lane_posts.py"):
            with self.subTest(command=command):
                self.assertIsNone(_decision(_run("Monitor", {"command": command}, lane="1")))

    def test_executions_are_still_denied(self):
        watcher = "~/harmonic-forge/tools/gh/watch_lane_posts.py"
        for command in (f"{watcher} --queue-for l1",
                        f"python {watcher} --queue-for l1",
                        f"cd /tmp && python3 -u {watcher} --queue-for l1",
                        f"env FOO=1 python3 {watcher} --queue-for l1",
                        f"timeout 600 python3 {watcher} --queue-for l1 | tee x.log"):
            with self.subTest(command=command):
                self.assertEqual(_decision(_run("Monitor", {"command": command}, lane="1")),
                                 "deny")


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
                   if e.get("matcher") ==
                   "Monitor|CronCreate|Skill|Bash|Write|Edit|MultiEdit|NotebookEdit"]
        self.assertEqual(len(entries), 1)
        commands = [h["command"] for h in entries[0]["hooks"]]
        self.assertIn('f="${HOME}/harmonic-forge/tools/hooks/enforce_belt_arming.py"; '
                      '[ -f "$f" ] && python3 "$f" || true', commands)

    def test_forge_settings_register_the_grant_hook_after_expansion(self):
        settings = json.loads((HERE.parent.parent / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        commands = [h["command"] for e in settings["hooks"]["UserPromptSubmit"]
                    for h in e["hooks"]]
        expand = next(i for i, c in enumerate(commands) if "expand_lane_shorthand.py" in c)
        grant = commands.index('f="${HOME}/harmonic-forge/tools/hooks/grant_loop_override.py"; '
                               '[ -f "$f" ] && python3 "$f" || true')
        self.assertGreater(grant, expand)


def _write_grant_file(arming, session_id, age_seconds=0.0, **extra):
    import time
    path = Path(arming) / f"{session_id}{guard.GRANT_SUFFIX}"
    path.write_text(json.dumps({"created": time.time() - age_seconds, "reason": "",
                                **extra}), encoding="utf-8")
    return path


class AllowLoopGrant(unittest.TestCase):
    """#659 operator ruling: one non-canonical arming per typed ALLOW LOOP."""

    LOOP = {"skill": "loop", "args": "5m x"}
    LOOP_CRON = {"cron": "*/5 * * * *", "prompt": "x", "recurring": True}

    def test_one_loop_and_its_cron_then_a_second_is_denied(self):
        with tempfile.TemporaryDirectory() as arming:
            grant = _write_grant_file(arming, "sess-g")
            skill = _run("Skill", self.LOOP, session_id="sess-g", arming_dir=arming)
            self.assertIsNone(_decision(skill))
            self.assertTrue(grant.exists(), "the Skill call must not consume the grant")
            self.assertIn("ALLOW LOOP", json.loads(skill.stdout).get("systemMessage", ""))
            cron = _run("CronCreate", self.LOOP_CRON, session_id="sess-g", arming_dir=arming)
            self.assertIsNone(_decision(cron))
            self.assertFalse(grant.exists(), "the loop's CronCreate consumes the grant")
            self.assertEqual(_decision(_run("Skill", self.LOOP, session_id="sess-g",
                                            arming_dir=arming)), "deny")
            self.assertEqual(_decision(_run("CronCreate", self.LOOP_CRON, session_id="sess-g",
                                            arming_dir=arming)), "deny")

    def test_a_second_skill_before_the_cron_is_denied(self):
        with tempfile.TemporaryDirectory() as arming:
            _write_grant_file(arming, "sess-g")
            _run("Skill", self.LOOP, session_id="sess-g", arming_dir=arming)
            second = _run("Skill", {"skill": "loop", "args": "1m y"},
                          session_id="sess-g", arming_dir=arming)
            self.assertEqual(_decision(second), "deny")

    def test_after_a_skill_only_that_skills_cron_consumes(self):
        with tempfile.TemporaryDirectory() as arming:
            grant = _write_grant_file(arming, "sess-g")
            _run("Skill", self.LOOP, session_id="sess-g", arming_dir=arming)
            other = dict(self.LOOP_CRON, prompt="something else")
            self.assertEqual(_decision(_run("CronCreate", other, session_id="sess-g",
                                            arming_dir=arming)), "deny")
            self.assertTrue(grant.exists())

    def test_a_direct_cron_consumes_the_grant(self):
        with tempfile.TemporaryDirectory() as arming:
            grant = _write_grant_file(arming, "sess-g")
            cron = {"cron": "0 * * * *", "prompt": "remind me"}
            self.assertIsNone(_decision(_run("CronCreate", cron, session_id="sess-g",
                                             arming_dir=arming)))
            self.assertFalse(grant.exists())
            self.assertEqual(_decision(_run("CronCreate", cron, session_id="sess-g",
                                            arming_dir=arming)), "deny")

    def test_an_expired_grant_is_ignored(self):
        with tempfile.TemporaryDirectory() as arming:
            _write_grant_file(arming, "sess-g", age_seconds=11 * 60)
            self.assertEqual(_decision(_run("Skill", self.LOOP, session_id="sess-g",
                                            arming_dir=arming)), "deny")
            self.assertEqual(_decision(_run("CronCreate", self.LOOP_CRON, session_id="sess-g",
                                            arming_dir=arming)), "deny")

    def test_another_sessions_grant_does_not_apply(self):
        with tempfile.TemporaryDirectory() as arming:
            _write_grant_file(arming, "sess-other")
            self.assertEqual(_decision(_run("Skill", self.LOOP, session_id="sess-g",
                                            arming_dir=arming)), "deny")

    def test_the_grant_never_unlocks_the_belt_monitor(self):
        with tempfile.TemporaryDirectory() as arming:
            grant = _write_grant_file(arming, "sess-g")
            self.assertEqual(_decision(_run("Monitor", {"command": INCIDENT_SWEEP},
                                            session_id="sess-g", arming_dir=arming)), "deny")
            self.assertTrue(grant.exists())

    def test_deny_names_the_operator_override_without_telling_the_agent_to_write_it(self):
        out = json.loads(_run("Skill", self.LOOP).stdout)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("the operator can grant one with a line starting `ALLOW LOOP`", reason)
        self.assertNotIn(".loop_grant", reason)
        self.assertNotIn("belt_arming", reason)
        monitor = json.loads(_run("Monitor", {"command": INCIDENT_SWEEP}).stdout)
        self.assertNotIn("ALLOW LOOP",
                         monitor["hookSpecificOutput"]["permissionDecisionReason"])


class GrantDirectoryIsHookOwned(unittest.TestCase):
    """#659 ruling: agents cannot forge a grant by writing the state file."""

    def _deny(self, tool, tool_input, lane="3"):
        return _decision(_run(tool, tool_input, lane=lane,
                              arming_dir=str(guard.DEFAULT_ARMING_DIR)))

    WRITES = (
        "echo '{}' > ~/.cache/harmonic-forge/belt_arming/s.loop_grant",
        "echo x >> $HOME/.cache/harmonic-forge/belt_arming/s.loop_grant",
        "touch ${HOME}/.cache/harmonic-forge/belt_arming/s.loop_grant",
        "mkdir -p ~/.cache/harmonic-forge/belt_arming && touch ~/.cache/harmonic-forge/belt_arming/s",
        "cp /tmp/g ~/.cache/harmonic-forge/belt_arming/s.loop_grant",
        "mv /tmp/g ~/.cache/harmonic-forge/belt_arming/",
        "printf x | tee ~/.cache/harmonic-forge/belt_arming/s.loop_grant",
        "cd ~/.cache/harmonic-forge && touch belt_arming/s.loop_grant",
        "cd ~/.cache/harmonic-forge/belt_arming && touch s.loop_grant",
        "python3 -c \"open('/home/u/.cache/harmonic-forge/belt_arming/s.loop_grant','w').write('{}')\"",
        "python3 - <<'EOF'\nimport pathlib\npathlib.Path.home().joinpath('.cache/harmonic-forge/belt_arming/s.loop_grant').write_text('{}')\nEOF",
        "rm -f ~/.cache/harmonic-forge/belt_arming/s",
        "find ~/.cache/harmonic-forge/belt_arming -name s -delete",
        "echo $(touch ~/.cache/harmonic-forge/belt_arming/s.loop_grant)",
    )

    def test_shell_writes_are_denied_in_every_session(self):
        for command in self.WRITES:
            for lane in ("3", None):
                with self.subTest(command=command, lane=lane):
                    self.assertEqual(self._deny("Bash", {"command": command}, lane=lane),
                                     "deny")
        self.assertEqual(self._deny("Monitor", {"command": self.WRITES[0]}), "deny")

    def test_file_tools_are_denied(self):
        target = str(guard.DEFAULT_ARMING_DIR / "s.loop_grant")
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"),
                          ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            with self.subTest(tool=tool):
                self.assertEqual(self._deny(tool, {key: target, "content": "{}"}), "deny")

    def test_reads_and_unrelated_commands_are_allowed(self):
        for command in ("ls -la ~/.cache/harmonic-forge/belt_arming",
                        "cat ~/.cache/harmonic-forge/belt_arming/s.loop_grant 2>/dev/null",
                        "find ~/.cache/harmonic-forge/belt_arming -mmin -10",
                        "grep -rn belt_arming tools/hooks | head",
                        "python3 tools/run_tests.py",
                        "echo hi > /tmp/x"):
            with self.subTest(command=command):
                self.assertIsNone(self._deny("Bash", {"command": command}))
        self.assertIsNone(self._deny("Write", {"file_path": "/tmp/x.py", "content": ""}))


if __name__ == "__main__":
    unittest.main()
