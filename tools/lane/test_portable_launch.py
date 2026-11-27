"""harmonic-forge#1531: lane launches on hosts without systemd or /proc.

TC1: no working inhibitor -> one warning, an empty prefix, and a real lane 2
     launch runs the CLI bare with the same --settings (hooks still deny).
TC2: a failing systemd-inhibit and a caffeinate -> `caffeinate -i`.
TC2b: a working systemd-inhibit -> the lane's own --who/--why, unchanged.
TC3: no procfs -> check_worktree_busy exits 3; a real lane 2 refresh treats
     only 3 as not busy; lane1_rebase refuses on 3 as on 1.
TC4: instance.py get: absent keys answer nothing, invalid values exit 1.
TC5: instance.py set lane3_policy: the install step keeps instance.toml valid
     and the key top level (no final newline, a trailing [table], key present).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parent
TOOLS = LANE.parent
sys.path.insert(0, str(LANE))
# Through the module, never by name: a TestCase bound here would run twice.
import test_lane_launchers as tll  # noqa: E402


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

    def test_tc1_a_launch_with_no_inhibitor_runs_the_cli_bare(self) -> None:
        """Sticky-wicket PATCH 2 (P6): a real lane 2 launch with no working
        inhibitor runs the agent CLI itself, warns once, and hands it the same
        `--settings` as a normal launch -- the hooks still deny.

        Pitch N7: no wrapper at all. The fake systemd-inhibit logs every call
        and only the probe may appear; the agent stub records `$_`, which bash
        sets to the program it executed, so an exec-ing prefix (`env`, K-H2)
        shows as that prefix, not the CLI. `$_` cannot see a shell wrapper
        that runs the CLI (post-verdict survivor 5), so the stub also records
        its parent's argv from procfs: the parent must be the launcher shell
        itself (`bash <lane_dir>/lane2`), never an intermediate process. A
        wrapper that `exec`s leaves no process behind and bash restores SHLVL
        on exec, so it is the direct launch by construction, not a gap.
        caffeinate is kept off PATH rather than skipped on, and the recorded
        argv is read directly (`_agent_args` drops the first five)."""
        with tll._FixtureTree() as tree:
            # The host PATH, minus every directory holding a caffeinate.
            path = os.pathsep.join([str(tree.stub_bin)] + [
                d for d in os.environ["PATH"].split(os.pathsep)
                if d and not os.path.exists(os.path.join(d, "caffeinate"))])
            self.assertIsNone(shutil.which("caffeinate", path=path))
            normal = tree.run("2", [], LANE_CLI="claude", PATH=path)
            self.assertTrue(normal["launched"], normal.get("stderr"))
            calls = tree.root / "inhibit-calls.log"
            # The probe fails, as with systemd off; every call is logged.
            stub(tree.stub_bin, "systemd-inhibit",
                 f'printf "%s\\t%s\\n" "${{HF_INHIBIT_PROBE:-}}" "$*" >> "{calls}"\nexit 1')
            agent = tree.stub_bin / "claude"
            agent.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "if sys.argv[1:2] == ['--version']:\n"
                f"    print({tll.bc.STUB_VERSIONS['claude']!r}); sys.exit(0)\n"
                "with open(f'/proc/{os.getppid()}/cmdline', 'rb') as fh:\n"
                "    parent = [a.decode() for a in fh.read().split(b'\\0') if a]\n"
                "with open(os.environ['LANE_CAPTURE_OUT'], 'w') as fh:\n"
                "    json.dump({'argv': [os.path.basename(sys.argv[0])] + sys.argv[1:],\n"
                "               'exec_by': os.environ.get('_', ''), 'parent': parent,\n"
                "               'cwd': os.getcwd(), 'env': {}}, fh)\n")
            agent.chmod(0o755)
            bare = tree.run("2", [], LANE_CLI="claude", PATH=path)
            self.assertTrue(bare["launched"], bare.get("stderr"))
            self.assertEqual(bare["returncode"], 0, bare.get("stderr"))
            self.assertEqual(sum("no sleep inhibitor" in line for line in bare["stderr"].splitlines()), 1,
                             bare["stderr"])
            logged = calls.read_text().splitlines()
            self.assertEqual(len(logged), 1, logged)
            self.assertTrue(logged[0].startswith("1\t"), logged)  # the probe, and nothing else
            self.assertEqual(os.path.basename(bare["exec_by"]), "claude", bare["exec_by"])
            # The CLI's parent is the launcher shell: no wrapper process between.
            self.assertEqual([os.path.basename(a) for a in bare["parent"]], ["bash", "lane2"],
                             bare["parent"])
            self.assertEqual(bare["argv"][0], "claude")
            settings = bare["argv"][bare["argv"].index("--settings") + 1]
            self.assertEqual(settings, normal["argv"][normal["argv"].index("--settings") + 1])

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
        """The guard is one probe, os.stat("/proc/self") (pitch N5), so each
        case fakes exactly that probe: (a) /proc present without /proc/self
        (procfs not mounted; an empty /proc used to read as idle) and (b) no
        /proc at all both raise FileNotFoundError; (c) a /proc that is not a
        directory raises NotADirectoryError. Each reads as cannot tell (3)."""
        sys.path.insert(0, str(TOOLS / "worktree"))
        import check_worktree_busy as cwb  # noqa: PLC0415
        for case, error in (("/proc present without /proc/self", FileNotFoundError),
                            ("no /proc", FileNotFoundError),
                            ("/proc not a directory", NotADirectoryError)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, \
                    self._probe_raises(cwb, error) as probed, \
                    mock.patch.object(sys, "argv", ["check_worktree_busy.py", tmp]), \
                    mock.patch.object(sys, "stderr", new_callable=__import__("io").StringIO) as err:
                self.assertEqual(cwb.main(), 3)
                self.assertIn("cannot tell (no procfs at /proc)", err.getvalue())
                with self.assertRaises(cwb.CannotTell):
                    cwb.busy_pids(Path(tmp), set(), None)
                self.assertTrue(probed, "the /proc/self probe never ran")

    def test_tc3_a_proc_that_is_a_regular_file_exits_3(self) -> None:
        """Post-verdict survivor 2, with nothing faked but where procfs is:
        PROC points at a real regular file (then at an empty directory), and
        main must answer 3. The ancestor scan reads procfs too, so it runs
        after the probe; ahead of it, a regular-file /proc raised
        NotADirectoryError out of main."""
        sys.path.insert(0, str(TOOLS / "worktree"))
        import check_worktree_busy as cwb  # noqa: PLC0415
        for case in ("a regular file", "an empty directory"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                proc = Path(tmp) / "proc"
                if case == "a regular file":
                    proc.write_text("")
                else:
                    proc.mkdir()
                with mock.patch.object(cwb, "PROC", proc), \
                        mock.patch.object(sys, "argv", ["check_worktree_busy.py", tmp]), \
                        mock.patch.object(sys, "stderr", new_callable=__import__("io").StringIO) as err:
                    self.assertEqual(cwb.main(), 3)
                    self.assertIn(f"cannot tell (no procfs at {proc})", err.getvalue())

    def test_tc3_busy_check_denied_procfs_fails_closed(self) -> None:
        """Pitch N5: a denied /proc/self stat is not "cannot tell" -- it exits
        1 (busy), fail closed, and busy_pids lets the PermissionError out (so
        worktree_gc skips the candidate)."""
        sys.path.insert(0, str(TOOLS / "worktree"))
        import check_worktree_busy as cwb  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp, \
                self._probe_raises(cwb, PermissionError) as probed, \
                mock.patch.object(sys, "argv", ["check_worktree_busy.py", tmp]), \
                mock.patch.object(sys, "stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(cwb.main(), 1)
            self.assertIn("procfs is not readable", err.getvalue())
            with self.assertRaises(PermissionError):
                cwb.busy_pids(Path(tmp), set(), None)
            self.assertTrue(probed, "the /proc/self probe never ran")

    @staticmethod
    def _probe_raises(cwb, error: type[OSError]):
        """Patch os.stat as check_worktree_busy sees it: the "/proc/self" probe
        raises `error`, every other path is stat'd for real. Yields the list of
        probe calls so a test can prove the branch ran."""
        import contextlib  # noqa: PLC0415
        real_stat = os.stat

        @contextlib.contextmanager
        def patched():
            probed: list[str] = []

            def fake_stat(path, *args, **kwargs):
                if os.fspath(path) == "/proc/self":
                    probed.append("/proc/self")
                    raise error(2, "faked", "/proc/self")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(cwb.os, "stat", fake_stat):
                yield probed
        return patched()

    def test_tc3_refresh_treats_only_3_as_not_busy(self) -> None:
        """Sticky-wicket PATCH 2 (P4): a real lane 2 refresh, with the busy
        check answering through a `python3` shim first on PATH. 3 refreshes
        with a warning; 1 (busy, post-verdict survivor 3) and 2 (an error)
        still skip as busy."""
        for rc, status in ((3, "updated"), (1, "skipped-busy"), (2, "skipped-busy")):
            with self.subTest(rc=rc), tll._FixtureTree() as tree:
                before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree.lane2, check=True,
                                        capture_output=True, text=True).stdout.strip()
                stub(tree.stub_bin, "python3",
                     'case "${1:-}" in *check_worktree_busy.py)\n'
                     f'  echo "check-worktree-busy: answering {rc}" >&2\n'
                     f'  exit {rc} ;;\nesac\n'
                     f'exec "{sys.executable}" "$@"')
                remote_sha = tll.Lane3RefreshesAtLaunch._advance_origin_from_elsewhere(tree)
                cell = tree.run("2", [], LANE_CLI="claude",
                                LANE_CAPTURE_EXTRA_ENV="LANE_REFRESH_STATUS",
                                LANE_REFRESH_LOG_DIR=str(tree.root / "refresh-log"))
                self.assertTrue(cell["launched"], cell.get("stderr"))
                self.assertEqual(cell["extra_env"]["LANE_REFRESH_STATUS"], status, cell["stderr"])
                head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree.lane2, check=True,
                                      capture_output=True, text=True).stdout.strip()
                if rc == 3:
                    self.assertEqual(head, remote_sha)
                    self.assertIn("refreshing anyway", cell["stderr"])
                else:
                    self.assertEqual(head, before)
                    self.assertNotIn("refreshing anyway", cell["stderr"])

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



class InstanceSet(unittest.TestCase):
    """Post-verdict survivor 1: `instance.py set lane3_policy` is the install
    step (tools/gate/install-hrse-gate.md), never a blind append. Run as the
    install doc runs it, against a file in each shape an append broke."""

    POLICY = "/usr/local/share/hrse-gate/claude-lane3.json"

    def run_cli(self, xdg: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-E", str(TOOLS / "settings" / "instance.py"), *args],
                              env={**os.environ, "XDG_CONFIG_HOME": xdg},
                              capture_output=True, text=True, timeout=30)

    def set_in(self, text: str | None) -> tuple[subprocess.CompletedProcess[str], str | None, str]:
        """(the set run, the file afterwards or None, `get` afterwards)."""
        import tomllib  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as xdg:
            toml = Path(xdg) / "harmonic-forge" / "instance.toml"
            if text is not None:
                toml.parent.mkdir()
                toml.write_text(text)
            proc = self.run_cli(xdg, "set", "lane3_policy", self.POLICY)
            after = toml.read_text() if toml.exists() else None
            if after is not None:
                tomllib.loads(after)  # still valid TOML
            got = self.run_cli(xdg, "get", "lane3_policy")
            return proc, after, got.stdout

    def test_tc5_set_keeps_the_file_valid_and_the_key_top_level(self) -> None:
        import tomllib  # noqa: PLC0415
        for case, text in (("no file", None),
                           ("no final newline", 'lane_hook_overlay = "operator"'),
                           ("ends in a [table]", 'lane_hook_overlay = "operator"\n\n[extra]\nx = 1\n'),
                           ("ends in a [table], no final newline", 'timezone = "UTC"\n[extra]\nx = 1')):
            with self.subTest(case=case):
                proc, after, got = self.set_in(text)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(got, self.POLICY + "\0")
                data = tomllib.loads(after)
                self.assertEqual(data.pop("lane3_policy"), self.POLICY)
                self.assertEqual(data, tomllib.loads(text or ""))  # the rest unchanged

    def test_tc5_set_with_the_key_already_present(self) -> None:
        same = f'lane3_policy = "{self.POLICY}"\n[extra]\nx = 1\n'
        proc, after, got = self.set_in(same)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, same)  # untouched, byte for byte
        self.assertIn("already set", proc.stderr)
        other = 'lane3_policy = "/elsewhere/policy.json"\n'
        proc, after, got = self.set_in(other)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(after, other)
        self.assertIn("refusing to replace", proc.stderr)
        self.assertEqual(got, "/elsewhere/policy.json\0")

    def test_tc5_set_refuses_an_invalid_value_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as xdg:
            for value in ("rel/x.json", "~/x.json", "/x\ny.json"):
                with self.subTest(value=value):
                    proc = self.run_cli(xdg, "set", "lane3_policy", value)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertFalse((Path(xdg) / "harmonic-forge").exists())
        broken = 'lane_hook_overlay = "operator'
        with tempfile.TemporaryDirectory() as xdg:
            toml = Path(xdg) / "harmonic-forge" / "instance.toml"
            toml.parent.mkdir()
            toml.write_text(broken)
            proc = self.run_cli(xdg, "set", "lane3_policy", self.POLICY)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(toml.read_text(), broken)

    def test_tc5_the_install_doc_runs_set_not_an_append(self) -> None:
        doc = (TOOLS / "gate" / "install-hrse-gate.md").read_text(encoding="utf-8")
        self.assertIn(f"tools/settings/instance.py set lane3_policy {self.POLICY}", doc)
        self.assertNotIn(">> ~/.config/harmonic-forge/instance.toml", doc)


if __name__ == "__main__":
    unittest.main()
