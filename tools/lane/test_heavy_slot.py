"""harmonic-forge#1271: load-aware admission. Real processes and real locks where they matter."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import heavy_slot as hs  # noqa: E402

TOOL = Path(__file__).with_name("heavy_slot.py")
CONFIG = hs.load_config()
IDLE = hs.Reading(8, 0.5, 20.0, 1.0, 0.0)


def holder(age=1000.0, cores=4, gb=6, lease=False, id_="h1"):
    return {"id": id_, "admitted_at": 10_000.0 - age, "cores": cores, "gb": gb, "lease": lease}


def decide(reading=IDLE, holders=(), waited=0.0, need=(4, 6), own=None):
    return hs.decide(need, reading, list(holders), 10_000.0, CONFIG, waited, own)[0]


class Admission(unittest.TestCase):
    def test_an_idle_machine_admits(self):
        self.assertTrue(decide())

    def test_the_incident_queues_load_ten_on_eight_cores(self):
        # H2604's dry run: load past 10 on 8 cores, with cpu PSI low enough to miss.
        self.assertFalse(decide(hs.Reading(8, 10.5, 15.0, 18.8, 0.0)))

    def test_high_cpu_pressure_queues_with_cores_free(self):
        self.assertFalse(decide(hs.Reading(8, 1.0, 20.0, 70.0, 0.0)))

    def test_memory_pressure_queues(self):
        self.assertFalse(decide(hs.Reading(8, 1.0, 20.0, 1.0, 40.0)))

    def test_a_recent_admission_counts_before_load_shows_it(self):
        reading = hs.Reading(8, 1.0, 20.0, 1.0, 0.0)
        self.assertTrue(decide(reading, need=(4, 6)))
        self.assertFalse(decide(reading, [holder(age=10.0)], need=(4, 6)))

    def test_an_older_admission_is_left_to_load(self):
        self.assertTrue(decide(hs.Reading(8, 1.0, 20.0, 1.0, 0.0), [holder(age=500.0)]))

    def test_recent_memory_counts_too(self):
        self.assertFalse(decide(hs.Reading(8, 0.0, 10.0, 0.0, 0.0), [holder(age=10.0, cores=0, gb=6)], need=(1, 6)))

    def test_a_lease_stands_while_idle(self):
        lease = holder(age=5000.0, cores=6, gb=2, lease=True)
        self.assertFalse(decide(hs.Reading(8, 0.5, 20.0, 1.0, 0.0), [lease], need=(4, 2)))

    def test_a_lease_credits_its_own_commands(self):
        lease = holder(age=5000.0, cores=4, gb=6, lease=True)
        reading = hs.Reading(8, 7.5, 7.0, 1.0, 0.0)
        self.assertFalse(decide(reading, [lease], need=(4, 6)))
        self.assertTrue(decide(reading, [lease], need=(4, 6), own=lease))

    def test_the_bound_admits_under_sustained_load(self):
        loaded = hs.Reading(8, 30.0, 20.0, 90.0, 0.0)
        self.assertFalse(decide(loaded, waited=599.0))
        self.assertTrue(decide(loaded, waited=600.0))

    def test_the_bound_never_bypasses_the_memory_floor(self):
        self.assertFalse(decide(hs.Reading(8, 0.0, 1.0, 0.0, 0.0), waited=100_000.0))

    def test_a_kind_reservation_is_clamped_so_it_can_fit(self):
        config = {"kinds": {"big": {"cores": 64, "gb": 1}, "default": {"cores": 1, "gb": 1}}}
        self.assertEqual(hs.reservation("big", config, 8), (4, 1))
        self.assertEqual(hs.reservation("unknown", config, 8), (1, 1))

    def test_a_missing_pressure_file_reads_as_none_and_still_decides(self):
        self.assertTrue(decide(hs.Reading(8, 0.5, 20.0, None, None)))


class Real(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "HEAVY_SLOT_DIR": self.tmp.name}
        self.env.pop("CI", None)
        self.env.pop("HEAVY_SLOT_DISABLE", None)
        self.state = Path(self.tmp.name)

    def _run(self, *cmd, env=None, **kw):
        return subprocess.run([sys.executable, str(TOOL), *cmd], env=env or self.env, text=True,
                              capture_output=True, timeout=60, **kw)

    def _holders(self):
        return list((self.state / "holders").glob("*.json"))

    def test_the_exit_code_passes_through(self):
        self.assertEqual(self._run("run", "--kind", "kill", "--label", "t", "--", "sh", "-c", "exit 7").returncode, 7)

    def test_a_slot_is_released_when_the_command_ends(self):
        self._run("run", "--kind", "kill", "--label", "t", "--", "true")
        self.assertEqual(self._holders(), [])

    def test_a_nested_run_re_enters_and_does_not_wait(self):
        inner = f"{sys.executable} {TOOL} run --kind kill --label inner -- true"
        out = self._run("run", "--kind", "kill", "--label", "outer", "--", "sh", "-c", inner)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("runs under kill outer", out.stderr)

    def test_a_sibling_is_not_an_ancestor(self):
        first = subprocess.Popen([sys.executable, str(TOOL), "run", "--kind", "kill", "--label", "a", "--",
                                  "sleep", "30"], env=self.env, stderr=subprocess.DEVNULL)
        self.addCleanup(first.kill)
        for _ in range(100):
            if self._holders():
                break
            time.sleep(0.1)
        record = json.loads(self._holders()[0].read_text())
        self.assertEqual(record["kind"], "kill")
        sys.path.insert(0, str(TOOL.parent))
        os.environ["HEAVY_SLOT_DIR"] = self.tmp.name
        self.addCleanup(os.environ.pop, "HEAVY_SLOT_DIR", None)
        self.assertIsNone(hs._ancestor_holder(self.state, CONFIG))

    def test_a_killed_holder_releases_at_once(self):
        child = subprocess.Popen([sys.executable, str(TOOL), "run", "--kind", "kill", "--label", "a", "--",
                                  "sleep", "30"], env=self.env, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self._holders():
                break
            time.sleep(0.1)
        self.assertTrue(self._holders())
        child.send_signal(signal.SIGKILL)
        child.wait()
        self.assertEqual(hs.live_holders(self.state, time.time(), CONFIG), [])

    def test_a_dead_ticket_is_skipped(self):
        tickets = self.state / "tickets"
        tickets.mkdir(parents=True)
        (tickets / "00000000000000000001-1.ticket").touch()  # nobody holds its flock
        self.assertEqual(hs._tickets(self.state), [])

    def test_first_come_first_served(self):
        tickets = self.state / "tickets"
        tickets.mkdir(parents=True)
        early = tickets / "00000000000000000001-1.ticket"
        handle = open(early, "a+")
        self.addCleanup(handle.close)
        hs.fcntl.flock(handle, hs.fcntl.LOCK_EX)
        admitted, clock, slept = [], [1000.0], []

        def stop_after_first(_seconds):
            slept.append(_seconds)
            raise TimeoutError

        with self.assertRaises(TimeoutError):
            admitted.append(hs._admit("kill", "late", self.state, CONFIG, False,
                                      read=lambda: IDLE, sleep=stop_after_first, clock=lambda: clock[0]))
        self.assertEqual(admitted, [])

    def test_a_dead_lease_is_reclaimed(self):
        holders = self.state / "holders"
        holders.mkdir(parents=True)
        (holders / "x.json").write_text(json.dumps({
            "id": "x", "lease": True, "owner": 999_999_999, "owner_start": "1", "admitted_at": time.time(),
            "cores": 2, "gb": 8}))
        self.assertEqual(hs.live_holders(self.state, time.time(), CONFIG), [])

    def test_a_lease_past_its_backstop_is_reclaimed(self):
        holders = self.state / "holders"
        holders.mkdir(parents=True)
        (holders / "x.json").write_text(json.dumps({
            "id": "x", "lease": True, "owner": os.getpid(), "owner_start": hs._start_time(os.getpid()),
            "admitted_at": time.time() - 14_401, "cores": 2, "gb": 8}))
        self.assertEqual(hs.live_holders(self.state, time.time(), CONFIG), [])

    def test_a_lease_survives_between_commands_and_releases(self):
        self.assertEqual(self._run("lease", "--kind", "gate", "--label", "H1").returncode, 0)
        leased = [json.loads(p.read_text()) for p in self._holders()]
        self.assertTrue(leased and leased[0]["lease"])
        self.assertEqual(self._run("release").returncode, 0)
        self.assertEqual(self._holders(), [])

    def test_ci_runs_unmanaged(self):
        out = self._run("run", "--kind", "check", "--label", "t", "--", "true", env={**self.env, "CI": "true"})
        self.assertEqual(out.returncode, 0)
        self.assertIn("unmanaged", out.stderr)
        self.assertFalse((self.state / "holders").exists())

    def test_the_test_switch_runs_unmanaged_and_says_so(self):
        out = self._run("run", "--kind", "check", "--label", "t", "--", "true",
                        env={**self.env, "HEAVY_SLOT_DISABLE": "1"})
        self.assertEqual(out.returncode, 0)
        self.assertIn("HEAVY_SLOT_DISABLE: running check t unmanaged", out.stderr)
        self.assertFalse((self.state / "holders").exists())

    def test_broken_state_warns_and_runs(self):
        blocked = self.state / "file"
        blocked.write_text("")
        out = self._run("run", "--kind", "check", "--label", "t", "--", "sh", "-c", "exit 3",
                        env={**self.env, "HEAVY_SLOT_DIR": str(blocked)})
        self.assertEqual(out.returncode, 3)
        self.assertIn("WARNING", out.stderr)

    def test_status_prints_readings(self):
        out = self._run("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("MemAvailable", out.stdout)


if __name__ == "__main__":
    unittest.main()
