"""harmonic-forge#1393 AC2/AC3: the belt's issue and thread reads are conditional, the page
count comes from the issue's `comments`, and `_fetch_comments` filters the shared thread.

Two layers: the first class fakes `_etag_cache.conditional_get` (thread shape and delivery);
the second runs the real cache and `gh_as_raw` against a fake GitHub (what goes on the wire).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _etag_cache  # noqa: E402
import watch_lane_posts  # noqa: E402
from test_etag_cache import FakeGitHub  # noqa: E402

REPO = "vitalharmony/harmonic-forge"


def comment(i, updated="2026-11-18T00:00:00Z"):
    return {"id": i, "body": f"c{i}", "created_at": "2026-11-17T00:00:00Z", "updated_at": updated}


class ThreadShapeTests(unittest.TestCase):
    """`_fetch_all_comments` against a faked `conditional_get`."""

    def setUp(self):
        self.addCleanup(watch_lane_posts._end_tick_reads)
        self.addCleanup(watch_lane_posts._ISSUE_COMMENTS.clear)
        self.paths = []

    def thread(self, count, pages, *, issue_status=200):
        """Run one tick's thread read. `pages` maps page number -> list of comments."""
        Response = _etag_cache.Response
        self.paths = []

        def cond(account, path, counter=None):
            self.paths.append(path)
            if "/comments" in path:
                n = int(path.rsplit("page=", 1)[1])
                return Response(200, json.dumps(pages.get(n, [])), False)
            body = json.dumps({"state": "open", "labels": [], "milestone": None, "comments": count})
            return Response(issue_status, body, issue_status == 304)

        watch_lane_posts._reset_tick_reads()
        with mock.patch.object(_etag_cache, "conditional_get", side_effect=cond):
            return watch_lane_posts._fetch_all_comments(REPO, 1)

    def pages_read(self):
        return [p for p in self.paths if "/comments" in p]

    def test_an_empty_thread_reads_no_page(self):
        self.assertEqual(self.thread(0, {}), [])
        self.assertEqual(self.pages_read(), [])

    def test_the_page_count_comes_from_the_issues_comment_count(self):
        got = self.thread(101, {1: [comment(i) for i in range(1, 101)], 2: [comment(101)]})
        self.assertEqual([c["id"] for c in got], list(range(1, 102)))
        self.assertEqual(len(self.pages_read()), 2)

    def test_exactly_100_comments_is_one_page_and_101_opens_page_2(self):
        self.thread(100, {1: [comment(i) for i in range(1, 101)]})
        self.assertEqual(len(self.pages_read()), 1)
        self.thread(101, {1: [comment(i) for i in range(1, 101)], 2: [comment(101)]})
        self.assertEqual(len(self.pages_read()), 2)

    def test_pages_are_read_even_when_the_issue_read_is_a_304(self):
        """An edit changes a page's ETag but not the issue's (measured on harmonic-forge#1393)."""
        edited = comment(1, updated="2026-11-18T09:00:00Z")
        got = self.thread(1, {1: [edited]}, issue_status=304)
        self.assertEqual(got[0]["updated_at"], "2026-11-18T09:00:00Z")
        self.assertEqual(len(self.pages_read()), 1)

    def test_a_comment_added_between_two_page_reads_is_neither_lost_nor_doubled(self):
        # the count said 200 (two pages); a comment lands before page 2 is read, shifting
        # nothing on page 1 and adding a 201st that the next tick's count will pick up
        first = self.thread(200, {1: [comment(i) for i in range(1, 101)],
                                  2: [comment(i) for i in range(101, 201)]})
        self.assertEqual([c["id"] for c in first], list(range(1, 201)))
        second = self.thread(201, {1: [comment(i) for i in range(1, 101)],
                                   2: [comment(i) for i in range(101, 201)], 3: [comment(201)]})
        ids = [c["id"] for c in second]
        self.assertEqual(ids, list(range(1, 202)))
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_comment_deleted_between_two_page_reads_is_not_delivered_twice(self):
        # page 1 was read as 1..100, then comment 50 was deleted, so page 2 now starts at 100
        got = self.thread(200, {1: [comment(i) for i in range(1, 101)],
                                2: [comment(i) for i in range(100, 200)]})
        ids = [c["id"] for c in got]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[:100], list(range(1, 101)))

    def test_a_comment_deleted_from_page_1_of_a_101_comment_thread(self):
        before = self.thread(101, {1: [comment(i) for i in range(1, 101)], 2: [comment(101)]})
        self.assertEqual(len(before), 101)
        # comment 1 deleted: the count shrinks to 100, one page, every boundary has moved
        after = self.thread(100, {1: [comment(i) for i in range(2, 102)]})
        ids = [c["id"] for c in after]
        self.assertEqual(ids, list(range(2, 102)))
        self.assertEqual(len(self.pages_read()), 1)

    def test_a_failed_page_fails_the_whole_read_never_a_short_thread(self):
        Response = _etag_cache.Response

        def cond(account, path, counter=None):
            if "/comments" in path:
                return Response(403, None, False)
            return Response(200, json.dumps({"state": "open", "labels": [], "milestone": None,
                                             "comments": 3}), False)
        watch_lane_posts._reset_tick_reads()
        with mock.patch.object(_etag_cache, "conditional_get", side_effect=cond):
            self.assertIsNone(watch_lane_posts._fetch_all_comments(REPO, 1))

    def test_an_unreadable_issue_means_no_thread_not_an_empty_one(self):
        Response = _etag_cache.Response
        watch_lane_posts._reset_tick_reads()
        with mock.patch.object(_etag_cache, "conditional_get", return_value=Response(502, None, False)):
            self.assertIsNone(watch_lane_posts._fetch_all_comments(REPO, 1))

    # the `since` reader filters the same thread
    def test_since_delivers_new_and_edited_comments_and_drops_old_ones(self):
        thread = [comment(1, updated="2026-11-17T00:00:00Z"),
                  comment(2, updated="2026-11-18T05:00:00Z"),          # edited after since
                  comment(3, updated="2026-11-18T08:00:00Z")]          # new
        with mock.patch.object(watch_lane_posts, "_fetch_all_comments", return_value=thread):
            got = watch_lane_posts._fetch_comments(REPO, 1, "2026-11-18T04:00:00Z")
        self.assertEqual([c["id"] for c in got], [2, 3])

    def test_since_does_not_drop_an_edit_by_filtering_on_created_at(self):
        old_but_edited = {"id": 9, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-11-18T07:00:00Z"}
        with mock.patch.object(watch_lane_posts, "_fetch_all_comments", return_value=[old_but_edited]):
            self.assertEqual(len(watch_lane_posts._fetch_comments(REPO, 1, "2026-11-18T00:00:00Z")), 1)

    def test_since_passes_a_failed_thread_read_through_as_none(self):
        with mock.patch.object(watch_lane_posts, "_fetch_all_comments", return_value=None):
            self.assertIsNone(watch_lane_posts._fetch_comments(REPO, 1, "2026-11-18T00:00:00Z"))

    def test_since_outside_a_tick_still_reads_the_whole_thread_never_page_1_only(self):
        watch_lane_posts._end_tick_reads()
        got = self._outside_a_tick_since()
        self.assertEqual([c["id"] for c in got], [101])
        self.assertTrue(any("page=2" in p for p in self.paths))

    def _outside_a_tick_since(self):
        Response = _etag_cache.Response
        self.paths = []
        pages = {1: [comment(i, updated="2026-11-17T00:00:00Z") for i in range(1, 101)],
                 2: [comment(101, updated="2026-11-18T09:00:00Z")]}

        def cond(account, path, counter=None):
            self.paths.append(path)
            if "/comments" in path:
                return Response(200, json.dumps(pages[int(path.rsplit("page=", 1)[1])]), False)
            return Response(200, json.dumps({"state": "open", "labels": [], "milestone": None,
                                             "comments": 101}), False)
        with mock.patch.object(_etag_cache, "conditional_get", side_effect=cond):
            return watch_lane_posts._fetch_comments(REPO, 1, "2026-11-18T00:00:00Z")


class OnTheWireTests(unittest.TestCase):
    """The real cache and `gh_as_raw` against a fake GitHub: a quiet issue costs only 304s."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        _etag_cache._IDENTITY.clear()
        self.addCleanup(_etag_cache._IDENTITY.clear)
        self.addCleanup(watch_lane_posts._end_tick_reads)
        self.gh = FakeGitHub()
        self.gh.logins["vitalharmony"] = "vitalharmony"
        sp = mock.patch("belt_mechanics.subprocess.run", side_effect=self.gh)
        sp.start()
        self.addCleanup(sp.stop)
        meta = {"state": "open", "labels": [], "milestone": None, "comments": 2}
        self.gh.resources[f"repos/{REPO}/issues/1"] = ('W/"i1"', json.dumps(meta))
        self.gh.resources[f"repos/{REPO}/issues/1/comments?per_page=100&page=1"] = (
            'W/"p1"', json.dumps([comment(1), comment(2)]))
        self.before = watch_lane_posts._COUNTER.calls_304

    def tick(self):
        watch_lane_posts._reset_tick_reads()
        return watch_lane_posts._fetch_comments(REPO, 1, "2026-11-18T00:00:00Z")

    def issue_requests(self):
        return [r for r in self.gh.requests if r[1] != "user"]

    def test_an_unchanged_thread_costs_only_conditional_reads_and_no_clock_url(self):
        self.tick()                              # primes the cache
        primed = len(self.issue_requests())
        self.tick()
        second = self.issue_requests()[primed:]
        self.assertEqual(len(second), 2)         # the issue read and one page
        self.assertTrue(all(r[2] for r in second), second)           # every one sent If-None-Match
        self.assertFalse(any("since=" in r[1] for r in self.gh.requests))
        self.assertEqual(watch_lane_posts._COUNTER.calls_304 - self.before, 2)

    def test_the_same_holds_in_the_minutes_after_a_post(self):
        """The old `?since=` URL slid with the clock after any post and could never be conditional."""
        self.tick()
        primed = len(self.issue_requests())
        for _ in range(3):
            self.tick()
        later = self.issue_requests()[primed:]
        self.assertTrue(all(r[2] for r in later))
        self.assertFalse(any("since=" in r[1] for r in self.gh.requests))

    def test_a_new_comment_is_delivered_once_and_the_tick_after_is_all_304s(self):
        self.tick()
        # a third comment lands: the issue count and page 1 both change
        self.gh.resources[f"repos/{REPO}/issues/1"] = (
            'W/"i2"', json.dumps({"state": "open", "labels": [], "milestone": None, "comments": 3}))
        self.gh.resources[f"repos/{REPO}/issues/1/comments?per_page=100&page=1"] = (
            'W/"p2"', json.dumps([comment(1), comment(2), comment(3, updated="2026-11-18T09:00:00Z")]))
        got = self.tick()
        self.assertEqual([c["id"] for c in got], [1, 2, 3])
        primed = len(self.issue_requests())
        self.tick()
        self.assertTrue(all(r[2] for r in self.issue_requests()[primed:]))


if __name__ == "__main__":
    unittest.main()
