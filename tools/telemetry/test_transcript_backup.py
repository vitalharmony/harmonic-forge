#!/usr/bin/env python3
"""Tests for transcript_backup.sh's telemetry-store backup (harmonic-forge#1246).

The script runs against stub `restic` and `insync` on PATH and temp config,
password and repo-id files; the store is a temp directory, never the real one.
Run: `python3 -m unittest tools.telemetry.test_transcript_backup`."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "transcript_backup.sh"

RESTIC_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if [[ "$1" == "cat" && "$2" == "config" ]]; then echo '{"id":"x"}'; fi
"""
INSYNC_STUB = """#!/usr/bin/env bash
echo SYNCED
"""


class TelemetryStoreBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in (("restic", RESTIC_STUB), ("insync", INSYNC_STUB)):
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(0o755)
        password = root / "password"
        password.write_text("x")
        password.chmod(0o600)
        self.conf = root / "backup.env"
        self.conf.write_text(f"RESTIC_REPOSITORY={root / 'repo'}\n"
                             f"RESTIC_PASSWORD_FILE={password}\n"
                             f"SOURCE_DIR={root / 'transcripts'}\n")
        self.store = root / "store"
        self.store.mkdir()
        self.log = root / "restic.log"
        self.env = {**os.environ,
                    "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "FORGE_TRANSCRIPT_BACKUP_ENV": str(self.conf),
                    "REPO_ID_FILE": str(root / "repo-id"),
                    "HARMONIC_FORGE_TELEMETRY_STORE": str(self.store),
                    "HARMONIC_FORGE_TESTING": "1",
                    "SYNC_POLL_S": "0",
                    "STUB_LOG": str(self.log)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self):
        return subprocess.run(["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_store_is_backed_up_before_the_check(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        backups = [i for i, c in enumerate(calls) if c.startswith("backup ")]
        self.assertEqual(len(backups), 2, calls)
        self.assertIn(f"backup --tag telemetry-store --one-file-system --no-scan {self.store}",
                      calls)
        check = next(i for i, c in enumerate(calls) if c.startswith("check "))
        self.assertTrue(all(i < check for i in backups), calls)

    def test_missing_store_stops_before_any_restic_call(self):
        self.env["HARMONIC_FORGE_TELEMETRY_STORE"] = str(Path(self.tmp.name) / "absent")
        result = self.run_script()
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("telemetry store", result.stderr)
        self.assertFalse(any(c.startswith(("backup", "cat config")) for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
