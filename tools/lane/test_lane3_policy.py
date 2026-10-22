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

    def test_a_misowned_host_policy_is_not_skipped(self):
        """Preclose pass 1: drift on the host fails loudly, never skips."""
        self.assertIsNone(policy.lane3_policy_untrusted(lstat=stat_as(1000), overflow=65534,
                                                        userns=True))

    def test_a_host_file_really_owned_by_the_overflow_uid_is_not_skipped(self):
        """Pass 2: outside a user namespace 65534 is a real owner."""
        self.assertIsNone(policy.lane3_policy_untrusted(lstat=stat_as(65534), overflow=65534,
                                                        userns=False))

    def test_a_read_error_runs_the_tests(self):
        """Pass 2: only absence and a remap are decided; an error is not a remap."""
        def denied(path):
            raise PermissionError(path)
        self.assertIsNone(policy.lane3_policy_untrusted(lstat=denied, userns=True))

    def test_the_identity_map_is_not_a_namespace(self):
        from unittest import mock  # noqa: PLC0415
        with mock.patch.object(policy.Path, "read_text", return_value="         0          0 4294967295\n"):
            self.assertFalse(policy.in_user_namespace())
        with mock.patch.object(policy.Path, "read_text", return_value="         0       1000          1\n"):
            self.assertTrue(policy.in_user_namespace())

    def test_another_uid_names_the_path_and_the_uid(self):
        reason = policy.lane3_policy_untrusted(lstat=stat_as(65534), overflow=65534, userns=True)
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


class TrustCheckCoverage(unittest.TestCase):
    def test_the_trust_check_tests_are_never_skipped(self):
        """harmonic-forge#1156 kill 4: the class that exercises the launcher's
        trust check must run everywhere, sandbox included."""
        import test_lane_launchers  # noqa: PLC0415
        cls = test_lane_launchers.ClaudeLane3Settings
        wrapped = [n for n in dir(cls) if n.startswith("test_")
                   and hasattr(getattr(cls, n), "__wrapped__")]
        self.assertEqual(wrapped, [])

if __name__ == "__main__":
    unittest.main()
