"""harmonic-forge#874 step 1 -- the probe-only `/auto-ae` UserPromptSubmit hook,
driven as a real hook process against a temporary HOME."""
import calendar
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "auto_ae_toggle.py"
sys.path.insert(0, str(HERE))
import auto_ae_toggle  # noqa: E402

SECRET_SESSION = "sess-SECRET-1234"
SECRET_TRANSCRIPT = "/home/x/.claude/projects/p/SECRET-transcript.jsonl"


def _payload(prompt):
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt,
            "session_id": SECRET_SESSION, "transcript_path": SECRET_TRANSCRIPT,
            "cwd": "/tmp"}


def _run(stdin, home, lane="1", entrypoint="cli"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("LANE", "CLAUDE_CODE_ENTRYPOINT")}
    env["HOME"] = home
    if lane is not None:
        env["LANE"] = lane
    if entrypoint is not None:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    result = subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                            capture_output=True, text=True)
    return result


PROBE = Path(".local") / "state" / "auto-ae" / "probe.jsonl"

#: Runs the hook under a PEP 578 audit hook that records every write-intent
#: open and every filesystem mutation, then writes the targets as JSON to the
#: trace file, opened BEFORE the hook is installed so it is never traced itself.
_TRACER = r"""
import json, os, runpy, sys
sys.dont_write_bytecode = True
TRACE_FD = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
MUTATIONS = {"os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.link",
             "os.symlink", "os.truncate", "os.chmod", "os.utime",
             "shutil.copyfile", "shutil.move", "shutil.rmtree"}
seen = []
def audit(event, args):
    if event == "open":
        path, mode, flags = (list(args) + [None, None])[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                 (isinstance(flags, int) and flags & WRITE_FLAGS)
        if writes and isinstance(path, (str, bytes, os.PathLike)):
            seen.append(os.path.abspath(os.fsdecode(path)))
    elif event in MUTATIONS and args and isinstance(args[0], (str, bytes, os.PathLike)):
        seen.append(os.path.abspath(os.fsdecode(args[0])))
sys.addaudithook(audit)
try:
    sys.argv = sys.argv[1:2]
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    pass
os.write(TRACE_FD, json.dumps(seen).encode())
"""


def _traced_writes(home, tmpdir, cwd):
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    env.update(HOME=home, TMPDIR=tmpdir, LANE="1", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory() as trace_dir:
        trace = Path(trace_dir) / "trace.json"
        result = subprocess.run([sys.executable, "-c", _TRACER, str(HOOK), str(trace)],
                                input=json.dumps(_payload("/auto-ae on")), env=env,
                                cwd=cwd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "recorded" in result.stdout, result.stdout  # the hook really ran
        return set(json.loads(trace.read_text()))


def _probe(home):
    return Path(home) / ".local" / "state" / "auto-ae" / "probe.jsonl"


class RecordsOneLine(unittest.TestCase):
    def test_lane1_auto_ae_prompt_appends_one_redacted_line(self):
        with tempfile.TemporaryDirectory() as home:
            prompt = "<command-name>/auto-ae</command-name>\n<command-args>status</command-args>"
            before = time.time()
            result = _run(json.dumps(_payload(prompt)), home)
            after = time.time()
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)
            self.assertEqual(out, {"systemMessage": auto_ae_toggle.RECORDED})
            lines = _probe(home).read_text().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["prompt"], prompt)
            self.assertEqual(entry["LANE"], "1")
            self.assertEqual(entry["CLAUDE_CODE_ENTRYPOINT"], "cli")
            self.assertEqual(entry["payload_keys"],
                             ["cwd", "hook_event_name", "prompt", "session_id",
                              "transcript_path"])
            # The run's own instant, not merely a well-formed one: a frozen
            # clock lands outside this window (#880 sticky-wicket, survivor 4).
            stamp = calendar.timegm(time.strptime(entry["timestamp"], "%Y-%m-%dT%H:%M:%SZ"))
            self.assertGreaterEqual(stamp, int(before) - 2)
            self.assertLessEqual(stamp, int(after) + 2)
            raw = _probe(home).read_text()
            self.assertNotIn(SECRET_SESSION, raw)
            self.assertNotIn(SECRET_TRANSCRIPT, raw)

    def test_match_is_case_insensitive_and_appends(self):
        with tempfile.TemporaryDirectory() as home:
            _run(json.dumps(_payload("AUTO-AE status")), home)
            _run(json.dumps(_payload("/Auto-Ae on")), home)
            self.assertEqual(len(_probe(home).read_text().splitlines()), 2)

    def test_missing_entrypoint_is_recorded_as_null(self):
        with tempfile.TemporaryDirectory() as home:
            _run(json.dumps(_payload("/auto-ae")), home, entrypoint=None)
            entry = json.loads(_probe(home).read_text())
            self.assertIsNone(entry["CLAUDE_CODE_ENTRYPOINT"])


class TimestampIsTheInjectedInstant(unittest.TestCase):
    def test_now_seam_sets_the_recorded_timestamp(self):
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                self.assertTrue(auto_ae_toggle.record(
                    _payload("/auto-ae"), "1", "cli", now=1_700_000_000))
            finally:
                if old is None:
                    os.environ.pop("HOME")
                else:
                    os.environ["HOME"] = old
            entry = json.loads(_probe(home).read_text())
            # now=0 would collide with any frozen-clock mutant's own output.
            self.assertEqual(entry["timestamp"], "2023-11-14T22:13:20Z")


class DecidesAndGrantsNothing(unittest.TestCase):
    def test_output_carries_no_decision_or_context(self):
        with tempfile.TemporaryDirectory() as home:
            out = json.loads(_run(json.dumps(_payload("/auto-ae on")), home).stdout)
            self.assertEqual(set(out), {"systemMessage"})
            self.assertIn("nothing toggled", out["systemMessage"])

    def test_writes_nothing_but_the_probe_log(self):
        """AC4, proven by what the hook ATTEMPTS to write, not by observing
        directories: every write-intent open and every filesystem mutation is
        traced with a PEP 578 audit hook, so a write anywhere -- /var/tmp, the
        repo root, any name -- fails, and no shared directory is read (#880
        sticky-wicket PATCH, survivors 2 and 3)."""
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as tmpdir, \
                tempfile.TemporaryDirectory() as cwd:
            targets = _traced_writes(home, tmpdir, cwd)
            allowed = {str(Path(home) / rel) for rel in (
                ".local", ".local/state", ".local/state/auto-ae",
                ".local/state/auto-ae/probe.jsonl")}
            self.assertIn(str(Path(home) / PROBE), targets)
            self.assertEqual(targets - allowed, set())
            # Cheap cross-check on the hook's own TMPDIR and cwd.
            self.assertEqual(list(Path(tmpdir).rglob("*")), [])
            self.assertEqual(list(Path(cwd).rglob("*")), [])

    def test_the_write_trace_ignores_foreign_files(self):
        """Hermetic by construction: a decoy another process leaves in the
        system temp root cannot affect the trace (survivor 3)."""
        with tempfile.NamedTemporaryFile(prefix="zz-auto-decoy-"), \
                tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as tmpdir, \
                tempfile.TemporaryDirectory() as cwd:
            targets = _traced_writes(home, tmpdir, cwd)
            self.assertEqual({t for t in targets if not t.startswith(home)}, set())


class SilentWhenNotApplicable(unittest.TestCase):
    def _assert_silent(self, stdin, lane="1"):
        with tempfile.TemporaryDirectory() as home:
            result = _run(stdin, home, lane=lane)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(_probe(home).exists())

    def test_other_lanes_and_no_lane_record_nothing(self):
        for lane in ("2", "3", None, ""):
            with self.subTest(lane=lane):
                self._assert_silent(json.dumps(_payload("/auto-ae on")), lane=lane)

    def test_prompt_without_auto_ae_records_nothing(self):
        self._assert_silent(json.dumps(_payload("BATCH F874 auto ae")))
        self._assert_silent(json.dumps(_payload("autoae")))

    def test_non_string_or_missing_prompt_records_nothing(self):
        self._assert_silent(json.dumps({"prompt": ["auto-ae"]}))
        self._assert_silent(json.dumps({"session_id": "x"}))
        self._assert_silent(json.dumps(["auto-ae"]))


class NeverRaises(unittest.TestCase):
    def test_malformed_stdin_exits_zero_and_records_nothing(self):
        """An unparseable payload is an unidentifiable prompt: silent, even
        when its bytes contain the token. There is exactly one gate, and it
        reads the parsed prompt (#880 sticky-wicket PATCH, survivor 1)."""
        with tempfile.TemporaryDirectory() as home:
            result = _run("not json auto-ae", home)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(_probe(home).exists())

    def test_truncated_payload_with_the_token_only_outside_the_prompt_is_silent(self):
        with tempfile.TemporaryDirectory() as home:
            result = _run('{"cwd":"/w/forge-874-auto-ae","prompt":"deploy the', home)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
            self.assertFalse(_probe(home).exists())

    def test_unparseable_lane1_prompt_without_the_token_is_silent(self):
        """AC2. Every output sits behind the one gate: LANE=1 AND the token
        in the parsed prompt. Empty, truncated and undecodable stdin all stay silent
        when nothing mentions auto-ae (#880 pass 1, reproduced cross-family)."""
        for stdin in (b"", b'{"prompt": "deploy the', b"\xff\xfe not json"):
            with self.subTest(stdin=stdin), tempfile.TemporaryDirectory() as home:
                env = {k: v for k, v in os.environ.items() if k != "LANE"}
                env.update(HOME=home, LANE="1")
                result = subprocess.run([sys.executable, str(HOOK)], input=stdin,
                                        env=env, capture_output=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), b"")
                self.assertFalse(_probe(home).exists())

    def test_unwritable_probe_dir_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            # A FILE where the state directory must go makes mkdir fail.
            (Path(home) / ".local").mkdir()
            (Path(home) / ".local" / "state").write_text("blocker")
            result = _run(json.dumps(_payload("/auto-ae")), home)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)
            self.assertEqual(set(out), {"systemMessage"})  # AC3: never a decision
            self.assertIn("NOT recorded", out["systemMessage"])

    def test_malformed_stdin_outside_lane1_is_silent(self):
        with tempfile.TemporaryDirectory() as home:
            result = _run("not json", home, lane="2")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")


class RegisteredInThisRepo(unittest.TestCase):
    """AC5. An unregistered probe is indistinguishable from "no prompt matched
    yet", and #874 would draw its conclusion from an empty log. Scoped to THIS
    repo's settings file, found relative to this test, for the reason
    `test_belt_wakeup.py`'s matcher test gives: a repo's CI speaks only for that
    repo. HRSE2 asserts its own registration in `scripts/test_auto_ae_registration.py`;
    `forge_onboard.check_hooks` does NOT detect an absent entry."""

    def test_the_probe_is_wired_on_user_prompt_submit(self):
        path = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"
        # Fail, never skip: a missing file is the unwired state this test
        # exists to catch (#880 pass 1).
        self.assertTrue(path.is_file(), f"no settings at {path}")
        settings = json.loads(path.read_text(encoding="utf-8"))
        commands = [hook.get("command", "")
                    for block in (settings.get("hooks") or {}).get("UserPromptSubmit") or []
                    for hook in block.get("hooks") or []]
        wired = [c for c in commands if "auto_ae_toggle.py" in c]
        self.assertEqual(len(wired), 1, "auto_ae_toggle.py is not wired exactly once")
        # The guarded form: a missing script is a no-op, never a failed prompt.
        self.assertIn('[ -f "$f" ]', wired[0])
        self.assertTrue(wired[0].rstrip().endswith("|| true"))


if __name__ == "__main__":
    unittest.main()
