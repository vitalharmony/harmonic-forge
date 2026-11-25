#!/usr/bin/env python3
"""harmonic-forge#1564 sticky-wicket PATCH: one test group per category rule.

(a) every thread signal the deferral acts on is bound to the recorded login by
    `deferral_store.same_login` (casefolded); an entry with no login binds no
    thread signal (post-verdict #1, superseding N3).
(b) the release and the anchor each fullmatch `_first_raw_line`: the first
    non-blank raw line, `""` for indented code (N2).
(c) `deferral_store.valid_key` is the one key rule (N5); its record_deferral.py
    tests live in `test_record_deferral.py`.
(d) one post-loop filter in `queue_cycle` drops and retracts a deferral item
    with no fresh read and no store entry, whichever branch carried it (N4).

The real `_deferral_trigger`, `discover_queue` and `queue_cycle` run on
fixture threads; only GitHub reads are patched, and every store write goes to
a temp directory."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import belt_candidates  # noqa: E402
import deferral_store  # noqa: E402
import watch_lane_posts  # noqa: E402

REPO = "vitalharmony/hrse"
N = 2764
OTHER = 2765
OP = "vitalharmony"
MARKER = "deferral-trigger owes=handoff"
DEFERRAL = ("## R-0039 filing-bar exception: `blocked-on-prerequisite`\n\n"
            "No Lane 1 handoff accompanies this filing.\n")
HANDOFF = ("## Handoff: H2764\n\nScope.\n\n"
           "<!-- l1-post v1; kind=handoff; plan-first=false; sha=abc -->")
LANE1 = "Not released yet.\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1; sha=abc -->"


def c(cid, body, login=OP):
    return {"id": cid, "body": body, "user": {"login": login}}


def trigger(*comments, login=OP):
    return watch_lane_posts._deferral_trigger([c(1, DEFERRAL), *comments], login)


class SameLoginRuleTests(unittest.TestCase):
    """(a) the anchor, release, handoff discharge and LANE1 clear."""

    def test_same_login_is_casefolded_and_never_matches_an_empty_login(self):
        self.assertTrue(deferral_store.same_login("VitalHarmony", "vitalharmony"))
        for a, b in (("", ""), (None, None), ("x", None), ("x", "y")):
            self.assertFalse(deferral_store.same_login(a, b))

    def test_a_differently_cased_author_anchors_and_releases(self):
        comments = [c(1, DEFERRAL, "VitalHarmony"), c(2, "go", "VITALHARMONY")]
        self.assertEqual(watch_lane_posts._deferral_trigger(comments, OP), ("queued", 2))

    def test_a_handoff_footer_by_another_login_does_not_discharge(self):
        self.assertEqual(trigger(c(2, "go"), c(3, HANDOFF, "kytxii")), ("queued", 2))

    def test_a_lane1_footer_by_another_login_does_not_clear(self):
        self.assertEqual(trigger(c(2, "go"), c(3, LANE1, "kytxii")), ("queued", 2))

    def test_the_recorded_logins_own_handoff_and_clear_still_act(self):
        self.assertEqual(trigger(c(2, "go"), c(3, HANDOFF, "VitalHarmony")), ("discharged", None))
        self.assertEqual(trigger(c(2, "go"), c(3, LANE1, "VitalHarmony")), ("cleared", None))

    def test_an_entry_with_no_login_binds_no_thread_signal(self):
        """Post-verdict #1 (supersedes N3): a legacy loginless entry is never
        discharged, cleared or released by a thread comment from any account;
        its anchor is still found, and a thread without one is still
        `no-anchor`."""
        for login in ("kytxii", OP):
            with self.subTest(login=login):
                self.assertEqual(trigger(c(3, HANDOFF, login), login=None), ("cleared", None))
                self.assertEqual(trigger(c(2, "go", login), login=None), ("cleared", None))
        self.assertEqual(watch_lane_posts._deferral_trigger([c(2, HANDOFF)], None),
                         ("no-anchor", None))


class RawLineRuleTests(unittest.TestCase):
    """(b) one raw-line helper, fullmatched, for the release and the anchor."""

    def test_indented_code_is_no_release(self):
        for body in ("    go", "\tgo", "  \tgo", "\n\n     go E3"):
            with self.subTest(body=body):
                self.assertEqual(watch_lane_posts._first_raw_line(body), "")
                self.assertEqual(trigger(c(2, body)), ("cleared", None))

    def test_up_to_three_leading_spaces_and_a_crlf_body_release(self):
        for body in ("   go", "go\r\n\r\nthanks", "\r\n\r\ngo E3\r\n", "go  "):
            with self.subTest(body=body):
                self.assertEqual(trigger(c(2, body)), ("queued", 2))

    def test_go_followed_by_text_on_its_line_is_no_release(self):
        for body in ("go ahead", "go\tE3 now", "> go", "```\ngo\n```", "Thanks. go"):
            with self.subTest(body=body):
                self.assertEqual(trigger(c(2, body)), ("cleared", None))

    def test_a_heading_then_prose_reply_is_no_anchor(self):
        """Pass 2 #6: the heading must be the whole first raw line, so a reply
        opening with it and running on into prose never becomes the newest
        anchor and never erases the pending `go`."""
        reply = DEFERRAL.split("\n", 1)[0] + " -- still waiting, see above"
        self.assertEqual(trigger(c(2, "go"), c(3, reply)), ("queued", 2))
        self.assertEqual(watch_lane_posts._deferral_trigger([c(1, reply), c(2, "go")], OP),
                         ("no-anchor", None))

    def test_the_real_deferral_comment_in_a_crlf_body_anchors(self):
        self.assertEqual(watch_lane_posts._deferral_trigger(
            [c(1, DEFERRAL.replace("\n", "\r\n")), c(2, "go")], OP), ("queued", 2))


class ValidKeyRuleTests(unittest.TestCase):
    """(c) the one validator; record_deferral.py's own use is in its tests."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(deferral_store, "DEFAULT_CANDIDATES_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_key_canonicalizes_and_rejects_every_non_positive_or_non_int_issue(self):
        self.assertEqual(deferral_store.valid_key("vitalharmony/HRSE", 5), (REPO, 5))
        for repo, issue in ((REPO, 0), (REPO, -5), (REPO, True), (REPO, 5.0), (REPO, "5"),
                            ("not a repo", 5), (None, 5)):
            with self.subTest(repo=repo, issue=issue):
                self.assertIsNone(deferral_store.valid_key(repo, issue))

    def test_every_store_function_goes_through_it(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(deferral_store.record_deferral(REPO, 0, "backfill", operator_login=OP))
            self.assertFalse(deferral_store.record_deferral(REPO, True, "backfill", operator_login=OP))
        self.assertTrue(deferral_store.record_deferral(REPO, 1, "backfill", operator_login=OP))
        self.assertIsNone(deferral_store.deferral_entry(REPO, True))
        self.assertFalse(deferral_store.discharge_deferral(REPO, True, "x"))
        self.assertEqual(deferral_store.read_deferrals([REPO]), {(REPO, 1)})

    def test_a_dot_git_repo_key_round_trips_under_either_spelling(self):
        """Post-verdict #3: one normalizer, to a fixed point, on the writer
        and on every reader lookup, bulk and direct."""
        self.assertTrue(deferral_store.record_deferral("vitalharmony/repo.GIT", 1, "backfill",
                                                       operator_login=OP))
        for spelling in ("vitalharmony/repo.GIT", "vitalharmony/repo.git", "vitalharmony/repo"):
            with self.subTest(spelling=spelling):
                self.assertEqual(deferral_store.read_deferrals([spelling]), {(spelling, 1)})
                self.assertIsNotNone(deferral_store.deferral_entry(spelling, 1))

    def test_the_repo_normalizer_runs_to_a_true_fixed_point(self):
        """Final post-verdict #2: three passes, and nine suffixes the manifest
        normalizer accepts, each land on the one key."""
        for spelling in ("vitalharmony/repo.GIT.GIT", "vitalharmony/repo" + ".git" * 9,
                         "vitalharmony/Repo" + ".GIT" * 9):
            with self.subTest(spelling=spelling):
                self.assertTrue(deferral_store.normalize_manifest_repo(spelling))
                self.assertEqual(deferral_store.valid_key(spelling, 1), ("vitalharmony/repo", 1))

    def _store_raw(self, issue, repo=REPO):
        path = deferral_store._deferral_path(REPO, 5, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"repo": repo, "issue": issue, "operator_login": OP}),
                        encoding="utf-8")

    def test_the_direct_reader_returns_only_the_requested_key(self):
        """Final post-verdict #3: a valid payload for another key, stored under
        the requested key's file, is not the requested entry."""
        for repo, issue in ((REPO, 6), ("vitalharmony/other", 5)):
            with self.subTest(repo=repo, issue=issue):
                self._store_raw(issue, repo)
                self.assertEqual(deferral_store.read_deferrals([repo]), {(repo, issue)})
                self.assertIsNone(deferral_store.deferral_entry(REPO, 5))

    def test_the_direct_reader_rejects_a_stored_float_or_string_issue(self):
        """Post-verdict #4: the direct reader validates the stored payload
        through `valid_key`, as the bulk reader does."""
        for issue in (5.0, "5", True, 0):
            with self.subTest(issue=issue), contextlib.redirect_stderr(io.StringIO()):
                self._store_raw(issue)
                self.assertIsNone(deferral_store.deferral_entry(REPO, 5))
        self._store_raw(5)
        self.assertEqual(deferral_store.deferral_entry(REPO, 5)["issue"], 5)

    def test_the_bulk_reader_never_coerces_a_stored_issue(self):
        """Post-verdict #8: rule (c) at the bulk-reader boundary; `int()`
        coercion would admit `5.0` and `"5"`."""
        for issue in (5.0, "5"):
            with self.subTest(issue=issue), contextlib.redirect_stderr(io.StringIO()):
                self._store_raw(issue)
                self.assertEqual(deferral_store.read_deferrals([REPO]), set())
        self._store_raw(5)
        self.assertEqual(deferral_store.read_deferrals([REPO]), {(REPO, 5)})

    def test_the_bulk_reader_rejects_a_stored_boolean_issue(self):
        """Final post-verdict #6: a stored `true` is never issue 1."""
        with contextlib.redirect_stderr(io.StringIO()):
            self._store_raw(True)
            self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_the_login_is_stored_casefolded(self):
        deferral_store.record_deferral(REPO, N, "backfill", operator_login="VitalHarmony")
        self.assertEqual(deferral_store.deferral_entry(REPO, N)["operator_login"], OP)


class Offers:
    """The `OfferState` surface `queue_cycle` uses, in memory."""
    def __init__(self, keys=()):
        self.entries = {k: {} for k in keys}
    def keys(self): return list(self.entries)
    def get(self, key): return self.entries.get(key)
    def remove(self, key): self.entries.pop(key, None)
    def suppresses(self, *_): return False
    def record(self, key, _marker, comment_id, *_): self.entries[key] = {"comment_id": comment_id}


class CarryForwardRuleTests(unittest.TestCase):
    """(d) one filter after the repo loop, whichever branch carried the item."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "candidates"
        for patcher in (patch.object(belt_candidates, "DEFAULT_CANDIDATES_DIR", base),
                        patch.object(deferral_store, "DEFAULT_CANDIDATES_DIR", base),
                        patch("watch_lane_posts._sticky_wicket_owed", return_value=False),
                        patch("watch_lane_posts._prune_batch"),
                        patch("watch_lane_posts._issue_meta", return_value=("open", set(), None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        deferral_store.record_deferral(REPO, N, "filing", operator_login=OP)

    def cycle(self, last, comments, pairs, err):
        with patch("watch_lane_posts._fetch_all_comments", return_value=comments), \
             contextlib.redirect_stderr(err):
            return watch_lane_posts.queue_cycle([REPO], "l1", last, "t", candidate_pairs=pairs)

    def test_a_discharged_item_on_a_scoped_belt_is_not_carried_forward(self):
        """Pass 2 #7: a worktree- or issue-scoped belt's healthy tick, after
        the writer discharged the entry and the issue left the candidate set."""
        err = io.StringIO()
        queue1, lines1, _ = self.cycle({}, [c(100, DEFERRAL), c(101, "go")], set(), err)
        self.assertEqual(lines1, [f"{REPO}#{N} queued-for-l1 kind={MARKER}"])
        self.assertTrue(deferral_store.discharge_deferral(REPO, N, "handoff posted"))
        queue2, lines2, ok2 = self.cycle(queue1, [], {(REPO, OTHER)}, err)
        self.assertEqual(ok2, {REPO})
        self.assertNotIn((REPO, N), queue2)
        self.assertEqual(lines2, [])
        self.assertEqual(err.getvalue().count(f"{REPO}#{N} left-queue-for-l1"), 1)

    def test_a_non_deferral_item_on_a_scoped_belt_is_still_carried(self):
        """The filter reads the deferral kind alone: an unchecked other item,
        with no store entry of its own, is carried as before."""
        last = {(REPO, 9): "handoff owes=review"}
        queue, _, _ = self.cycle(last, [], {(REPO, OTHER)}, io.StringIO())
        self.assertEqual(queue[(REPO, 9)], "handoff owes=review")

    def test_a_failed_fetch_on_another_issue_never_masks_a_read_clear(self):
        """Post-verdict #5: retraction is per item. N's clear was read; OTHER's
        fetch failed. N retracts, OTHER carries forward, the repo is not ok."""
        last = {(REPO, N): MARKER, (REPO, OTHER): "handoff owes=review"}
        thread = [c(100, DEFERRAL), c(101, "go"), c(102, LANE1)]
        err = io.StringIO()
        with patch("watch_lane_posts._fetch_all_comments",
                   side_effect=lambda repo, issue: thread if issue == N else None), \
             contextlib.redirect_stderr(err):
            queue, _, ok = watch_lane_posts.queue_cycle(
                [REPO], "l1", last, "t", candidate_pairs={(REPO, OTHER)})
        self.assertEqual(ok, set())
        self.assertNotIn((REPO, N), queue)
        self.assertEqual(queue[(REPO, OTHER)], "handoff owes=review")
        self.assertEqual(err.getvalue().count(f"{REPO}#{N} left-queue-for-l1"), 1)
        self.assertNotIn(f"{REPO}#{OTHER} left-queue-for-l1", err.getvalue())


    def mixed(self, last, thread, offers=None):
        """A mixed-fetch tick: N's thread reads, OTHER's fetch fails."""
        err = io.StringIO()
        with patch("watch_lane_posts._fetch_all_comments",
                   side_effect=lambda repo, issue: thread if issue == N else None), \
             contextlib.redirect_stderr(err):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                [REPO], "l1", last, "t", candidate_pairs={(REPO, OTHER)}, offers=offers)
        self.assertEqual(ok, set())
        return queue, lines, err.getvalue()

    def test_a_read_release_queues_fresh_on_a_mixed_fetch_tick(self):
        """Final post-verdict #4: N's own read queues and offers it."""
        offers = Offers()
        queue, lines, _ = self.mixed({(REPO, OTHER): "handoff owes=review"},
                                     [c(100, DEFERRAL), c(101, "go")], offers)
        self.assertEqual(queue[(REPO, N)], MARKER)
        self.assertEqual(lines, [f"{REPO}#{N} queued-for-l1 kind={MARKER}"])
        self.assertEqual(offers.get((REPO, N)), {"comment_id": 101})

    def test_a_persisted_offer_whose_read_cleared_retracts_on_a_mixed_fetch_tick(self):
        """Final post-verdict #4: an offer an earlier process made, with no
        `last_queue` entry, retracts by N's completed read; OTHER's stays."""
        offers = Offers([(REPO, N), (REPO, OTHER)])
        queue, _, err = self.mixed({}, [c(100, DEFERRAL), c(101, "go"), c(102, LANE1)], offers)
        self.assertNotIn((REPO, N), queue)
        self.assertEqual(offers.keys(), [(REPO, OTHER)])
        self.assertEqual(err.count(f"{REPO}#{N} left-queue-for-l1"), 1)

    def test_a_closed_state_from_the_thread_read_wins_on_a_mixed_fetch_tick(self):
        """Final post-verdict #5: N's first issue read fails and the thread
        read's own issue read reports it closed. N is never queued or offered,
        and its deferral is discharged."""
        thread = [c(100, DEFERRAL), c(101, "go")]
        states = iter([(None, None, None), ("closed", set(), None)])
        offers = Offers()
        with patch("watch_lane_posts._issue_meta", side_effect=lambda repo, issue:
                   next(states) if issue == N else (None, None, None)), \
             patch("watch_lane_posts._read_thread_pages", return_value=thread), \
             patch("watch_lane_posts._retire_imc"), \
             patch.object(watch_lane_posts, "_TICK_READS_ON", False), \
             patch.dict(watch_lane_posts._ISSUE_COMMENTS, {(REPO, N): len(thread)}), \
             contextlib.redirect_stderr(io.StringIO()):
            queue, lines, ok = watch_lane_posts.queue_cycle(
                [REPO], "l1", {(REPO, OTHER): "handoff owes=review"}, "t",
                candidate_pairs={(REPO, OTHER)}, offers=offers)
        self.assertEqual((ok, lines), (set(), []))
        self.assertNotIn((REPO, N), queue)
        self.assertIsNone(offers.get((REPO, N)))
        self.assertIsNone(deferral_store.deferral_entry(REPO, N))


if __name__ == "__main__":
    unittest.main()
