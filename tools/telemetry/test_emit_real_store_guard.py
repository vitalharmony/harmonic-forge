"""harmonic-forge#907: a test process never writes the operator's real
telemetry store, however it was launched -- the harmonic-forge#865 guard
applied to `emit.py`. Every case points `emit.REAL_STORE` (or, for a
subprocess, HOME) at a temp directory standing in for the real store, so
nothing here touches ~/Harmonic_Projects/telemetry-store."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emit  # noqa: E402

GH_DIR = HERE.parent / "gh"


def _event(**over):
    base = {"ts": "2026-09-30T00:00:00Z", "source": "gh-thread", "account": "acct", "org": "o",
            "repo": "o/a", "event_type": "handoff.posted", "subject_id": "comment:1",
            "provenance": "footer", "validated": True, "extractor_version": "t"}
    base.update(over)
    return base


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class RealStoreGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.real = Path(tmp.name) / "real-store"
        self.other = Path(tmp.name) / "other-store"
        patch = mock.patch.object(emit, "REAL_STORE", self.real)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_test_process_is_refused_the_real_store(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), redirect_stderr(err):
            os.environ.pop(emit.STORE_ENV, None)
            counts = emit.emit([_event()])
        self.assertEqual(counts["written"], 0)
        self.assertEqual(_files(self.real), [], "a test wrote the real store")
        self.assertIn("not writing the real telemetry store", err.getvalue())

    def test_a_test_process_still_writes_a_redirected_store(self):
        with mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.other)}):
            counts = emit.emit([_event()])
        self.assertEqual(counts["written"], 1)
        self.assertTrue(_files(self.other))
        self.assertEqual(_files(self.real), [])

    def test_a_subprocess_marked_as_a_test_is_refused(self):
        home = self.real.parent / "home"
        real = home / "Harmonic_Projects" / "telemetry-store"
        env = {k: v for k, v in os.environ.items() if k != emit.STORE_ENV}
        env.update(HOME=str(home), HARMONIC_FORGE_TESTING="1")
        code = ("import sys; sys.path.insert(0, %r); import emit; "
                "print(emit.emit([%r])['written'])" % (str(HERE), _event()))
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")
        self.assertEqual(_files(real), [])


class RunnerRedirects(unittest.TestCase):
    """The two runners that reach telemetry outside run_tests.py redirect it."""

    def test_the_lane1_transport_runner_redirects_telemetry(self):
        src = (GH_DIR / "run_lane1_transport_tests.py").read_text()
        self.assertIn('os.environ["HARMONIC_FORGE_TELEMETRY_STORE"]', src)

    def test_kill_check_children_get_a_scratch_store(self):
        src = (GH_DIR / "kill_check.py").read_text()
        self.assertIn('"HARMONIC_FORGE_TELEMETRY_STORE": str(directory', src)


if __name__ == "__main__":
    unittest.main()
