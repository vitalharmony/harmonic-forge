#!/usr/bin/env python3
"""Tests for emit.py and census.py (harmonic-forge#828)."""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for sub in ("telemetry", "onboard"):
    if str(HERE.parent / sub) not in sys.path:
        sys.path.insert(0, str(HERE.parent / sub))

import archive  # noqa: E402
import census  # noqa: E402
import emit  # noqa: E402


def _event(**over):
    base = {"ts": "2026-09-30T00:00:00Z", "source": "gh-thread", "account": "acct", "org": "o",
            "repo": "o/a", "event_type": "handoff.posted", "subject_id": "comment:1",
            "provenance": "footer", "validated": True, "extractor_version": "t"}
    base.update(over)
    return base


class EmitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _rows(self):
        return [json.loads(l) for p in self.store.rglob("*.jsonl") for l in p.read_text().splitlines()]

    def test_partitioned_by_account_org_source_month(self):
        emit.emit([_event()])
        self.assertTrue((self.store / "events/acct/o/gh-thread/2026-09.jsonl").exists())

    def test_event_id_is_deterministic_and_reemit_is_idempotent(self):
        first = emit.emit([_event(), _event()])
        second = emit.emit([_event()])
        self.assertEqual((first["written"], first["duplicate"]), (1, 1))
        self.assertEqual((second["written"], second["duplicate"]), (0, 1))
        self.assertEqual(self._rows()[0]["event_id"],
                         emit.event_id("gh-thread", "o/a", None, "comment:1", "handoff.posted",
                                       "2026-09-30T00:00:00Z"))

    def test_same_subject_in_two_repos_is_two_events(self):
        counts = emit.emit([_event(repo="o/a", issue=12, subject_id="12"),
                            _event(repo="o/b", issue=12, subject_id="12")])
        self.assertEqual(counts["written"], 2)

    def test_equivalent_offsets_are_one_event(self):
        emit.emit([_event(ts="2026-10-01T00:30:00+05:30")])
        counts = emit.emit([_event(ts="2026-09-30T19:00:00Z")])
        self.assertEqual(counts["duplicate"], 1)
        self.assertTrue((self.store / "events/acct/o/gh-thread/2026-09.jsonl").exists())

    def test_bad_ts_and_missing_subject_are_rejected_not_raised(self):
        counts = emit.emit([_event(ts=12345), _event(ts="yesterday"),
                            _event(ts="2026-09-30T00:00:00"), _event(subject_id=None)])
        self.assertEqual(len(counts["rejected"]), 4)
        self.assertEqual(self._rows(), [])

    def test_bodies_refused_by_token_nesting_and_length(self):
        bad = [{"body": "x"}, {"comment_excerpt": "free text"}, {"detail": {"body": "x"}},
               {"verdict": "y" * 201}]
        counts = emit.emit([_event(attrs=a) for a in bad])
        self.assertEqual(len(counts["rejected"]), 4)
        for _, _, reason in counts["rejected"]:
            self.assertNotIn("free text", reason)
        ok = emit.emit([_event(attrs={"comment_id": "5", "verdict": "PASS", "count": 3})])
        self.assertEqual(ok["written"], 1)

    def test_one_bad_event_does_not_drop_its_batch(self):
        counts = emit.emit([_event(), _event(source="nope"), _event(subject_id="comment:2")])
        self.assertEqual(counts["written"], 2)
        self.assertEqual([r[0] for r in counts["rejected"]], [1])

    def test_unresolved_is_kept_and_not_rewritten_once_resolved(self):
        emit.emit([_event(account="", org=None, repo=None)])
        self.assertTrue((self.store / "events/unresolved/unresolved/gh-thread/2026-09.jsonl").exists())
        self.assertEqual(self._rows()[0]["repo"], "unresolved")
        counts = emit.emit([_event(repo=None)])  # same event, account now resolved
        self.assertEqual(counts["duplicate"], 1)

    def test_corrupt_partition_line_does_not_wedge_writes(self):
        part = self.store / "events/acct/o/gh-thread/2026-09.jsonl"
        part.parent.mkdir(parents=True)
        part.write_text("<<<<<<< HEAD\n{truncated\n")
        self.assertEqual(emit.emit([_event()])["written"], 1)

    def _archive(self, root, batches):
        from datetime import datetime, timezone
        with mock.patch.dict(os.environ, {archive.ROOT_ENV: str(root),
                                          "XDG_STATE_HOME": str(root / "state")}):
            for records, minute in batches:
                archive.archive("belt", records, origin=archive.Origin("acct", "o", "o/a"),
                                now=datetime(2026, 9, 30, 1, minute, tzinfo=timezone.utc))

    def test_ingest_archive_dedupes_on_record_hash_across_archive_calls(self):
        root = archive.make_test_root(self.store.parent / (self.store.name + "-arch"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        # Two separate calls: different archived_at, same record -> one event.
        self._archive(root, [([{"n": 1}], 0), ([{"n": 1}], 5), ([{"n": 2}], 5)])
        counts = emit.ingest_archive(root)
        self.assertEqual((counts["written"], counts["duplicate"]), (2, 1))
        rows = self._rows()
        self.assertEqual({r["subject_kind"] for r in rows}, {"record_hash"})
        self.assertEqual({r["event_type"] for r in rows}, {"archived.belt"})
        self.assertEqual({r["repo"] for r in rows}, {"o/a"})


class CensusTests(unittest.TestCase):
    @staticmethod
    def _gh(path):
        return [[{"state": "closed", "comments": 3}, {"state": "closed", "comments": 0},
                 {"state": "open", "comments": 1}, {"state": "open", "pull_request": {}}]]

    def test_counts_exclude_prs(self):
        self.assertEqual(census.census_repo("o/a", self._gh),
                         {"issues": 3, "open": 1, "closed": 2,
                          "closed_with_comments": 1, "closed_zero_comments": 1})

    def test_repo_less_entry_and_crashing_repo_are_rows_not_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = tmp / "projects.toml"
            manifest.write_text(
                '[[project]]\nname = "a"\nprefix = "A"\nrepo = "o/a"\naccount = "x"\n\n'
                '[[project]]\nname = "norepo"\nprefix = "N"\naccount = "x"\n\n'
                '[[project]]\nname = "c"\nprefix = "C"\nrepo = "o/c"\naccount = "x"\n', encoding="utf-8")
            out = tmp / "census.csv"

            def gh(path):
                if "o/a" in path:
                    raise FileNotFoundError("gh")
                return self._gh(path)
            with mock.patch.object(census, "gh_pages", gh), \
                    mock.patch("manifest_identity.apply_project_identity"):
                rc = census.main(["--out", str(out), "--manifest", str(manifest)])
            rows = list(csv.DictReader(out.read_text().splitlines()))
        self.assertEqual(rc, 1)
        self.assertEqual(len(rows), 3)
        self.assertIn("FileNotFoundError", rows[0]["error"])
        self.assertIn("no repo", rows[1]["error"])
        self.assertEqual(rows[2]["issues"], "3")

    def test_second_account_and_missing_path_with_zero_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = tmp / "projects.toml"
            manifest.write_text(
                '[[project]]\nname = "a"\nprefix = "A"\nrepo = "o/a"\naccount = "acct-one"\n\n'
                '[[project]]\nname = "b"\nprefix = "B"\nrepo = "other/b"\naccount = "acct-two"\n'
                f'path = "{tmp / "gone"}"\n', encoding="utf-8")
            out = tmp / "census.csv"
            with mock.patch.object(census, "gh_pages", self._gh), \
                    mock.patch("manifest_identity.apply_project_identity"):
                census.main(["--out", str(out), "--manifest", str(manifest)])
            rows = list(csv.DictReader(out.read_text().splitlines()))
        self.assertEqual([(r["account"], r["org"]) for r in rows],
                         [("acct-one", "o"), ("acct-two", "other")])
        self.assertIn("path not on disk", rows[1]["note"])


if __name__ == "__main__":
    unittest.main()
