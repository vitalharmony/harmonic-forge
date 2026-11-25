#!/usr/bin/env python3
"""Tests for the Forge lane hook set and its builder (harmonic-forge#1307).

Hermetic: the set is validated against THIS checkout (`forge_root`), or under a
fixture HOME whose `harmonic-forge/tools` links to it, never against the
operator's `~/harmonic-forge`. Every CLI run pins `XDG_CONFIG_HOME` to the
fixture HOME's `.config`, so the overlay declaration it reads is the fixture's,
never the operator's `instance.toml` (harmonic-forge#1525). The launcher half (the set reaches every Claude
lane as one `--settings`) is `test_lane_launchers.py`'s LaneHookSet class.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import lane_hookset as hs  # noqa: E402

SET_FILE = HERE / "lane_hooks.json"
OVERLAY_FILE = HERE / "lane_hooks.operator.json"
F61AC37 = HERE / "testdata" / "lane_hooks_f61ac37_tuples.json"
# The six operator hooks that left the core set for the overlay (harmonic-forge#1525).
SIX = {"belt_wakeup.py", "enforce_belt_arming.py", "deny_retired_belt_sweep.py",
       "grant_loop_override.py", "gate_feedback_memory_write.py", "memory_fold_notice.py"}
SNAPSHOT = HERE / "testdata" / "hrse2_hooks_snapshot.json"
POLICY = HERE / "policies" / "claude-lane3.json"

# The plan's class A: each returns before deciding when LANE is unset.
CLASS_A = {
    "block_lane2_status_claims.py", "deny_lane3_ae_self_post.py", "require_ci_plan.py",
    "grant_ci_plan_override.py", "grant_loop_override.py", "tasklist_hold.py",
    "record_tasklist_post.py", "tier_model_trigger_check.py", "tier_downshift_reminder.py",
    "memory_fold_notice.py", "lane_launch_notice.py",
}


def _set() -> dict:
    return json.loads(SET_FILE.read_text(encoding="utf-8"))


def _effective() -> dict:
    """The operator's lane set: the core set plus the operator overlay."""
    return hs.load_effective(SET_FILE, "operator", forge_root=ROOT)


def _tuple_key(t: list) -> tuple:
    return t[0], t[1] or "", t[2], t[3] or 0


def _tuples(data: dict) -> list:
    """The (event, matcher, command, timeout) multiset, sorted."""
    return sorted(([e, m, h["command"], h.get("timeout")] for e, m, h in hs.handlers(data["hooks"])),
                  key=_tuple_key)


def _named(data: dict) -> set[str]:
    return {s for _e, _m, h in hs.handlers(data["hooks"]) for s in hs.SCRIPT_RE.findall(h["command"])}


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name: str, data) -> Path:
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def fixture_home(self) -> Path:
        home = self.tmp / "home"
        if not (home / "harmonic-forge" / "tools").exists():
            (home / "harmonic-forge").mkdir(parents=True)
            (home / "harmonic-forge" / "tools").symlink_to(ROOT / "tools")
        return home

    def build(self, data, **kw) -> dict:
        path = self.write("set.json", data)
        kw.setdefault("overlay", "none")
        with mock.patch.dict(os.environ, {"HOME": str(self.fixture_home())}):
            return hs.build(path, **kw)

    def declare(self, text: str | None) -> Path:
        """Write the fixture HOME's instance.toml (None: no file) and return
        its path. The CLI env from `cli_env` reads exactly this file."""
        path = self.fixture_home() / ".config" / "harmonic-forge" / "instance.toml"
        if path.exists():
            path.unlink()
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return path

    def cli_env(self) -> dict:
        home = self.fixture_home()
        return dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", "-E", str(HERE / "lane_hookset.py"), *args],
                              capture_output=True, text=True, env=self.cli_env(), timeout=60)


class Build(_Tmp):

    def test_the_committed_set_validates_against_this_checkout(self):
        hs.validate(_set(), forge_root=ROOT)

    def test_build_emits_only_the_disable_flag_and_hooks(self):
        value = self.build(_set())
        self.assertEqual(list(value), ["disableAllHooks", "hooks"])
        self.assertIs(value["disableAllHooks"], False)
        self.assertEqual(value["hooks"], _set()["hooks"])

    def test_a_missing_set_refuses(self):
        with self.assertRaisesRegex(hs.SetError, "missing"):
            hs.build(self.tmp / "absent.json", overlay="none")

    def test_an_empty_set_refuses(self):
        for data in ("", "{}", "[]"):
            with self.subTest(data=data), self.assertRaises(hs.SetError):
                self.build(data)
        empty = {**_set(), "hooks": {}, "lane_only": []}
        with self.assertRaisesRegex(hs.SetError, "empty"):
            self.build(empty)

    def test_a_set_with_no_handler_refuses(self):
        data = {**_set(), "lane_only": [], "hooks": {"Stop": [{"hooks": []}]}}
        with self.assertRaisesRegex(hs.SetError, "no handler"):
            self.build(data)

    def test_each_schema_violation_refuses(self):
        def event_not_a_list(d): d["hooks"]["Stop"] = "oops"
        def block_without_hooks(d): d["hooks"]["Stop"][0].pop("hooks")
        def handler_without_command(d): d["hooks"]["Stop"][0]["hooks"][0].pop("command")
        def type_not_command(d): d["hooks"]["Stop"][0]["hooks"][0]["type"] = "prompt"
        def non_integer_timeout(d): d["hooks"]["Stop"][0]["hooks"][0]["timeout"] = "10"
        def bool_timeout(d): d["hooks"]["Stop"][0]["hooks"][0]["timeout"] = True
        def zero_timeout(d): d["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 0
        def uncompilable_matcher(d): d["hooks"]["PreToolUse"][0]["matcher"] = "Bash("
        def non_string_matcher(d): d["hooks"]["PreToolUse"][0]["matcher"] = 3
        def unknown_event(d): d["hooks"]["Stopp"] = d["hooks"].pop("Stop")
        def unknown_top_level_key(d): d["permissions"] = {"allow": ["Bash(*)"]}
        def unknown_handler_key(d): d["hooks"]["Stop"][0]["hooks"][0]["async"] = True
        def unknown_block_key(d): d["hooks"]["Stop"][0]["extra"] = 1
        def schema_two(d): d["schema"] = 2
        def lane_only_stale(d): d["lane_only"].append("no_such_hook.py")
        # Each violation is pinned to the check that names it, so a check that
        # only a later one happens to back up still shows as removed.
        for mutate, reason in ((event_not_a_list, "must be a non-empty list"),
                               (block_without_hooks, "a block must be"),
                               (handler_without_command, "has no command"),
                               (type_not_command, "type is not"),
                               (non_integer_timeout, "not a positive integer"),
                               (bool_timeout, "not a positive integer"),
                               (zero_timeout, "not a positive integer"),
                               (uncompilable_matcher, "does not compile"),
                               (non_string_matcher, "matcher is not a string"),
                               (unknown_event, "unknown hook event"),
                               (unknown_top_level_key, "unknown top-level"),
                               (unknown_handler_key, "a handler must be"),
                               (unknown_block_key, "a block must be"),
                               (schema_two, "schema"),
                               (lane_only_stale, "lane_only")):
            with self.subTest(mutate=mutate.__name__):
                data = _set()
                mutate(data)
                with self.assertRaisesRegex(hs.SetError, reason):
                    self.build(data)

    def test_disable_all_hooks_must_be_declared_false(self):
        for value in ("absent", True, 0, None):
            with self.subTest(value=value):
                data = _set()
                if value == "absent":
                    del data["disableAllHooks"]
                else:
                    data["disableAllHooks"] = value
                with self.assertRaisesRegex(hs.SetError, "disableAllHooks"):
                    self.build(data)

    def test_an_unresolvable_script_refuses(self):
        data = _set()
        data["hooks"]["Stop"][0]["hooks"][0]["command"] = (
            'f="${HOME}/harmonic-forge/tools/hooks/no_such_hook.py"; [ -f "$f" ] && python3 "$f" || true')
        with self.assertRaisesRegex(hs.SetError, "do not exist"):
            self.build(data)
        with self.assertRaisesRegex(hs.SetError, "do not exist"):
            hs.validate(data, forge_root=ROOT)

    def test_the_set_refuses_under_a_home_with_no_checkout(self):
        """Why the launcher tests link the fixture HOME's harmonic-forge/tools."""
        path = self.write("set.json", _set())
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "empty-home")}):
            with self.assertRaisesRegex(hs.SetError, "do not exist"):
                hs.build(path, overlay="none")

    def test_build_refuses_an_unmatched_command(self):
        """Change 4: a command the platform-path pattern does not match is never
        existence-checked, and a guarded form then fails open at runtime."""
        for command in ('python3 "$HOME/harmonic-forge/tools/hooks/batch_gate.py"',
                        'python3 "${HOME}/harmonic_forge/tools/hooks/batch_gate.py"',
                        'python3 "${CLAUDE_PROJECT_DIR}/tools/hooks/batch_gate.py"',
                        "echo hi"):
            with self.subTest(command=command):
                data = _set()
                data["hooks"]["PreToolUse"][0]["hooks"][3]["command"] = command
                with self.assertRaisesRegex(hs.SetError, "platform path"):
                    self.build(data)

    def test_no_set_command_uses_the_project_dir(self):
        overlay = json.loads(OVERLAY_FILE.read_text(encoding="utf-8"))
        for data in (_set(), overlay):
            for _e, _m, handler in hs.handlers(data["hooks"]):
                self.assertNotIn("CLAUDE_PROJECT_DIR", handler["command"])
                self.assertIn("${HOME}/harmonic-forge/tools/hooks/", handler["command"])

    def test_the_core_set_holds_47_entries(self):
        # +1: TaskCreate tasklist_hold (harmonic-forge#1477); +1: UserPromptSubmit
        # surface_dry_run_failures (harmonic-forge#1544); -9: the operator
        # overlay (harmonic-forge#1525)
        self.assertEqual(sum(1 for _ in hs.handlers(_set()["hooks"])), 47)

    def test_the_effective_operator_set_holds_56_entries(self):
        self.assertEqual(sum(1 for _ in hs.handlers(_effective()["hooks"])), 56)


class Policy(_Tmp):

    def install(self, content: bytes, digest: str | None = None) -> tuple[Path, Path]:
        policy = self.tmp / "claude-lane3.json"
        policy.write_bytes(content)
        manifest = self.write("manifest.json", {
            "policy_sha256": digest or hashlib.sha256(content).hexdigest()})
        return policy, manifest

    def test_the_policy_and_the_set_merge_into_one_object(self):
        policy, manifest = self.install(POLICY.read_bytes())
        value = self.build(_set(), policy=policy, manifest=manifest)
        self.assertEqual(value["permissions"], json.loads(POLICY.read_text())["permissions"])
        self.assertEqual(value["hooks"], _set()["hooks"])
        self.assertIs(value["disableAllHooks"], False)

    def test_a_key_in_both_sources_refuses(self):
        for key in ("hooks", "disableAllHooks"):
            with self.subTest(key=key):
                policy, manifest = self.install(json.dumps({key: {}, "permissions": {}}).encode())
                with self.assertRaisesRegex(hs.SetError, "only the set may declare"):
                    self.build(_set(), policy=policy, manifest=manifest)
        data = _set()
        data["permissions"] = {"allow": []}
        policy, manifest = self.install(POLICY.read_bytes())
        with self.assertRaisesRegex(hs.SetError, "unknown top-level"):
            self.build(data, policy=policy, manifest=manifest)

    def test_a_policy_not_matching_its_manifest_refuses(self):
        policy, manifest = self.install(POLICY.read_bytes(), digest="0" * 64)
        with self.assertRaisesRegex(hs.SetError, "policy_sha256"):
            self.build(_set(), policy=policy, manifest=manifest)

    def test_a_policy_without_its_manifest_refuses(self):
        policy, _manifest = self.install(POLICY.read_bytes())
        with self.assertRaisesRegex(hs.SetError, "manifest"):
            self.build(_set(), policy=policy)

    def test_the_cli_prints_nothing_on_a_refusal(self):
        # The declaration is read before the set, so an undeclared fixture
        # refuses first (harmonic-forge#1525).
        self.declare(None)
        proc = self.cli("build", "--set", str(self.tmp / "absent.json"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("not declared", proc.stderr)


class Snapshot(unittest.TestCase):
    """AC4: no deny hook lost in the move."""

    def setUp(self):
        self.snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    def test_the_snapshot_names_its_source(self):
        self.assertEqual(self.snapshot["source"], {
            "repo": "vitalharmony/hrse",
            "commit": "82f28bf949c21de5bd4633c4c1d05a9c9387a95c",
            "path": ".claude/settings.json",
            "blob": "aaad559c5bc55725c3dc25a86f6bda683c00a321"})
        self.assertEqual(len(self.snapshot["entries"]), 61)
        self.assertEqual(len(self.snapshot["project_specific"]), 10)

    def test_every_snapshot_entry_is_in_the_set_or_named_project_specific(self):
        self.assertEqual(hs.snapshot_gaps(_effective(), self.snapshot), [])

    def _mutated(self, script: str, mutate) -> list[str]:
        data = _effective()
        for _e, _m, handler in hs.handlers(data["hooks"]):
            if script in handler["command"]:
                break
        for event, blocks in data["hooks"].items():
            for block in blocks:
                for handler in block["hooks"]:
                    if script in handler["command"]:
                        mutate(block, handler)
                        return hs.snapshot_gaps(data, self.snapshot)
        raise AssertionError(script)

    def test_a_narrowed_matcher_fails_the_snapshot(self):
        gaps = self._mutated("enforce_belt_arming.py",
                             lambda b, h: b.__setitem__("matcher", "Monitor|CronCreate"))
        self.assertTrue(gaps)

    def test_a_shortened_timeout_fails_the_snapshot(self):
        gaps = self._mutated("block_data_migration_close.py", lambda b, h: h.__setitem__("timeout", 5))
        self.assertTrue(gaps)

    def test_a_longer_timeout_does_not_fail_the_snapshot(self):
        self.assertEqual(self._mutated("block_data_migration_close.py",
                                       lambda b, h: h.__setitem__("timeout", 60)), [])

    def test_a_changed_command_fails_the_snapshot(self):
        gaps = self._mutated("deny_cross_session_contact.py",
                             lambda b, h: h.__setitem__("command", h["command"] + " --x"))
        self.assertTrue(gaps)

    def test_a_dropped_deny_hook_fails_the_snapshot(self):
        data = _effective()
        data["hooks"]["PreToolUse"] = [b for b in data["hooks"]["PreToolUse"]
                                       if b.get("matcher") != "SendMessage"]
        self.assertTrue(any("deny_cross_session_contact.py" in g
                            for g in hs.snapshot_gaps(data, self.snapshot)))

    def test_project_specific_names_only_snapshot_entries(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["project_specific"].append(
            {"event": "Stop", "matcher": None, "command": "python3 gone.py", "reason": "x"})
        self.assertEqual(len(hs.snapshot_gaps(_effective(), snapshot)), 1)

    def test_freeze_reproduces_entries_from_a_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                            capture_output=True, text=True).stdout
            git("init", "-q", "-b", "main")
            (repo / ".claude").mkdir()
            (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "python3 a.py", "timeout": 5}]}]}}))
            git("add", "-A")
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
            frozen = hs.freeze(repo, "HEAD", [{"event": "Stop", "reason": "r"}])
            self.assertEqual(frozen["entries"], [
                {"event": "Stop", "matcher": None, "command": "python3 a.py", "timeout": 5}])
            self.assertEqual(frozen["source"]["commit"], git("rev-parse", "HEAD").strip())
            self.assertEqual(frozen["project_specific"], [{"event": "Stop", "reason": "r"}])


class LaneOnly(_Tmp):

    def test_lane_only_names_only_set_scripts(self):
        """Every `lane_only` name is a set script, and it is exactly the plan's
        class A: no class B script (one that acts without LANE) is in it."""
        data = _effective()
        self.assertEqual(set(data["lane_only"]), CLASS_A)
        named = {s for _e, _m, h in hs.handlers(data["hooks"])
                 for s in hs.SCRIPT_RE.findall(h["command"])}
        self.assertLessEqual(set(data["lane_only"]), named)

    # What a class A script must never touch with LANE unset: the fixture HOME
    # (state, task files, grants), a subprocess (gh), or the network.
    _PROBE = r'''
import json, os, runpy, sys
home = os.environ["HOME"]
hits = []
def audit(event, args):
    if event in ("open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename"):
        path = args[0] if args else None
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).startswith(home):
            hits.append(f"{event} {os.fsdecode(path)}")
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
                   "socket.connect"):
        hits.append(event)
sys.addaudithook(audit)
script = sys.argv[1]
sys.argv = [script]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    code = exc.code or 0
sys.stdout.flush()
sys.stderr.write("\nPROBE " + json.dumps({"code": code, "hits": hits}) + "\n")
'''

    # Inputs that reach each lane-only hook's gated branch (pass 1 survivor 3):
    # with its LANE gate removed, every one of the 11 then prints, exits
    # non-zero, or touches the fixture HOME, a subprocess or the network.
    _SESSION = "sess-1307"
    _PROMPT = ("H1307 harmonic-forge#1307\n"
               "ALLOW EDIT probe\n"
               "ALLOW LOOP probe\n"
               "/model opus")
    _COMMAND = ("gh issue comment 1307 --repo vitalharmony/harmonic-forge --body 'L2D H1307'"
                " && gh issue comment 1307 --repo vitalharmony/harmonic-forge --body '## AE H1307'"
                " && gh issue close 1307 --repo vitalharmony/harmonic-forge"
                " && mise run check && git commit -m x")
    # What lane_launch_notice.py and tier_model_trigger_check.py read: a lane
    # launch exports them, and with them set only the LANE gate keeps either quiet.
    _LANE_ENV = {"LANE_REFRESH_STATUS": "current", "LANE_REFRESH_FROM": "a" * 40,
                 "LANE_REFRESH_TO": "b" * 40, "LANE_REFRESH_DETAIL": "probe",
                 "LANE_REFRESH_ENV": "relinked", "LANE_MODEL": "opus"}

    def _seed(self, home: Path) -> Path:
        """Fixture state each gated branch reads: an open task entry, an owed
        marker and a pending removal, and a transcript on a high model whose
        last reply asks a question. Returns the transcript path."""
        tasks = home / ".claude" / "tasks" / self._SESSION
        tasks.mkdir(parents=True)
        (tasks / "1.json").write_text(json.dumps(
            {"id": "1", "subject": "H1307 — x", "status": "in_progress"}))
        owed = home / ".cache" / "harmonic-forge" / "tasklist_owed" / self._SESSION
        (owed / ".pending").mkdir(parents=True)
        (owed / "H1307.json").write_text(json.dumps({"key": "H1307", "kind": "post", "ts": 1}))
        (owed / ".pending" / "1").write_text("H1307")
        transcript = home / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "Shall I go on?"}]}}) + "\n")
        return transcript

    def _payload(self, event: str, matcher: str | None, transcript: Path) -> dict:
        # TaskUpdate where a matcher offers it: a TaskCreate carrying a
        # completed status returns before tasklist_hold's PostToolUse branch.
        tools = (matcher or "Bash").split("|")
        tool = "TaskUpdate" if "TaskUpdate" in tools else tools[0]
        return {
            "hook_event_name": event, "session_id": self._SESSION, "cwd": str(self.tmp),
            "transcript_path": str(transcript),
            "tool_name": tool, "source": "startup", "stop_hook_active": False,
            "prompt": self._PROMPT,
            "tool_input": {"command": self._COMMAND,
                           "file_path": str(self.tmp / "x.py"), "content": "x",
                           "taskId": "1", "subject": "H1307 — x", "status": "completed"},
            "tool_response": {"stdout": "https://github.com/vitalharmony/harmonic-forge/issues/1307#issuecomment-1"},
        }

    def test_every_lane_only_hook_is_silent_without_lane(self):
        """Each `lane_only` script, on each event it is wired for, with LANE
        unset: exit 0, nothing on stdout, and nothing touched -- no read of the
        fixture HOME, no subprocess, no network. This is what makes removing it
        from a project's settings safe for a non-lane session."""
        home = self.fixture_home()
        transcript = self._seed(home)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LANE", "CLAUDE_", "GH_", "XDG_"))}
        env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                   XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".state"),
                   **self._LANE_ENV)
        seen = set()
        for event, matcher, handler in hs.handlers(_effective()["hooks"]):
            for script in hs.SCRIPT_RE.findall(handler["command"]):
                if script not in CLASS_A or (script, event, matcher) in seen:
                    continue
                seen.add((script, event, matcher))
                with self.subTest(script=script, event=event, matcher=matcher):
                    proc = subprocess.run(
                        [sys.executable, "-c", self._PROBE, str(ROOT / "tools" / "hooks" / script)],
                        input=json.dumps(self._payload(event, matcher, transcript)), env=env,
                        cwd=self.tmp, capture_output=True, text=True, timeout=60)
                    report = json.loads(proc.stderr.rsplit("PROBE ", 1)[1])
                    self.assertEqual(report["code"], 0, proc.stderr[-500:])
                    # An empty object is the hook protocol's explicit no-op.
                    self.assertIn(proc.stdout.strip(), ("", "{}"))
                    self.assertEqual(report["hits"], [])
        self.assertEqual({s for s, _e, _m in seen}, CLASS_A)


def _declare_operator(home: Path) -> None:
    config = home / ".config" / "harmonic-forge"
    config.mkdir(parents=True, exist_ok=True)
    (config / "instance.toml").write_text('lane_hook_overlay = "operator"\n', encoding="utf-8")


class Doubled(unittest.TestCase):

    def _entry(self, script: str) -> tuple[str, str | None, dict]:
        for event, matcher, handler in hs.handlers(_set()["hooks"]):
            if script in handler["command"]:
                return event, matcher, dict(handler)
        raise AssertionError(script)

    def _settings(self, event, matcher, handler) -> dict:
        block = {"hooks": [handler]}
        if matcher is not None:
            block["matcher"] = matcher
        return {"hooks": {event: [block]}}

    def test_forge_settings_double_no_set_script(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        doubles, _removable = hs.doubled(_effective(), {"forge": settings})
        self.assertEqual(doubles, [])

    def test_doubled_reports_a_different_string_and_a_different_timeout(self):
        event, matcher, handler = self._entry("block_blind_repeat.py")
        plain = dict(handler, command='python3 "${HOME}/harmonic-forge/tools/hooks/block_blind_repeat.py"')
        doubles, _ = hs.doubled(_set(), {"p": self._settings(event, matcher, plain)})
        self.assertEqual(len(doubles), 1)
        self.assertIn("runs twice", doubles[0])
        slow = dict(handler, timeout=60)
        doubles, _ = hs.doubled(_set(), {"p": self._settings(event, matcher, slow)})
        self.assertIn("timeout 60 differs", doubles[0])

    def test_doubled_reports_a_user_level_double(self):
        """Change 6: ~/.claude/settings.json wires two set scripts by absolute
        path, so both run twice at every lane."""
        user = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 /home/u/harmonic-forge/tools/hooks/batch_gate.py"},
            {"type": "command", "command": "python3 /home/u/harmonic-forge/tools/hooks/block_irreversible_ops.py"},
            {"type": "command", "command": "python3 /home/u/harmonic-forge/tools/gh/block_closing_keywords.py"},
        ]}]}}
        doubles, _ = hs.doubled(_set(), {"user": user})
        self.assertEqual(len(doubles), 2)
        self.assertTrue(any("batch_gate.py" in d for d in doubles))
        self.assertTrue(any("block_irreversible_ops.py" in d for d in doubles))

    def test_the_cli_reads_the_user_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "settings.json"
            user.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 /x/harmonic-forge/tools/hooks/batch_gate.py"}]}]}}))
            env = dict(os.environ, HOME=str(Path(tmp) / "home"),
                       XDG_CONFIG_HOME=str(Path(tmp) / "home" / ".config"))
            (Path(tmp) / "home" / "harmonic-forge").mkdir(parents=True)
            _declare_operator(Path(tmp) / "home")
            (Path(tmp) / "home" / "harmonic-forge" / "tools").symlink_to(ROOT / "tools")
            proc = subprocess.run([sys.executable, "-I", str(HERE / "lane_hookset.py"), "doubled",
                                   "--user-settings", str(user)],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("DOUBLED", proc.stdout)
            self.assertIn("batch_gate.py runs twice", proc.stdout)

    def test_the_cli_reads_home_user_settings_by_default(self):
        """Pass 1 survivor 4: with no --user-settings, `doubled` reads
        `~/.claude/settings.json` -- here a temporary HOME's, never the real one."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".claude").mkdir(parents=True)
            (home / "harmonic-forge").mkdir()
            (home / "harmonic-forge" / "tools").symlink_to(ROOT / "tools")
            user = home / ".claude" / "settings.json"
            user.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 /x/harmonic-forge/tools/hooks/batch_gate.py"}]}]}}))
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
            _declare_operator(home)
            cli = [sys.executable, "-I", str(HERE / "lane_hookset.py"), "doubled"]
            proc = subprocess.run(cli, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn(f"DOUBLED {user}", proc.stdout)
            self.assertIn("batch_gate.py runs twice", proc.stdout)
            # The control: skipping the user file reports no double, so the one
            # above came from the default path.
            proc = subprocess.run([*cli, "--user-settings", ""], capture_output=True,
                                  text=True, env=env)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr)

    def test_a_byte_identical_class_a_entry_is_removable_and_class_b_is_not(self):
        settings = {"hooks": {}}
        for script in ("require_ci_plan.py", "deny_cross_session_contact.py"):
            event, matcher, handler = self._entry(script)
            block = {"hooks": [handler], **({"matcher": matcher} if matcher else {})}
            settings["hooks"].setdefault(event, []).append(block)
        doubles, removable = hs.doubled(_set(), {"p": settings})
        self.assertEqual(doubles, [])
        self.assertEqual(len(removable), 1)
        self.assertIn("require_ci_plan.py", removable[0])


class Overlay(_Tmp):
    """harmonic-forge#1525: the core set carries none of the six operator
    hooks; the declared overlay merges them back, and nothing builds a
    core-only set without a literal `"none"`."""

    def _pair(self, core: dict, overlay) -> Path:
        """A set and its overlay side by side in the tmp dir; overlay None
        leaves the overlay file absent."""
        path = self.write("lane_hooks.json", core)
        if overlay is not None:
            self.write("lane_hooks.operator.json", overlay)
        return path

    # Test Case 1 (A-AC2, core half)
    def test_core_set_names_no_operator_script(self):
        overlay = json.loads(OVERLAY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(_named(overlay), SIX)
        self.assertEqual(_named(hs.load_set(SET_FILE, forge_root=ROOT)) & _named(overlay), set())
        core = _set()
        moved = next(b for b in overlay["hooks"]["SessionStart"]
                     if any("belt_wakeup.py" in h["command"] for h in b["hooks"]))
        core["hooks"]["SessionStart"].append(
            {"matcher": moved["matcher"],
             "hooks": [h for h in moved["hooks"] if "belt_wakeup.py" in h["command"]]})
        path = self._pair(core, overlay)
        with self.assertRaisesRegex(hs.SetError, "an overlay only adds hooks"):
            hs.load_effective(path, "operator", forge_root=ROOT)

    # Test Case 2 (A-AC2, operator half)
    def test_effective_operator_set_equals_f61ac37(self):
        want = json.loads(F61AC37.read_text(encoding="utf-8"))
        data = _effective()
        self.assertEqual(_tuples(data), sorted(want["handlers"], key=_tuple_key))
        self.assertEqual(sorted(data["lane_only"]), want["lane_only"])
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(hs.snapshot_gaps(data, snapshot), [])

    def test_none_is_the_core_set_and_unknown_refuses(self):
        self.assertEqual(hs.load_effective(SET_FILE, "none", forge_root=ROOT), _set())
        for bad in ("yes", "", "Operator", None, True):
            with self.subTest(bad=bad), self.assertRaisesRegex(hs.SetError, "operator, none"):
                hs.load_effective(SET_FILE, bad, forge_root=ROOT)

    def test_merge_appends_overlay_blocks_after_core_blocks(self):
        data = _effective()
        core = _set()
        for event, blocks in core["hooks"].items():
            self.assertEqual(data["hooks"][event][:len(blocks)], blocks)
        self.assertEqual(data["lane_only"][:len(core["lane_only"])], core["lane_only"])

    # Test Case 3 (A-AC3, undeclared)
    def test_an_undeclared_or_unknown_overlay_refuses_the_cli_build(self):
        for text, reason in ((None, "not declared"),
                             ('timezone = "America/Los_Angeles"\n', "not declared"),
                             ('lane_hook_overlay = "yes"\n', "operator, none"),
                             ("lane_hook_overlay = true\n", "operator, none")):
            with self.subTest(text=text):
                config = self.declare(text)
                proc = self.cli("build", "--set", str(SET_FILE))
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(str(config), proc.stderr)
                self.assertIn(reason, proc.stderr)

    # Test Case 4 (A-AC3, declared but broken overlay)
    def test_a_declared_operator_overlay_that_is_broken_refuses(self):
        overlay = json.loads(OVERLAY_FILE.read_text(encoding="utf-8"))
        missing_script = copy.deepcopy(overlay)
        missing_script["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            'f="${HOME}/harmonic-forge/tools/hooks/no_such_hook.py"; [ -f "$f" ] && python3 "$f" || true')
        self.declare('lane_hook_overlay = "operator"\n')
        for name, data in (("absent", None),
                           ("disableAllHooks", {**overlay, "disableAllHooks": True}),
                           ("unknown key", {**overlay, "permissions": {}}),
                           ("missing script", missing_script)):
            with self.subTest(name=name):
                for stale in self.tmp.glob("lane_hooks*.json"):
                    stale.unlink()
                path = self._pair(_set(), data)
                proc = self.cli("build", "--set", str(path))
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(str(path.parent / "lane_hooks.operator.json"), proc.stderr)

    # Pass-1 finding (hostile-author): an incomplete overlay refuses.
    def test_an_overlay_missing_any_operator_handler_refuses_naming_it(self):
        overlay = json.loads(OVERLAY_FILE.read_text(encoding="utf-8"))
        frozen = json.loads(F61AC37.read_text(encoding="utf-8"))["handlers"]
        core_named = _named(_set())
        required = [t for t in frozen if not set(hs.SCRIPT_RE.findall(t[2])) & core_named]
        self.assertEqual(len(required), 9)
        self.assertEqual({s for t in required for s in hs.SCRIPT_RE.findall(t[2])}, SIX)
        seen = 0
        for event, blocks in overlay["hooks"].items():
            for bi, block in enumerate(blocks):
                for hi, handler in enumerate(block["hooks"]):
                    script = hs.SCRIPT_RE.findall(handler["command"])[0]
                    for how in ("dropped", "timeout changed", "command changed"):
                        with self.subTest(event=event, script=script, how=how):
                            data = copy.deepcopy(overlay)
                            entries = data["hooks"][event][bi]["hooks"]
                            if how == "dropped":
                                del entries[hi]
                                if not entries:
                                    del data["hooks"][event][bi]
                                if not data["hooks"][event]:
                                    del data["hooks"][event]
                                # Dropped coherently, so `validate`'s own
                                # lane_only check cannot be what refuses.
                                if script not in _named(data):
                                    data["lane_only"] = [n for n in data["lane_only"] if n != script]
                            elif how == "timeout changed":
                                entries[hi]["timeout"] = entries[hi].get("timeout", 10) + 1
                            else:
                                entries[hi]["command"] += " "
                            path = self._pair(_set(), data)
                            with self.assertRaises(hs.SetError) as raised:
                                hs.load_effective(path, "operator", forge_root=ROOT)
                            message = str(raised.exception)
                            self.assertIn("missing 1 operator handler", message)
                            self.assertIn(f"{event} ", message)
                            self.assertIn(script, message)
                            self.assertIn(str(path.parent / "lane_hooks.operator.json"), message)
                    seen += 1
        self.assertEqual(seen, 9)
        # Every deny hook dropped at once: all three named, and the CLI refuses.
        data = copy.deepcopy(overlay)
        deny = ("enforce_belt_arming.py", "deny_retired_belt_sweep.py", "gate_feedback_memory_write.py")
        data["hooks"] = {e: kept for e, blocks in data["hooks"].items()
                         if (kept := [{**b, "hooks": hk} for b in blocks
                                      if (hk := [h for h in b["hooks"]
                                                 if not any(d in h["command"] for d in deny)])])}
        self.declare('lane_hook_overlay = "operator"\n')
        for stale in self.tmp.glob("lane_hooks*.json"):
            stale.unlink()
        path = self._pair(_set(), data)
        proc = self.cli("build", "--set", str(path))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        for name in deny:
            self.assertIn(name, proc.stderr)

    def test_an_overlay_narrowing_a_handlers_matcher_refuses_naming_it(self):
        """The matcher is part of a handler's identity (harmonic-forge#1525
        pass-2 survivor 2): enforce_belt_arming.py moved to a block that
        matches only `Monitor` is a missing handler, not the same one."""
        data = json.loads(OVERLAY_FILE.read_text(encoding="utf-8"))
        blocks = data["hooks"]["PreToolUse"]
        (bi, hi), = [(bi, hi) for bi, b in enumerate(blocks) for hi, h in enumerate(b["hooks"])
                     if "enforce_belt_arming.py" in h["command"]]
        self.assertNotEqual(blocks[bi]["matcher"], "Monitor")
        handler = blocks[bi]["hooks"].pop(hi)
        if not blocks[bi]["hooks"]:
            del blocks[bi]
        blocks.append({"matcher": "Monitor", "hooks": [handler]})
        path = self._pair(_set(), data)
        overlay_path = path.parent / "lane_hooks.operator.json"
        for name, load in (("load_overlay", lambda: hs.load_overlay(overlay_path, _set(), forge_root=ROOT)),
                           ("load_effective", lambda: hs.load_effective(path, "operator", forge_root=ROOT))):
            with self.subTest(load=name):
                with self.assertRaises(hs.SetError) as raised:
                    load()
                message = str(raised.exception)
                self.assertIn("missing 1 operator handler", message)
                self.assertIn("PreToolUse ", message)
                self.assertIn("enforce_belt_arming.py", message)
                self.assertNotIn("deny_retired_belt_sweep.py", message)

    # Test Case 5 (A-AC3, "none" is loud)
    def test_none_builds_core_only_and_says_so(self):
        config = self.declare('lane_hook_overlay = "none"\n')
        proc = self.cli("build", "--set", str(SET_FILE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(_named(value) & SIX, set())
        self.assertIn("NOT wired", proc.stderr)
        self.assertIn(str(config), proc.stderr)
        config = self.declare('lane_hook_overlay = "operator"\n')
        proc = self.cli("build", "--set", str(SET_FILE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_named(json.loads(proc.stdout)) & SIX, SIX)
        self.assertIn("47 core + 9 operator", proc.stderr)
        self.assertIn(str(config), proc.stderr)


class Running(_Tmp):
    """Change 2: a process counts as carrying the set only when its
    `--settings` VALUE is a JSON object with non-empty `hooks`."""

    def proc(self, pid: int, argv: list[str], env: dict, cwd: Path) -> None:
        d = self.tmp / "proc" / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        (d / "cwd").symlink_to(cwd)

    def test_running_reports_a_lane3_process_whose_settings_is_only_the_policy(self):
        project = self.tmp / "proj"
        project.mkdir()
        value = json.dumps({"disableAllHooks": False, "hooks": {"Stop": [{"hooks": [{}]}]}})
        self.proc(10, ["claude", "--settings", "/usr/local/share/hrse-gate/claude-lane3.json"],
                  {"LANE": "3"}, project)
        self.proc(11, ["claude", "--settings", value], {"LANE": "1"}, project)
        self.proc(12, ["claude", "-p", "x"], {"LANE": "2"}, project)
        self.proc(13, ["claude"], {}, project)                      # not a lane
        self.proc(14, ["python3", "hook.py"], {"LANE": "2"}, project)  # not claude
        self.proc(15, ["claude", f"--settings={value}"], {"LANE": "2"}, project)
        self.proc(16, ["claude", "--settings", '{"hooks": {}}'], {"LANE": "2"}, project)
        found = hs.running(self.tmp / "proc")
        self.assertEqual([line.split()[0] for line in found], ["10", "12", "16"])
        self.assertIn("LANE=3", found[0])

    def test_running_filters_by_project(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir()
        b.mkdir()
        self.proc(20, ["claude"], {"LANE": "1"}, a)
        self.proc(21, ["claude"], {"LANE": "1"}, b)
        self.assertEqual([line.split()[0] for line in hs.running(self.tmp / "proc", a)], ["20"])


class ProbeFixture(_Tmp):
    """The re-measurement tool stays runnable; without --real-run it launches
    nothing, which is all a test may do."""

    def test_every_variant_writes_a_fixture_and_launches_nothing(self):
        proc = subprocess.run([sys.executable, str(HERE / "probe_hook_merge.py"),
                               "--dir", str(self.tmp)], capture_output=True, text=True,
                              env={**os.environ, "PATH": "/nonexistent"}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("== "), 4)
        self.assertTrue((self.tmp / "proj" / ".claude" / "settings.json").is_file())

    def test_variant_and_env_select_without_launching(self):
        """The refused-env sweep's measurement form, launch-free."""
        base = [sys.executable, str(HERE / "probe_hook_merge.py"), "--dir", str(self.tmp)]
        env = {**os.environ, "PATH": "/nonexistent"}
        proc = subprocess.run([*base, "--variant", "merge", "--env", "CLAUDE_CODE_X=1"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("== "), 1)
        self.assertIn("env: CLAUDE_CODE_X=1", proc.stdout)
        proc = subprocess.run([*base, "--env", "NOEQUALS"], capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NAME=VALUE", proc.stderr)

    def test_other_hooks_are_the_cli_count_less_the_stub_count(self):
        import probe_hook_merge as probe
        log = self.write("log.txt", "A event=PreToolUse\nB event=PreToolUse\n")
        stream = "\n".join(json.dumps({"type": "system", "subtype": "hook_started",
                                       "hook_event": ev}) for ev in
                           ["PreToolUse"] * 5 + ["Stop"]) + "\nnot json\n"
        self.assertEqual(probe.other_hooks(stream, log), [
            "      3 OTHER PreToolUse (cli reported 5, stub 2)",
            "      1 OTHER Stop (cli reported 1, stub 0)"])


if __name__ == "__main__":
    unittest.main()
