"""harmonic-forge#1531: lane launches on hosts without systemd or /proc.

TC1: no working inhibitor -> one warning, an empty prefix (the CLI still runs).
TC2: a failing systemd-inhibit and a caffeinate -> `caffeinate -i`.
TC2b: a working systemd-inhibit -> the lane's own --who/--why, unchanged.
TC3: no /proc -> check_worktree_busy exits 3; the refresh treats only 3 as
     not busy; lane1_rebase refuses on 3 as on 1.
TC4: instance.py get: absent keys answer nothing, invalid values exit 1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parent
TOOLS = LANE.parent


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


class Inhibitor(unittest.TestCase):
    def run_inhibitor(self, stubs: dict[str, str], wsl: bool = False) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            for name, body in stubs.items():
                stub(bin_dir, name, body)
            for tool in ("timeout", "bash", "printf", "dirname", "readlink"):
                found = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True,
                                       text=True).stdout.strip()
                if found and not (bin_dir / tool).exists():
                    (bin_dir / tool).symlink_to(found)
            env = {"PATH": str(bin_dir), "HOME": tmp}
            if wsl:
                env["WSL_DISTRO_NAME"] = "Ubuntu"
            script = (f'source "{LANE / "_lane_cleanup.sh"}"\n'
                      'lane_inhibitor "lane2" "Lane 2 (implementer) Claude Code session"\n'
                      'printf "%s\\n" "${#lane_inhibit[@]}" "${lane_inhibit[@]}"\n')
            return subprocess.run([str(bin_dir / "bash"), "-c", script], env=env,
                                  capture_output=True, text=True, timeout=30)

    def test_tc1_no_inhibitor_warns_once_and_runs_bare(self) -> None:
        proc = self.run_inhibitor({"systemd-inhibit": "exit 1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[0], "0")
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertIn("no sleep inhibitor", proc.stderr)
        wsl = self.run_inhibitor({"systemd-inhibit": "exit 1"}, wsl=True)
        self.assertIn("keep Windows awake", wsl.stderr)

    def test_tc2_caffeinate_when_systemd_inhibit_fails(self) -> None:
        proc = self.run_inhibitor({"systemd-inhibit": "exit 1", "caffeinate": "exit 0"})
        self.assertEqual(proc.stdout.split("\n")[:3], ["2", "caffeinate", "-i"])
        self.assertEqual(proc.stderr, "")

    def test_tc2b_working_systemd_inhibit_keeps_the_lane_strings(self) -> None:
        proc = self.run_inhibitor({"systemd-inhibit": 'exit 0', "caffeinate": "exit 0"})
        self.assertEqual(proc.stdout.split("\n")[:6],
                         ["5", "systemd-inhibit", "--what=sleep:idle", "--who=lane2",
                          "--why=Lane 2 (implementer) Claude Code session", "--mode=block"])


class NoProc(unittest.TestCase):
    def test_tc3_busy_check_cannot_tell_exits_3(self) -> None:
        sys.path.insert(0, str(TOOLS / "worktree"))
        import check_worktree_busy as cwb  # noqa: PLC0415
        real_is_dir = Path.is_dir

        def is_dir(self: Path) -> bool:
            return False if str(self) == "/proc" else real_is_dir(self)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(Path, "is_dir", is_dir), \
                mock.patch.object(sys, "argv", ["check_worktree_busy.py", tmp]):
            self.assertEqual(cwb.main(), 3)

    def test_tc3_refresh_treats_only_3_as_not_busy(self) -> None:
        text = (LANE / "_lane_refresh.sh").read_text()
        self.assertIn('if [ "$busy_rc" = 3 ]; then', text)
        self.assertIn('elif [ "$busy_rc" != 0 ]; then\n      LANE_REFRESH_STATUS="skipped-busy"', text)

    def test_tc3_lane1_rebase_refuses_on_3(self) -> None:
        sys.path.insert(0, str(LANE))
        import lane1_rebase as lr  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            prep = Path(tmp) / "x-1-prep"
            prep.mkdir()
            done = subprocess.CompletedProcess([], 3, "", "cannot tell (no /proc)")
            with mock.patch.object(lr.subprocess, "run", return_value=done), \
                    mock.patch.object(lr, "remove_prep") as removed:
                with self.assertRaises(lr.Exit):
                    lr.prune_stale_prep(tmp, prep)
                removed.assert_not_called()


class InstanceGet(unittest.TestCase):
    def get(self, text: str | None, key: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as xdg:
            if text is not None:
                (Path(xdg) / "harmonic-forge").mkdir()
                (Path(xdg) / "harmonic-forge" / "instance.toml").write_text(text)
            return subprocess.run([sys.executable, "-I", "-E", str(TOOLS / "settings" / "instance.py"),
                                   "get", key], env={**os.environ, "XDG_CONFIG_HOME": xdg},
                                  capture_output=True, text=True, timeout=30)

    def test_tc4_absent_is_empty_invalid_refuses(self) -> None:
        for key in ("lane3_policy", "codex_hook_paths"):
            with self.subTest(key=key, case="absent"):
                proc = self.get("", key)
                self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        for text, key in (('lane3_policy = "rel/x.json"\n', "lane3_policy"),
                          ('lane3_policy = "~/x.json"\n', "lane3_policy"),
                          ('codex_hook_paths = "/x"\n', "codex_hook_paths"),
                          ('codex_hook_paths = ["rel"]\n', "codex_hook_paths"),
                          # Sticky-wicket PATCH (Codex): `get` NUL-terminates each
                          # value, so a control character must refuse, not truncate.
                          ('lane3_policy = "/x\\u0000y.json"\n', "lane3_policy"),
                          ('lane3_policy = "/x\\ny.json"\n', "lane3_policy"),
                          ('lane3_policy = "/x\\u007fy.json"\n', "lane3_policy"),
                          ('codex_hook_paths = ["/a\\u0000b"]\n', "codex_hook_paths"),
                          ('codex_hook_paths = ["/a\\nb"]\n', "codex_hook_paths")):
            with self.subTest(text=text):
                proc = self.get(text, key)
                self.assertEqual((proc.returncode, proc.stdout), (1, ""), proc.stderr)
        proc = self.get('codex_hook_paths = ["/a/hooks.json", "/b/hooks.json"]\n', "codex_hook_paths")
        self.assertEqual(proc.stdout, "/a/hooks.json\0/b/hooks.json\0")


if __name__ == "__main__":
    unittest.main()
