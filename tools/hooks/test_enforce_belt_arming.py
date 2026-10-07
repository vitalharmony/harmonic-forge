"""harmonic-forge#659 AC3 -- the belt-arming guard, driven as a real hook process.

Each deny case below is a wrong arm from the 2026-09-14 incident or its
transcripts; each allow case is the canonical call, or something unrelated the
guard must not touch.
"""
import json
import re
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

#: harmonic-forge#917: the hook resolves the session's workspace from the payload's
#: `cwd`, so every payload carries one -- a fixture checkout in workspace `vh`, never
#: this test's own checkout (a CI runner path is registered nowhere).
_FIXTURE = Path(_ARMING_ROOT.name) / "fixture"
_VH = _FIXTURE / "alpha"
_LEASEPAL = _FIXTURE / "beta"
_OUTSIDE = _FIXTURE / "outside"
_MANIFEST = _FIXTURE / "projects.toml"
for _d in (_VH, _LEASEPAL, _OUTSIDE):
    _d.mkdir(parents=True, exist_ok=True)
_MANIFEST.write_text("\n".join(
    f'[[project]]\nname = "{n}"\nprefix = "{p}"\nrepo = "o/{n}"\naccount = "vitalharmony"\n'
    f'path = "{d}"\nonboarded = true\nworkspace = "{w}"\n[project.protocol]\n'
    'worktree_name = "{checkout}-lane{lane}"\nl1_post_task = "l1-post"\n'
    'lane_comment_task = "lane-comment"\ngate_checkout_task = "gate-checkout"\n'
    'lane3_begin_task = "lane3-begin"\nlane3_end_task = "lane3-end"\ngate_task = "check"\nruns_lane3 = true\n'
    for n, p, d, w in (("alpha", "A", _VH, "vh"), ("beta", "B", _LEASEPAL, "leasepal"))),
    encoding="utf-8")


def _run(tool_name, tool_input, lane="3", raw=None, session_id=None, arming_dir=None,
         event=None, tool_response=None, cwd=_VH):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    env["FORGE_PROJECTS_MANIFEST"] = str(_MANIFEST)
    if lane is not None:
        env["LANE"] = lane
    env[guard.ARMING_DIR_ENV] = arming_dir or tempfile.mkdtemp(dir=_ARMING_ROOT.name)
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    if session_id is not None:
        payload["session_id"] = session_id
    if event is not None:
        payload["hook_event_name"] = event
    if tool_response is not None:
        payload["tool_response"] = tool_response
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
        lane2 = belt_plan.canonical_calls("2", "vh")["monitor"]["command"]
        self.assertEqual(_decision(_run("Monitor", {"command": lane2}, lane="3")), "deny")

    def test_off_table_interval_is_denied(self):
        cmd = belt_plan.canonical_calls("3", "vh")["monitor"]["command"].replace("300", "60")
        self.assertEqual(_decision(_run("Monitor", {"command": cmd})), "deny")

    def test_every_deny_quotes_the_exact_calls(self):
        calls = belt_plan.canonical_calls("3", "vh")
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
                calls = belt_plan.canonical_calls(lane, "vh")
                result = _run("Monitor", calls["monitor"], lane=lane)
                self.assertEqual(json.loads(result.stdout), {})

    def test_home_spellings_are_equivalent(self):
        monitor = belt_plan.canonical_calls("3", "vh")["monitor"]
        cmd = monitor["command"]
        home = os.path.expanduser("~")
        for spelling in ("$HOME/", "${HOME}/", home + "/"):
            with self.subTest(spelling=spelling):
                variant = cmd.replace("~/", spelling).replace(" --", "   --", 1)
                # harmonic-forge#680 NC3 compares `timeout_ms` alongside the
                # command, so an otherwise-canonical call must carry it. This
                # test is about SPELLING equivalence; the timeout is held at
                # its canonical value so only the spelling varies.
                self.assertIsNone(_decision(_run("Monitor", {
                    "command": variant, "timeout_ms": monitor["timeout_ms"]})))

    def test_a_non_canonical_timeout_is_denied(self):
        """harmonic-forge#680 NC3. The command alone was compared, so a belt
        armed canonically with a SHORTER lifetime passed while the poller held
        a `--deadline-seconds` derived from the intended one — the original
        defect one layer up: a derived number and a real lifetime that nothing
        checks against each other."""
        monitor = dict(belt_plan.canonical_calls("3", "vh")["monitor"])
        monitor["timeout_ms"] = 300000
        result = _run("Monitor", monitor, lane="3")
        self.assertEqual(_decision(result), "deny",
                         "a non-canonical timeout_ms must deny")
        reason = (json.loads(result.stdout or "{}").get("hookSpecificOutput")
                  or {}).get("permissionDecisionReason", "")
        self.assertIn("timeout_ms", reason,
                      "the denial must name what is actually wrong")

    def test_an_absent_timeout_is_denied(self):
        """Absent means the Monitor takes its own 300000 default — five
        minutes, not thirty — so the poller's deadline would be six times the
        window it actually has."""
        monitor = dict(belt_plan.canonical_calls("3", "vh")["monitor"])
        monitor.pop("timeout_ms")
        self.assertIsNotNone(_decision(_run("Monitor", monitor, lane="3")))

    def test_canonical_loop_is_allowed(self):
        result = _run("Skill", belt_plan.canonical_calls("3", "vh")["loop"])
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
            skill = _run("Skill", belt_plan.canonical_calls("3", "vh")["loop"],
                         session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(skill))
            cron = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(cron))


class DeletedCronClearsTheArmingRecord(unittest.TestCase):
    """harmonic-forge#675: a deleted, failed or resumed suspenders cron must not
    lock the session out of re-arming."""

    def _arm(self, arming, session_id="sess-a", job_id="cron-x"):
        """PreToolUse create, then PostToolUse create carrying `job_id`."""
        created = _run("CronCreate", CANONICAL_CRON, session_id=session_id, arming_dir=arming)
        self.assertIsNone(_decision(created))
        _run("CronCreate", CANONICAL_CRON, session_id=session_id, arming_dir=arming,
             event="PostToolUse", tool_response={"id": job_id, "recurring": True})
        record = json.loads((Path(arming) / session_id).read_text())
        self.assertEqual(record["id"], job_id)
        return record

    def test_ac1_after_crondelete_the_next_canonical_create_is_allowed(self):
        with tempfile.TemporaryDirectory() as arming:
            self._arm(arming)
            _run("CronDelete", {"id": "cron-x"}, session_id="sess-a", arming_dir=arming,
                 event="PostToolUse", tool_response={"id": "cron-x"})
            self.assertFalse((Path(arming) / "sess-a").exists())
            again = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertIsNone(_decision(again))
            self.assertTrue((Path(arming) / "sess-a").exists())

    def test_ac2_a_live_recorded_job_still_denies_a_second_create(self):
        with tempfile.TemporaryDirectory() as arming:
            self._arm(arming)
            second = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            self.assertEqual(_decision(second), "deny")
            reason = json.loads(second.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("already armed", reason)
            self.assertIn("cron-x", reason)

    def test_ac3_deleting_another_cron_leaves_the_record(self):
        with tempfile.TemporaryDirectory() as arming:
            self._arm(arming)
            _run("CronDelete", {"id": "other-y"}, session_id="sess-a", arming_dir=arming,
                 event="PostToolUse", tool_response={"id": "other-y"})
            self.assertEqual(json.loads((Path(arming) / "sess-a").read_text())["id"], "cron-x")
            # A PreToolUse CronDelete is not the successful delete: the record stays.
            _run("CronDelete", {"id": "cron-x"}, session_id="sess-a", arming_dir=arming)
            self.assertEqual(json.loads((Path(arming) / "sess-a").read_text())["id"], "cron-x")
            self.assertEqual(_decision(_run("CronCreate", CANONICAL_CRON, session_id="sess-a",
                                            arming_dir=arming)), "deny")

    def test_ac4_an_id_less_record_denies_with_an_honest_message(self):
        with tempfile.TemporaryDirectory() as arming:
            _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            second = _run("CronCreate", CANONICAL_CRON, session_id="sess-a", arming_dir=arming)
            reason = json.loads(second.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("job id unknown", reason)
            # harmonic-forge#814: the record expires on its own, so the instruction is to WAIT
            # (naming the seconds), never to delete a file.
            self.assertIn("Retry in", reason)
            self.assertRegex(reason, r"Retry in \d+s")
            # ...but only AFTER CronList: an id-less record can also mean the job exists and its
            # id could not be parsed, and a blind retry would stack a duplicate.
            self.assertLess(reason.index("CronList"), reason.index("Retry in"))
            self.assertNotIn(str(Path(arming) / "sess-a"), reason)
            self.assertNotIn("removes", reason)
            self.assertNotIn("remove ", reason)
            self.assertNotIn("the existing job keeps running",
                             guard.__file__ and Path(guard.__file__).read_text(encoding="utf-8"))

    def test_the_retry_wait_is_within_the_stale_window(self):
        """harmonic-forge#814: an id-less record younger than ID_LESS_STALE_SECONDS names the
        seconds LEFT, so waiting that long clears it."""
        with tempfile.TemporaryDirectory() as arming:
            _run("CronCreate", CANONICAL_CRON, session_id="sess-b", arming_dir=arming)
            reason = json.loads(_run("CronCreate", CANONICAL_CRON, session_id="sess-b",
                                     arming_dir=arming).stdout)["hookSpecificOutput"][
                "permissionDecisionReason"]
        found = int(re.search(r"Retry in (\d+)s", reason).group(1))
        self.assertLessEqual(found, guard.ID_LESS_STALE_SECONDS)
        self.assertGreater(found, guard.ID_LESS_STALE_SECONDS - 30)

    def test_the_settings_allow_rule_for_the_canonical_cron_is_present(self):
        """harmonic-forge#814: CronCreate is allowed by rule, not by the nondeterministic
        auto-mode classifier. RESIDUAL, stated here so removing this test is a decision: a static
        allow cannot be scoped to lane sessions, and this hook does not restrict a session with no
        LANE set, so such a session's CronCreate is now allowed without the classifier."""
        settings = json.loads((Path(guard.__file__).resolve().parents[2] / ".claude"
                               / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("CronCreate", settings["permissions"]["allow"])

    def test_ac7_posttooluse_never_decides(self):
        with tempfile.TemporaryDirectory() as arming:
            payloads = [
                ("CronCreate", CANONICAL_CRON, {"id": "cron-x"}),
                ("CronCreate", {"cron": "0 * * * *", "prompt": "anything"}, {"id": "z"}),
                ("CronDelete", {"id": "cron-x"}, {"id": "cron-x"}),
                ("CronDelete", {"id": "missing"}, {"id": "missing"}),
            ]
            for tool, tool_input, response in payloads:
                with self.subTest(tool=tool, tool_input=tool_input):
                    result = _run(tool, tool_input, session_id="sess-a", arming_dir=arming,
                                  event="PostToolUse", tool_response=response)
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn("permissionDecision", result.stdout)

    def test_a_payload_without_an_event_name_is_still_pretooluse(self):
        self.assertEqual(_decision(_run("CronCreate", INCIDENT_CRON)), "deny")


class StaleArmingRecordsDoNotLockTheSessionOut(unittest.TestCase):
    """#675 AC6, against an injected /proc rather than the host's."""

    def setUp(self):
        self.arming = tempfile.TemporaryDirectory()
        self.proc = tempfile.TemporaryDirectory()
        self.addCleanup(self.arming.cleanup)
        self.addCleanup(self.proc.cleanup)
        self.marker = Path(self.arming.name) / "sess-a"
        os.environ[guard.ARMING_DIR_ENV] = self.arming.name
        self.addCleanup(os.environ.pop, guard.ARMING_DIR_ENV, None)

    def _fake_process(self, pid, cmdline):
        entry = Path(self.proc.name) / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in cmdline))
        (entry / "status").write_text("PPid:\t1\n")

    def _write_record(self, **fields):
        self.marker.write_text(json.dumps(dict(guard.LOOP_CRON, **fields)), encoding="utf-8")

    def _decide(self, now):
        return guard.decide({"tool_name": "CronCreate", "tool_input": CANONICAL_CRON,
                             "session_id": "sess-a"}, "3", now=now, proc_root=self.proc.name)

    def test_an_id_less_record_past_the_window_is_stale(self):
        now = 1_000_000.0
        self._write_record(created=now - guard.ID_LESS_STALE_SECONDS - 1, owner_pid=None)
        self.assertIsNone(self._decide(now))
        self.assertEqual(json.loads(self.marker.read_text())["created"], now)

    def test_an_id_less_record_inside_the_window_still_denies(self):
        now = 1_000_000.0
        self._write_record(created=now - 5, owner_pid=None)
        self.assertIn("job id unknown", self._decide(now) or "")

    def test_a_record_whose_owner_process_is_gone_is_stale(self):
        now = 1_000_000.0
        self._write_record(created=now, owner_pid=4242, id="cron-x")
        self.assertIsNone(self._decide(now))

    def test_a_record_whose_owner_is_no_longer_claude_is_stale(self):
        now = 1_000_000.0
        self._fake_process(4242, ["node", "server.js"])
        self._write_record(created=now, owner_pid=4242, id="cron-x")
        self.assertIsNone(self._decide(now))

    def test_a_live_claude_owner_with_an_id_denies(self):
        now = 1_000_000.0
        self._fake_process(4242, ["claude", "--permission-mode", "auto"])
        self._write_record(created=now, owner_pid=4242, id="cron-x")
        self.assertIn("cron-x", self._decide(now) or "")

    def test_a_pre_675_record_is_stale_only_once_the_window_passes(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(guard.LOOP_CRON), encoding="utf-8")
        mtime = self.marker.stat().st_mtime
        self.assertIn("job id unknown", self._decide(mtime + 5) or "")
        self.assertIsNone(self._decide(mtime + guard.ID_LESS_STALE_SECONDS + 1))

    def test_the_owner_pid_walk_finds_the_nearest_claude_ancestor(self):
        self._fake_process(11, ["claude", "--permission-mode", "auto"])
        entry = Path(self.proc.name) / "12"
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"python3\0hook.py")
        (entry / "status").write_text("PPid:\t11\n")
        self.assertEqual(guard.owner_claude_pid(self.proc.name, pid=12), 11)
        self.assertIsNone(guard.owner_claude_pid(self.proc.name, pid=99))


class PostToolUseFailsOpen(unittest.TestCase):
    """#675 AC5: an exception in the new branches allows, with a systemMessage."""

    def test_a_raising_record_hook_still_allows(self):
        payload = json.dumps({"tool_name": "CronDelete", "tool_input": {"id": "x"},
                              "session_id": "sess-a", "hook_event_name": "PostToolUse"})
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(HERE)!r}); "
             "import enforce_belt_arming as g; "
             "g.record_post_tool_use = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')); "
             "sys.exit(g.main())"],
            input=payload, env={**os.environ, "LANE": "3"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("permissionDecision", result.stdout)
        self.assertIn("guard did not run", result.stdout)


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


class WrappedWatcherIsDenied(unittest.TestCase):
    """harmonic-forge#922: a watcher started inside `bash -c` / `sh -c` is still a belt Monitor.

    `strip_invocation_prefix` unwrapped the shell but split the inner string as one command, so
    a leading `cd X;` made `cd` the program and the whole Monitor passed every check here.
    """

    WATCHER = "~/harmonic-forge/tools/gh/watch_lane_posts.py"

    def _monitor(self, command, lane="1"):
        return _run("Monitor", {"command": command, "description": "belt",
                                "timeout_ms": belt_plan.MONITOR_TIMEOUT_MS}, lane=lane)

    def test_wrapped_watchers_are_denied_naming_the_wrapper(self):
        w = self.WATCHER
        cases = {
            "bash": (f"bash -c 'python3 {w} --queue-for l1'",
                     f"bash -c 'cd ~/Harmonic_Projects/LeasePAL-App-Prototype; python3 {w} --workspace leasepal'",
                     f"bash -lc 'python3 {w} --queue-for l1'",
                     f"bash -c \"cd /tmp && python3 {w} --queue-for l1\"",
                     f"timeout 600 bash -c 'cd /tmp; python3 {w} --queue-for l1'",
                     f"bash -c 'sh -c \"cd /y; python3 {w} --queue-for l1\"'",
                     f"bash -c 'cd /x && exec python3 {w} --queue-for l1'",
                     f"bash -c 'exec {w} --queue-for l1'"),
            "sh": (f"sh -c 'cd /tmp; python3 {w} --queue-for l1'",),
        }
        for shell, commands in cases.items():
            for command in commands:
                with self.subTest(command=command):
                    result = self._monitor(command)
                    self.assertEqual(_decision(result), "deny")
                    self.assertIn("-c`", result.stdout)
                    self.assertIn("never the canonical belt command", result.stdout)

    def test_a_wrapper_is_denied_even_around_the_canonical_command(self):
        canonical = belt_plan.canonical_calls("1", "vh")["monitor"]["command"]
        for command in (f"bash -c '{canonical}'", f"bash -c 'cd ~/x; {canonical}'"):
            with self.subTest(command=command):
                self.assertEqual(_decision(self._monitor(command)), "deny")

    def test_the_canonical_command_and_unrelated_wrappers_are_still_allowed(self):
        canonical = belt_plan.canonical_calls("1", "vh")["monitor"]["command"]
        self.assertIsNone(_decision(self._monitor(canonical)))
        for command in ("bash -c 'tail -f logs/backend.log'",
                        "sh -c 'cd /tmp; tail -F x.log | grep watch_lane_posts.py'",
                        "bash -c 'pgrep -af watch_lane_posts.py'"):
            with self.subTest(command=command):
                self.assertIsNone(_decision(self._monitor(command)))


class FailsOpen(unittest.TestCase):
    def test_malformed_payload_allows_with_exit_0(self):
        result = _run(None, None, raw="not json")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(_decision(result))
        self.assertIn("guard did not run", result.stdout)

    def test_internal_error_denies_every_arming_call(self):
        """harmonic-forge#917 sticky-wicket: a belt plan that cannot load fails every
        arming branch closed (Monitor, /loop, CronCreate), not main()'s fail-open."""
        original = guard._load_belt_plan
        try:
            guard._load_belt_plan = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            for payload in ({"tool_name": "Monitor", "tool_input": {"command": INCIDENT_SWEEP}},
                            {"tool_name": "Skill", "tool_input": {"skill": "loop", "args": "x"}},
                            {"tool_name": "CronCreate", "tool_input": dict(guard.LOOP_CRON)}):
                with self.subTest(tool=payload["tool_name"]):
                    self.assertIn("cannot load", guard.decide(payload, "3"))
        finally:
            guard._load_belt_plan = original


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

    def test_forge_settings_register_the_hook_on_posttooluse_crons(self):
        """#675: PostToolUse CronCreate|CronDelete maintains the arming record."""
        settings = json.loads((HERE.parent.parent / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        entries = [e for e in settings["hooks"]["PostToolUse"]
                   if e.get("matcher") == "CronCreate|CronDelete"]
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


class ScopedToTheSessionsWorkspace(unittest.TestCase):
    """harmonic-forge#917 AC3."""

    def _monitor(self, command, cwd=_VH, lane="2"):
        return _run("Monitor", {"command": command, "description": "belt",
                                "timeout_ms": belt_plan.MONITOR_TIMEOUT_MS},
                    lane=lane, cwd=cwd)

    def test_the_session_checkouts_scoped_command_is_allowed(self):
        for cwd, ws in ((_VH, "vh"), (_LEASEPAL, "leasepal")):
            with self.subTest(ws=ws):
                cmd = belt_plan.canonical_calls("2", ws)["monitor"]["command"]
                self.assertIsNone(_decision(self._monitor(cmd, cwd)))

    def test_another_workspaces_command_is_denied(self):
        cmd = belt_plan.canonical_calls("2", "leasepal")["monitor"]["command"]
        self.assertEqual(_decision(self._monitor(cmd, _VH)), "deny")

    def test_the_unscoped_command_is_denied(self):
        cmd = belt_plan.canonical_calls("2", "vh")["monitor"]["command"]
        unscoped = cmd.replace(" --workspace vh", "")
        self.assertNotEqual(cmd, unscoped)
        self.assertEqual(_decision(self._monitor(unscoped, _VH)), "deny")

    def test_an_unresolved_session_cannot_arm_a_belt(self):
        """Fail closed: an unregistered cwd, or none, never arms some default workspace."""
        cmd = belt_plan.canonical_calls("2", "vh")["monitor"]["command"]
        for cwd in (_OUTSIDE, None):
            with self.subTest(cwd=cwd):
                result = self._monitor(cmd, cwd)
                self.assertEqual(_decision(result), "deny")
                self.assertIn("cannot be resolved", result.stdout)

    def test_an_unresolved_session_may_still_run_the_canonical_loop(self):
        """`/loop` is the same in every workspace; resolution never gates it."""
        result = _run("Skill", belt_plan.loop_call("2"), lane="2", cwd=_OUTSIDE)
        self.assertIsNone(_decision(result))


class FailsClosedOnAnUnloadableManifest(unittest.TestCase):
    """harmonic-forge#917 preclose + sticky-wicket: a manifest the loader rejects must
    not let a belt, /loop or CronCreate arm; the plan-free ALLOW LOOP grant still holds."""

    def _hook(self, tool_name, tool_input, session_id=None, arming=None):
        bad = Path(_ARMING_ROOT.name) / "bad-projects.toml"
        bad.write_text(_MANIFEST.read_text(encoding="utf-8")
                       .replace('workspace = "vh"\n', "", 1), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        env.update({"LANE": "2", "FORGE_PROJECTS_MANIFEST": str(bad),
                    guard.ARMING_DIR_ENV: arming or tempfile.mkdtemp(dir=_ARMING_ROOT.name)})
        payload = {"tool_name": tool_name, "cwd": str(_VH), "tool_input": tool_input}
        if session_id:
            payload["session_id"] = session_id
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              env=env, capture_output=True, text=True)

    def test_a_workspace_less_onboarded_row_denies_the_belt(self):
        cmd = belt_plan.canonical_calls("2", "vh")["monitor"]["command"]
        result = self._hook("Monitor", {"command": cmd, "description": "belt",
                                        "timeout_ms": belt_plan.MONITOR_TIMEOUT_MS})
        self.assertEqual(_decision(result), "deny", result.stdout)
        self.assertIn("cannot load", result.stdout)

    def test_the_loop_is_denied(self):
        result = self._hook("Skill", belt_plan.loop_call("2"))
        self.assertEqual(_decision(result), "deny", result.stdout)
        self.assertIn("cannot load", result.stdout)

    def test_the_cron_is_denied(self):
        result = self._hook("CronCreate", dict(guard.LOOP_CRON))
        self.assertEqual(_decision(result), "deny", result.stdout)
        self.assertIn("cannot load", result.stdout)

    def test_an_allow_loop_grant_still_admits_the_loop(self):
        with tempfile.TemporaryDirectory() as arming:
            _write_grant_file(arming, "sess-b")
            result = self._hook("Skill", {"skill": "loop", "args": "5m x"},
                                session_id="sess-b", arming=arming)
        self.assertIsNone(_decision(result), result.stdout)

    def test_an_unrelated_monitor_is_still_allowed(self):
        result = self._hook("Monitor", {"command": "tail -f /tmp/x.log",
                                        "description": "log", "timeout_ms": 60000})
        self.assertIsNone(_decision(result), result.stdout)
