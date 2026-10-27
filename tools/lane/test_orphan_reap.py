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
        self.btime = 1_700_000_000
        (self.root / "stat").write_text(f"cpu 0 0 0 0\nbtime {self.btime}\n")

    def add(self, pid, ppid, comm="bash", cmdline="bash", cwd=None, age_s=10 * 3600,
            cpu_s=0.0, cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.scope",
            uid=UID, session=None, argv=None):
        d = self.root / str(pid)
        d.mkdir()
        start = int((UPTIME - age_s) * HZ)
        ticks = int(cpu_s * HZ)
        fields = ["S", ppid, 0, session or pid] + [0] * 7 + [ticks, 0] + [0] * 6 + [start]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_text("\0".join(argv) if argv else cmdline.replace(" ", "\0"))
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
                                         origin_dirs=(self.fake.origin,), hz=HZ)}

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
        """The holder's work, reparented to the manager when its parent
        exited, is protected by its session."""
        self.fake.add(800, 2102)
        self.fake.add(801, 2102, session=800)
        self.fake.add(802, 2102, session=999)  # unrelated orphan: still selected
        self.fake.lock.write_text("pid 800 l1-post br@aaaa")
        with self.held_lock():
            self.assertEqual(self.scan(), {802})

    def test_released_lock_protects_nothing(self):
        """The text names only the last holder: once the flock is released, the
        recorded pid (still alive, same start time) protects nothing."""
        self.fake.add(800, 2102)
        self.fake.lock.write_text("pid 800 l1-post br@aaaa")
        self.assertEqual(self.scan(), {800})
        with self.held_lock():
            self.assertEqual(self.scan(), set())

    def held_lock(self):
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def held():
            with open(self.fake.lock) as handle:  # a separate open file description
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield
        return held()

    def test_stale_lock_pid_protects_nothing(self):
        """A pid started after the lock was written is a reused pid."""
        self.fake.add(800, 2102, age_s=60)
        self.fake.lock.write_text("pid 800 l1-post br@aaaa")
        os.utime(self.fake.lock, (self.fake.btime + UPTIME - 3600,) * 2)
        with self.held_lock():  # held, so only the start-time guard can reject it
            self.assertEqual(self.scan(), {800})

    def test_systemd_run_service_not_selected(self):
        self.fake.add(900, 2102, cgroup="0::/user.slice/user-1000.slice/user@1000.service/"
                                        "app.slice/hrse-process-compose-9996.service")
        self.assertEqual(self.scan(), set())

    def test_check_descendant_not_selected(self):
        self.fake.add(1000, 2102, cmdline="/usr/bin/mise run check", cwd=self.fake.root)
        self.fake.add(1001, 2102, session=1000)
        self.assertEqual(self.scan(), set())

    def test_check_recognized_by_argv_not_substring(self):
        self.fake.add(1000, 2102, cmdline="grep mise run check log", cwd=self.fake.root)
        self.fake.add(1001, 2102, session=1000)
        self.assertEqual(self.scan(), {1001})

    def test_check_with_a_space_in_its_path_recognized(self):
        """argv is read NUL-separated, so a path holding a space stays one word."""
        self.fake.add(1000, 2102, argv=["/opt/tool suite/mise", "run", "check"], cwd=self.fake.root)
        self.fake.add(1001, 2102, session=1000)
        self.assertEqual(self.scan(), set())

    def test_service_subgroup_not_selected(self):
        self.fake.add(900, 2102, cgroup="0::/user.slice/user-1000.slice/user@1000.service/"
                                        "app.slice/hrse-pc.service/payload")
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
        # 1B: a host that suspended reads a low lifetime; the busy sample decides.
        self.assertEqual(reap.classify(30.0, 90.0), "runaway")


class RunTests(unittest.TestCase):  # AC2-AC3, report-only
    def setUp(self):
        self.fake = FakeProc()
        manager(self.fake)
        self.ntfy, self.events = [], []

    def run_once(self, sample=60, burn=None, events=None):
        def sleep(seconds):
            if seconds == sample and burn:
                for pid, cpu in burn.items():
                    self.fake.burn(pid, cpu)
        return reap.run(proc_root=self.fake.root, sample_seconds=sample, lock=self.fake.lock,
                        origin_dirs=(self.fake.origin,), sleep=sleep, ntfy=self.ntfy.append,
                        events=events or self.events.extend, uptime=UPTIME, hz=HZ, uid=UID)

    def test_report_only_never_signals(self):
        """The auto-stop was deleted: no signal API is reachable from a run."""
        self.fake.add(500, 2102, cmdline="python3 -c x")
        from unittest import mock
        with mock.patch.object(reap.os, "kill") as kill:
            found = self.run_once()
        kill.assert_not_called()
        self.assertEqual(found[0]["action"], "reported")
        self.assertFalse(hasattr(reap, "stop"))
        self.assertNotIn("pidfd", (HERE / "orphan_reap.py").read_text())

    def test_young_process_not_an_orphan(self):
        self.fake.add(500, 2102, cmdline="python3 -c x", age_s=3600)
        self.assertEqual(self.run_once(), [])

    def test_runaway_needs_a_busy_sample(self):
        self.fake.add(500, 2102, cmdline="python3 -c x", age_s=10 * 3600, cpu_s=9 * 3600)
        self.assertEqual(self.run_once(), [])  # 90% lifetime, idle sample: neither class
        found = self.run_once(burn={500: 55})
        self.assertEqual(found[0]["class"], "runaway")

    def test_one_event_per_orphan_and_one_ntfy(self):
        self.fake.add(500, 2102, cmdline="python3 -c secret-token-here")
        self.fake.add(501, 2102, cmdline="bash -c sleep 99")
        self.run_once()
        self.assertEqual({e["pid"] for e in self.events}, {500, 501})
        self.assertEqual(len(self.ntfy), 1)
        self.assertNotIn("secret-token-here", self.ntfy[0])

    def test_missing_topic_file_is_reported(self):
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            reap.notify("1 orphaned agent process(es)", None)
        self.assertIn("ntfy not sent", err.getvalue())

    def test_unreadable_uptime_fails_closed(self):
        (self.fake.root / "uptime").unlink()
        with self.assertRaises(SystemExit):
            reap.run(proc_root=self.fake.root, sample_seconds=0, lock=self.fake.lock,
                     origin_dirs=(self.fake.origin,), sleep=lambda s: None,
                     ntfy=self.ntfy.append, events=self.events.extend, hz=HZ, uid=UID)

    def test_no_ntfy_when_nothing_found(self):
        self.run_once()
        self.assertEqual(self.ntfy, [])
        self.assertEqual(self.events, [])

    def test_real_telemetry_event_passes_the_schema_without_cmdline(self):
        """The real emit path, into a temporary store: the event validates
        against schema_v1.json (orphan-reap is a known source) and carries no
        cmdline."""
        self.fake.add(500, 2102, cmdline="python3 -c secret-token-here")
        store = Path(tempfile.mkdtemp())
        from unittest import mock
        sys.path.insert(0, str(HERE.parent / "telemetry"))
        import emit
        with mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(store)}):
            emit.validate  # the module the real path uses
            self.run_once(events=reap.emit_events)
        lines = [json.loads(l) for f in store.rglob("*.jsonl") for l in f.read_text().splitlines() if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["source"], "orphan-reap")
        self.assertEqual(lines[0]["attrs"]["pid"], 500)
        self.assertNotIn("secret-token-here", json.dumps(lines))


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
