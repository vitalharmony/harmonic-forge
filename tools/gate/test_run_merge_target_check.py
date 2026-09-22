#!/usr/bin/env python3
"""The declared-query executor: pass, fail, and the BLOCKED cases.

The distinction under test is the one ADR-008 AC4 cares about: a check that
could not run (no manifest, no connection variable, no database) reports
`blocked`, never `pass`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_merge_target_check as executor  # noqa: E402

DECLARED = {
    "merge_target_check": {
        "query": "MATCH (n:Thing) WHERE elementId(n) = $id RETURN count(n) AS c",
        "connection_env": "TEST_MERGE_TARGET_URI",
        "expected_shape": {"field": "c", "op": "gt", "value": 0},
    }
}


def _repo(manifest: dict | None) -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    if manifest is not None:
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / "gate-adapter.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp


class _Session:
    def __init__(self, rows, raises=None):
        self._rows, self._raises = rows, raises

    def run(self, query, **params):
        if self._raises:
            raise self._raises
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Driver:
    def __init__(self, rows, raises=None):
        self._rows, self._raises, self.closed = rows, raises, False

    def session(self):
        return _Session(self._rows, self._raises)

    def close(self):
        self.closed = True


def _factory(rows, raises=None):
    driver = _Driver(rows, raises)
    return (lambda uri: driver), driver


class ExecutorTests(unittest.TestCase):
    def test_pass_when_the_declared_shape_holds(self) -> None:
        factory, driver = _factory([{"c": 1}])
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "pass")
        self.assertIn("1 gt 0", result["evidence"][0])
        self.assertTrue(driver.closed)

    def test_fail_when_the_value_does_not_satisfy_the_comparison(self) -> None:
        factory, _ = _factory([{"c": 0}])
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "fail")

    def test_fail_when_the_query_returns_no_rows(self) -> None:
        factory, _ = _factory([])
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "fail")
        self.assertIn("no rows", result["evidence"][0])

    def test_fail_when_the_declared_field_is_absent(self) -> None:
        factory, _ = _factory([{"other": 1}])
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "fail")
        self.assertIn("no 'c' field", result["evidence"][0])

    def test_blocked_when_the_connection_env_is_unset(self) -> None:
        factory, _ = _factory([{"c": 1}])
        with mock.patch.dict("os.environ", {}, clear=True):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("TEST_MERGE_TARGET_URI is unset", result["evidence"][0])

    def test_blocked_when_not_declared(self) -> None:
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo({}), driver_factory=_factory([])[0])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not declared", result["evidence"][0])

    def test_blocked_when_the_query_raises(self) -> None:
        factory, _ = _factory([], raises=RuntimeError("connection refused"))
        with mock.patch.dict("os.environ", {"TEST_MERGE_TARGET_URI": "bolt://x"}):
            result = executor.run(cwd=_repo(DECLARED), driver_factory=factory)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("connection refused", result["evidence"][0])

    def test_a_type_mismatch_fails_rather_than_crashing(self) -> None:
        held, why = executor.compare("not-a-number", "gt", 0)
        self.assertFalse(held)
        self.assertIn("cannot compare", why)

    def test_every_declared_operator_is_implemented(self) -> None:
        schema = json.loads((HERE.parents[1] / "schemas" / "gate-adapter.schema.json")
                            .read_text(encoding="utf-8"))
        ops = schema["properties"]["merge_target_check"]["properties"]["expected_shape"]["properties"]["op"]["enum"]
        self.assertEqual(sorted(ops), sorted(executor._OPS))


class NotWiredTests(unittest.TestCase):
    def test_no_call_site_invokes_this_executor(self) -> None:
        """harmonic-forge#721: built, tested, deliberately unwired -- no repo
        declares `merge_target_check` yet."""
        root = HERE.parents[1]
        hits = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", "run_merge_target_check"],
            capture_output=True, text=True, check=False,
        ).stdout.split()
        allowed = {"tools/gate/run_merge_target_check.py", "tools/gate/test_run_merge_target_check.py",
                   "schemas/gate-adapter.schema.json"}
        self.assertTrue(set(hits) <= allowed | {p for p in hits if p.startswith("docs/")}, hits)


if __name__ == "__main__":
    unittest.main()
