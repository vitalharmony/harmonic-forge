"""Tests for resolve_model.py (harmonic-forge#938)."""

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

import resolve_model as rm

# Today's catalog shape (2026-10-07), `upgrade` written explicitly as null,
# plus two priority-0 Sol entries the visibility and upgrade filters must skip.
# The Sol rows are NOT in priority order, so taking the first match fails.
CATALOG = {"models": [
    {"slug": "gpt-5.6-sol", "visibility": "list", "priority": 5, "upgrade": None},
    {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1, "upgrade": None},
    {"slug": "gpt-6-sol", "visibility": "list", "priority": 3, "upgrade": None},
    {"slug": "gpt-6-luna", "visibility": "list", "priority": 4, "upgrade": None},
    {"slug": "gpt-5.6-luna", "visibility": "list", "priority": 9, "upgrade": None},
    {"slug": "gpt-5.6-terra", "visibility": "list", "priority": 7, "upgrade": None},
    {"slug": "gpt-5.5", "visibility": "hide", "priority": 13,
     "upgrade": {"model": "gpt-6.1-sol"}},
    {"slug": "gpt-6.2-sol", "visibility": "hide", "priority": 0, "upgrade": None},
    {"slug": "gpt-6.3-sol", "visibility": "list", "priority": 0,
     "upgrade": {"model": "gpt-6.1-sol"}},
]}


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(["codex"], returncode, stdout, "boom")


class _Base(unittest.TestCase):
    def setUp(self):
        rm.list_codex.cache_clear()
        self.addCleanup(rm.list_codex.cache_clear)
        patcher = mock.patch.object(rm.subprocess, "run",
                                    return_value=_completed(json.dumps(CATALOG)))
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rm.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CodexSelectionTests(_Base):
    def test_sol_is_lowest_priority_listed_current(self):
        # Also proves the hidden gpt-6.2-sol and upgraded gpt-6.3-sol (both
        # priority 0) are skipped, and that highest-rank is not chosen.
        self.assertEqual(rm.resolve("sol"), "gpt-6.1-sol")

    def test_luna(self):
        self.assertEqual(rm.resolve("luna"), "gpt-6-luna")

    def test_terra(self):
        # Resolves only because `upgrade: null` reads as no upgrade.
        self.assertEqual(rm.resolve("terra"), "gpt-5.6-terra")

    def test_family_with_no_current_model_is_lookup_error(self):
        with self.assertRaises(LookupError):
            rm.resolve("astra")

    def test_list_runs_the_binary_once(self):
        code, out, _ = self.cli("--list")
        self.assertEqual(code, 0)
        self.assertEqual(self.run_mock.call_count, 1)
        self.assertIn("sol\tgpt-6.1-sol", out.splitlines())
        self.assertIn("astra\t-", out.splitlines())


class AliasTests(_Base):
    def test_opus(self):
        self.assertEqual(rm.resolve("opus"), "opus")
        self.run_mock.assert_not_called()

    def test_gemini_pro(self):
        self.assertEqual(rm.resolve("gemini-pro"), "gemini-pro-latest")


class CliTests(_Base):
    def test_sol_prints_id(self):
        self.assertEqual(self.cli("sol")[:2], (0, "gpt-6.1-sol\n"))

    def test_unknown_family_exits_2(self):
        self.assertEqual(self.cli("nosuch")[0], 2)

    def test_lister_failure_exits_1(self):
        self.run_mock.return_value = _completed("", returncode=3)
        code, out, err = self.cli("sol")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("exited 3", err)

    def test_lister_timeout_exits_1(self):
        self.run_mock.side_effect = subprocess.TimeoutExpired("codex", 30)
        self.assertEqual(self.cli("sol")[0], 1)


class RegistryTests(unittest.TestCase):
    def test_every_family_maps_to_a_registered_provider(self):
        for family, provider in rm.FAMILIES.items():
            with self.subTest(family=family):
                self.assertIn(provider, rm.PROVIDERS)
                self.assertIsNotNone(rm.PROVIDERS[provider])

    def test_alias_providers_cover_their_families(self):
        for family, provider in rm.FAMILIES.items():
            shape = rm.PROVIDERS[provider]
            if "aliases" in shape:
                with self.subTest(family=family):
                    self.assertIn(family, shape["aliases"])

    def test_documented_stub_provider(self):
        self.assertIn("example", rm.PROVIDERS)
        self.assertIsNone(rm.PROVIDERS["example"])


if __name__ == "__main__":
    unittest.main()
