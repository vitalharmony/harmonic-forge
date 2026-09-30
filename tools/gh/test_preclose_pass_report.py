#!/usr/bin/env python3
"""harmonic-forge#838 AC6: the pass report's rows and its undercount warnings."""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_pass_report as report  # noqa: E402


class PassReportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.receipts = self.root / "preclose"
        self.receipts.mkdir()
        self.archive = self.root / "archive"
        (self.receipts / "o_r_1.json").write_text(json.dumps(
            {"repo": "o/r", "issue": 1, "status": "complete", "reviewed_sha": "c3"}))
        (self.receipts / "o_r_2.json").write_text(json.dumps(
            {"repo": "o/r", "issue": 2, "status": "complete", "pass_history": [
                {"sha": "a", "surviving": 1, "epoch": 0}, {"sha": "b", "surviving": 1, "epoch": 0},
                {"sha": "c", "surviving": 1, "epoch": 0}]}))

    def archive_file(self, records: list[dict], name: str = "2026-09.jsonl.gz") -> Path:
        folder = self.archive / "o" / "preclose-receipts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps({"record": record}) + "\n")
        return path

    def test_archive_restores_pre_834_pass_counts(self) -> None:
        self.archive_file([{"repo": "o/r", "issue": 1, "status": "complete", "reviewed_sha": s}
                           for s in ("c1", "c2")])
        table = {r["issue"]: r for r in report.rows(self.receipts, archive=self.archive)}
        self.assertEqual(table["o/r#1"]["passes"], 3)  # c1, c2 archived + c3 current
        self.assertEqual(table["o/r#2"]["passes"], 3)
        self.assertTrue(table["o/r#2"]["forced"])

    def test_missing_archive_warns_on_stdout_in_the_report(self) -> None:
        text = report.report(self.receipts, None, self.archive)
        self.assertIn("no receipt archive", text)
        self.assertIn("| o/r#1 |", text)

    def test_corrupt_archive_file_warns_instead_of_silently_undercounting(self) -> None:
        self.archive_file([{"repo": "o/r", "issue": 1, "status": "complete", "reviewed_sha": "c1"}])
        bad = self.archive / "o" / "preclose-receipts" / "2026-08.jsonl.gz"
        bad.write_bytes(b"not gzip")
        text = report.report(self.receipts, None, self.archive)
        self.assertIn("1 archive file(s) could not be read", text)
        self.assertNotIn("no receipt archive", text)


if __name__ == "__main__":
    unittest.main()
