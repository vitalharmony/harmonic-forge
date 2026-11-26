#!/usr/bin/env python3
"""harmonic-forge#1594: `hooks_report` and `_print_rule` accept both shapes of
a rule's `hooks` entry (a table or a bare path string) and report any other
type as a finding instead of crashing.

Every test patches `_WIRING_LOCATIONS` to temp files, so the operator's real
settings are never read.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import query_rules as query  # noqa: E402


def _rule(rule_id: str, hooks: list) -> dict:
    return {"id": rule_id, "file": "rules/x.md", "statement": "s", "hooks": hooks}


class HooksReportShapes(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        settings = self.root / "settings.json"
        command = {"hooks": [{"command": "python3 tools/hooks/x.py"}]}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [command]}}), encoding="utf-8")
        patcher = mock.patch.object(query, "_WIRING_LOCATIONS", {"only": settings})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _report(self, rules: list[dict]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = query.hooks_report(rules, self.root)
        return status, out.getvalue()

    def test_mixed_shapes_count_as_one_claim(self) -> None:
        rules = [_rule("R-0001", [{"script": "tools/hooks/x.py", "events": ["PreToolUse"]}]),
                 _rule("R-0002", ["x.py"])]
        status, text = self._report(rules)
        self.assertIn("rules claiming a hook: 1 distinct script(s)", text)
        self.assertNotIn("UNBACKED", text)
        self.assertEqual(status, 0, text)

    def test_full_path_string_reduces_to_basename(self) -> None:
        status, text = self._report([_rule("R-0003", ["tools/hooks/x.py"])])
        self.assertIn("rules claiming a hook: 1 distinct script(s)", text)
        self.assertEqual(status, 0, text)

    def test_integer_entry_is_a_finding_naming_its_rule(self) -> None:
        rules = [_rule("R-0001", [{"script": "x.py"}]), _rule("R-0009", [7])]
        status, text = self._report(rules)
        self.assertIn("MALFORMED", text)
        self.assertIn("rule R-0009: hook entry of type int is neither a table nor a path", text)
        self.assertEqual(status, 1)

    def test_print_rule_shows_string_and_table_entries(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            query._print_rule(_rule("R-0354", ["tools/gh/gate_ci.py", {"script": "tools/hooks/y.py", "events": ["Stop"]}]))
        self.assertIn("hook: gate_ci.py", out.getvalue())
        self.assertIn("hook: y.py  events=['Stop']", out.getvalue())


if __name__ == "__main__":
    unittest.main()
