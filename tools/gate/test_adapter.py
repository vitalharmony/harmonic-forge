#!/usr/bin/env python3
"""The gate adapter's contract: every failure is BLOCKED, never a silent pass.

ADR-008 AC4's absent-manifest rule is the property under test here. A step that
could not run must be distinguishable from a step that ran and found nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adapter  # noqa: E402

GOOD_MODULE = '''
def sweep(**kwargs):
    return {"status": "pass", "evidence": ["swept", repr(sorted(kwargs))]}


def wrong_shape(**kwargs):
    return "not a finding"


def explodes(**kwargs):
    raise RuntimeError("boom")
'''


def _repo(manifest: dict | None, module: str | None = GOOD_MODULE) -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    if manifest is not None:
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / "gate-adapter.json").write_text(json.dumps(manifest), encoding="utf-8")
    if module is not None:
        (tmp / "scripts").mkdir(exist_ok=True)
        (tmp / "scripts" / "adapter_module.py").write_text(module, encoding="utf-8")
    return tmp


DECLARED = {"residue_sweep": {"module": "scripts/adapter_module.py", "entrypoint": "sweep"}}


class AbsentAdapterIsBlockedTests(unittest.TestCase):
    def test_no_manifest_at_all(self) -> None:
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(None))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("no adapter manifest", result["evidence"][0])

    def test_manifest_without_that_capability(self) -> None:
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo({"lease": {}}))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("not declared", result["evidence"][0])

    def test_capability_without_that_entrypoint(self) -> None:
        manifest = {"lease": {"module": "scripts/adapter_module.py", "check_owner": "sweep"}}
        result = adapter.call("lease", "acquire", cwd=_repo(manifest))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("lease.acquire", result["evidence"][0])

    def test_a_malformed_manifest_is_not_an_empty_one(self) -> None:
        tmp = _repo({})
        (tmp / ".claude" / "gate-adapter.json").write_text("{not json", encoding="utf-8")
        result = adapter.call("residue_sweep", "entrypoint", cwd=tmp)
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("could not be read", result["evidence"][0])

    def test_missing_module_file(self) -> None:
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(DECLARED, module=None))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("not found", result["evidence"][0])

    def test_a_module_path_escaping_the_repo_is_refused(self) -> None:
        manifest = {"residue_sweep": {"module": "../../etc/passwd.py", "entrypoint": "sweep"}}
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(manifest))
        self.assertEqual(result["status"], adapter.BLOCKED)

    def test_outside_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.call("residue_sweep", "entrypoint", cwd=Path(tmp))
        self.assertEqual(result["status"], adapter.BLOCKED)


class AdapterCallTests(unittest.TestCase):
    def test_a_declared_entrypoint_runs_and_its_finding_passes_through(self) -> None:
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(DECLARED),
                              issue=7, target_sha="abc", report_only=True)
        self.assertEqual(result["status"], adapter.PASS)
        self.assertIn("swept", result["evidence"])
        # The platform's fixed kwargs reach the adapter module.
        self.assertIn("issue", result["evidence"][1])
        self.assertIn("target_sha", result["evidence"][1])
        self.assertIn("report_only", result["evidence"][1])

    def test_an_entrypoint_that_raises_is_blocked_not_failed(self) -> None:
        manifest = {"residue_sweep": {"module": "scripts/adapter_module.py", "entrypoint": "explodes"}}
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(manifest))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("RuntimeError: boom", result["evidence"][0])

    def test_a_wrong_return_shape_is_blocked(self) -> None:
        manifest = {"residue_sweep": {"module": "scripts/adapter_module.py", "entrypoint": "wrong_shape"}}
        result = adapter.call("residue_sweep", "entrypoint", cwd=_repo(manifest))
        self.assertEqual(result["status"], adapter.BLOCKED)
        self.assertIn("not {\"status\"", result["evidence"][0])

    def test_declared_returns_data_only_entries(self) -> None:
        manifest = {"tier_w_message": {"text": "hello"}}
        self.assertEqual(adapter.declared("tier_w_message", _repo(manifest)), {"text": "hello"})
        self.assertIsNone(adapter.declared("tier_w_message", _repo({})))


class SchemaTests(unittest.TestCase):
    def test_the_schema_ships_and_declares_every_capability_optional(self) -> None:
        schema = json.loads(adapter.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["additionalProperties"], False)
        self.assertNotIn("required", schema)
        for key in ("residue_sweep", "lease", "merge_target_check", "tier_w_message"):
            self.assertIn(key, schema["properties"])


if __name__ == "__main__":
    unittest.main()
