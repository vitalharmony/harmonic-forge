"""harmonic-forge#1271: load-aware admission. Real processes and real locks where they matter."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import heavy_slot as hs  # noqa: E402

TOOL = Path(__file__).with_name("heavy_slot.py")
CONFIG = hs.load_config()
IDLE = hs.Reading(8, 0.5, 20.0, 1.0, 0.0)


def holder(age=1000.0, cores=4, gb=6, id_="h1"):
    return {"id": id_, "admitted_at": 10_000.0 - age, "cores": cores, "gb": gb}


def decide(reading=IDLE, holders=(), waited=0.0, need=(4, 6)):
    return hs.decide(need, reading, list(holders), 10_000.0, CONFIG, waited)[0]


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

    def test_the_bound_admits_under_sustained_load(self):
        loaded = hs.Reading(8, 30.0, 20.0, 90.0, 0.0)
        self.assertFalse(decide(loaded, waited=599.0))
        self.assertTrue(decide(loaded, waited=600.0))

    def test_the_bound_never_bypasses_the_memory_floor(self):
        self.assertFalse(decide(hs.Reading(8, 0.0, 1.0, 0.0, 0.0), waited=100_000.0))

    def test_a_kind_bigger_than_the_machine_reaches_the_bound(self):
        config = {"kinds": {"big": {"cores": 64, "gb": 1}, "default": {"cores": 1, "gb": 1}}}
        self.assertEqual(hs.reservation("big", config), (64, 1))
        self.assertEqual(hs.reservation("unknown", config), (1, 1))
        small_idle = hs.Reading(4, 0.5, 20.0, 1.0, 0.0)
        self.assertFalse(decide(small_idle, need=(5, 6)))
        self.assertTrue(decide(small_idle, need=(5, 6), waited=600.0))

    def test_forge_tasks_go_through_admission(self):
        tasks = tomllib.loads((TOOL.parent.parent.parent / "mise.toml").read_text())["tasks"]
        self.assertIn("heavy_slot.py run --kind check", tasks["check"]["run"])
        begin = tasks["lane3-begin"]["run"]
        self.assertLess(begin.index("heavy_slot.py run --kind gate"), begin.index("check_lane3_ready.py"))

    def test_the_measured_reservations_apply_on_eight_cores(self):
        self.assertEqual(hs.reservation("check", CONFIG), (5, 6))
        self.assertEqual(hs.reservation("e2e", CONFIG), (5, 6))

    def test_check_full_never_reserves_less_than_the_check_it_contains(self):
        check, full = hs.reservation("check", CONFIG), hs.reservation("check-full", CONFIG)
        self.assertGreaterEqual(full[0], check[0])
        self.assertGreaterEqual(full[1], check[1])

    def test_the_floor_counts_what_was_just_promised(self):
        self.assertFalse(decide(hs.Reading(8, 12.0, 9.0, 20.0, 0.0), [holder(age=10.0, gb=8)], waited=600.0))

    def test_an_older_job_is_not_counted_against_memory_twice(self):
        # MemAvailable already excludes what a running job uses: 13 GB is 13 GB free.
        self.assertTrue(decide(hs.Reading(8, 0.5, 13.0, 1.0, 0.0), [holder(age=5000.0, gb=8)]))

    def test_the_real_readings_are_parsed(self):
        pages = {"/proc/loadavg": "3.50 2.00 1.00 1/100 200\n", "/proc/meminfo": "MemTotal: 9 kB\nMemAvailable: 2097152 kB\n",
                 "/proc/pressure/cpu": "some avg10=12.50 avg60=1.00 avg300=1.00 total=1\n",
                 "/proc/pressure/memory": "some avg10=7.25 avg60=1.00 avg300=1.00 total=1\n"}
        with mock.patch.object(hs.Path, "read_text", lambda path, *a, **k: pages[str(path)]), \
                mock.patch.object(hs.os, "cpu_count", lambda: 8):
            self.assertEqual(hs.read_system(), hs.Reading(8, 3.5, 2.0, 12.5, 7.25))

    def test_absent_memory_is_not_zero_free(self):
        pages = {"/proc/loadavg": "3.50 2.00 1.00 1/100 200\n", "/proc/meminfo": "MemTotal: 9 kB\n"}
        with mock.patch.object(hs.Path, "read_text", lambda path, *a, **k: pages[str(path)]), \
                self.assertRaises(ValueError):
            hs.read_system()

    def test_absent_pressure_reads_as_none(self):
        def missing(path, *a, **k):
            raise OSError(str(path))

        with mock.patch.object(hs.Path, "read_text", missing):
            self.assertIsNone(hs._psi("cpu"))

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
        # Real processes and locks, but an idle reading: these tests are not about the
        # machine, and a busy one queued them until their waits ran out.
        self.tool = str(self.state / "drive.py")
        Path(self.tool).write_text(
            f"import sys, time\nsys.path.insert(0, {str(TOOL.parent)!r})\nimport heavy_slot as hs\n"
            "idle = lambda: hs.Reading(8, 0.5, 20.0, 1.0, 0.0)\n"
            "hs.read_system = idle\nhs._admit.__defaults__ = (idle, time.sleep, time.time)\n"
            "raise SystemExit(hs.main(sys.argv[1:]))\n")

    def _run(self, *cmd, env=None, **kw):
        return subprocess.run([sys.executable, self.tool, *cmd], env=env or self.env, text=True,
                              capture_output=True, timeout=60, **kw)

    @contextlib.contextmanager
    def _managed(self):
        """This test's state, with the suite's own unmanaged switches off."""
        with mock.patch.dict(os.environ, {"HEAVY_SLOT_DIR": self.tmp.name}):
            os.environ.pop("HEAVY_SLOT_DISABLE", None)
            os.environ.pop("CI", None)
            yield

    def _holders(self):
        return list((self.state / "holders").glob("*.json"))

    def test_the_exit_code_passes_through(self):
        self.assertEqual(self._run("run", "--kind", "kill", "--label", "t", "--", "sh", "-c", "exit 7").returncode, 7)

    def test_a_slot_is_released_when_the_command_ends(self):
        self._run("run", "--kind", "kill", "--label", "t", "--", "true")
        self.assertEqual(self._holders(), [])

    def test_a_nested_run_re_enters_and_does_not_wait(self):
        inner = f"{sys.executable} {self.tool} run --kind kill --label inner -- true"
        out = self._run("run", "--kind", "kill", "--label", "outer", "--", "sh", "-c", inner)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("runs under kill outer", out.stderr)

    def test_a_sibling_is_not_an_ancestor(self):
        first = subprocess.Popen([sys.executable, self.tool, "run", "--kind", "kill", "--label", "a", "--",
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
        self.assertIsNone(hs._ancestor_holder(self.state))

    def test_a_killed_holder_releases_at_once(self):
        child = subprocess.Popen([sys.executable, self.tool, "run", "--kind", "kill", "--label", "a", "--",
                                  "sleep", "30"], env=self.env, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self._holders():
                break
            time.sleep(0.1)
        self.assertTrue(self._holders())
        child.send_signal(signal.SIGKILL)
        child.wait()
        self.assertEqual(hs.live_holders(self.state), [])

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
            admitted.append(hs._admit("kill", "late", self.state, CONFIG,
                                      read=lambda: IDLE, sleep=stop_after_first, clock=lambda: clock[0]))
        self.assertEqual(admitted, [])

    def test_a_wrong_shaped_record_is_removed_and_the_command_runs(self):
        holders = self.state / "holders"
        holders.mkdir(parents=True)
        for name, body in (("a", "[]"), ("b", "null"), ("c", '{"cores": "5", "gb": 1, "admitted_at": 1}')):
            (holders / f"{name}.json").write_text(body)
        out = self._run("run", "--kind", "kill", "--label", "t", "--", "sh", "-c", "exit 3")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self._holders(), [])

    def test_a_config_of_the_wrong_type_warns_and_runs(self):
        config = dict(CONFIG, kinds={"default": {"cores": "5", "gb": 1}})
        with self._managed(), \
                mock.patch.object(hs, "load_config", lambda: config), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with hs.slot("kill", "t"):
                ran = True
        self.assertTrue(ran)
        self.assertIn("unmanaged", err.getvalue())

    def test_no_memory_reading_warns_and_runs(self):
        real = hs.Path.read_text

        def fake(path, *args, **kwargs):
            return "MemTotal: 1 kB\n" if str(path) == "/proc/meminfo" else real(path, *args, **kwargs)

        with self._managed(), \
                mock.patch.object(hs.Path, "read_text", fake), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with hs.slot("kill", "t"):
                ran = True
        self.assertTrue(ran)
        self.assertIn("MemAvailable", err.getvalue())

    def test_admission_waits_for_the_guard(self):
        with hs._guard(self.state):
            child = subprocess.Popen([sys.executable, self.tool, "run", "--kind", "kill", "--label", "a", "--", "true"],
                                     env=self.env, stderr=subprocess.DEVNULL)
            self.addCleanup(child.kill)
            time.sleep(1.5)
            self.assertIsNone(child.poll(), "admitted while another decision held the guard")
            self.assertEqual(self._holders(), [])
        self.assertEqual(child.wait(30), 0)

    def test_sigterm_reaches_the_job(self):
        mark = self.state / "got-term"
        job = f"trap 'touch {mark}; exit 0' TERM; sleep 30 & wait"
        child = subprocess.Popen([sys.executable, self.tool, "run", "--kind", "kill", "--label", "a", "--", "sh", "-c", job],
                                 env=self.env, stderr=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        for _ in range(100):
            if self._holders():
                break
            time.sleep(0.1)
        time.sleep(0.5)
        child.send_signal(signal.SIGTERM)
        child.wait(20)
        self.assertTrue(mark.exists())

    def test_the_wait_is_bounded_on_the_live_path(self):
        loaded, now, slept = hs.Reading(8, 30.0, 20.0, 90.0, 0.0), [1000.0], []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
            if len(slept) > 100:
                raise TimeoutError

        with contextlib.redirect_stderr(io.StringIO()) as err:
            holder, handle = hs._admit("kill", "t", self.state, CONFIG, read=lambda: loaded,
                                       sleep=sleep, clock=lambda: now[0])
        self.addCleanup(handle.close)
        self.assertEqual(holder["kind"], "kill")
        self.assertGreaterEqual(sum(slept), CONFIG["limits"]["max_wait_seconds"])
        self.assertIn("waiting on", err.getvalue())
        self.assertIn("load 30.0/8", err.getvalue())

    def test_two_waiters_past_the_bound_do_not_start_together(self):
        loaded = hs.Reading(8, 10.5, 40.0, 18.8, 0.0)
        for age, admitted in ((5.0, False), (121.0, False), (599.0, False), (601.0, True)):
            with self.subTest(age=age):
                self.assertEqual(hs.decide((5, 6), loaded, [holder(age=age, cores=5, gb=6)],
                                           10_000.0, CONFIG, 700.0)[0], admitted)

    def _running(self, label):
        """A real job holding its slot, with an idle reading."""
        child = subprocess.Popen([sys.executable, self.tool, "run", "--kind", "kill", "--label", label, "--",
                                  "sleep", "30"], env=self.env, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for _ in range(100):
            if self._holders():
                return child
            time.sleep(0.1)
        self.fail("the job never held its slot")

    def test_a_second_job_starts_while_the_first_runs(self):
        self._running("first")
        out = self._run("run", "--kind", "kill", "--label", "second", "--", "true")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("waiting on", out.stderr)
        self.assertEqual(list((self.state / "tickets").glob("*.ticket")), [])

    def test_the_wait_line_comes_once_a_minute_and_names_the_holder(self):
        self._running("first")
        loaded, now, slept = hs.Reading(8, 30.0, 20.0, 90.0, 0.0), [time.time()], []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
            if sum(slept) >= 300:
                raise TimeoutError

        with self._managed(), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(TimeoutError):
            hs._admit("kill", "t", self.state, CONFIG, read=lambda: loaded, sleep=sleep, clock=lambda: now[0])
        lines = [line for line in err.getvalue().splitlines() if "waiting on" in line]
        self.assertEqual(len(lines), 5)
        self.assertTrue(all("kill first" in line for line in lines), lines)

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
