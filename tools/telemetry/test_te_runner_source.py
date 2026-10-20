"""harmonic-forge#1054: the te-runner source is valid, emit() accepts a runner
event, and the event lands in the te-runner source partition."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402


def _event() -> dict:
    return {
        "ts": "2026-10-19T12:00:00Z", "source": "te-runner",
        "repo": "vitalharmony/harmonic-forge", "issue": 1054, "sha": "0" * 40,
        "account": "vitalharmony", "org": "vitalharmony", "actor": "unset",
        "event_type": "te-runner.item", "subject_kind": "te-issue",
        "subject_id": "vitalharmony/harmonic-forge#1054@000000000000", "provenance": "manual",
        "attrs": {"reason": "green", "total_cost_usd": 0.17},
    }


class TeRunnerSource(unittest.TestCase):
    def _emit(self, store: str) -> list[Path]:
        with mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": store}):
            counts = emit.emit([_event()])
        self.assertEqual(counts["written"], 1, counts)
        written = list(Path(store).rglob("*.jsonl"))
        self.assertEqual(len(written), 1)
        return written

    def test_te_runner_source_is_valid(self):
        self.assertIn("te-runner", emit.SCHEMA["columns"]["source"])

    def test_te_runner_event_is_accepted(self):
        with tempfile.TemporaryDirectory() as store:
            written = self._emit(store)
            event = json.loads(written[0].read_text().splitlines()[0])
            self.assertEqual(event["source"], "te-runner")

    def test_te_runner_event_lands_in_its_own_partition(self):
        # _partition (emit.py) writes events/<account>/<org>/<source>/YYYY-MM.jsonl
        with tempfile.TemporaryDirectory() as store:
            written = self._emit(store)
            rel = written[0].relative_to(store).parts
            self.assertEqual(rel[:4], ("events", "vitalharmony", "vitalharmony", "te-runner"))


if __name__ == "__main__":
    unittest.main()
