#!/usr/bin/env python3
"""Orphan reaper on fixture /proc trees (harmonic-forge#1181 AC1-AC4, AC6).
No test reads the real /proc for a decision or sends a real signal."""
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_cgroup_delegation as delegation  # noqa: E402
import orphan_reap as reap  # noqa: E402

UID = 1000
HZ = 100
UPTIME = 100_000.0  # seconds


class FakeProc:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.origin = self.root / "worktrees"
        self.origin.mkdir()
        self.lock = self.root / "lock"
        (self.root / "uptime").write_text(f"{UPTIME} 0\n")

    def add(self, pid, ppid, comm="bash", cmdline="bash", cwd=None, age_s=10 * 3600,
            cpu_s=0.0, cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.scope",
            uid=UID):
        d = self.root / str(pid)
        d.mkdir()
        start = int((UPTIME - age_s) * HZ)
        ticks = int(cpu_s * HZ)
        fields = ["S", ppid] + [0] * 9 + [ticks, 0] + [0] * 6 + [start]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_text(cmdline.replace(" ", "\0"))
        (d / "cgroup").write_text(cgroup + "\n")
        os.symlink(str(cwd or self.origin / "w"), d / "cwd")
        return start

    def burn(self, pid, cpu_s):
        """Raise a process's CPU ticks, as if it ran during the sample."""
        stat = (self.root / str(pid) / "stat").read_text()
        head, rest = stat[:stat.rindex(")") + 2], stat[stat.rindex(")") + 2:].split()
        rest[11] = str(int(rest[11]) + int(cpu_s * HZ))
        (self.root / str(pid) / "stat").write_text(head + " ".join(rest) + "\n")


def manager(fake):
    fake.add(2102, 1, comm="systemd", cmdline="/usr/lib/systemd/systemd --user", age_s=99_000,
             cpu_s=5, cgroup="0::/user.slice/user-1000.slice/user@1000.service/init.scope",
             cwd=fake.root)


class ScanTests(unittest.TestCase):  # AC1
    def setUp(self):
        self.fake = FakeProc()
        manager(self.fake)

    def scan(self):
        return {p.pid for p in reap.scan(self.fake.root, uid=UID, lock=self.fake.lock,
                                         origin_dirs=(self.fake.origin,))}

    def test_orphan_of_agent_origin_selected(self):
        self.fake.add(500, 2102)
        self.fake.add(501, 2102, cmdline="bash -c source /home/u/.claude/shell-snapshots/s.sh",
                      cwd=self.fake.root)
        self.assertEqual(self.scan(), {500, 501})

    def test_desktop_service_not_selected(self):
        self.fake.add(600, 2102, comm="pipewire", cmdline="pipewire", cwd=self.fake.root)
        self.assertEqual(self.scan(), set())

    def test_live_parent_not_selected(self):
        self.fake.add(700, 2102, comm="claude", cmdline="claude", cwd=self.fake.root)
        self.fake.add(701, 700)
        self.assertEqual(self.scan(), set())

    def test_lock_holder_descendant_not_selected(self):
        self.fake.add(800, 2102)
        self.fake.add(801, 800)
        self.fake.lock.write_text("pid 800 l1-post br@aaaa")
        self.assertEqual(self.scan(), set())

    def test_systemd_run_service_not_selected(self):
        self.fake.add(900, 2102, cgroup="0::/user.slice/user-1000.slice/user@1000.service/"
                                        "app.slice/hrse-process-compose-9996.service")
        self.assertEqual(self.scan(), set())

    def test_check_descendant_not_selected(self):
        self.fake.add(1000, 2102, cmdline="mise run check")
        self.fake.add(1001, 1000)
        self.assertEqual(self.scan(), set())

    def test_other_users_not_selected(self):
        self.fake.add(1100, 2102, uid=1001)
        self.assertEqual(self.scan(), set())


class PredicateTests(unittest.TestCase):  # AC2
    def test_idle_predicate(self):
        self.assertEqual(reap.classify(0.5, 0.5), "idle")
        self.assertIsNone(reap.classify(0.5, 5.0))
        self.assertIsNone(reap.classify(5.0, 0.5))

    def test_runaway_predicate(self):
        self.assertEqual(reap.classify(94.0, 90.0), "runaway")
        self.assertIsNone(reap.classify(94.0, 10.0))
        self.assertIsNone(reap.classify(30.0, 90.0))

    def test_allowlist_exactly_two(self):
        self.assertTrue(reap.allowlisted("podman-compose -f x.yml up -d hrse-web"))
        self.assertTrue(reap.allowlisted("/usr/bin/python3 /usr/bin/podman-compose up -d"))
        self.assertTrue(reap.allowlisted("python3 -c import glob"))
        for cmd in ("podman-compose down", "python3 script.py", "bash -c sleep 1", "node x.js",
                    "uvicorn app:app", ""):
            with self.subTest(cmd=cmd):
                self.assertFalse(reap.allowlisted(cmd))


class RunTests(unittest.TestCase):  # AC2-AC4
    def setUp(self):
        self.fake = FakeProc()
        manager(self.fake)
        self.state = self.fake.root / "state.json"
        self.sent, self.ntfy, self.events = [], [], []
        self.clock = [1_000_000.0]

    def run_once(self, execute=True, sample=60, burn=None):
        def sleep(seconds):
            if seconds == sample and burn:
                for pid, cpu in burn.items():
                    self.fake.burn(pid, cpu)
        return reap.run(proc_root=self.fake.root, execute=execute, sample_seconds=sample,
                        state_path=self.state, lock=self.fake.lock, origin_dirs=(self.fake.origin,),
                        now=lambda: self.clock[0], sleep=sleep, send=lambda p, s: self.sent.append((p, s)),
                        ntfy=self.ntfy.append, events=self.events.extend, uptime=UPTIME, hz=HZ)

    def test_no_stop_on_first_report(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        found = self.run_once()
        self.assertEqual(found[0]["action"], "reported")
        self.assertEqual(self.sent, [])

    def test_stop_after_grace_on_a_later_run(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        self.run_once()
        self.clock[0] += 2 * 3600
        found = self.run_once()
        self.assertIn(found[0]["action"], ("terminated", "killed"))
        self.assertEqual(self.sent[0], (500, signal.SIGTERM))

    def test_not_stopped_inside_grace(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        self.run_once()
        self.clock[0] += 0.5 * 3600
        self.run_once()
        self.assertEqual(self.sent, [])

    def test_pid_reuse_not_stopped(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        self.run_once()
        state = json.loads(self.state.read_text())
        (key,) = state
        state = {"500:1": state[key]}  # the earlier sighting was a different start time
        self.state.write_text(json.dumps(state))
        self.clock[0] += 2 * 3600
        found = self.run_once()
        self.assertEqual(found[0]["action"], "reported")
        self.assertEqual(self.sent, [])

    def test_recheck_before_sigkill(self):
        start = self.fake.add(500, 2102, cmdline="python3 -c x")
        sent = []

        def send(pid, sig):
            sent.append(sig)
            if sig == signal.SIGTERM:  # the pid exits and is reused while we wait
                (self.fake.root / "500" / "stat").write_text(
                    "500 (bash) S 1 " + " ".join(["0"] * 17) + " 9\n")
        result = reap.stop(self.fake.root, 500, start, send=send, sleep=lambda s: None)
        self.assertEqual(sent, [signal.SIGTERM])
        self.assertEqual(result, "terminated")

    def test_corrupt_state_stops_nothing(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        for corrupt in ("{not json", '["500:1"]', '{"x": "yesterday"}'):
            with self.subTest(corrupt=corrupt):
                self.state.write_text(corrupt)
                self.clock[0] += 10 * 3600
                self.run_once()
                self.assertEqual(self.sent, [])
                self.state.unlink()

    def test_non_allowlisted_is_report_only(self):
        self.fake.add(500, 2102, cmdline="node server.js")
        self.run_once()
        self.clock[0] += 48 * 3600
        found = self.run_once()
        self.assertEqual(found[0]["action"], "reported")
        self.assertEqual(self.sent, [])

    def test_dry_run_never_signals(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        self.run_once(execute=False)
        self.clock[0] += 2 * 3600
        found = self.run_once(execute=False)
        self.assertEqual(found[0]["action"], "would-stop")
        self.assertEqual(self.sent, [])

    def test_young_process_not_an_orphan(self):
        self.fake.add(500, 2102, cmdline="python3 -c x", age_s=3600)
        self.assertEqual(self.run_once(), [])

    def test_runaway_needs_a_busy_sample(self):
        self.fake.add(500, 2102, cmdline="python3 -c x", age_s=10 * 3600, cpu_s=9 * 3600)
        self.assertEqual(self.run_once(), [])  # 90% lifetime, idle sample: neither class
        found = self.run_once(burn={500: 55})
        self.assertEqual(found[0]["class"], "runaway")

    def test_one_event_per_orphan_without_cmdline(self):
        self.fake.add(500, 2102, cmdline="python3 -c secret-token-here")
        self.fake.add(501, 2102, cmdline="bash -c sleep 99")
        self.run_once()
        self.assertEqual(len(self.events), 2)
        self.assertEqual({e["pid"] for e in self.events}, {500, 501})
        self.assertNotIn("secret-token-here", json.dumps(self.events))
        self.assertEqual(len(self.ntfy), 1)

    def test_no_ntfy_when_nothing_found(self):
        self.run_once()
        self.assertEqual(self.ntfy, [])
        self.assertEqual(self.events, [])

    def test_state_written_atomically_and_pruned(self):
        self.fake.add(500, 2102, cmdline="python3 -c x")
        self.run_once()
        self.assertEqual(len(json.loads(self.state.read_text())), 1)
        import shutil
        shutil.rmtree(self.fake.root / "500")
        self.run_once()
        self.assertEqual(json.loads(self.state.read_text()), {})


class DelegationTests(unittest.TestCase):  # AC6
    def write(self, text):
        root = Path(tempfile.mkdtemp())
        path = delegation.controllers_path(UID, root)
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return root

    def test_delegation_check_fails_without_memory(self):
        self.assertEqual(delegation.missing(UID, self.write("pids\n")), ["cpu", "memory"])
        self.assertEqual(delegation.missing(UID, self.write("cpu pids\n")), ["memory"])

    def test_delegation_check_passes_with_cpu_and_memory(self):
        self.assertEqual(delegation.missing(UID, self.write("cpu memory pids\n")), [])

    def test_unreadable_controllers_fail_closed(self):
        self.assertEqual(delegation.missing(UID, Path(tempfile.mkdtemp())), ["cpu", "memory"])


if __name__ == "__main__":
    unittest.main()
