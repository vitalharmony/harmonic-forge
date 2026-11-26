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


def _rule(rule_id: str, hooks: object) -> dict:
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

    def _agent(self, frontmatter: str) -> None:
        (self.root / "agents").mkdir(exist_ok=True)
        (self.root / "agents" / "a.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")

    def test_mixed_shapes_count_as_one_claim(self) -> None:
        rules = [_rule("R-0001", [{"script": "tools/hooks/x.py", "events": ["PreToolUse"]}]),
                 _rule("R-0002", ["x.py"])]
        claimed, malformed = query.claims_by_script(rules)
        self.assertEqual(dict(claimed), {"x.py": {"R-0001", "R-0002"}})
        self.assertEqual(malformed, [])
        status, text = self._report(rules)
        self.assertIn("rules claiming a hook: 1 distinct script(s)", text)
        self.assertNotIn("UNBACKED", text)
        self.assertEqual(status, 0, text)

    def test_each_mixed_claim_is_reported_when_unwired(self) -> None:
        rules = [_rule("R-0001", [{"script": "tools/hooks/z.py"}]), _rule("R-0002", ["z.py"])]
        status, text = self._report(rules)
        self.assertIn("  z.py  claimed by ['R-0001', 'R-0002']", text)
        self.assertEqual(status, 1)

    def test_frontmatter_prose_is_not_wiring(self) -> None:
        self._agent("description: refuses until `tools/gh/l1_post.py` runs\nhooks:\n  PreToolUse:\n"
                    "    - hooks:\n        - type: command\n          command: python3 \"$HOME/f/tools/hooks/d.py\"")
        found = query._scripts_in_agent_frontmatter(self.root)
        self.assertEqual(set(found), {"d.py"})
        status, text = self._report([_rule("R-0001", ["x.py", "d.py"]), _rule("R-0335", ["tools/gh/l1_post.py"])])
        self.assertIn("  l1_post.py  claimed by ['R-0335']", text)
        self.assertEqual(status, 1)

    def test_full_path_string_reduces_to_basename(self) -> None:
        self.assertEqual(set(query.live_wiring(self.root)["only"]), {"x.py"})
        self.assertEqual(dict(query.claims_by_script([_rule("R-0003", ["tools/hooks/x.py"])])[0]), {"x.py": {"R-0003"}})
        status, text = self._report([_rule("R-0003", ["tools/hooks/x.py"])])
        self.assertIn("rules claiming a hook: 1 distinct script(s)", text)
        self.assertNotIn("UNBACKED", text)
        self.assertEqual(status, 0, text)

    def test_integer_entry_is_a_finding_naming_its_rule(self) -> None:
        rules = [_rule("R-0001", [{"script": "x.py"}]), _rule("R-0009", [7])]
        status, text = self._report(rules)
        self.assertIn("MALFORMED", text)
        self.assertIn("rule R-0009: hook entry of type int is neither a table nor a path", text)
        self.assertEqual(status, 1)

    def test_malformed_tables_and_fields_are_findings(self) -> None:
        cases = {
            "R-0011": ([{"path": "tools/hooks/x.py"}], "hook table has no non-empty string `script` (NoneType)"),
            "R-0012": ([{"script": 7}], "hook table has no non-empty string `script` (int)"),
            "R-0013": ("gate_ci.py", "`hooks` field of type str is not a list"),
            "R-0014": ({"script": "x.py", "events": ["Stop"]}, "`hooks` field of type dict is not a list"),
            "R-0015": (3, "`hooks` field of type int is not a list"),
            "R-0016": (False, "`hooks` field of type bool is not a list"),
        }
        for rule_id, (hooks, message) in cases.items():
            with self.subTest(rule_id):
                rules = [_rule("R-0001", ["x.py"]), _rule(rule_id, hooks)]
                status, text = self._report(rules)
                self.assertIn(f"rule {rule_id}: {message}", text)
                self.assertIn("rules claiming a hook: 1 distinct script(s)", text)
                self.assertEqual(status, 1)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    found = query._print_rule(rules[1])
                self.assertEqual(found, [f"rule {rule_id}: {message}"])
                self.assertIn(f"MALFORMED: rule {rule_id}: {message}", out.getvalue())
                self.assertNotIn("hook:", out.getvalue())

    def test_id_exits_1_on_a_malformed_rule(self) -> None:
        registry = self.root / "registry.toml"
        registry.write_text('[[rule]]\nid = "R-0013"\nfile = "f"\nstatement = "s"\nhooks = "gate_ci.py"\n', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["q", "--registry", str(registry), "--id", "R-0013"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(query.main(), 1)
        self.assertIn("MALFORMED: rule R-0013", out.getvalue())

    def test_print_rule_shows_string_and_table_entries(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            query._print_rule(_rule("R-0354", ["tools/gh/gate_ci.py", {"script": "tools/hooks/y.py", "events": ["Stop"]}]))
        self.assertIn("hook: tools/gh/gate_ci.py\n", out.getvalue())
        self.assertIn("hook: tools/hooks/y.py  events=['Stop']", out.getvalue())


if __name__ == "__main__":
    unittest.main()
