"""hrse#2258: `lanes-claude` (formerly HRSE2 `research/tmux_lanes_claude.sh`) scopes its daemon-hosted name fallback to the workspace.
At teardown the scope is derived from where the live tmux panes are; at launch it is the
directories being launched. Driven as the real script with stub `tmux`/`lane*`/`claude`
binaries on PATH and HOME in a temp dir: nothing here starts tmux, touches a real session, or
reads the real `claude`. Only `--dry-run` and `--stop-advisor` are driven; `--stop-lanes`
also signals unowned belt watchers machine-wide, so it is covered through `lane_targets`."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "lanes-claude"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _env(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    if not bindir.exists():
        bindir.mkdir()
        for name in ("lane1", "lane2", "lane3"):
            _exe(bindir / name, "exit 0\n")
        # The stubs are steered by env vars so one set serves every test.
        _exe(bindir / "tmux", 'case "$1" in\n'
             '  has-session) [ -n "$STUB_HAS_SESSION" ] && exit 0 || exit 1;;\n'
             '  list-panes) target=""; prev=""\n'
             '    for a in "$@"; do [ "$prev" = "-t" ] && target="$a"; prev="$a"; done\n'
             '    case "$*" in\n'
             '      *pane_current_path*) [ "$target" = "=$STUB_PANE_TARGET" ] && printf "%s\\n" $STUB_PANE_PATHS;;\n'
             '      *) [ "$target" = "=$STUB_PANE_TARGET" ] && printf "%s\\n" $STUB_PANE_PIDS;;\n'
             '    esac; exit 0;;\n'
             'esac\nexit 0\n')
        _exe(bindir / "claude", 'case "$1" in\n'
             '  agents) printf "%s" "${STUB_AGENTS:-[]}";;\n'
             '  stop) echo "stop $2" >> "$STUB_LOG";;\n'
             'esac\nexit 0\n')
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("DISPLAY", "WAYLAND_DISPLAY", "TMUX", "STUB_HAS_SESSION", "STUB_PANE_PIDS",
                        "STUB_PANE_PATHS", "STUB_PANE_TARGET", "STUB_AGENTS", "STUB_LOG")}
    return {**env, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home),
            "STUB_LOG": str(tmp_path / "claude.log")}


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def _run(tmp_path: Path, *args: str, cwd=None, **env_extra) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env={**_env(tmp_path), **env_extra}, cwd=cwd or tmp_path, timeout=60)


def _stopped(tmp_path: Path) -> list:
    log = tmp_path / "claude.log"
    return log.read_text().split("\n")[:-1] if log.exists() else []


def _expected(base: Path) -> set:
    return {str(base), f"{base}-lane2", f"{base}-lane3"}


def _advisor(session_id: str, cwd: Path) -> dict:
    return {"sessionId": session_id, "id": session_id, "kind": "background", "name": "Advisor",
            "cwd": str(cwd)}


_SHELL_TARGET = re.compile(r'tmux (?P<cmd>[a-z-]+)[^\n]*? -t (?P<target>"[^"]*"|\S+)')
_PY_TARGET = re.compile(r'"tmux", "(?P<cmd>[a-z-]+)"[^\n]*"-t", (?P<target>[^,\n]+)')
#: Commands that stop, probe or rewire a session BY NAME: on the teardown/launch-guard path a
#: bare `-t NAME` prefix-matches (`lanes` also matches `lanes-b`), so each must be `=`-anchored.
_ANCHORED = {"kill-session", "has-session", "set-hook", "list-panes", "list-clients"}
#: Non-destructive attach/config/creation targets on a session this script just created or
#: verified by an exact has-session; deliberately not anchored (hrse#2258 sticky-wicket: pulling
#: the attach flow into this diff is scope creep). A NEW tmux command must be added here or
#: to _ANCHORED on purpose.
_ATTACH_ALLOWLIST = {"select-window", "switch-client", "attach-session", "attach", "select-layout", "set-option",
                     "select-pane", "new-session", "split-window"}


def _tmux_env(tmp_path):
    env = {**os.environ, "TMUX_TMPDIR": str(tmp_path)}
    env.pop("TMUX", None)
    return env


def _stub_path(tmp_path, **stubs):
    """A PATH dir of shell stubs: name -> script body; every call is appended to <name>.log."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/{name}.log\n{body}\n')
        stub.chmod(0o755)
    return bin_dir


class TmuxLanesScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()


    def _socket_dir(self):
        """A short TMUX_TMPDIR (harmonic-forge#1211): tmux's socket path must fit sun_path
        (~107 bytes), and l1-post's private TMPDIR under ~/.cache/hrse-scratch does not."""
        path = tempfile.mkdtemp(prefix="tmx", dir="/tmp")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path
    def test_dry_run_prints_the_launch_dirs_and_writes_nothing(self):
        repo = _repo(self.tmp_path, "proj")
        done = _run(self.tmp_path, "--session", "t1", "--project", str(repo), "--dry-run")
        assert done.returncode == 0, done.stderr
        for directory in _expected(repo):
            assert f"  {directory}\n" in done.stdout
        assert not (self.tmp_path / "home" / ".cache" / "lanes-claude").exists()

    def test_the_advisor_launches_at_opus_medium_effort(self):
        """harmonic-forge#1010: both Advisor launch strings and the banner say medium,
        so the first-launch and re-create copies cannot drift apart."""
        text = SCRIPT.read_text()
        launch = "claude --model opus --effort medium --permission-mode plan --name '$advisor_label'"
        assert text.count(launch) == 2
        assert "--effort high" not in text
        done = _run(self.tmp_path, "--session", "t9", "--project", str(_repo(self.tmp_path, "proj")),
                    "--dry-run")
        assert done.returncode == 0, done.stderr
        assert "(Opus, medium effort, plan mode)" in done.stdout


    def test_a_lane_worktree_source_derives_its_main_checkout_base(self):
        main, lane2 = _repo(self.tmp_path, "proj"), _repo(self.tmp_path, "proj-lane2")
        done = _run(self.tmp_path, "--session", "t3", "--lane1-dir", str(main), "--lane2-dir", str(lane2),
                    "--lane3-dir", str(main), "--advisor-dir", str(main), "--dry-run")
        assert done.returncode == 0, done.stderr
        listed = {line.strip() for line in done.stdout.split("workspace dirs")[1].splitlines()[1:] if line.strip()}
        assert listed == _expected(main)


    def test_stop_advisor_stops_only_the_closing_workspaces_daemon_hosted_advisor(self):
        """The scope comes from the panes: a pane in repo A means advisor A is stopped and
        advisor B (another workspace, same name) is left alone. Works with no state file at
        all, which is every workspace launched before this fix."""
        a, b = _repo(self.tmp_path, "wsA"), _repo(self.tmp_path, "wsB")
        lane_a = {"sessionId": "laneA", "id": "laneA", "kind": "background", "name": "Lane 1",
                  "cwd": str(a)}
        agents = json.dumps([_advisor("advA", a), _advisor("advB", b), lane_a])
        done = _run(self.tmp_path, "--session", "t5", "--stop-advisor", STUB_AGENTS=agents,
                    STUB_PANE_PATHS=str(a), STUB_PANE_TARGET="t5-advisor")
        assert done.returncode == 0, done.stderr
        assert _stopped(self.tmp_path) == ["stop advA"]
        # the sessions the SCOPE excluded are named (never silently dropped); this workspace's own
        # in-scope Lane 1, which --stop-advisor simply does not target, is not "outside" it
        assert "left 1 lane-named background session(s) outside this workspace's scope: wsB" in done.stderr


    def test_stop_advisor_matches_through_a_lane_worktree_pane_path(self):
        main, lane2 = _repo(self.tmp_path, "wsA"), _repo(self.tmp_path, "wsA-lane2")
        agents = json.dumps([_advisor("advA", main), _advisor("advB", _repo(self.tmp_path, "wsB"))])
        done = _run(self.tmp_path, "--session", "t5c", "--stop-advisor", STUB_AGENTS=agents,
                    STUB_PANE_PATHS=str(lane2), STUB_PANE_TARGET="t5c-advisor")
        assert done.returncode == 0, done.stderr
        assert _stopped(self.tmp_path) == ["stop advA"]


    def test_stop_advisor_with_panes_outside_any_repository_matches_nothing_by_name(self):
        """A pane back at a shell in a non-repository directory must not become a scope root."""
        a = _repo(self.tmp_path, "wsA")
        plain = self.tmp_path / "plain"
        plain.mkdir()
        done = _run(self.tmp_path, "--session", "t5b", "--stop-advisor",
                    STUB_AGENTS=json.dumps([_advisor("advA", a)]), STUB_PANE_PATHS=str(plain),
                    STUB_PANE_TARGET="t5b-advisor")
        assert done.returncode == 0, done.stderr
        assert "no repository under this workspace's panes" in done.stderr
        assert _stopped(self.tmp_path) == []


    def test_the_detach_hook_stops_the_lanes_before_it_kills_the_tmux_sessions(self):
        """Teardown derives its scope from the live panes, so the hook must read them first:
        `--stop-lanes` has to run before `tmux kill-session` in the hook's shell string."""
        line = next(l for l in SCRIPT.read_text().splitlines() if l.strip().startswith("cleanup_shell="))
        assert 0 <= line.index("--stop-lanes") < line.index("tmux kill-session")
        advisor = next(l for l in SCRIPT.read_text().splitlines() if l.strip().startswith("advisor_shell="))
        assert "kill-session" not in advisor  # the advisor hook's script stops, the caller kills afterward


    def test_pane_lookups_use_tmuxs_exact_session_target(self):
        """`-t NAME` prefix-matches (`lanes` would also read `lanes-b`'s panes); the script must
        ask for `=NAME`. The stub answers only an exact `=NAME`, so a bare name finds no panes,
        no scope, and stops nothing by name."""
        a = _repo(self.tmp_path, "wsA")
        agents = json.dumps([_advisor("advA", a)])
        wrong = _run(self.tmp_path, "--session", "t9", "--stop-advisor", STUB_AGENTS=agents,
                     STUB_PANE_PATHS=str(a), STUB_PANE_TARGET="t9-adv")  # a prefix of the real name
        assert _stopped(self.tmp_path) == [] and "no repository under this workspace's panes" in wrong.stderr
        right = _run(self.tmp_path, "--session", "t9", "--stop-advisor", STUB_AGENTS=agents,
                     STUB_PANE_PATHS=str(a), STUB_PANE_TARGET="t9-advisor")
        assert right.returncode == 0 and _stopped(self.tmp_path) == ["stop advA"]


    def test_every_destructive_tmux_session_target_is_exact_anchored(self):
        """An enumerating check, not a per-site string test: a third unanchored site of this class
        (pass 2's finding was the second) cannot hide, and a tmux command nobody classified fails."""
        text = SCRIPT.read_text()
        seen = set()
        for pattern in (_SHELL_TARGET, _PY_TARGET):
            for match in pattern.finditer(text):
                command, target = match.group("cmd"), match.group("target").strip().strip('"')
                seen.add(command)
                if command in _ANCHORED:
                    assert target.startswith("="), f"unanchored `{command} -t {target}`"
        assert seen & _ANCHORED >= {"kill-session", "has-session", "set-hook", "list-panes"}, seen
        assert not (seen - _ANCHORED - _ATTACH_ALLOWLIST), seen - _ANCHORED - _ATTACH_ALLOWLIST


    def test_every_anchored_target_the_script_uses_is_accepted_by_real_tmux(self):
        """The stub tmux accepts any target, which is how `set-hook -t =NAME` shipped: real tmux
        answers `no such session: =NAME` for that form (target-session for set-hook wants `=NAME:`),
        the launcher died under `set -e` before attaching, and no workspace could launch. Run each
        `-t` target the script passes, with its variables filled in, against a real private tmux."""
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        env = {**os.environ, "TMUX_TMPDIR": self._socket_dir()}
        env.pop("TMUX", None)

        def tmux(*argv):
            return subprocess.run(["tmux", "-L", "t2258real", *argv], env=env, capture_output=True, text=True)

        assert tmux("new-session", "-d", "-s", "wsreal", "sleep", "60").returncode == 0
        try:
            text = SCRIPT.read_text()
            argv_for = {"set-hook": lambda t: ["set-hook", "-t", t, "client-detached", ""],
                        "has-session": lambda t: ["has-session", "-t", t],
                        "list-panes": lambda t: ["list-panes", "-s", "-t", t],
                        "list-clients": lambda t: ["list-clients", "-t", t]}
            tried = set()
            for pattern in (_SHELL_TARGET, _PY_TARGET):
                for match in pattern.finditer(text):
                    command = match.group("cmd")
                    if command not in argv_for:
                        continue
                    target = match.group("target").strip().strip('"')
                    target = re.sub(r"\$\{?(advisor_)?session\}?", "wsreal", target)
                    target = re.sub(r'"\s*\+\s*s\b|^=" \+ s|" \+ s', "wsreal", target)
                    if "$" in target or "+" in target:
                        target = "=wsreal"  # the Python helpers build `"=" + s`; same form
                    result = tmux(*argv_for[command](target))
                    assert result.returncode == 0, f"real tmux rejects `{command} -t {target}`: {result.stderr.strip()}"
                    tried.add(command)
            assert tried >= {"set-hook", "has-session", "list-panes"}, tried
        finally:
            tmux("kill-server")


    def test_set_window_titles_sets_both_titles_on_real_tmux(self):
        """hrse#2261 AC1/AC3: run the script's own `set_window_titles` against a private tmux server
        and read the session options back, so the target form and the strings are checked by tmux."""
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        env = _tmux_env(self._socket_dir())
        match = re.search(r"^set_window_titles\(\) \{\n(?P<body>.*?)\n\}$", SCRIPT.read_text(), re.S | re.M)
        assert match, "set_window_titles() not found in the launcher"

        def tmux(*argv):
            return subprocess.run(["tmux", "-L", "t2261real", *argv], env=env, capture_output=True, text=True)

        for name in ("wsreal", "wsreal-advisor"):
            assert tmux("new-session", "-d", "-s", name, "sleep", "60").returncode == 0
        try:
            script = ('tmux() { command tmux -L t2261real "$@"; }\n'
                      'session=wsreal; advisor_session=wsreal-advisor\n'
                      f'set_window_titles() {{\n{match.group("body")}\n}}\nset_window_titles\n')
            run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            assert run.returncode == 0, run.stderr
            for name, title in (("wsreal", "Harmonic Forge - 3-Lane Protocol"),
                                ("wsreal-advisor", "Harmonic Forge - Advisor")):
                on = tmux("show-options", "-v", "-t", f"={name}:", "set-titles")
                string = tmux("show-options", "-v", "-t", f"={name}:", "set-titles-string")
                assert on.stdout.strip() == "on", (name, on.stderr)
                assert string.stdout.strip() == title, (name, string.stderr)
        finally:
            tmux("kill-server")


    def test_both_launch_paths_set_the_window_titles(self):
        """hrse#2261 AC2: the re-attach path (the one an operator hits on a second launch) and the
        fresh-launch path each call set_window_titles after the hooks are installed."""
        text = SCRIPT.read_text()
        reattach = text[text.index('if tmux has-session -t "=$session"'):]
        reattach = reattach[:reattach.index("  attach_or_switch\n  exit 0")]
        assert "set_window_titles" in reattach, "re-attach path does not relabel"
        fresh = text[text.index("created_advisor=1"):text.index("launch_advisor_window\n\n# Record each lane")]
        assert "set_window_titles" in fresh, "fresh-launch path does not relabel"


    def test_a_failing_title_option_never_stops_the_launch(self):
        """hrse#2261 pitch finding: the launcher runs under `set -e` and an ERR trap that kills both
        sessions, so a cosmetic option failing must not propagate."""
        match = re.search(r"^set_window_titles\(\) \{\n.*?\n\}$", SCRIPT.read_text(), re.S | re.M)
        assert match
        bin_dir = _stub_path(self.tmp_path, tmux="exit 1")
        script = ("set -euo pipefail\nsession=s; advisor_session=a\n" + match.group(0)
                  + "\nset_window_titles\necho reached-after")
        run = subprocess.run(["bash", "-c", script], env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.tmp_path),
                                  # harmonic-forge#1114: keep the run's temp root.
                                  **{k: v for k, v in os.environ.items() if k in ("TMPDIR", "TMP", "TEMP")}},
                             capture_output=True, text=True)
        assert run.returncode == 0 and "reached-after" in run.stdout, run.stderr


    def test_the_advisor_window_is_opened_with_the_window_title_as_its_tab_title(self):
        """hrse#2261: Konsole's default tab format ignores the title tmux sends via `set-titles`;
        `-p tabtitle=%w` on the window the launcher itself creates makes it show that title."""
        text = SCRIPT.read_text()
        advisor = text[text.index("launch_advisor_window() {"):text.index("reap_stale_belt_locks() {")]
        # The launch command itself, not the comment above it, which also names the flag.
        assert re.search(r"^\s*\( env -u TMUX konsole --desktopfile lanes-claude-advisor[^\n]*-p tabtitle=%w",
                         advisor, re.M), "the advisor konsole launch line lacks -p tabtitle=%w"
        assert "tmux attach-session -t \"$advisor_session\"" in advisor


    def test_the_launcher_makes_no_konsole_dbus_calls(self):
        """Operator ruling 2026-10-07: titles come from tmux; a window title must not require
        reading, rewriting or restoring state in a terminal the launcher does not own."""
        run = subprocess.run(["git", "grep", "-n", "-i", "-e", "qdbus", "-e", "KONSOLE_DBUS", "--",
                              "tools/lane/lanes-claude"], cwd=SCRIPT.parent.parent.parent,
                             capture_output=True, text=True)
        assert run.returncode == 1 and run.stdout == "", run.stdout


if __name__ == "__main__":
    unittest.main()
