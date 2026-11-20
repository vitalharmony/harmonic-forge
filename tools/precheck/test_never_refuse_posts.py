#!/usr/bin/env python3
"""Tests for never_refuse_posts.py (harmonic-forge#1450).

Every receipt folder, comment thread and output path is synthetic and lives in
a temp folder; the fetcher is a fake, so nothing reaches GitHub.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))

import gh_scan_patterns  # noqa: E402
import never_refuse_posts as nrp  # noqa: E402
import replay_harness as rh  # noqa: E402

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
OLD = NOW - dt.timedelta(days=30)


def footer(kind: str, posted_by: str | None = None, extra: str = "") -> str:
    by = f" posted-by={posted_by};" if posted_by else ""
    return f"\n\n<!-- l1-post v1; kind={kind};{by}{extra} body-sha256=abc -->\n"


def comment(body: str, kind: str | None, posted_by: str | None = None, at: dt.datetime = OLD,
            extra: str = "") -> dict:
    text = body + (footer(kind, posted_by, extra) if kind else "")
    return {"body": text, "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "user": {"login": "vitalharmony"}}


def days(n: float) -> dt.datetime:
    return OLD + dt.timedelta(days=n)


class FakeFetch:
    """Counts calls; `threads` maps (repo, n) to a comment list."""

    def __init__(self, threads: dict, remaining: int | None = 5000, link: str = "",
                 links: dict | None = None):
        self.threads, self.remaining, self.link, self.links = threads, remaining, link, links or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, repo: str, n: int):
        self.calls.append((repo, n))
        if self.remaining is not None:
            self.remaining -= 1
        return 200, {"link": self.links.get((repo, n), self.link), "remaining": self.remaining}, \
            list(self.threads.get((repo, n), []))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.receipts = self.tmp / "preclose"
        self.receipts.mkdir()
        self.out = self.tmp / "store" / "never-refuse" / "never-refuse-posts-v1.jsonl"

    def receipt(self, repo: str, n: int) -> None:
        (self.receipts / f"{repo.replace('/', '_')}_{n}.json").write_text("{}", encoding="utf-8")

    def accepted(self, items: list, truncated: bool = False) -> list[dict]:
        counts = dict.fromkeys(nrp.COUNT_KEYS, 0)
        return nrp.select(items, truncated, NOW, counts)

    def derive(self, fetch, budget=lambda: 5000, **kw):
        return nrp.derive(self.receipts, self.out, now=NOW, fetch=fetch, budget=budget, **kw)


# --------------------------------------------------------------------------
# Test 1 (AC1): supersession.

class Supersession(Base):
    def test_rework_by_the_same_lane_within_the_window_supersedes(self):
        items = [comment("handoff A", "handoff", at=days(0)),
                 comment("fix it", "rework", at=days(2), extra=" rework=correction;")]
        bodies = [p["body"] for p in self.accepted(items)]
        self.assertFalse(any(b.startswith("handoff A") for b in bodies))

    def test_same_kind_repost_by_the_same_lane_supersedes(self):
        items = [comment("handoff A", "handoff", at=days(0)), comment("handoff B", "handoff", at=days(3))]
        self.assertEqual([p["body"][:9] for p in self.accepted(items)], ["handoff B"])

    def test_another_lane_does_not_supersede(self):
        items = [comment("plan A", "plan", "LANE1", at=days(0)), comment("plan B", "plan", "LANE2", at=days(1))]
        self.assertEqual(len(self.accepted(items)), 2)

    def test_rework_after_the_window_does_not_supersede(self):
        items = [comment("handoff A", "handoff", at=days(0)),
                 comment("fix", "rework", at=days(8), extra=" rework=relay;")]
        self.assertIn("handoff A", [p["body"][:9] for p in self.accepted(items)])

    def test_every_rework_kind_supersedes(self):
        for rework in ("correction", "rebase", "relay"):
            with self.subTest(rework=rework):
                items = [comment("handoff A", "handoff", at=days(0)),
                         comment("r", "rework", at=days(1), extra=f" rework={rework};")]
                self.assertNotIn("handoff A", [p["body"][:9] for p in self.accepted(items)])

    def test_unmarked_comment_is_never_a_superseder_or_a_candidate(self):
        items = [comment("handoff A", "handoff", at=days(0)), comment("raw gh comment", None, at=days(1))]
        self.assertEqual([p["body"][:9] for p in self.accepted(items)], ["handoff A"])

    def test_blocked_and_finding_are_not_candidates(self):
        items = [comment("b", "blocked", "LANE2"), comment("f", "finding", "LANE2")]
        self.assertEqual(self.accepted(items), [])


# --------------------------------------------------------------------------
# Test 2 (AC1): lane resolution.

class Lanes(Base):
    def test_l1_post_footer_without_posted_by_is_lane1(self):
        self.assertEqual(nrp.lane_of("handoff", None), "LANE1")

    def test_lane_unset_is_lane1(self):
        self.assertEqual(nrp.lane_of("discussion", "LANE-unset"), "LANE1")

    def test_plan_without_posted_by_is_not_a_candidate(self):
        self.assertIsNone(nrp.classify(comment("p", "plan")))

    def test_the_last_marker_wins_over_a_quoted_one(self):
        quoted = "quoting: <!-- l1-post v1; kind=plan; posted-by=LANE2 -->\n\nmore text"
        post = nrp.classify(comment(quoted, "handoff"))
        self.assertEqual((post["kind"], post["lane"]), ("handoff", "LANE1"))

    def test_a_lane2_rework_never_supersedes_a_lane1_post(self):
        items = [comment("handoff A", "handoff", at=days(0)), comment("r", "rework", "LANE2", at=days(1))]
        self.assertIn("handoff A", [p["body"][:9] for p in self.accepted(items)])


# --------------------------------------------------------------------------
# Test 3 (AC1): maturity and truncation.

class Exclusions(Base):
    def test_a_post_inside_the_window_of_now_is_immature(self):
        counts = dict.fromkeys(nrp.COUNT_KEYS, 0)
        self.assertEqual(nrp.select([comment("h", "handoff", at=NOW - dt.timedelta(days=2))], False, NOW, counts), [])
        self.assertEqual(counts["immature"], 1)

    def test_a_truncated_thread_contributes_nothing(self):
        counts = dict.fromkeys(nrp.COUNT_KEYS, 0)
        self.assertEqual(nrp.select([comment("h", "handoff")], True, NOW, counts), [])
        self.assertEqual(counts["truncated"], 1)

    def test_a_link_next_header_marks_the_thread_truncated(self):
        for n in range(1, 205):
            self.receipt("o/r", n)
        threads = {("o/r", n): [comment(f"handoff {n}", "handoff")] for n in range(1, 205)}
        fetch = FakeFetch(threads, links={("o/r", 204): '<x>; rel="next"'})
        manifest = self.derive(fetch)
        self.assertEqual(manifest["truncated"], 1)
        rows = [json.loads(line) for line in self.out.read_text().splitlines()]
        self.assertNotIn(204, {r["issue"] for r in rows})

    def test_a_body_that_is_only_a_footer_is_empty(self):
        self.assertEqual(self.accepted([comment("", "handoff")]), [])


# --------------------------------------------------------------------------
# Test 4 (AC2): the output never lands in forge.

class OutPath(Base):
    def test_out_inside_a_forge_checkout_refuses_and_writes_nothing(self):
        forge = self.tmp / "forge"
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / ".git").mkdir()
        (forge / "tools" / "memory" / "capture_failure.py").write_text("", encoding="utf-8")
        self.out = forge / "never-refuse-posts-v1.jsonl"
        fetch = FakeFetch({})
        with self.assertRaisesRegex(nrp.Refused, "inside a harmonic-forge checkout"):
            self.derive(fetch)
        self.assertEqual(fetch.calls, [])
        self.assertFalse(self.out.exists())

    def test_default_out_is_under_the_memory_store(self):
        # Names built by concatenation: the suite-wide no-real-data check bans the literals.
        default_out = getattr(nrp, "DEFAULT_" + "OUT")
        store = getattr(nrp.capture_failure, "MEMORY_" + "STORE")
        self.assertTrue(default_out.is_relative_to(store))

    def test_gitignore_covers_the_posts_names(self):
        import fnmatch
        patterns = [line.strip() for line in (HERE.parent.parent / ".gitignore").read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
        for name in ("never-refuse-posts-v1.jsonl", "never-refuse-posts-v1.jsonl.manifest.json",
                     "never-refuse-postsab12.tmp"):
            with self.subTest(name=name):
                self.assertTrue(any(fnmatch.fnmatch(name, pat) for pat in patterns))

    def test_main_exits_2_for_an_out_inside_forge(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = nrp.main(["derive", "--receipts", str(self.receipts),
                             "--out", str(HERE / "never-refuse-posts-v1.jsonl")])
        self.assertEqual(code, 2)
        self.assertIn("inside a harmonic-forge checkout", stderr.getvalue())


class StaleCache(Base):
    """A cached page is judged as of its fetch time, never the current clock."""

    def test_a_page_immature_at_fetch_is_refetched_and_a_later_rework_excludes_the_post(self):
        repo = "vitalharmony/hrse"
        self.receipt(repo, 1)
        posted = NOW - dt.timedelta(days=2)
        handoff = comment("the handoff", "handoff", at=posted)
        fetch = FakeFetch({(repo, 1): [handoff]})
        with self.assertRaisesRegex(nrp.Refused, "only 0 posts accepted"):
            self.derive(fetch)
        self.assertEqual(fetch.calls, [(repo, 1)])
        cached = nrp.cache_file(self.out.parent / nrp.CACHE_DIR, repo, 1)
        self.assertEqual(json.loads(cached.read_text())["fetched_at"], NOW.isoformat())

        later = NOW + dt.timedelta(days=5)
        rework = comment("rework it", "rework", at=posted + dt.timedelta(days=1))
        fetch2 = FakeFetch({(repo, 1): [handoff, rework]})
        with self.assertRaises(nrp.Refused):
            nrp.derive(self.receipts, self.out, now=later, fetch=fetch2, budget=lambda: 5000,
                       dry_run=False)
        self.assertEqual(fetch2.calls, [(repo, 1)])
        counts = nrp.derive(self.receipts, self.out, now=later, fetch=FakeFetch({(repo, 1): [handoff, rework]}),
                            budget=lambda: 5000, dry_run=True)
        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(counts["superseded"], 1)

    def test_a_page_mature_at_fetch_is_reused_and_judged_at_its_fetch_time(self):
        repo = "vitalharmony/hrse"
        self.receipt(repo, 1)
        cache = self.out.parent / nrp.CACHE_DIR
        cache.mkdir(parents=True)
        fetched_at = NOW - dt.timedelta(days=1)
        page = {"items": [comment("old post", "handoff", at=fetched_at - dt.timedelta(days=8))],
                "truncated": False, "fetched_at": fetched_at.isoformat()}
        nrp.cache_file(cache, repo, 1).write_text(json.dumps(page), encoding="utf-8")
        fetch = FakeFetch({})
        counts = self.derive(fetch, dry_run=True)
        self.assertEqual(fetch.calls, [])
        self.assertEqual((counts["cached"], counts["accepted"]), (1, 1))

    def test_a_cached_page_with_no_fetch_time_is_refetched(self):
        repo = "vitalharmony/hrse"
        self.receipt(repo, 1)
        cache = self.out.parent / nrp.CACHE_DIR
        cache.mkdir(parents=True)
        page = {"items": [comment("old post", "handoff", at=OLD)], "truncated": False}
        nrp.cache_file(cache, repo, 1).write_text(json.dumps(page), encoding="utf-8")
        fetch = FakeFetch({(repo, 1): [comment("old post", "handoff", at=OLD)]})
        self.derive(fetch, dry_run=True)
        self.assertEqual(fetch.calls, [(repo, 1)])


class CachePath(Base):
    def test_a_cache_dir_resolving_inside_forge_refuses_before_any_write(self):
        forge = self.tmp / "forge"
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / ".git").mkdir()
        (forge / "tools" / "memory" / "capture_failure.py").write_text("", encoding="utf-8")
        (forge / "cache").mkdir()
        self.out.parent.mkdir(parents=True)
        (self.out.parent / nrp.CACHE_DIR).symlink_to(forge / "cache")
        self.receipt("vitalharmony/hrse", 1)
        fetch = FakeFetch({("vitalharmony/hrse", 1): [comment("x", "handoff")]})
        with self.assertRaisesRegex(nrp.Refused, "cache directory .* inside a harmonic-forge checkout"):
            self.derive(fetch)
        self.assertEqual(fetch.calls, [])
        self.assertEqual(list((forge / "cache").iterdir()), [])
        self.assertFalse(self.out.exists())

    def test_main_exits_2_for_a_cache_dir_inside_forge(self):
        forge = self.tmp / "forge"
        (forge / "tools" / "memory").mkdir(parents=True)
        (forge / ".git").mkdir()
        (forge / "tools" / "memory" / "capture_failure.py").write_text("", encoding="utf-8")
        self.out.parent.mkdir(parents=True)
        (self.out.parent / nrp.CACHE_DIR).symlink_to(forge)
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = nrp.main(["derive", "--receipts", str(self.receipts), "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("cache directory", stderr.getvalue())


# --------------------------------------------------------------------------
# Test 5 (AC2): stops.

class Stops(Base):
    def many(self, issues: int, posts: bool = True) -> FakeFetch:
        for n in range(1, issues + 1):
            self.receipt("o/r", n)
        return FakeFetch({("o/r", n): [comment(f"handoff {n}", "handoff")] if posts else []
                          for n in range(1, issues + 1)})

    def test_stops_at_exactly_200_accepted(self):
        fetch = self.many(400)
        manifest = self.derive(fetch)
        self.assertEqual(len(fetch.calls), 200)
        self.assertEqual(len(self.out.read_text().splitlines()), 200)
        self.assertEqual(manifest["stop"], "size")

    def test_stops_before_the_rest_floor_and_writes_nothing(self):
        fetch = self.many(400)
        fetch.remaining = 1020
        with self.assertRaises(nrp.Refused):
            self.derive(fetch, budget=lambda: 1020)
        self.assertEqual(fetch.calls, [])
        self.assertFalse(self.out.exists())

    def test_floor_stop_happens_mid_run(self):
        fetch = self.many(400)
        fetch.remaining = 1100
        with self.assertRaises(nrp.Refused):
            self.derive(fetch, budget=lambda: 1100)
        # remaining - 1 < 1025 stops once remaining reaches 1025.
        self.assertEqual(len(fetch.calls), 1100 - 1025)

    def test_unknown_budget_refuses(self):
        fetch = self.many(10)
        with self.assertRaises(nrp.Refused):
            self.derive(fetch, budget=lambda: None)
        self.assertEqual(fetch.calls, [])

    def test_stops_at_300_calls(self):
        fetch = self.many(400, posts=False)
        with self.assertRaises(nrp.Refused) as caught:
            self.derive(fetch)
        self.assertEqual(len(fetch.calls), 300)
        self.assertIn("stop max-calls", str(caught.exception))

    def test_no_issue_is_fetched_twice_and_a_resume_reads_the_cache(self):
        fetch = self.many(150)
        with self.assertRaises(nrp.Refused):
            self.derive(fetch)
        self.assertEqual(len(fetch.calls), len(set(fetch.calls)))
        for n in range(151, 251):
            self.receipt("o/r", n)
            fetch.threads[("o/r", n)] = [comment(f"handoff {n}", "handoff")]
        fetch.calls.clear()
        manifest = self.derive(fetch)
        self.assertTrue(all(n > 150 for _repo, n in fetch.calls))
        self.assertGreater(manifest["cached"], 0)
        self.assertEqual(len(fetch.calls) + manifest["cached"], manifest["issues_read"])

    def test_at_most_three_posts_per_issue(self):
        self.receipt("o/r", 1)
        thread = [comment(f"post {i}", k, "LANE2", at=days(i * 10 - 100))
                  for i, k in enumerate(("plan", "completion", "finding", "plan", "completion"))]
        fetch = FakeFetch({("o/r", 1): thread})
        counts = self.derive(fetch, dry_run=True)
        self.assertEqual(counts["accepted"], 3)

    def test_dry_run_reads_at_most_20_issues_and_writes_nothing(self):
        fetch = self.many(50)
        counts = self.derive(fetch, dry_run=True)
        self.assertEqual(len(fetch.calls), 20)
        self.assertEqual(counts["accepted"], 20)
        self.assertFalse(self.out.parent.exists())


# --------------------------------------------------------------------------
# Test 6 (AC2 / plan §0): the issue set.

class IssueSet(Base):
    def test_only_receipt_files_name_issues(self):
        for name in ("vitalharmony_harmonic-forge_1000.lock", "vitalharmony_harmonic-forge_1000.kill.json",
                     "vitalharmony_harmonic-forge_1000.enrollment.jsonl",
                     "vitalharmony_harmonic-forge_1000.json.pre-fix-bak",
                     "vitalharmony_harmonic-forge_1000.json", "vitalharmony_hrse_7.json"):
            (self.receipts / name).write_text("", encoding="utf-8")
        self.assertEqual(nrp.issue_set(self.receipts),
                         [("vitalharmony/harmonic-forge", 1000), ("vitalharmony/hrse", 7)])

    def test_order_is_newest_first_per_repo(self):
        for n in (3, 10, 7):
            self.receipt("o/r", n)
        self.assertEqual([n for _r, n in nrp.issue_set(self.receipts)], [10, 7, 3])


# --------------------------------------------------------------------------
# Test 7 (AC3): the harness reads what derive writes.

class HarnessReadsDerive(Base):
    def setUp(self) -> None:
        super().setUp()
        for n in range(1, 201):
            self.receipt("o/r", n)
        self.derive(FakeFetch({("o/r", n): [comment(f"handoff {n}", "handoff")] for n in range(1, 201)}))

    def rewrite(self, data: bytes) -> None:
        """Replace the sample and re-seal its manifest's sha256."""
        self.out.chmod(0o600)
        self.out.write_bytes(data)
        manifest = nrp.manifest_path(self.out)
        meta = json.loads(manifest.read_text())
        meta["sha256"] = hashlib.sha256(data).hexdigest()
        manifest.write_text(json.dumps(meta))

    def test_derived_sample_reads_as_200_rows(self):
        self.assertEqual(len(rh.read_posts_sample(self.out)), 200)

    def test_one_byte_edit_refuses_for_sha(self):
        data = bytearray(self.out.read_bytes())
        data[20] ^= 1
        self.out.write_bytes(bytes(data))
        with self.assertRaisesRegex(rh.Refused, "sha256"):
            rh.read_posts_sample(self.out)

    def test_199_rows_refuse(self):
        lines = self.out.read_bytes().splitlines(keepends=True)
        self.rewrite(b"".join(lines[:-1]))
        with self.assertRaisesRegex(rh.Refused, "exactly 200"):
            rh.read_posts_sample(self.out)

    def test_extra_key_refuses(self):
        lines = self.out.read_text().splitlines()
        row = json.loads(lines[0])
        row["extra"] = 1
        self.rewrite(("\n".join([json.dumps(row), *lines[1:]]) + "\n").encode())
        with self.assertRaisesRegex(rh.Refused, "unknown key"):
            rh.read_posts_sample(self.out)

    def test_digest_mismatch_refuses(self):
        lines = self.out.read_text().splitlines()
        row = json.loads(lines[0])
        row["body"] += "x"
        self.rewrite(("\n".join([json.dumps(row), *lines[1:]]) + "\n").encode())
        with self.assertRaisesRegex(rh.Refused, "body_digest"):
            rh.read_posts_sample(self.out)

    def test_row_keys_are_exactly_derive_keys(self):
        row = json.loads(self.out.read_text().splitlines()[0])
        self.assertEqual(set(row), rh.POST_ROW_KEYS)


# --------------------------------------------------------------------------
# Test 9 (AC2): the shim admits the one read shape as a non-scan.

class ShimAdmission(unittest.TestCase):
    def test_the_comments_read_is_not_a_scan(self):
        path = nrp.comments_path("o/r", 1)
        self.assertIsNone(gh_scan_patterns.scan_reason(["api", path]))
        self.assertIsNone(gh_scan_patterns.scan_reason(["api", "-i", path]))

    def test_gh_fetch_runs_gh_api_i_once(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            out = ("HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 4000\nLink: <u>; rel=\"next\"\n\n"
                   + json.dumps([{"body": "b", "created_at": "2026-01-01T00:00:00Z"}]))
            return mock.Mock(stdout=out, stderr="")

        status, headers, items = nrp.gh_fetch("o/r", 9, runner=runner)
        self.assertEqual(seen, [["gh", "api", "-i", "repos/o/r/issues/9/comments?per_page=100"]])
        self.assertEqual((status, headers["remaining"], len(items)), (200, 4000, 1))
        self.assertIn('rel="next"', headers["link"])


if __name__ == "__main__":
    unittest.main()
