"""harmonic-forge#1382 cluster verdict C1: one definition of the current
release. R-0404 (`l1_post.current_release_milestone`), the parking test in
`gh_issue` and the belt's `@current-release` resolver must agree on every title
list, across a turnover and on patch milestones."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gh_issue  # noqa: E402
import l1_post  # noqa: E402
import release_milestone  # noqa: E402
import watch_lane_posts  # noqa: E402

ROWS = (
    (["0.3", "Later"], "0.3"),
    (["0.4", "Later"], "0.4"),
    (["3.2.1", "3.3", "Platform"], "3.2.1"),
    (["3.9", "3.10"], "3.9"),
    (["Later"], None),
    ([], None),
    (["v3.0", "3.0-rc", "3.1"], "3.1"),
)


def _belt(titles):
    body = json.dumps([{"title": t} for t in titles])
    resp = mock.Mock(status=200, body=body)
    watch_lane_posts._TICK_RELEASE.clear()
    try:
        with mock.patch.object(watch_lane_posts._etag_cache, "conditional_get", return_value=resp):
            return watch_lane_posts._current_release("o/r")
    finally:
        watch_lane_posts._TICK_RELEASE.clear()


class OneDefinition(unittest.TestCase):
    def test_every_reader_agrees(self):
        for titles, want in ROWS:
            with self.subTest(titles=titles):
                self.assertEqual(release_milestone.current_release(titles), want)
                self.assertEqual(l1_post.current_release_milestone(titles), want)
                self.assertEqual(_belt(titles), want)

    def test_the_filing_gate_turns_over_with_the_belt(self):
        """After 0.3 closes, 0.4 owes a handoff (not parking) and Later parks."""
        titles = ["0.4", "Later"]
        self.assertFalse(gh_issue.is_parking_milestone("vitalharmony/harmonic-forge", "0.4", titles))
        self.assertTrue(gh_issue.is_parking_milestone("vitalharmony/harmonic-forge", "Later", titles))
        self.assertEqual(_belt(titles), "0.4")


if __name__ == "__main__":
    unittest.main()
