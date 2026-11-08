#!/usr/bin/env python3
"""Tests for the protocol-failure capture writer (harmonic-forge#520)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import capture_failure as m  # noqa: E402

MINIMAL = [
    "--caught-by", "self",
    "--rule-existed", "none",
    "--expected", "branch before writing",
    "--happened", "wrote on main",
    "--session", "s",
    "--turn", "t",
]

#: A full, valid failure key.
KEY = ["--tool", "l1_post", "--class", "cwd", "--probe", "ls the target dir"]

_PATCHES: list = []


def setUpModule() -> None:
    """No test depends on the runner: the session variable is cleared and the
    transcript root points at an empty temp folder (harmonic-forge#1288)."""
    tmp = tempfile.TemporaryDirectory()
    env = mock.patch.dict(os.environ)
    env.start()
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    root = mock.patch.object(m, "TRANSCRIPT_ROOT", Path(tmp.name))
    root.start()
    _PATCHES.extend([env, root, tmp])


def tearDownModule() -> None:
    env, root, tmp = _PATCHES
    root.stop()
    env.stop()
    tmp.cleanup()


class _Written(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / "protocol-failures.jsonl"

    def run_capture(self, *extra: str) -> int:
        return m.main([*MINIMAL, *extra, "--path", str(self.log)])

    def lines(self) -> list[dict]:
        return [json.loads(line)
                for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class TestRequiredFieldsRefuse(unittest.TestCase):
    """AC3 — `caught_by` and `rule_existed` are required, not optional.

    A record without them is a diary entry. Refusal has to be a hard exit with
    no partial write, because a default would be a guess recorded as data.
    """

    def test_missing_caught_by_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            m.main(["--rule-existed", "none", "--expected", "x", "--happened", "y"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_rule_existed_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            m.main(["--caught-by", "hook", "--expected", "x", "--happened", "y"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_expected_or_happened_refuses(self):
        for drop in ("--expected", "--happened"):
            with self.subTest(drop=drop):
                argv = list(MINIMAL)
                index = argv.index(drop)
                del argv[index:index + 2]
                with self.assertRaises(SystemExit) as caught:
                    m.main(argv)
                self.assertNotEqual(caught.exception.code, 0)

    def test_every_caught_by_value_is_accepted(self):
        for value in m.CAUGHT_BY:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "f.jsonl"
                argv = list(MINIMAL)
                argv[argv.index("--caught-by") + 1] = value
                if value in m.KEY_REQUIRED_CAUGHT_BY:
                    argv += KEY
                self.assertEqual(m.main([*argv, "--path", str(log)]), 0)

    def test_every_rule_existed_value_is_accepted_with_its_ref_rule(self):
        for value in m.RULE_EXISTED:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "f.jsonl"
                argv = list(MINIMAL)
                argv[argv.index("--rule-existed") + 1] = value
                if value in m.RULE_REF_REQUIRED:
                    argv += ["--rule-ref", "R-0344"]
                self.assertEqual(m.main([*argv, "--path", str(log)]), 0)

    def test_an_out_of_enum_value_refuses(self):
        for flag, bogus in (("--caught-by", "somehow"), ("--rule-existed", "maybe")):
            with self.subTest(flag=flag):
                argv = list(MINIMAL)
                argv[argv.index(flag) + 1] = bogus
                with self.assertRaises(SystemExit) as caught:
                    m.main(argv)
                self.assertNotEqual(caught.exception.code, 0)


class TestRuleRefCrossField(_Written):
    """The cross-field rule argparse cannot express."""

    def test_a_named_rule_class_without_a_ref_refuses_without_writing(self):
        code = self.run_capture()  # baseline write so the file exists
        self.assertEqual(code, 0)
        before = self.log.read_text(encoding="utf-8")
        argv = list(MINIMAL)
        argv[argv.index("--rule-existed") + 1] = "rule"
        self.assertEqual(m.main([*argv, "--path", str(self.log)]), 2)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before,
                         "a refusal must not partially write")

    def test_none_with_a_ref_refuses(self):
        self.assertEqual(self.run_capture("--rule-ref", "R-0344"), 2)
        self.assertFalse(self.log.exists())


class TestAppendOnly(_Written):
    """AC2 — three sequential writes produce three lines, none overwritten.

    Asserted against the actual file rather than a mocked handle: the property
    is about what is on disk after the third call, and a mock would assert the
    code's intent instead of its effect.
    """

    def test_three_sequential_captures_produce_three_lines(self):
        for index in range(3):
            self.assertEqual(self.run_capture("--account", f"event {index}"), 0)
        lines = self.lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["account"] for line in lines],
                         ["event 0", "event 1", "event 2"])

    def test_an_existing_log_is_not_truncated(self):
        self.log.write_text('{"ts":"earlier","account":"pre-existing"}\n',
                            encoding="utf-8")
        self.assertEqual(self.run_capture(), 0)
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["account"], "pre-existing")

    def test_the_writer_never_opens_the_log_for_writing(self):
        """No rewrite path exists, and nothing may add one quietly."""
        source = Path(m.__file__).read_text(encoding="utf-8")
        for forbidden in ('"w"', "'w'", "write_text", "truncate", "os.replace"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} would introduce a rewrite path")

    def test_each_line_is_independently_parseable_json(self):
        for _ in range(3):
            self.run_capture()
        for line in self.log.read_text(encoding="utf-8").splitlines():
            json.loads(line)


class TestPerLaneDefaultPath(unittest.TestCase):
    """AC4 — the default honors Lane 3's restriction without a flag.

    Asking the invoker to remember the restriction is the failure mode this
    whole issue is about, so the default carries it.
    """

    def test_lane_3_defaults_inside_testplan(self):
        path = m.default_log_path("3")
        self.assertEqual(path.parent, m.TESTPLAN_ROOT)
        self.assertTrue(str(path).startswith(str(m.TESTPLAN_ROOT)))

    def test_other_lanes_default_to_the_shared_memory_store(self):
        for lane in ("1", "2", None, ""):
            with self.subTest(lane=lane):
                self.assertEqual(m.default_log_path(lane).parent, m.MEMORY_STORE)

    def test_lane_3_default_is_not_the_memory_store(self):
        """The specific denial: a Lane 3 write there is refused by the guard,
        so a shared default would make the capture unusable from Lane 3 —
        which is the lane most likely to observe a protocol failure."""
        self.assertNotEqual(m.default_log_path("3").parent, m.MEMORY_STORE)

    def test_an_explicit_path_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nested" / "f.jsonl"
            self.assertEqual(m.main([*MINIMAL, "--path", str(log)]), 0)
            self.assertTrue(log.exists(), "parent directories are created")


class TestNoNetwork(unittest.TestCase):
    """AC5 — no GitHub call, no network call, anywhere in the capture path.

    Telemetry that costs quota reproduces the class of defect it exists to
    catch, and a capture that can fail on a network error gets skipped.
    """

    def test_the_source_imports_nothing_that_reaches_the_network(self):
        source = Path(m.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket",
                          "subprocess", "gh api", "gh issue"):
            self.assertNotIn(forbidden, source)


class TestMemoryStoreIsReadOnlyEvidence(unittest.TestCase):
    """Running a capture must not touch any existing memory file.

    The pre-flight states existing memory files are read-only evidence. This
    asserts it holds in practice, not just in intent — a writer that rewrote a
    memory file while appending its record would corrupt the very corpus the
    analysis reads.
    """

    def test_existing_memory_file_mtimes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            memories = []
            for name in ("feedback_a.md", "feedback_b.md", "MEMORY.md"):
                path = store / name
                path.write_text(f"# {name}\n", encoding="utf-8")
                memories.append(path)
            before = {p: p.stat().st_mtime_ns for p in memories}
            time.sleep(0.01)
            self.assertEqual(
                m.main([*MINIMAL, "--path", str(store / "protocol-failures.jsonl")]),
                0)
            after = {p: p.stat().st_mtime_ns for p in memories}
            self.assertEqual(before, after)
            self.assertTrue((store / "protocol-failures.jsonl").exists())


class TestRecordShape(_Written):
    def test_the_two_required_fields_are_always_present(self):
        self.run_capture()
        record = self.lines()[0]
        self.assertEqual(record["caught_by"], "self")
        self.assertEqual(record["rule_existed"], "none")
        self.assertIn("ts", record)

    def test_optional_fields_are_recorded_as_null_not_omitted(self):
        """An absent key and a null value read differently to an analysis
        pass; keeping the shape stable means a missing field is visible."""
        self.run_capture()
        record = self.lines()[0]
        for key in ("repo", "issue", "session", "turns_lost",
                    "work_discarded", "account", "rule_ref", "lane"):
            self.assertIn(key, record)

    def test_cost_fields_round_trip(self):
        self.run_capture("--turns-lost", "4", "--work-discarded", "one commit")
        record = self.lines()[0]
        self.assertEqual(record["turns_lost"], 4)
        self.assertEqual(record["work_discarded"], "one commit")


class TestSkillText(unittest.TestCase):
    """AC6 — the skill states the enforcement finding in its own text, so the
    person invoking it is prompted toward the right remedy at the moment they
    have the context."""

    SKILL = Path(__file__).resolve().parents[2] / "skills" / "protocol-failure" / "SKILL.md"

    def test_the_skill_exists_and_names_the_enforcement_finding(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("ENFORCEMENT", text)
        self.assertIn("Writing another rule", text)
        self.assertIn("R-0344", text)

    def test_the_skill_documents_the_self_late_line(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("self_late", text)
        self.assertIn("before any artifact left the session", text)

    def test_the_skill_states_that_absence_is_a_finding(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("not clean, it is unaudited", text)

    def test_every_enum_value_the_writer_accepts_appears_in_the_skill(self):
        text = self.SKILL.read_text(encoding="utf-8")
        for value in list(m.CAUGHT_BY) + list(m.RULE_EXISTED):
            with self.subTest(value=value):
                self.assertIn(value, text)


class _Quiet(_Written):
    """Captures stderr so refusals can be inspected and do not clutter output."""

    def capture(self, *argv: str) -> int:
        self.err = io.StringIO()
        with redirect_stderr(self.err):
            return m.main([*argv, "--path", str(self.log)])

    def base(self, caught_by: str = "self", existed: str = "none", ref: str | None = None) -> list[str]:
        argv = list(MINIMAL)
        argv[argv.index("--caught-by") + 1] = caught_by
        argv[argv.index("--rule-existed") + 1] = existed
        return argv + (["--rule-ref", ref] if ref else [])

    def assert_refused_unchanged(self, *argv: str) -> None:
        before = self.log.read_bytes() if self.log.exists() else None
        self.assertEqual(self.capture(*argv), 2, self.err.getvalue())
        after = self.log.read_bytes() if self.log.exists() else None
        self.assertEqual(after, before, "a refusal must leave the log byte-identical")


class TestFailureKey(_Quiet):
    """AC1 (harmonic-forge#1288)."""

    def test_precheck_rule_without_key_refuses_without_writing(self):
        self.assertEqual(self.capture(*self.base()), 0)  # the log exists
        for ref in ("R-0419", "R-0419 / its skill"):
            with self.subTest(ref=ref):
                self.assert_refused_unchanged(*self.base(existed="rule", ref=ref))
        self.assertEqual(self.capture(*self.base(existed="rule", ref="R-0419"), *KEY), 0)

    def test_hook_or_gate_without_key_refuses(self):
        for caught in sorted(m.KEY_REQUIRED_CAUGHT_BY):
            with self.subTest(caught=caught):
                self.assert_refused_unchanged(*self.base(caught_by=caught))
                self.assertFalse(self.log.exists())

    def test_partial_key_refuses_without_writing(self):
        pairs = [KEY[i:i + 2] for i in range(0, len(KEY), 2)]
        partials = [pair for pair in pairs] + [a + b for i, a in enumerate(pairs) for b in pairs[i + 1:]]
        for base in (self.base(), self.base(caught_by="hook"), self.base(caught_by="operator"),
                     self.base(existed="rule", ref="R-0419"), self.base(existed="rule", ref="R-0344")):
            for partial in partials:
                with self.subTest(base=base[:4], partial=partial):
                    self.assert_refused_unchanged(*base, *partial)
        self.assertFalse(self.log.exists())

    def test_blank_key_field_counts_as_absent(self):
        """Survivor 7: a whitespace-only key field counted as present."""
        for blank in ("", " ", "\t ", "\n"):
            for flag in ("--tool", "--class", "--probe"):
                key = list(KEY)
                key[key.index(flag) + 1] = blank
                for base in (self.base(existed="rule", ref="R-0419"), self.base(caught_by="hook"),
                             self.base()):
                    with self.subTest(blank=blank, flag=flag, base=base[:4]):
                        self.assert_refused_unchanged(*base, *key)
        self.assertFalse(self.log.exists())
        # All three blank is no key at all: refused where a key is required...
        blank_key = ["--tool", " ", "--class", " ", "--probe", " "]
        self.assert_refused_unchanged(*self.base(existed="rule", ref="R-0419"), *blank_key)
        # ...and stored as null, never as blanks, where it is not.
        self.assertEqual(self.capture(*self.base(), *blank_key), 0)
        self.assertEqual([self.lines()[0][f] for f in ("tool", "class", "probe")], [None, None, None])

    def test_record_without_key_is_accepted(self):
        self.assertEqual(self.capture(*self.base(caught_by="operator")), 0)
        self.assertEqual(self.capture(*self.base(existed="rule", ref="R-0344")), 0)
        self.assertEqual(len(self.lines()), 2)

    def test_off_list_class_refuses(self):
        for klass in ("argz", "Args", "other:", "other:Bad", "other:args", "other:1x"):
            with self.subTest(klass=klass):
                self.assert_refused_unchanged(*self.base(), "--tool", "l1_post",
                                              "--class", klass, "--probe", "p")

    def test_other_class_is_accepted(self):
        self.assertEqual(self.capture(*self.base(), "--tool", "l1_post", "--class",
                                      "other:new-thing", "--probe", "p"), 0)
        self.assertEqual(self.lines()[0]["class"], "other:new-thing")

    def test_off_list_tool_refuses(self):
        for tool in ("l1-post", "other:thing", "bash", ""):
            with self.subTest(tool=tool):
                self.assert_refused_unchanged(*self.base(caught_by="hook"), "--tool", tool,
                                              "--class", "cwd", "--probe", "p")

    def test_every_listed_and_pseudo_tool_is_accepted(self):
        classes = m.load_classes()
        for tool in classes["interim_tools"] + classes["pseudo_tools"]:
            with self.subTest(tool=tool):
                self.assertEqual(self.capture(*self.base(caught_by="gate"), "--tool", tool,
                                              "--class", "args", "--probe", "p"), 0)

    def test_tool_none_only_with_other_reasoning(self):
        for klass in ("cwd", "other:thinking", "policy"):
            with self.subTest(klass=klass):
                self.assert_refused_unchanged(*self.base(caught_by="hook"), "--tool", "none",
                                              "--class", klass, "--probe", "re-read the AC")
        self.assertEqual(self.capture(*self.base(existed="rule", ref="R-0419"), "--tool", "none",
                                      "--class", "other:reasoning", "--probe", "re-read the AC"), 0)
        self.assertEqual(self.lines()[-1]["tool"], "none")


class TestClassList(_Quiet):
    """AC2."""

    V1 = ["args", "cwd", "freshness", "receipt", "target-state", "exists", "env",
          "local-check", "body", "shell", "provenance", "owed-work", "policy"]

    def test_class_list_v1_is_frozen(self):
        classes = m.load_classes()
        self.assertEqual(classes["versions"]["1"], self.V1)
        self.assertEqual(len(classes["versions"]["1"]), 13)

    def test_tool_lists_are_pinned(self):
        classes = m.load_classes()
        self.assertEqual(classes["pseudo_tools"], ["bash-string", "operator-commands-block"])
        self.assertEqual(len(classes["interim_tools"]), 31)
        self.assertFalse(set(classes["interim_tools"]) & set(classes["pseudo_tools"]))
        self.assertNotIn("none", classes["interim_tools"])

    def test_new_key_fields_are_null_not_omitted(self):
        self.assertEqual(self.capture(*self.base()), 0)
        record = self.lines()[0]
        for key in ("tool", "class", "probe"):
            with self.subTest(key=key):
                self.assertIn(key, record)
                self.assertIsNone(record[key])
        self.assertNotIn("command", record)
        self.assertEqual(record["class_list_version"], m.load_classes()["current"])
        self.assertEqual((record["session"], record["turn"]), ("s", "t"))

    def test_key_fields_round_trip(self):
        self.assertEqual(self.capture(*self.base(caught_by="hook"), *KEY), 0)
        record = self.lines()[0]
        self.assertEqual([record[k] for k in ("tool", "class", "probe")],
                         ["l1_post", "cwd", "ls the target dir"])


def _no_ids(argv: list[str]) -> list[str]:
    out = list(argv)
    for flag in ("--session", "--turn"):
        index = out.index(flag)
        del out[index:index + 2]
    return out


class TestIds(_Quiet):
    """AC3."""

    def setUp(self) -> None:
        super().setUp()
        self.transcripts = self.root / "projects"
        patcher = mock.patch.object(m, "TRANSCRIPT_ROOT", self.transcripts)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def transcript(self, session: str, records: list, slug: str = "-home-x") -> Path:
        path = self.transcripts / slug / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records),
                        encoding="utf-8")
        return path

    def test_missing_session_refuses_without_writing(self):
        self.assert_refused_unchanged(*_no_ids(MINIMAL), "--turn", "t")
        self.assertFalse(self.log.exists())
        self.assertIn("--session", self.err.getvalue())

    def test_session_defaults_from_the_environment(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "env-session"
        self.assertEqual(self.capture(*_no_ids(MINIMAL), "--turn", "t"), 0)
        self.assertEqual(self.lines()[0]["session"], "env-session")

    def test_unresolved_turn_refuses_without_writing(self):
        self.assert_refused_unchanged(*_no_ids(MINIMAL), "--session", "sid-1")
        self.transcript("sid-1", [{"type": "assistant", "promptId": "p-a"}])
        self.assert_refused_unchanged(*_no_ids(MINIMAL), "--session", "sid-1")
        self.assertFalse(self.log.exists())

    def test_turn_is_newest_prompt_id(self):
        self.transcript("sid-2", [
            {"type": "user", "promptId": "p-1", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": "x"}},
            {"type": "user", "promptId": "p-2", "message": {"content": "second"}},
            {"type": "assistant", "message": {"content": "y"}},
            "not json at all",
        ])
        self.assertEqual(m.resolve_turn("sid-2", self.transcripts), "p-2")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-2"
        self.assertEqual(self.capture(*_no_ids(MINIMAL)), 0)
        self.assertEqual((self.lines()[0]["session"], self.lines()[0]["turn"]), ("sid-2", "p-2"))

    def test_sidechain_and_text_prompt_ids_never_count(self):
        foreign = json.dumps({"type": "user", "promptId": "p-foreign"})
        self.transcript("sid-3", [
            {"type": "user", "promptId": "p-own", "message": {"content": "go"}},
            {"type": "user", "promptId": "p-own", "message": {"content": [
                {"type": "tool_result", "content": f"another transcript: {foreign}"}]}},
            {"type": "user", "promptId": "p-side", "isSidechain": True},
            {"type": "assistant", "message": {"content": '"promptId": "p-text"'}},
        ])
        self.assertEqual(m.resolve_turn("sid-3", self.transcripts), "p-own")

    def test_newest_transcript_of_the_session_wins(self):
        old = self.transcript("sid-4", [{"type": "user", "promptId": "p-old"}], slug="a")
        new = self.transcript("sid-4", [{"type": "user", "promptId": "p-new"}], slug="b")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        self.assertEqual(m.resolve_turn("sid-4", self.transcripts), "p-new")

    def test_only_the_tail_is_read_and_its_partial_line_dropped(self):
        padding = {"type": "assistant", "message": {"content": "z" * 400}}
        self.transcript("sid-5", [{"type": "user", "promptId": "p-early"}, padding,
                                  {"type": "user", "promptId": "p-late"}, padding])
        with mock.patch.object(m, "TAIL_BYTES", 600):
            self.assertEqual(m.resolve_turn("sid-5", self.transcripts), "p-late")
        with mock.patch.object(m, "TAIL_BYTES", 300):
            self.assertIsNone(m.resolve_turn("sid-5", self.transcripts))

    def test_a_session_id_with_path_characters_never_globs(self):
        self.transcript("sid-6", [{"type": "user", "promptId": "p-6"}])
        for bad in ("*", "../sid-6", "sid-6/x", "-home-x/sid-6"):
            with self.subTest(bad=bad):
                self.assertIsNone(m.resolve_turn(bad, self.transcripts))

    def test_explicit_turn_wins_over_the_transcript(self):
        self.transcript("sid-7", [{"type": "user", "promptId": "p-7"}])
        self.assertEqual(self.capture(*_no_ids(MINIMAL), "--session", "sid-7", "--turn", "mine"), 0)
        self.assertEqual(self.lines()[0]["turn"], "mine")


class TestOneSlip(_Quiet):
    """AC4."""

    def test_repeated_key_flag_refuses(self):
        for flag, value in (("--tool", "git-push"), ("--class", "args"), ("--probe", "other probe")):
            with self.subTest(flag=flag):
                self.assert_refused_unchanged(*self.base(caught_by="hook"), *KEY, flag, value)
                self.assertIn("One slip per record", self.err.getvalue())
        self.assertFalse(self.log.exists())


#: Token-shaped values, built by concatenation so no literal token sits here.
GH_TOKEN_SHAPE = "gh" + "p_" + "A1b2C3d4" * 5

#: The sticky-wicket REFORGE verdict's strings, every one given as a --probe
#: (harmonic-forge#1288, pass 1 and pass 2 survivors plus the verdict's own).
#: Each must refuse; `test_secret_scan.SURVIVORS` names the rule per string.
PROBE_SURVIVORS = [
    "export DB_PASS" + "WORD='$3cr3tHunter2'", "PASS" + "WORD=$'literal'",
    "postgresql://app:" + "$ecretPw@db/x", "headers={'Author" + "ization': 'Bearer q9A1b2'}",
    "curl https://alice" + "@localhost/x", "mail person" + "@evilnoreply.github.com",
    "export DB_PASS" + "WORD=${PREFIX}hunter2", "TOK" + "EN=$(echo hunter2)",
    "mvn deploy -Dpass" + "word=hunter2", "tool --access" + "Token=hunter2",
    'curl -d "{\\"pass' + 'word\\":\\"hunter2\\"}" https://x', "PASS" + "WORD=Bearer hunter2",
    "echo hunter2 | gh auth login --with-" + "token",
]


class TestSecretScan(_Quiet):
    """AC5: --probe is one line, at most 200 characters, and carries no
    credential keyword, @, :// or token shape. No command is stored."""

    def probe(self, value: str) -> list[str]:
        return [*self.base(caught_by="hook"), "--tool", "gh-api", "--class", "env", "--probe", value]

    def test_every_survivor_refuses_as_a_probe(self):
        for value in PROBE_SURVIVORS:
            with self.subTest(value=value[:16]):
                self.assert_refused_unchanged(*self.probe(value))
        self.assertFalse(self.log.exists())

    def test_over_length_probe_refuses(self):
        self.assert_refused_unchanged(*self.probe("x" * 201))
        self.assertIn("over-length", self.err.getvalue())

    def test_two_line_probe_refuses(self):
        self.assert_refused_unchanged(*self.probe("first line\nsecond line"))
        self.assertIn("multi-line", self.err.getvalue())

    def test_a_200_character_one_line_probe_is_stored(self):
        value = ("ls the target dir " * 12)[:200]
        self.assertEqual(self.capture(*self.probe(value)), 0)
        self.assertEqual(self.lines()[0]["probe"], value)

    def test_refusal_never_echoes_the_secret(self):
        self.assertEqual(self.capture(*self.probe(f"run {GH_TOKEN_SHAPE}")), 2)
        message = self.err.getvalue()
        self.assertIn("github-token", message)
        self.assertNotIn(GH_TOKEN_SHAPE, message)
        self.assertNotIn(GH_TOKEN_SHAPE[:12], message)

    def test_refusal_never_echoes_any_kind(self):
        """Every content kind, end to end: exit 2, the log byte-identical, the
        kind named, the value never repeated."""
        mixed = "A1b2C3d4E5f6G7h8"
        cases = {
            "credential-keyword": ("set DB_PASS" + "WORD to hunter2zz", "hunter2zz"),
            "at-sign": ("mail person@" + "evil.example", "person@evil"),
            "url-scheme": ("open ftp:" + "//hunter2zz", "hunter2zz"),
            "jwt": ("ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig9", "JhbGciOiJIUzI1NiIs"),
            "opaque-token": ("key AI" + "za" + mixed * 2 + "Qz9", "za" + mixed),
        }
        self.assertEqual(self.capture(*self.base()), 0)
        for kind, (value, secret) in cases.items():
            with self.subTest(kind=kind):
                self.assert_refused_unchanged(*self.probe(value))
                message = self.err.getvalue()
                self.assertIn(kind, message)
                self.assertNotIn(secret, message)
                self.assertNotIn(value, message)

    def test_command_flag_is_gone(self):
        with self.assertRaises(SystemExit):
            self.capture(*self.base(), "--command", "ls")
        self.assertFalse(self.log.exists())


class TestForgeHoldsNoData(_Quiet):
    """AC6: a target inside a harmonic-forge checkout or worktree refuses."""

    def fake_forge(self, name: str, git_file: bool = False) -> Path:
        forge = self.root / name
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / "tools" / "memory" / "capture_failure.py").write_text("# fake\n", encoding="utf-8")
        if git_file:
            (forge / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        else:
            (forge / ".git").mkdir()
        return forge

    def refused_at(self, target: Path) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(m.main([*self.base(), "--path", str(target)]), 2)
        self.assertFalse(target.exists())
        self.assertIn("harmonic-forge", err.getvalue())

    def test_path_inside_a_forge_checkout_refuses(self):
        forge = self.fake_forge("forge")
        self.refused_at(forge / "protocol-failures.jsonl")
        self.refused_at(forge / "deep" / "nested" / "f.jsonl")
        self.assertFalse((forge / "deep").exists())

    def test_path_inside_this_checkout_refuses(self):
        self.refused_at(Path(m.__file__).resolve().parent / "protocol-failures-probe.jsonl")

    def test_path_inside_a_forge_worktree_refuses(self):
        self.refused_at(self.fake_forge("worktree", git_file=True) / "f.jsonl")

    def test_symlink_into_forge_refuses(self):
        forge = self.fake_forge("forge-target")
        link = self.root / "looks-safe"
        link.symlink_to(forge)
        self.refused_at(link / "f.jsonl")

    def test_tilde_path_is_expanded_once(self):
        """Survivor 6: a literal `~` passed the guard (which expanded it) and
        was then written unexpanded, under the working directory."""
        home = self.root / "home"
        home.mkdir()
        forge = self.fake_forge("forge-cwd")
        cwd = os.getcwd()
        os.chdir(forge)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(m.main([*self.base(), "--path", "~/protocol-failures.jsonl"]), 0)
            self.assertTrue((home / "protocol-failures.jsonl").is_file())
            self.assertFalse((forge / "~").exists())
            # A `~` path that expands into forge is refused.
            (home / "hf").symlink_to(forge)
            with redirect_stderr(err):
                self.assertEqual(m.main([*self.base(), "--path", "~/hf/f.jsonl"]), 2)
            self.assertFalse((forge / "f.jsonl").exists())
            self.assertFalse((forge / "~").exists())

    def test_a_git_repo_that_is_not_forge_is_accepted(self):
        other = self.root / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.assertEqual(m.main([*self.base(), "--path", str(other / "f.jsonl")]), 0)


class TestFailureKeySkill(unittest.TestCase):
    """AC10."""

    SKILL = TestSkillText.SKILL

    def test_every_class_appears_in_the_skill(self):
        text = self.SKILL.read_text(encoding="utf-8")
        classes = m.load_classes()
        for klass in classes["versions"][str(classes["current"])]:
            with self.subTest(klass=klass):
                self.assertIn(f"`{klass}`", text)

    def test_the_skill_documents_the_key_rules(self):
        text = self.SKILL.read_text(encoding="utf-8")
        for needle in ("--tool", "--class", "--probe", "--turn", "One slip per record",
                       "The command that failed is not stored", "at most 200",
                       "other:reasoning", "A Codex caller passes `--session` and\n`--turn` itself"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
