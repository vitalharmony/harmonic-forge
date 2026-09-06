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
