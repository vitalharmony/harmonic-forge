#!/usr/bin/env python3
"""Tests for archive.py and ci_history_export.py (harmonic-forge#826)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "onboard"))

import archive  # noqa: E402
import ci_history_export as cix  # noqa: E402

WHEN = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


def _manifest(tmp: Path, entries: list[tuple[str, str, str, str, Path]]) -> Path:
    """A fixture registry: (name, prefix, repo, account, path) per entry."""
    body = ""
    for name, prefix, repo, account, path in entries:
        body += (f'[[project]]\nname = "{name}"\nprefix = "{prefix}"\n'
                 f'repo = "{repo}"\naccount = "{account}"\npath = "{path}"\n\n')
    target = tmp / "projects.toml"
    target.write_text(body, encoding="utf-8")
    return target


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = archive.make_test_root(self.tmp / "archive")
        self._env = mock.patch.dict(os.environ, {archive.ROOT_ENV: str(self.root),
                                                 "XDG_STATE_HOME": str(self.tmp / "state")})
        self._env.start()
        archive.reset_registry_cache()
        self.checkout_a = self.tmp / "work" / "Alpha_Repo"
        self.checkout_b = self.tmp / "work" / "second-acct"
        self.checkout_a.mkdir(parents=True)
        self.checkout_b.mkdir(parents=True)
        self.manifest = _manifest(self.tmp, [
            ("alpha", "A", "OrgOne/Alpha", "acct-one", self.checkout_a),
            ("beta", "B", "OtherOrg/Beta", "acct-two", self.checkout_b),
            ("gone", "G", "OrgOne/Gone", "acct-one", self.tmp / "work" / "missing"),
        ])
        self._reg = mock.patch.object(
            archive, "registry", side_effect=lambda mf=None: archive._load_registry(self.manifest))
        self._reg.start()

    def tearDown(self) -> None:
        self._reg.stop()
        self._env.stop()
        archive.reset_registry_cache()
        self._tmp.cleanup()

    def test_envelope_partition_and_count(self) -> None:
        wrote = archive.archive("lane3-audit", [{"a": 1}, {"a": 2}], where=self.checkout_a, now=WHEN)
        self.assertEqual(wrote, 2)
        target = self.root / "acct-one" / "orgone" / "lane3-audit" / "2026-09.jsonl.gz"
        rows = archive.read(target)
        self.assertEqual([r["record"] for r in rows], [{"a": 1}, {"a": 2}])
        for row in rows:
            self.assertEqual(row["schema_version"], 1)
            self.assertEqual(row["source"], "lane3-audit")
            self.assertEqual((row["account"], row["org"], row["repo"]),
                             ("acct-one", "orgone", "orgone/alpha"))
            self.assertTrue(row["archived_at"])

    def test_appends_accumulate_as_multi_member_gzip(self) -> None:
        archive.archive("belt", [{"n": 1}], where=self.checkout_a, now=WHEN)
        archive.archive("belt", [{"n": 2}], where=self.checkout_a, now=WHEN)
        target = self.root / "acct-one" / "orgone" / "belt" / "2026-09.jsonl.gz"
        self.assertEqual([r["record"]["n"] for r in archive.read(target)], [1, 2])

    def test_unmatched_is_kept_unresolved_never_dropped(self) -> None:
        wrote = archive.archive("belt", [{"n": 1}], where="/nowhere/at/all", now=WHEN)
        self.assertEqual(wrote, 1)
        rows = archive.read(self.root / "unresolved" / "unresolved" / "belt" / "2026-09.jsonl.gz")
        self.assertEqual(rows[0]["repo"], None)
        self.assertEqual(archive.archive("belt", [{"n": 2}], where=None, now=WHEN), 1)

    def test_second_account_needs_no_code_change(self) -> None:
        """AC7: a registry entry under a second account is covered by config alone."""
        wrote = archive.archive("handoff-owed", [{"x": 1}], where=self.checkout_b / "sub", now=WHEN)
        self.assertEqual(wrote, 1)
        self.assertTrue((self.root / "acct-two" / "otherorg" / "handoff-owed" / "2026-09.jsonl.gz").exists())

    def test_encoded_transcript_dir_resolves_forward_longest_match(self) -> None:
        encoded = archive.encode_cwd(self.checkout_a.resolve())
        self.assertEqual(archive.resolve(encoded).repo, "orgone/alpha")
        self.assertEqual(archive.resolve(encoded + "-lane2").repo, "orgone/alpha")
        self.assertIs(archive.resolve("-some-other-place"), archive.UNRESOLVED)

    def test_symlinked_checkout_resolves_to_its_target(self) -> None:
        link = self.tmp / "link-to-alpha"
        link.symlink_to(self.checkout_a)
        self.assertEqual(archive.resolve(link).repo, "orgone/alpha")

    def test_sibling_lane_worktree_resolves_to_its_repo(self) -> None:
        sibling = self.checkout_a.parent / (self.checkout_a.name + "-lane2")
        sibling.mkdir()
        self.assertEqual(archive.resolve(sibling).repo, "orgone/alpha")

    def test_failure_returns_zero_never_raises(self) -> None:
        self.assertEqual(archive.archive("Bad Source!", [{"a": 1}]), 0)
        self.assertEqual(archive.archive("belt", []), 0)
        blocker = self.tmp / "blocked"
        blocker.write_text("a file where a directory must go", encoding="utf-8")
        with mock.patch.dict(os.environ, {archive.ROOT_ENV: str(blocker)}):
            self.assertEqual(archive.archive("belt", [{"a": 1}], where=self.checkout_a), 0)


class CiHistoryExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = archive.make_test_root(Path(self._tmp.name) / "archive")
        self._env = mock.patch.dict(os.environ, {archive.ROOT_ENV: str(self.root)})
        self._env.start()
        self.origin = archive.Origin("acct-one", "orgone", "orgone/alpha")

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def _gh(path: str):
        if path.endswith("/actions/runs?per_page=100"):
            return [{"workflow_runs": [
                {"id": 1, "status": "completed", "conclusion": "success", "head_sha": "s1", "name": "ci"},
                {"id": 2, "status": "in_progress", "conclusion": None, "head_sha": "s2", "name": "ci"},
            ]}]
        if "/actions/runs/1/jobs" in path:
            return [{"jobs": [{"id": 11, "run_id": 1, "status": "completed", "conclusion": "success",
                               "name": "verify", "steps": [{"log": "never exported"}]}]}]
        if path.endswith("/commits?per_page=100"):
            return [[{"sha": "s1"}, {"sha": "s3"}]]
        if "/statuses" in path:
            return [[{"id": 100 + len(path), "context": "ci", "state": "success",
                      "description": "free text, never exported"}]]
        raise AssertionError(path)

    def test_exports_completed_metadata_only_and_is_idempotent(self) -> None:
        first = cix.export_repo("orgone/alpha", self.origin, self._gh, dry_run=False)
        self.assertEqual(first["runs"], 1)  # the in-progress run waits for a later pass
        self.assertEqual(first["jobs"], 1)
        rows = archive.read(next(self.root.glob("acct-one/orgone/ci-history/*.jsonl.gz")))
        kinds = sorted(r["record"]["kind"] for r in rows)
        self.assertEqual(kinds.count("workflow_run"), 1)
        for row in rows:
            self.assertNotIn("steps", row["record"])
            self.assertNotIn("description", row["record"])
        second = cix.export_repo("orgone/alpha", self.origin, self._gh, dry_run=False)
        self.assertEqual(second, {"runs": 0, "jobs": 0, "statuses": 0})

    def test_dry_run_writes_nothing(self) -> None:
        counts = cix.export_repo("orgone/alpha", self.origin, self._gh, dry_run=True)
        self.assertGreater(counts["runs"], 0)
        self.assertEqual(list(self.root.rglob("*.jsonl.gz")), [])

    def test_unreadable_retention_is_treated_as_at_risk(self) -> None:
        def denied(path: str):
            raise RuntimeError("HTTP 404")
        self.assertIsNone(cix.retention("orgone/alpha", denied))


if __name__ == "__main__":
    unittest.main()
