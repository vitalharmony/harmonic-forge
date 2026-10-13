"""Tests for gate_feedback_memory_write.py (harmonic-forge#981, AC1-AC3, AC7, AC8).

Every test runs against a temp store. The subprocess tests pin
`CLAUDE_CONFIG_DIR` to a temp directory whose `settings.json` points
`autoMemoryDirectory` at that store, so nothing here reads the operator's real
settings or memory (the `test_memory_lint.py:84` lesson).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gate_feedback_memory_write as gate  # noqa: E402

IDS = {"R-0039", "R-0160"}


def memory(rule_check: str | None, *, mtype: str = "feedback", body: str = "A lesson.\n") -> str:
    lines = ["---", "name: x", "description: d", "metadata:", f"  type: {mtype}"]
    if rule_check is not None:
        lines.append(f"rule_check: {rule_check}")
    return "\n".join(lines + ["---", body])


class Decide(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "store"
        self.store.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> str | None:
        return gate.decide("Write", {"file_path": str(self.store / name), "content": content},
                           self.store, IDS)

    # AC1
    def test_ac1_resolving_rule_check_is_denied_with_commands(self) -> None:
        msg = self.write("feedback_x.md", memory("R-0039"))
        self.assertIsNotNone(msg)
        self.assertIn("capture_failure.py --caught-by", msg)
        self.assertIn("--rule-ref R-0039", msg)
        self.assertIn(f'grep -l "R-0039" {self.store}/feedback_*.md', msg)

    def test_ac1_untyped_feedback_filename_is_gated(self) -> None:
        msg = self.write("feedback_x.md", memory("R-0039", mtype="project"))
        self.assertIsNotNone(msg)
        self.assertIn("R-0039", msg)

    def test_ac1_feedback_type_with_other_filename_is_gated(self) -> None:
        self.assertIsNotNone(self.write("lesson.md", memory("R-0039")))

    # AC2
    def test_ac2_none_declaration_is_allowed(self) -> None:
        self.assertIsNone(self.write("feedback_x.md", memory("none (searched: foo, bar)")))

    def test_ac2_missing_rule_check_is_denied_naming_both_forms(self) -> None:
        msg = self.write("feedback_x.md", memory(None))
        self.assertIsNotNone(msg)
        self.assertIn("rule_check: R-NNNN", msg)
        self.assertIn("rule_check: none (searched: <terms>)", msg)
        self.assertIn("grep -rn -i", msg)

    def test_ac2_non_resolving_id_is_denied(self) -> None:
        msg = self.write("feedback_x.md", memory("R-9999"))
        self.assertIsNotNone(msg)
        self.assertIn("R-9999", msg)
        self.assertIn("rule_check: none", msg)

    # AC3
    def test_ac3_write_outside_store_passes(self) -> None:
        outside = Path(self._tmp.name) / "feedback_x.md"
        self.assertIsNone(gate.decide("Write", {"file_path": str(outside), "content": memory(None)},
                                      self.store, IDS))

    def test_ac3_edit_of_existing_feedback_memory_passes(self) -> None:
        path = self.store / "feedback_x.md"
        path.write_text(memory(None))
        for tool, tool_input in (
            ("Edit", {"file_path": str(path), "old_string": "A lesson.", "new_string": "B"}),
            ("Write", {"file_path": str(path), "content": memory(None)}),
        ):
            self.assertIsNone(gate.decide(tool, tool_input, self.store, IDS), tool)

    def test_ac3_non_feedback_memory_passes(self) -> None:
        self.assertIsNone(self.write("project_x.md", memory(None, mtype="project")))

    def test_ac3_empty_old_string_edit_on_missing_file_is_gated(self) -> None:
        path = self.store / "feedback_x.md"
        msg = gate.decide("Edit", {"file_path": str(path), "old_string": "",
                                   "new_string": memory("R-0039")}, self.store, IDS)
        self.assertIsNotNone(msg)

    def test_multiedit_creating_a_file_is_gated(self) -> None:
        path = self.store / "feedback_x.md"
        msg = gate.decide("MultiEdit", {"file_path": str(path), "edits": [
            {"old_string": "", "new_string": memory(None)}]}, self.store, IDS)
        self.assertIsNotNone(msg)

    def test_write_over_existing_non_feedback_file_is_gated(self) -> None:
        path = self.store / "feedback_x.md"
        path.write_text("plain notes, no frontmatter\n")
        path_other = self.store / "notes.md"
        path_other.write_text(memory(None, mtype="project"))
        msg = gate.decide("Write", {"file_path": str(path_other), "content": memory("R-0039")},
                          self.store, IDS)
        self.assertIsNotNone(msg)

    # AC8
    def test_ac8_none_with_resolving_body_id_is_denied_naming_it(self) -> None:
        msg = self.write("feedback_x.md", memory("none (searched: x)", body="See R-0160 here.\n"))
        self.assertIsNotNone(msg)
        self.assertIn("R-0160", msg)

    def test_ac8_none_with_non_resolving_body_id_is_allowed(self) -> None:
        self.assertIsNone(self.write("feedback_x.md",
                                     memory("none (searched: x)", body="See R-9999.\n")))


class Registry(unittest.TestCase):
    def test_real_registry_resolves_a_known_rule(self) -> None:
        self.assertIn("R-0039", gate.rule_ids())


class Main(unittest.TestCase):
    """The hook end to end, with CLAUDE_CONFIG_DIR pinned to a temp settings file."""

    def run_hook(self, payload: dict, store: Path, config: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config)}
        return subprocess.run([sys.executable, str(HERE / "gate_feedback_memory_write.py")],
                              input=json.dumps(payload), capture_output=True, text=True, env=env,
                              timeout=30)

    def test_main_denies_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, config = Path(tmp) / "store", Path(tmp) / "config"
            store.mkdir()
            config.mkdir()
            (config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(store)}))
            deny = self.run_hook({"tool_name": "Write", "tool_input": {
                "file_path": str(store / "feedback_x.md"), "content": memory("R-0039")}},
                store, config)
            self.assertEqual(deny.returncode, 0)
            out = json.loads(deny.stdout)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            allow = self.run_hook({"tool_name": "Write", "tool_input": {
                "file_path": str(Path(tmp) / "elsewhere.md"), "content": "x"}}, store, config)
            self.assertEqual((allow.returncode, allow.stdout), (0, ""))

    def test_main_garbage_input_passes_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(HERE / "gate_feedback_memory_write.py")],
                                    input="not json", capture_output=True, text=True,
                                    env={**os.environ, "CLAUDE_CONFIG_DIR": tmp}, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
