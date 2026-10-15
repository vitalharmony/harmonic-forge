"""hrse#2017: prove the stale-lock and orphaned-worktree-stack reaping never
touches a live holder (AC2), never touches the main checkout (AC4), and
actually frees debris (AC1, AC3), end to end (AC7) -- including the four
gaps a preclose-inspection pass found in the first version of this diff:
release-before-unlink on the lock (AC2), process-compose restarting a
SIGTERMed uvicorn child so the port is never actually freed (AC3), a live
non-Claude (e.g. Codex) occupant of a lane worktree being killed anyway
(AC4's class of harm), and a tautological termination assertion."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes_reap  # noqa: E402


def _write_stale_lock(path: Path, pid: int = 999999, session_pid: int = 999998) -> None:
    # 999999/999998 are not real pids in any test environment; a dead
    # pid/session_pid is exactly what "stale" means here.
    path.write_text(json.dumps({"pid": pid, "start": "2026-09-14T23:34:53Z",
                                 "session_pid": session_pid}))


def _wait_for_proc(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not (Path("/proc") / str(pid)).exists():
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _spawn_fake_process(*extra_argv: str, cwd: Path | None = None):
    """A real, live process whose /proc/<pid>/cmdline contains extra_argv
    verbatim -- python3 -c passes trailing args through as sys.argv, so this
    fakes a uvicorn/vite/process-compose/Codex command line without needing
    any of them installed."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *extra_argv],
        cwd=str(cwd) if cwd else None,
    )
    _wait_for_proc(proc.pid)
    return proc


_REAL_LISTDIR = os.listdir


def _only_procs(*procs):
    """Restrict the reaper's /proc enumeration to this test's own processes,
    so no real process on the machine is scanned or can be signalled."""
    pids = [str(p.pid) for p in procs]

    def fake_listdir(path="."):
        if os.fspath(path) == "/proc":
            return list(pids)
        return _REAL_LISTDIR(path)

    return mock.patch.object(lanes_reap.os, "listdir", side_effect=fake_listdir)


# ---------------------------------------------------------------------------
# reap_orphaned_lane_worktree_servers
# ---------------------------------------------------------------------------

def _make_worktree(tmp_path, name: str, port: str | None = None) -> Path:
    root = tmp_path / name
    root.mkdir()
    if port is not None:
        (root / ".mise.local.toml").write_text(
            f'[env]\nPROCESS_COMPOSE_PORT = "{port}"\n'
        )
    return root


class LanesReapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()

    # ---------------------------------------------------------------------------
    # reap_stale_belt_locks
    # ---------------------------------------------------------------------------

    def test_removes_lock_whose_holder_is_dead(self):
        lock_dir = self.tmp_path / "harmonic-forge"
        lock_dir.mkdir()
        stale = lock_dir / "belt-lane3.lock"
        _write_stale_lock(stale)

        removed = lanes_reap.reap_stale_belt_locks(lock_dir)

        assert [name for name, _ in removed] == ["belt-lane3.lock"]
        assert not stale.exists()


    def test_never_removes_a_lock_a_live_process_holds(self):
        # AC2: hold a real flock on the file for the duration of the check, the
        # same way _acquire_belt_lock holds one -- proving liveness by the same
        # mechanism the code under test uses to detect it, not by inspection.
        lock_dir = self.tmp_path / "harmonic-forge"
        lock_dir.mkdir()
        live = lock_dir / "belt-lane1.lock"
        live.write_text(json.dumps({"pid": 12345, "start": "now", "session_pid": 12344}))

        fh = open(live, "r+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            removed = lanes_reap.reap_stale_belt_locks(lock_dir)

            assert removed == []
            assert live.exists()
            assert json.loads(live.read_text())["pid"] == 12345
        finally:
            fh.close()


    def test_unlink_happens_while_still_holding_the_lock(self):
        # Preclose finding: the original implementation released the flock
        # (closing `fh` inside `with fh:`) BEFORE calling unlink, leaving a
        # window where another process's fresh open()+flock() on the
        # still-existing path could succeed and then be orphaned by our delete.
        # Prove the fix holds the lock through the unlink call: instrument
        # Path.unlink so that, at the moment it runs, a second file handle on
        # the SAME path attempting a non-blocking flock must fail (contended by
        # the reaper's own still-held lock) rather than succeed.
        lock_dir = self.tmp_path / "harmonic-forge"
        lock_dir.mkdir()
        stale = lock_dir / "belt-lane2.lock"
        _write_stale_lock(stale)

        observed = {}
        real_unlink = Path.unlink

        def spying_unlink(self, *a, **kw):
            probe = open(self, "r+")
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed["still_locked_at_unlink_time"] = True
            else:
                observed["still_locked_at_unlink_time"] = False
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            finally:
                probe.close()
            return real_unlink(self, *a, **kw)

        with mock.patch.object(Path, "unlink", spying_unlink):
            removed = lanes_reap.reap_stale_belt_locks(lock_dir)

        assert [name for name, _ in removed] == ["belt-lane2.lock"]
        assert observed["still_locked_at_unlink_time"] is True


    def test_leaves_non_session_lock_files_alone(self):
        lock_dir = self.tmp_path / "harmonic-forge"
        lock_dir.mkdir()
        (lock_dir / "gh_core_budget.json.lock").write_text("")
        (lock_dir / "gh_scan_override.lock").write_text("")
        # A malformed/foreign .lock file with no pid/session_pid shape.
        (lock_dir / "something_else.lock").write_text('{"note": "not a session lock"}')

        removed = lanes_reap.reap_stale_belt_locks(lock_dir)

        assert removed == []
        assert (lock_dir / "gh_core_budget.json.lock").exists()
        assert (lock_dir / "gh_scan_override.lock").exists()
        assert (lock_dir / "something_else.lock").exists()


    def test_missing_lock_dir_is_a_noop(self):
        assert lanes_reap.reap_stale_belt_locks(self.tmp_path / "does-not-exist") == []


    def test_stops_via_systemd_unit_when_one_is_active(self):
        # AC3 (preclose fix): selection must not stop at SIGTERMing the uvicorn
        # child, because process-compose's exit_on_failure restart policy just
        # brings it back and the port stays held. Prove the unit-stop path is
        # actually chosen and given the right unit name, via an injected stub
        # (no real systemd/process-compose needed for this assertion) --
        # decision logic is what had the bug, and this is what proves it fixed.
        root = _make_worktree(self.tmp_path, "HRSE2-lane3", port="8004")
        proc = _spawn_fake_process(
            str(root / "backend/.venv/bin/python3.13"), "uvicorn",
            "app.main:app", "--host", "0.0.0.0", "--port", "8004",
        )
        stop_calls = []

        def fake_stop_unit(unit: str) -> bool:
            stop_calls.append(unit)
            proc.kill()  # simulate the unit stop actually taking the child down
            return True

        try:
            with _only_procs(proc):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(stop_unit=fake_stop_unit)

            matches = [r for r in results if r["worktree"] == str(root)]
            assert len(matches) == 1
            assert matches[0]["action"] == "stopped-unit"
            assert matches[0]["unit"] == "hrse-process-compose-8004.service"
            assert stop_calls == ["hrse-process-compose-8004.service"]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


    def test_falls_back_to_direct_kill_and_actually_terminates(self):
        # No resolvable systemd unit (no .mise.local.toml) -- must fall back to
        # signalling the matched pid directly, and the process must actually be
        # dead afterward (preclose fix: the original test's
        # `assert proc.wait(...) != 0 or True` was a tautology that passed even
        # with the kill logic deleted).
        root = _make_worktree(self.tmp_path, "HRSE2-lane3")  # no port file
        proc = _spawn_fake_process(
            str(root / "backend/.venv/bin/python3.13"), "uvicorn",
            "app.main:app", "--host", "0.0.0.0", "--port", "8004",
        )
        try:
            def stub_stop_unit(unit: str) -> bool:
                raise AssertionError("no systemd unit should be resolvable here")

            with _only_procs(proc):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(stop_unit=stub_stop_unit)

            matches = [r for r in results if r["worktree"] == str(root)]
            assert len(matches) == 1
            assert matches[0]["action"] == "stopped-pids"
            # `proc` is this test's own child, so it is a zombie (still
            # "alive" to os.kill(pid, 0)) until reaped here via wait() -- in
            # production the process-compose/systemd parent reaps it instead.
            # wait() completing promptly, rather than timing out, is the real
            # assertion: the SIGTERM/SIGKILL was actually sent.
            proc.wait(timeout=5)
            assert not _alive(proc.pid)  # real assertion, not a tautology
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


    def test_never_stops_a_worktree_with_a_live_foreign_occupant(self):
        # Preclose finding: the guard the comment relies on ("orphaned by
        # construction") only ever confirmed Claude sessions are dead.  A live
        # Codex (or anything else) process with its cwd inside the worktree is
        # invisible to that guard and must not have its dev-server stack pulled
        # out from under it.
        root = _make_worktree(self.tmp_path, "HRSE2-lane2", port="8003")
        dev_server = _spawn_fake_process(
            str(root / "backend/.venv/bin/python3.13"), "uvicorn",
            "app.main:app", "--host", "0.0.0.0", "--port", "8003",
        )
        # Simulate a live Codex Lane 2 session: any live process whose cwd is
        # inside the worktree and isn't part of the dev-server stack itself.
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(root),
        )
        _wait_for_proc(foreign.pid)

        def must_not_be_called(unit: str) -> bool:
            raise AssertionError("stop_unit must not be called for a busy worktree")

        try:
            with _only_procs(dev_server, foreign):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(stop_unit=must_not_be_called)

            matches = [r for r in results if r["worktree"] == str(root)]
            assert len(matches) == 1
            assert matches[0]["action"] == "skipped-busy"
            assert foreign.pid in {pid for pid, _ in matches[0]["foreign"]}
            # The dev server itself was never touched.
            assert dev_server.poll() is None
        finally:
            for p in (dev_server, foreign):
                if p.poll() is None:
                    p.kill()
                    p.wait()


    def test_never_stops_the_main_checkout_dev_server(self):
        # AC4: same dev-server binaries, but the main checkout's own path has no
        # "-lane2"/"-lane3" segment, so it is never even a candidate.
        proc = _spawn_fake_process(
            "HRSE2/backend/.venv/bin/python3.13", "uvicorn",
            "app.main:app", "--host", "0.0.0.0", "--port", "8002",
        )
        try:
            def must_not_be_called(unit: str) -> bool:
                raise AssertionError("stop_unit must not be called for the main checkout")

            with _only_procs(proc):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(stop_unit=must_not_be_called)

            assert all(proc.pid not in r.get("pids", []) for r in results)
            assert proc.poll() is None  # still alive
        finally:
            proc.kill()
            proc.wait()


    def test_ignores_lane_worktree_processes_that_are_not_dev_servers(self):
        # A process whose cmdline mentions a lane worktree path but is not
        # uvicorn/vite (e.g. an editor or shell) must never be a candidate.
        root = _make_worktree(self.tmp_path, "HRSE2-lane2")
        proc = _spawn_fake_process(str(root / "some/other/tool.py"))
        try:
            with _only_procs(proc):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(
                    stop_unit=lambda unit: (_ for _ in ()).throw(AssertionError("unexpected"))
                )

            assert results == []
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait()


    # ---------------------------------------------------------------------------
    # CLI, end to end
    # ---------------------------------------------------------------------------

    def test_cli_end_to_end(self):
        # AC7: create a stale lock and an orphaned worktree dev-server stack
        # (with an active-looking systemd unit simulated via a stub), run the
        # cleanup entry points exactly as the shell script would, show both
        # handled and confirm the main checkout's equivalent is untouched.
        lock_dir = self.tmp_path / "harmonic-forge"
        lock_dir.mkdir()
        stale = lock_dir / "sweep-lane3.lock"
        _write_stale_lock(stale)

        lock_result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("lanes_reap.py")),
             "reap-belt-locks", str(lock_dir)],
            capture_output=True, text=True, check=True,
        )
        assert "removed stale lock sweep-lane3.lock" in lock_result.stdout
        assert not stale.exists()

        root = _make_worktree(self.tmp_path, "HRSE2-lane3", port="8004")
        lane_proc = _spawn_fake_process(
            str(root / "frontend/node_modules/.bin/vite"), "vite", "--host", "--port", "5175",
        )
        main_root = _make_worktree(self.tmp_path, "HRSE2")
        main_proc = _spawn_fake_process(
            str(main_root / "frontend/node_modules/.bin/vite"), "vite", "--host", "--port", "5173",
        )
        try:
            with _only_procs(lane_proc, main_proc):
                results = lanes_reap.reap_orphaned_lane_worktree_servers(
                    stop_unit=lambda unit: (lane_proc.kill(), True)[1]
                )

            lane_matches = [r for r in results if r["worktree"] == str(root)]
            assert lane_matches and lane_matches[0]["action"] == "stopped-unit"
            assert lane_matches[0]["unit"] == "hrse-process-compose-8004.service"
            lane_proc.wait(timeout=5)
            assert not _alive(lane_proc.pid)

            assert all(r["worktree"] != str(main_root) for r in results)
            assert main_proc.poll() is None
        finally:
            for p in (lane_proc, main_proc):
                if p.poll() is None:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    unittest.main()
