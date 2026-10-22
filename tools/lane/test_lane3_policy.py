#!/usr/bin/env python3
"""harmonic-forge#1156 AC2: the Lane 3 policy trust helper, both ways."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lane3_policy as policy  # noqa: E402


def stat_as(uid):
    def lstat(path):
        return SimpleNamespace(st_uid=uid)
    return lstat


def absent(path):
    raise FileNotFoundError(path)


class Helper(unittest.TestCase):
    def test_the_registry_names_the_installed_path_and_root(self):
        path, owner = policy._registry()
        self.assertEqual(path, "/usr/local/share/hrse-gate/claude-lane3.json")
        self.assertEqual(owner, 0)

    def test_root_owned_is_trusted(self):
        self.assertIsNone(policy.lane3_policy_untrusted(lstat=stat_as(0)))

    def test_absent_is_trusted(self):
        """The launcher launches without a policy, so nothing is refused."""
        self.assertIsNone(policy.lane3_policy_untrusted(lstat=absent))

    def test_another_uid_names_the_path_and_the_uid(self):
        reason = policy.lane3_policy_untrusted(lstat=stat_as(65534))
        self.assertIn("/usr/local/share/hrse-gate/claude-lane3.json", reason)
        self.assertIn("65534", reason)

    def test_the_decorator_skips_only_where_untrusted(self):
        ran = []

        class Case(unittest.TestCase):
            @policy.needs_trusted_lane3_policy
            def test_it(self):
                ran.append(True)

        for value, skipped in ((None, 0), ("untrusted", 1)):
            ran.clear()
            original = policy.lane3_policy_untrusted
            policy.lane3_policy_untrusted = lambda value=value: value
            try:
                result = unittest.TestResult()
                Case("test_it").run(result)
            finally:
                policy.lane3_policy_untrusted = original
            with self.subTest(value=value):
                self.assertEqual(len(result.skipped), skipped)
                self.assertEqual(bool(ran), not skipped)


if __name__ == "__main__":
    unittest.main()
