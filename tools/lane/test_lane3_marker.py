#!/usr/bin/env python3
"""The marker guard's protocol half (harmonic-forge#721).

Everything here is repo-independent: the `/proc` parse, the ancestry walk, the
three outcomes, and -- the part #721 actually moves -- what happens when the
consuming repo declares no `lease` adapter. That case must ALLOW and say so
loudly, because this guard's anti-deadlock property outranks ADR-008 AC4's
refuse-on-unavailable posture.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gate"))

import lane3_marker as marker  # noqa: E402

LEASE_MODULE = '''
def held(**kwargs):
    return {"status": "fail", "evidence": ["gate lease held by session 4242"]}


def free(**kwargs):
    return {"status": "pass", "evidence": ["no lease held"]}


def explodes(**kwargs):
    raise RuntimeError("lease store unreachable")
'''


def _worktree(entrypoint: str | None) -> Path:
    """A git checkout whose manifest declares (or omits) a lease adapter."""
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "lease.py").write_text(LEASE_MODULE, encoding="utf-8")
    (tmp / ".claude").mkdir()
    manifest: dict = {}
    if entrypoint is not None:
        manifest["lease"] = {"module": "scripts/lease.py", "check_owner": entrypoint}
    (tmp / ".claude" / "gate-adapter.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp


def _marker(worktree: Path, owner: int | None) -> Path:
    """A LANE3_ACTIVE marker where `lane3-begin` writes it: the git dir."""
    path = worktree / ".git" / "LANE3_ACTIVE"
    path.write_text(f"owner_pid={owner}\n" if owner is not None else "started\n",
                    encoding="utf-8")
    return path


DEAD_PID = 2 ** 22 - 1  # above /proc/sys/kernel/pid_max on any normal Linux


class ProcParsingTests(unittest.TestCase):
    def test_a_comm_containing_spaces_and_parens_still_yields_the_ppid(self) -> None:
        line = "1234 (we ird) (name)) S 99 1234 0 0 -1 4194304 1 0"
        self.assertEqual(marker.parse_stat(line), ("we ird) (name)", 99))

    def test_a_plain_line(self) -> None:
        self.assertEqual(marker.parse_stat("7 (bash) S 3 7 0")[1], 3)

    def test_a_malformed_line_is_none_not_a_crash(self) -> None:
        for raw in ("", "no parens here", "12 (bash)", "12 (bash) S notapid"):
            self.assertIsNone(marker.parse_stat(raw), raw)

    def test_ancestry_starts_at_this_process_and_reaches_init(self) -> None:
        chain = marker.ancestry()
        self.assertEqual(chain[0], os.getpid())
        self.assertGreater(len(chain), 1)

    def test_session_owner_skips_shell_and_mise_plumbing(self) -> None:
        owner = marker.session_owner()
        if owner is not None:
            comm = marker._stat(owner)[0]
            self.assertNotIn(comm, marker._TRANSPARENT)

    def test_marker_owner_reads_the_recorded_pid(self) -> None:
        wt = _worktree(None)
        self.assertEqual(marker.marker_owner(_marker(wt, 4242)), 4242)
        self.assertIsNone(marker.marker_owner(_marker(wt, None)))
        self.assertIsNone(marker.marker_owner(wt / ".git" / "NOPE"))


class RefusalTests(unittest.TestCase):
    def test_no_marker_allows(self) -> None:
        self.assertIsNone(marker.refusal(_worktree(None) / ".git" / "absent"))

    def test_a_marker_older_than_the_cap_allows(self) -> None:
        path = _marker(_worktree("held"), DEAD_PID)
        now = path.stat().st_mtime + marker.MARKER_MAX_AGE_SECONDS + 1
        self.assertIsNone(marker.refusal(path, now=now))

    def test_an_ownerless_marker_allows_with_a_warning(self) -> None:
        path = _marker(_worktree("held"), None)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(marker.refusal(path))
        self.assertIn("no recorded owner", err.getvalue())

    def test_my_own_gates_marker_allows(self) -> None:
        path = _marker(_worktree("held"), os.getpid())
        self.assertIsNone(marker.refusal(path))

    def test_another_live_session_refuses(self) -> None:
        # PID 1 is alive and is not in this process's ancestry chain as an
        # owner candidate... except it IS the chain's end. Use the parent of
        # a process we know is not ours: a live child we spawn and keep.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        path = _marker(_worktree("held"), child.pid)
        message = marker.refusal(path, pid=os.getpid())
        self.assertIsNotNone(message)
        self.assertIn("another live Lane 3 session", message)
        self.assertIn(str(child.pid), message)


class DeadOwnerAsksTheLeaseAdapterTests(unittest.TestCase):
    """The #721 seam itself: a dead owner delegates to the repo's lease."""

    def test_lease_still_held_refuses_with_the_adapters_evidence(self) -> None:
        path = _marker(_worktree("held"), DEAD_PID)
        message = marker.refusal(path)
        self.assertIsNotNone(message)
        self.assertIn("gate lease held by session 4242", message)
        self.assertIn("handed over mid-run", message)

    def test_lease_free_allows_the_abandoned_gate_to_be_released(self) -> None:
        self.assertIsNone(marker.refusal(_marker(_worktree("free"), DEAD_PID)))

    def test_no_lease_adapter_declared_allows_loudly(self) -> None:
        """ADR-008 AC4 says report BLOCKED explicitly; this guard's own
        anti-deadlock rule says never refuse on an unproven claim. Both:
        printed, and allowed."""
        path = _marker(_worktree(None), DEAD_PID)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(marker.refusal(path))
        self.assertIn("BLOCKED", err.getvalue())
        self.assertIn("not declared", err.getvalue())

    def test_an_adapter_that_raises_also_allows_loudly(self) -> None:
        path = _marker(_worktree("explodes"), DEAD_PID)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(marker.refusal(path))
        self.assertIn("BLOCKED", err.getvalue())
        self.assertIn("lease store unreachable", err.getvalue())

    def test_a_marker_whose_worktree_cannot_be_resolved_allows_silently(self) -> None:
        loose = Path(tempfile.mkdtemp()) / "LANE3_ACTIVE"
        loose.write_text(f"owner_pid={DEAD_PID}\n", encoding="utf-8")
        self.assertIsNone(marker.refusal(loose))

    def test_a_linked_worktrees_marker_resolves_to_its_own_checkout(self) -> None:
        main = _worktree("held")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"],
                       cwd=main, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        linked = Path(tempfile.mkdtemp()) / "wt"
        subprocess.run(["git", "worktree", "add", "-q", str(linked), "-b", "side"],
                       cwd=main, check=True)
        gitdir = Path(subprocess.run(
            ["git", "-C", str(linked), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True).stdout.strip())
        self.assertEqual(marker._worktree_of(gitdir / "LANE3_ACTIVE").resolve(),
                         linked.resolve())


class CliTests(unittest.TestCase):
    def test_print_owner_is_the_same_function_the_writer_stamps_with(self) -> None:
        out = subprocess.run([sys.executable, str(HERE / "lane3_marker.py"), "--print-owner"],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().isdigit(), out.stdout)

    def test_a_refusal_exits_nonzero(self) -> None:
        path = _marker(_worktree("held"), DEAD_PID)
        out = subprocess.run([sys.executable, str(HERE / "lane3_marker.py"), str(path)],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 1)
        self.assertIn("refusing", out.stderr)

    def test_an_allow_exits_zero(self) -> None:
        path = _marker(_worktree("free"), DEAD_PID)
        out = subprocess.run([sys.executable, str(HERE / "lane3_marker.py"), str(path)],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
