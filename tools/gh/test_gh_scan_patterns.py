"""harmonic-forge#650 -- gh_scan_patterns classification, budget cache,
lock-contention behavior, and the one-shot override.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import gh_scan_patterns as gsp  # noqa: E402


class ScanReason(unittest.TestCase):
    def test_issue_list_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["issue", "list"]))

    def test_pr_list_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["pr", "list"]))

    def test_search_subcommand_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["search", "issues", "foo"]))

    def test_project_item_list_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["project", "item-list", "1", "--owner", "x"]))

    def test_api_issues_list_with_paginate_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["api", "repos/o/r/issues", "--paginate"]))

    def test_api_issues_list_with_query_string_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["api", "repos/o/r/issues?state=open"]))

    def test_api_pulls_list_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["api", "repos/o/r/pulls"]))

    def test_api_repo_wide_comments_list_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["api", "repos/o/r/issues/comments"]))

    def test_api_search_path_is_a_scan(self):
        self.assertIsNotNone(gsp.scan_reason(["api", "search/issues"]))

    def test_graphql_search_query_separate_token_is_a_scan(self):
        self.assertIsNotNone(
            gsp.scan_reason(["api", "graphql", "-f", 'query={search(query:"x"){nodes{id}}}'])
        )

    def test_graphql_search_query_glued_token_is_a_scan(self):
        self.assertIsNotNone(
            gsp.scan_reason(["api", "graphql", '-fquery={search(query:"x"){nodes{id}}}'])
        )

    def test_single_issue_read_is_not_a_scan(self):
        self.assertIsNone(gsp.scan_reason(["api", "repos/o/r/issues/123"]))

    def test_single_issue_comments_is_not_a_scan(self):
        self.assertIsNone(gsp.scan_reason(["api", "repos/o/r/issues/123/comments"]))

    def test_project_item_add_is_not_a_scan(self):
        self.assertIsNone(gsp.scan_reason(["project", "item-add", "1"]))

    def test_graphql_without_search_is_not_a_scan(self):
        self.assertIsNone(gsp.scan_reason(["api", "graphql", "-f", "query={viewer{login}}"]))

    def test_unrelated_commands_are_not_scans(self):
        for argv in ([], ["auth", "status"], ["--version"], ["pr", "view", "5"]):
            with self.subTest(argv=argv):
                self.assertIsNone(gsp.scan_reason(argv))


class Budget(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch_cache_dir = mock.patch.object(gsp, "_CACHE_DIR", Path(self._tmpdir.name))
        self._patch_budget_file = mock.patch.object(
            gsp, "_BUDGET_FILE", Path(self._tmpdir.name) / "gh_core_budget.json")
        self._patch_lock_file = mock.patch.object(
            gsp, "_BUDGET_LOCK_FILE", Path(self._tmpdir.name) / "gh_core_budget.json.lock")
        self._patch_cache_dir.start()
        self._patch_budget_file.start()
        self._patch_lock_file.start()

    def tearDown(self):
        self._patch_cache_dir.stop()
        self._patch_budget_file.stop()
        self._patch_lock_file.stop()
        self._tmpdir.cleanup()

    def test_403_response_still_parses_headers(self):
        """A 403 rate-limit response still carries the X-Ratelimit-* headers
        in `gh api -i`'s stdout, regardless of exit code."""
        fake_result = mock.Mock(
            stdout=(
                "HTTP/2.0 403 Forbidden\r\n"
                "X-Ratelimit-Remaining: 0\r\n"
                "X-Ratelimit-Reset: 1234567890\r\n"
                "\r\n{\"message\": \"rate limited\"}"
            ),
            returncode=1,
        )
        with mock.patch.object(gsp, "_fetch_budget_live", wraps=gsp._fetch_budget_live), \
             mock.patch("subprocess.run", return_value=fake_result):
            result = gsp.budget()
        self.assertEqual(result, (0, 1234567890.0))

    def test_stale_cache_triggers_refetch(self):
        gsp._atomic_write_json(gsp._BUDGET_FILE, {
            "remaining": 500, "reset_epoch": 111.0, "fetched_at": time.time() - 120,
        })
        with mock.patch.object(gsp, "_fetch_budget_live", return_value=(4000, 222.0)) as fetch:
            result = gsp.budget()
        fetch.assert_called_once()
        self.assertEqual(result, (4000, 222.0))

    def test_fresh_cache_does_not_refetch(self):
        gsp._atomic_write_json(gsp._BUDGET_FILE, {
            "remaining": 4000, "reset_epoch": 333.0, "fetched_at": time.time(),
        })
        with mock.patch.object(gsp, "_fetch_budget_live") as fetch:
            result = gsp.budget()
        fetch.assert_not_called()
        self.assertEqual(result, (4000, 333.0))

    def test_lock_contention_second_caller_reads_freshly_written_cache(self):
        """Simulate two 'processes' racing on a stale/absent cache: the first
        writes under the lock, the second re-checks under the lock and reads
        that write instead of re-fetching."""
        call_count = {"n": 0}

        def fake_fetch_budget_live():
            call_count["n"] += 1
            # The first caller's fetch writes the cache as a side effect of
            # budget() itself; simulate a second caller entering after that
            # write has already landed by pre-seeding the cache here.
            gsp._atomic_write_json(gsp._BUDGET_FILE, {
                "remaining": 999, "reset_epoch": 444.0, "fetched_at": time.time(),
            })
            return 999, 444.0

        with mock.patch.object(gsp, "_fetch_budget_live", side_effect=fake_fetch_budget_live):
            first = gsp.budget()
            second = gsp.budget()  # cache is now fresh; must not refetch
        self.assertEqual(first, (999, 444.0))
        self.assertEqual(second, (999, 444.0))
        self.assertEqual(call_count["n"], 1)

    def test_unparseable_headers_return_none(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="garbage", returncode=1)):
            self.assertIsNone(gsp._fetch_budget_live())


class Override(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            gsp, "_OVERRIDE_FILE", Path(self._tmpdir.name) / "gh_scan_override")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_absent_override_is_false(self):
        self.assertFalse(gsp.consume_override())

    def test_fresh_override_consumed_exactly_once(self):
        gsp._OVERRIDE_FILE.write_text("")
        self.assertTrue(gsp.consume_override())
        self.assertFalse(gsp._OVERRIDE_FILE.exists())
        self.assertFalse(gsp.consume_override())

    def test_stale_override_is_deleted_and_treated_as_absent(self):
        gsp._OVERRIDE_FILE.write_text("")
        old_time = time.time() - gsp._OVERRIDE_MAX_AGE_SECONDS - 60
        os.utime(gsp._OVERRIDE_FILE, (old_time, old_time))
        self.assertFalse(gsp.consume_override())
        self.assertFalse(gsp._OVERRIDE_FILE.exists())


if __name__ == "__main__":
    unittest.main()
