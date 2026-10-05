"""hrse#2221: the gate-run source is valid, and emit() accepts the event the
HRSE2 step runner writes (one per `mise run check`, no step output in it)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402


class GateRunSource(unittest.TestCase):
    def test_gate_run_is_a_declared_source(self):
        self.assertIn("gate-run", emit.SCHEMA["columns"]["source"])

    def test_emit_accepts_a_gate_run_event(self):
        with tempfile.TemporaryDirectory() as store, \
                mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": store}):
            counts = emit.emit([{
                "ts": "2026-10-05T23:59:00Z", "source": "gate-run", "repo": "vitalharmony/hrse",
                "issue": 2221, "sha": "0" * 40, "account": "vitalharmony", "org": "vitalharmony",
                "actor": "lane1", "event_type": "gate.run", "subject_kind": "gate-run",
                "subject_id": "vitalharmony/hrse#2221@000000000000", "provenance": "manual",
                "attrs": {"task": "check-steps", "steps_run": 22, "failed_steps": "3,17",
                          "duration_ms": 240000, "fail_fast": False},
            }])
            self.assertEqual(counts["written"], 1, counts)
            written = list(Path(store).rglob("*.jsonl"))
            self.assertEqual(len(written), 1)
            event = json.loads(written[0].read_text().splitlines()[0])
            self.assertEqual(event["source"], "gate-run")


if __name__ == "__main__":
    unittest.main()
