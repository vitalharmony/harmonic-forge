#!/usr/bin/env python3
"""Tests for the PreToolUse reload probe (harmonic-forge#480)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compaction_marker as cm  # noqa: E402
import compaction_reload_probe as probe  # noqa: E402


class Probe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for module in (cm, probe):
            patcher = mock.patch.object(module, "MARKER_DIR", self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)

    def marker(self, session="s1", **extra):
        (self.dir / f"{session}.json").write_text(json.dumps(
            {"compacted_at": "2026-09-05T07:55:34+00:00", "source": "compact",
             "lane": "2", "cwd": "/x/HRSE2-lane2", **extra}))

    def fire(self, command="cat rules/universal-claude.md", session="s1",
             tool="Bash", when="2026-09-05T08:00:00+00:00"):
        key = "file_path" if tool == "Read" else "command"
        return probe.handle({"session_id": session, "tool_name": tool,
                             "tool_input": {key: command}}, now=when)

    def written(self, session="s1"):
        return json.loads((self.dir / f"{session}.json").read_text())

    # --- the recording path ------------------------------------------------

    def test_a_real_reload_is_recorded(self) -> None:
        self.marker()
        self.fire()
        self.assertEqual(self.written()["reloaded_at"], "2026-09-05T08:00:00+00:00")

    def test_a_read_tool_reload_is_recorded(self) -> None:
        self.marker()
        self.fire("/home/mmangus/harmonic-forge/rules/testing-gate.md", tool="Read")
        self.assertIn("reloaded_at", self.written())

    # --- the things it must not do ----------------------------------------

    def test_it_never_creates_a_marker(self) -> None:
        """The constraint that keeps harmonic-forge#451 honest: a PreToolUse
        that could create a marker would manufacture a compaction that never
        happened, and the deny would fire on it."""
        self.fire()
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_a_non_reload_leaves_the_marker_untouched(self) -> None:
        self.marker()
        self.fire('grep -rn "3-lane-protocol.md" .')
        self.assertNotIn("reloaded_at", self.written())

    def test_a_heredoc_write_leaves_the_marker_untouched(self) -> None:
        self.marker()
        self.fire("cat >> tests.py <<EOF\nsee 3-lane-protocol.md\nEOF")
        self.assertNotIn("reloaded_at", self.written())

    def test_it_never_blocks_or_comments_on_the_tool_call(self) -> None:
        """This hook records; enforcement is harmonic-forge#451 and is
        deliberately not here. Every path returns an empty object."""
        self.marker()
        self.assertEqual(self.fire(), {})
        self.assertEqual(self.fire('grep -rn "x" .'), {})
        self.assertEqual(probe.handle({}), {})

    def test_a_session_with_no_id_is_a_no_op(self) -> None:
        self.assertEqual(probe.handle({"tool_name": "Bash",
                                       "tool_input": {"command": "cat x"}}), {})

    def test_it_is_set_once(self) -> None:
        self.marker(reloaded_at="2026-09-05T08:00:00+00:00")
        self.fire(when="2026-09-05T09:00:00+00:00")
        self.assertEqual(self.written()["reloaded_at"], "2026-09-05T08:00:00+00:00")


class Cli(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent / "compaction_reload_probe.py"

    def run_it(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.SCRIPT)], input=stdin,
                              capture_output=True, text=True)

    def test_a_malformed_payload_is_a_quiet_no_op(self) -> None:
        """Deliberately unlike `compaction_marker.main`, which emits a
        systemMessage. That hook's failure costs a session its recovery note;
        this one's costs a flag nothing yet consumes, and a message on every
        malformed PreToolUse payload would be noise on the hottest event
        there is."""
        result = self.run_it("{not json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_a_non_object_payload_is_a_quiet_no_op(self) -> None:
        self.assertEqual(json.loads(self.run_it("[1,2]").stdout), {})

    def test_an_unrelated_tool_call_is_a_quiet_no_op(self) -> None:
        payload = json.dumps({"session_id": "nope", "tool_name": "Bash",
                              "tool_input": {"command": "ls"}})
        self.assertEqual(json.loads(self.run_it(payload).stdout), {})


class SharesOneDetector(unittest.TestCase):
    """harmonic-forge#451's JDC1: one module, imported by both, not a third
    copy of the corpus list."""

    def test_the_probe_imports_rather_than_redefines(self) -> None:
        source = Path(probe.__file__).read_text()
        self.assertIn("from compaction_marker import", source)
        self.assertNotIn("READ_VERBS = ", source)
        self.assertNotIn("CorpusFile(", source)

    def test_both_names_resolve_to_the_marker_module(self) -> None:
        self.assertIs(probe.is_corpus_reload, cm.is_corpus_reload)
        self.assertIs(probe.note_reload, cm.note_reload)


if __name__ == "__main__":
    unittest.main()


class TheFastPathIsRealNotDecorative(unittest.TestCase):
    """The marker-exists check is a performance guard, not a correctness one
    — `note_reload` is update-only and would refuse anyway. Mutation testing
    showed removing it broke nothing, which means it needed a test that
    asserts what it is actually for: not parsing a command on the hot path
    for the overwhelming majority of sessions, which never compact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for module in (cm, probe):
            patcher = mock.patch.object(module, "MARKER_DIR", Path(self.tmp.name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_marker_means_the_detector_is_never_even_called(self) -> None:
        with mock.patch.object(probe, "is_corpus_reload") as detector:
            probe.handle({"session_id": "never-compacted", "tool_name": "Bash",
                          "tool_input": {"command": "cat rules/universal-agent.md"}})
        detector.assert_not_called()

    def test_an_already_flagged_marker_still_short_circuits_in_note_reload(self) -> None:
        (Path(self.tmp.name) / "s1.json").write_text(json.dumps(
            {"compacted_at": "x", "reloaded_at": "already"}))
        probe.handle({"session_id": "s1", "tool_name": "Bash",
                      "tool_input": {"command": "cat rules/universal-agent.md"}},
                     now="later")
        self.assertEqual(
            json.loads((Path(self.tmp.name) / "s1.json").read_text())["reloaded_at"],
            "already")


AUTO_MARKER = {
    "compacted_at": "2026-09-05T07:55:34+00:00", "source": "compact",
    "lane": "3", "lane_source": "cwd_override", "cwd": "/x/HRSE2-lane3",
    "trigger": "auto",
}


class DenyBackstop(unittest.TestCase):
    """harmonic-forge#451 — enforcement. F446 injects and asks; the salience
    test showed a session with the rule text already in context not acting on
    it. This does not require the session's cooperation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for module in (cm, probe):
            patcher = mock.patch.object(module, "MARKER_DIR", self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)

    def marker(self, **overrides):
        payload = {**AUTO_MARKER, **overrides}
        (self.dir / "s1.json").write_text(json.dumps(payload))
        return payload

    def call(self, tool="Bash", value="ls -la", env=None):
        key = "file_path" if tool == "Read" else "command"
        return probe.handle({"session_id": "s1", "tool_name": tool,
                             "tool_input": {key: value}}, now="T", env=env or {})

    def decision(self, out):
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision")

    # --- TC1: the deny itself -------------------------------------------

    def test_an_auto_compacted_unreloaded_session_is_denied(self) -> None:
        self.marker()
        self.assertEqual(self.decision(self.call()), "deny")

    def test_it_is_deny_not_ask(self) -> None:
        """Ratified. `ask` presumes a human at the keyboard, which is exactly
        what a background-spawned session does not have — and that is the
        session this backstop most needs to catch."""
        self.marker()
        self.assertNotEqual(self.decision(self.call()), "ask")

    def test_the_reason_names_this_lanes_corpus(self) -> None:
        """Lane-specific via F479's corrected `lane`. A Lane 3 session must
        be pointed at `testing-gate.md`; a Lane 1 one must not."""
        self.marker(lane="3")
        reason = self.call()["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("rules/testing-gate.md", reason)
        self.marker(lane="1")
        reason = self.call()["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("rules/universal-lane1.md", reason)
        self.assertNotIn("rules/testing-gate.md", reason)

    def test_the_reason_names_the_escape_hatch(self) -> None:
        self.marker()
        self.assertIn(probe.ESCAPE_HATCH,
                      self.call()["hookSpecificOutput"]["permissionDecisionReason"])

    # --- the deadlock, which is the point -------------------------------

    def test_a_corpus_read_is_allowed_while_blocked(self) -> None:
        """Without this the deny blocks the reads that clear it, on the first
        tool call of every enforced compaction — a permanent lockout, and
        `deny` rather than `ask` means no human can override it in the
        moment."""
        self.marker()
        self.assertIsNone(self.decision(self.call("Bash", "cat rules/testing-gate.md")))

    def test_a_corpus_read_via_the_read_tool_is_allowed_while_blocked(self) -> None:
        self.marker()
        self.assertIsNone(self.decision(
            self.call("Read", "/h/harmonic-forge/rules/testing-gate.md")))

    def test_the_allowed_read_also_clears_the_block(self) -> None:
        """Allowing it is half the job; the next call must not be denied."""
        self.marker()
        self.call("Bash", "cat rules/testing-gate.md")
        self.assertEqual(json.loads((self.dir / "s1.json").read_text())["reloaded_at"], "T")
        self.assertIsNone(self.decision(self.call()))

    def test_a_non_read_that_merely_mentions_the_corpus_is_still_denied(self) -> None:
        """The exemption is `is_corpus_reload`, not "the command contains a
        corpus path" — otherwise the block is bypassed by naming a file."""
        self.marker()
        self.assertEqual(
            self.decision(self.call("Bash", 'grep -rn "rules/testing-gate.md" .')), "deny")

    # --- TC2-TC5: the allow paths ---------------------------------------

    def test_an_already_reloaded_session_is_allowed(self) -> None:
        self.marker(reloaded_at="2026-09-05T08:00:00+00:00")
        self.assertIsNone(self.decision(self.call()))

    def test_a_manual_compaction_is_allowed(self) -> None:
        """JDC2: the operator compacting deliberately is not the failure this
        catches."""
        self.marker(trigger="manual", reloaded_at=None)
        self.assertIsNone(self.decision(self.call()))

    def test_an_undeterminable_trigger_is_allowed(self) -> None:
        """`None` means the compaction record could not be read, not that it
        was automatic. Denying on it would block work on a fact nobody
        established."""
        self.marker(trigger=None)
        self.assertIsNone(self.decision(self.call()))

    def test_a_session_that_never_compacted_is_allowed(self) -> None:
        self.assertIsNone(self.decision(self.call()))

    # --- TC6/TC7: hatch and failure -------------------------------------

    def test_the_escape_hatch_allows_and_announces_itself(self) -> None:
        self.marker()
        out = self.call(env={probe.ESCAPE_HATCH: "1"})
        self.assertIsNone(self.decision(out))
        self.assertIn(probe.ESCAPE_HATCH, out["systemMessage"])

    def test_the_hatch_is_checked_before_anything_else(self) -> None:
        """Including before the marker is read — an operator overriding a
        gate should not depend on the gate's own state being readable."""
        (self.dir / "s1.json").write_text("{not json")
        out = self.call(env={probe.ESCAPE_HATCH: "1"})
        self.assertIn(probe.ESCAPE_HATCH, out["systemMessage"])

    def test_a_malformed_marker_fails_open_and_visibly(self) -> None:
        """AC5. Silence would be harmonic-forge#440 — a gate that stopped
        working and told nobody. Blocking would be worse: the state needed to
        clear the block is the state that is broken."""
        (self.dir / "s1.json").write_text("{not json")
        out = self.call()
        self.assertIsNone(self.decision(out))
        self.assertIn("unreadable", out["systemMessage"])

    def test_a_non_object_marker_also_fails_open_visibly(self) -> None:
        (self.dir / "s1.json").write_text("[1, 2, 3]")
        out = self.call()
        self.assertIsNone(self.decision(out))
        self.assertTrue(out.get("systemMessage"))


class DenyLogicIsNotDuplicated(unittest.TestCase):
    """AC4 — harmonic-forge#328's finding. The enforcement half must import
    the detection, not restate it."""

    SOURCE = Path(probe.__file__).read_text()

    def test_it_imports_rather_than_reimplements(self) -> None:
        self.assertIn("from compaction_marker import", self.SOURCE)
        for token in ("READ_VERBS", "CorpusFile(", "compactMetadata"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.SOURCE)

    def test_the_corpus_comes_from_the_shared_module(self) -> None:
        self.assertIs(probe.corpus_for, cm.corpus_for)
        self.assertIs(probe.is_corpus_reload, cm.is_corpus_reload)

    def test_the_docstring_no_longer_claims_it_never_blocks(self) -> None:
        """It said so deliberately, and that stopped being true."""
        self.assertNotIn("It never blocks", probe.__doc__)
        self.assertIn("harmonic-forge#451", probe.__doc__)
