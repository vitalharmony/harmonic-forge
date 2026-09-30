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
        self.assertEqual(first, {"written": 1, "duplicate": 1})
        self.assertEqual(second, {"written": 0, "duplicate": 1})
        self.assertEqual(self._rows()[0]["event_id"],
                         emit.event_id("gh-thread", "comment:1", "handoff.posted", "2026-09-30T00:00:00Z"))

    def test_required_fields_and_no_bodies(self):
        with self.assertRaises(emit.SchemaError):
            emit.emit([_event(org="")])
        with self.assertRaises(emit.SchemaError):
            emit.emit([_event(attrs={"body": "free text"})])
        with self.assertRaises(emit.SchemaError):
            emit.emit([_event(source="nope")])
        self.assertEqual(self._rows(), [])

    def test_a_bad_event_writes_nothing_from_its_batch(self):
        with self.assertRaises(emit.SchemaError):
            emit.emit([_event(), _event(account="")])
        self.assertEqual(self._rows(), [])

    def test_ingest_archive_preserves_record_hash_as_dedupe_key(self):
        root = archive.make_test_root(self.store.parent / (self.store.name + "-arch"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        with mock.patch.dict(os.environ, {archive.ROOT_ENV: str(root),
                                          "XDG_STATE_HOME": str(root / "state")}):
            archive.archive("belt", [{"n": 1}, {"n": 1}], origin=archive.Origin("acct", "o", "o/a"))
        counts = emit.ingest_archive(root)
        self.assertEqual(counts, {"written": 1, "duplicate": 1})
        rows = self._rows()
        self.assertEqual(rows[0]["subject_kind"], "record_hash")
        self.assertEqual(rows[0]["event_type"], "archived.belt")


class CensusTests(unittest.TestCase):
    @staticmethod
    def _gh(path):
        return [[{"state": "closed", "comments": 3}, {"state": "closed", "comments": 0},
                 {"state": "open", "comments": 1}, {"state": "open", "pull_request": {}}]]

    def test_counts_exclude_prs(self):
        self.assertEqual(census.census_repo("o/a", self._gh),
                         {"issues": 3, "open": 1, "closed": 2,
                          "closed_with_comments": 1, "closed_zero_comments": 1})

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
            rows = list(csv.DictReader(out.open()))
        self.assertEqual([(r["account"], r["org"]) for r in rows],
                         [("acct-one", "o"), ("acct-two", "other")])
        self.assertIn("path not on disk", rows[1]["note"])


if __name__ == "__main__":
    unittest.main()
