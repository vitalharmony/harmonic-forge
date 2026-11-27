"""harmonic-forge#1529: the six lane files read instance.toml `projects_root`.

Each case runs the code under test in a subprocess with its own
XDG_CONFIG_HOME, so module-level roots are evaluated against that fixture and
never the developer's (or the run's) instance.toml.

TC1 / TC1b: Lane 3's write fence allows only the configured testplan root,
  and with the root unset denies everything with exit 0 (never a crash).
TC2: impl-worktree detection under the configured root.
TC3: the tier gate's detached-HEAD fallback matches more, never less, unset.
TC4: describe(), registry_add_dirs and queue_resume_args.
TC5: stale_worktrees warns and returns; lane1_rebase exits 4.
TC8 (sticky-wicket epoch 2, P1-P3): every consumer of the projects root, in
  both states (set, unset), through its real entry point and only
  `RootsCase.declare()` -- no env override, no patched module constant -- plus
  a completeness check that every call site under tools/ has a row.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
HOOKS = TOOLS / "hooks"
LANE = TOOLS / "lane"


class RootsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "srv projects"
        (self.root / "testplan").mkdir(parents=True)
        (self.root / ".worktrees").mkdir()
        self.xdg = self.tmp / "xdg"
        (self.xdg / "harmonic-forge").mkdir(parents=True)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def declare(self, root: Path | None, home: Path | None = None) -> None:
        """Declare `root` as projects_root, or omit it (None). With `home`, the
        declaration is a launcher fixture's own (`<home>/.config`, through
        `baseline_capture.write_overlay_declaration`), not this case's XDG."""
        if home is not None:
            if str(LANE) not in sys.path:
                sys.path.insert(0, str(LANE))
            import baseline_capture  # noqa: PLC0415
            baseline_capture.write_overlay_declaration(
                home, projects_root=None if root is None else str(root))
            return
        text = "" if root is None else f'projects_root = "{root}"\n'
        (self.xdg / "harmonic-forge" / "instance.toml").write_text(text, encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items()
                if k not in ("LANE", "HARMONIC_FORGE_ROOT", "STALE_WORKTREE_ROOT",
                             "LANE1_REBASE_WORKTREES")}
        return {**base, "XDG_CONFIG_HOME": str(self.xdg), **extra}

    def py(self, code: str, **extra: str) -> subprocess.CompletedProcess[str]:
        prelude = (f"import sys; sys.path[:0] = [{str(HOOKS)!r}, {str(LANE)!r}, "
                   f"{str(TOOLS / 'settings')!r}]\n")
        return subprocess.run([sys.executable, "-c", prelude + code], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def fence(self, tool_input: dict, tool: str = "Write") -> subprocess.CompletedProcess[str]:
        payload = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(self.tmp)})
        return subprocess.run([sys.executable, str(HOOKS / "block_lane1_status_claims.py")],
                              input=payload, env=self.env(LANE="3"), capture_output=True,
                              text=True, timeout=60)

    def decision(self, proc: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return "allow"
        out = json.loads(proc.stdout)
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")


class TC1Fence(RootsCase):
    def test_inside_the_configured_testplan_is_allowed_outside_denied(self) -> None:
        self.declare(self.root)
        inside = {"file_path": str(self.root / "testplan" / "x.md"), "content": "x"}
        outside = {"file_path": str(Path.home() / "Harmonic_Projects" / "testplan" / "x.md"),
                   "content": "x"}
        self.assertEqual(self.decision(self.fence(inside)), "allow")
        self.assertEqual(self.decision(self.fence(outside)), "deny")

    def test_unset_root_denies_every_target_without_crashing(self) -> None:
        self.declare(None)
        for target in (self.root / "testplan" / "x.md",
                       Path.home() / "Harmonic_Projects" / "testplan" / "x.md"):
            with self.subTest(str(target)):
                proc = self.fence({"file_path": str(target), "content": "x"})
                self.assertEqual(self.decision(proc), "deny")
                self.assertIn("projects root", proc.stdout)
                bash = self.fence({"command": f"echo x > '{target}'"}, tool="Bash")
                self.assertEqual(self.decision(bash), "deny")

    def test_the_deny_message_names_an_unset_root_without_crashing(self) -> None:
        """Pass-1 survivor: the deny message is where a crash would turn a deny
        into an allow; with the root unset it names the setting, not a path."""
        self.declare(None)
        proc = self.fence({"file_path": "/etc/x", "content": "x"})
        self.assertEqual(self.decision(proc), "deny")
        reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("<projects root>/testplan", reason)
        self.assertIn("projects_root", reason)

    def test_the_runner_reads_no_config_and_supplies_none(self) -> None:
        """K-A1 (harmonic-forge#1529 reforge): inside `contained_tempdir()` no
        instance.toml is read -- not the developer's (here a fixture declaring
        /srv/dev-root) and not one the runner wrote -- whether TMPDIR is plain
        or under a `.git` directory (a Codex lane's). On exit TMPDIR holds only
        what it held before. Reverting to a runner-written fixture yields the
        scratch root; dropping the XDG override leaks /srv/dev-root."""
        self.declare(Path("/srv/dev-root"))
        plain = self.tmp / "plain tmp"
        plain.mkdir()
        checkout = self.tmp / "lane tmp"
        (checkout / ".git").mkdir(parents=True)
        code = ("import os, run_tests, roots\n"
                "with run_tests.contained_tempdir():\n"
                "    print(roots.projects_root_or_none())\n"
                "    print(os.environ['XDG_CONFIG_HOME'] == run_tests.CONFIG_FREE_XDG)\n")
        for tmpdir in (plain, checkout):
            with self.subTest(tmpdir=tmpdir.name):
                before = sorted(p.name for p in tmpdir.iterdir())
                proc = subprocess.run(
                    [sys.executable, "-c", f"import sys; sys.path[:0] = [{str(TOOLS)!r}, "
                     f"{str(TOOLS / 'settings')!r}]\n" + code],
                    env={**self.env(), "TMPDIR": str(tmpdir)},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.split(), ["None", "True"], proc.stderr)
                self.assertEqual(sorted(p.name for p in tmpdir.iterdir()), before)

    def test_tier_gate_matches_a_symlinked_worktree_root_when_unset(self) -> None:
        """Pass-1 survivor (cross-family): the cwd is realpath-ed, so a worktree
        under a symlinked root has no `.worktrees` segment; unset still gates it."""
        self.declare(None)
        probe = ("import model_tier_gate as m; "
                 "print(bool(m.WORKTREE_ISSUE_RE.match('/srv/forge-worktrees/hrse2-7-impl')))")
        self.assertEqual(self.py(probe).stdout.strip(), "True")


class TC2TC3Worktrees(RootsCase):
    def test_impl_worktree_under_the_configured_root(self) -> None:
        self.declare(self.root)
        path = self.root / ".worktrees" / "acme-12-impl"
        # The parent exactly (a prefix test would pass via the legacy `/tmp`
        # root whenever TMPDIR is /tmp, the epoch-2 kill re-run's finding).
        proc = self.py(f"import worktree_issue as w; print(str({str(path.parent)!r}) in w._IMPL_ROOTS)")
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)
        self.declare(None)
        proc = self.py("import worktree_issue as w; print(w._IMPL_ROOTS)")
        self.assertEqual(proc.stdout.strip(), "('/tmp',)", proc.stderr)

    def test_tier_gate_fallback_matches_more_when_unset(self) -> None:
        probe = ("import model_tier_gate as m; "
                 f"print(bool(m.WORKTREE_ISSUE_RE.match({str(self.root / '.worktrees' / 'hrse2-7-impl')!r})))")
        for root in (self.root, None):
            with self.subTest(root=root):
                self.declare(root)
                self.assertEqual(self.py(probe).stdout.strip(), "True")

    def test_tier_gate_fallback_matrix_every_config_state(self) -> None:
        """K-B1 (harmonic-forge#1529 reforge): projects_root unset, set to the
        worktrees' own root, and set elsewhere, against five worktree layouts.
        A miss skips the gate, so every cell must gate hrse 7. Kills: a
        set-root branch dropping the legacy home root (P2 #2), a fallback that
        needs a `.worktrees` segment (P1 #3), and `^/.*/` without the optional
        group, which misses `/hrse2-7-impl` (Codex P2 #3)."""
        paths = [str(self.root / ".worktrees" / "hrse2-7-impl"),
                 str(Path.home() / "Harmonic_Projects" / ".worktrees" / "hrse2-7-impl"),
                 "/srv/forge-worktrees/hrse2-7-impl", "/tmp/hrse2-7-impl", "/hrse2-7-impl",
                 "/srv/a\nb/hrse2-7-impl",
                 # A letter-suffixed reforge worktree is hrse 7 (sticky-wicket R3).
                 str(self.root / ".worktrees" / "hrse2-7b-impl"), "/tmp/hrse2-7b-impl"]
        probe = ("import json, subprocess, model_tier_gate as m\n"
                 "m._run = lambda *a, **k: subprocess.CompletedProcess(a, 0, '', '')\n"
                 f"print(json.dumps([m.resolve_issue_target(p) for p in {paths!r}]))\n")
        for root in (None, self.root, Path("/srv/elsewhere")):
            with self.subTest(root=root):
                self.declare(root)
                proc = self.py(probe)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), [[7, "hrse"]] * len(paths))

    def test_tc7_no_operator_projects_literal_in_the_six_files(self) -> None:
        """AC3 / TC7 as the handoff states it: a grep, comments included."""
        six = ["tools/hooks/block_lane1_status_claims.py", "tools/hooks/model_tier_gate.py",
               "tools/hooks/worktree_issue.py", "tools/hooks/stale_worktrees.py",
               "tools/lane/_agent_registry.sh", "tools/lane/lane1_rebase.py"]
        literal = "Harmonic" + "_Projects"
        for rel in six:
            with self.subTest(file=rel):
                self.assertNotIn(literal, (TOOLS.parent / rel).read_text(encoding="utf-8"))

    def test_tier_gate_reads_no_projects_root(self) -> None:
        """K-B2 (harmonic-forge#1529 reforge): the tier gate's fallback has no
        config branch to regress, so its source names no root at all."""
        import ast  # noqa: PLC0415
        tree = ast.parse((HOOKS / "model_tier_gate.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
        for needle in ("roots", "instance", "projects_root", "projects_root_or_none",
                       "under_projects", "_PROJECT_WORKTREES", "_WORKTREE_ROOTS"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, names)


class TC4Helpers(RootsCase):
    def test_describe_names_both_roots_and_never_raises(self) -> None:
        self.declare(self.root)
        out = self.py("import roots; print(roots.describe())").stdout
        self.assertIn(f"projects root {self.root}", out)
        self.declare(None)
        proc = self.py("import roots; print(roots.describe())", HARMONIC_FORGE_ROOT="relative")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("unset", proc.stdout)

    def test_queue_resume_args_expands_and_refuses(self) -> None:
        self.declare(self.root)
        proc = subprocess.run(["bash", str(LANE / "queue_resume_args.sh"), "2"], env=self.env(),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"--add-dir\0{self.root}/.worktrees\0", proc.stdout)
        self.declare(None)
        proc = subprocess.run(["bash", str(LANE / "queue_resume_args.sh"), "3"], env=self.env(),
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("projects_root", proc.stderr)
        self.assertNotIn("bad array subscript", proc.stderr)


class TC5Tools(RootsCase):
    def test_stale_worktrees_warns_and_returns_when_unset(self) -> None:
        self.declare(None)
        proc = self.py("import stale_worktrees as s, batch_auth\n"
                       "batch_auth.classify_pr_merge = lambda t: ('o/r', 1)\n"
                       "s.landed = lambda *a, **k: True\n"
                       "print(s.warn('gh pr merge 1', None))")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "None")
        self.assertIn("projects_root", proc.stderr)

    def test_lane1_rebase_exits_4_when_unset(self) -> None:
        self.declare(None)
        proc = self.py("import lane1_rebase as l\n"
                       "try:\n    l.worktrees_root()\nexcept l.Exit as e:\n    print(e.code)")
        self.assertEqual(proc.stdout.strip(), "4", proc.stderr)


#: lane1_rebase.run() with git stubbed: it stops at the prep worktree's first
#: use (prune_stale_prep), printing the exit code and the prep path it was given.
_REBASE_RUN = (
    "import subprocess, lane1_rebase as l\n"
    "l.fetch = lambda *a, **k: None\n"
    "l.rev = lambda repo, ref: 'b' * 40 if ref.endswith('/main') else 'a' * 40\n"
    "l.baseline_render.refuse_marked_head = lambda *a, **k: None\n"
    "l.check_preconditions = lambda *a, **k: None\n"
    "l.git = lambda *a, **k: subprocess.CompletedProcess(a, 1, '', '')\n"
    "seen = []\n"
    "def prune(repo, prep):\n"
    "    seen.append(str(prep))\n"
    "    raise l.Exit(9, 'stopped at the prep')\n"
    "l.prune_stale_prep = prune\n"
    "def go(repo):\n"
    "    try:\n"
    "        l.run(repo, 7, 'feat/7-thing')\n"
    "    except l.Exit as exc:\n"
    "        print(exc.code)\n"
    "    print(seen)\n")


class Consumer(collections.namedtuple("Consumer", "name entry_point test sites")):
    """One row: a consumer of the projects root, the entry point its test
    drives, the TC8 test method, and every grep call site it covers."""


_FENCE = "tools/hooks/block_lane1_status_claims.py"
_REGISTRY = "tools/lane/_agent_registry.sh"
#: P3: the consumer table. Every `roots.` / `registry_add_dirs` call site under
#: tools/ (outside tools/settings) is listed under exactly one row; the
#: completeness test fails on any site not listed or listed but gone.
CONSUMERS = (
    Consumer("fence TESTPLAN and its deny message", "block_lane1_status_claims.py hook, LANE=3",
             "test_row_fence_testplan",
             ((_FENCE, 'TESTPLAN_ROOT = roots.under_projects("testplan", resolved=True)'),
              (_FENCE, "path = roots.under_projects(sub)"))),
    Consumer("fence MEMORY", "block_lane1_status_claims.py hook, LANE=3",
             "test_row_fence_memory",
             ((_FENCE, 'MEMORY_ROOT = roots.under_projects("operator-memory", resolved=True)'),)),
    Consumer("fence testplan/memory overlap (D63)", "block_lane1_status_claims.py hook, LANE=3",
             "test_row_fence_aliased_memory", ()),
    Consumer("worktree_issue.is_impl_worktree", "worktree_issue.is_impl_worktree",
             "test_row_impl_worktree",
             (("tools/hooks/worktree_issue.py",
               '_PROJECT_WORKTREES = roots.under_projects(".worktrees")'),)),
    Consumer("stale_worktrees default root", "stale_worktrees.warn",
             "test_row_stale_worktrees_default_root",
             (("tools/hooks/stale_worktrees.py",
               'DEFAULT_ROOT = roots.under_projects(".worktrees")'),)),
    Consumer("lane1_rebase worktrees root", "lane1_rebase main(--check), prep_path and run",
             "test_row_lane1_rebase",
             (("tools/lane/lane1_rebase.py",
               'configured = os.environ.get("LANE1_REBASE_WORKTREES") or '
               'roots.under_projects(".worktrees")'),)),
    Consumer("_cli_launch.sh codex:3 add-dirs", "lane3 launcher, LANE_CLI=codex",
             "test_row_launcher_codex3",
             (("tools/lane/_cli_launch.sh",
               "#     harmonic-forge#1529: `registry_add_dirs` expands `@projects/` entries"),
              ("tools/lane/_cli_launch.sh",
               "mapfile -d '' -t _lane_add_dirs < <(rc=0; registry_add_dirs "
               "\"$_lane_agent:$LANE\" || rc=$?; printf '%s\\0' \"$rc\")"),
              (_REGISTRY, "# (harmonic-forge#1529), expanded by `registry_add_dirs` below. Lanes 2"),
              (_REGISTRY, "# registry_add_dirs <agent:lane> -- harmonic-forge#1529: the slot's "
                          "add-dirs as"),
              (_REGISTRY, "registry_add_dirs() {"))),
    Consumer("queue_resume_args.sh", "queue_resume_args.sh", "test_queue_resume_args_expands_and_refuses",
             (("tools/lane/queue_resume_args.sh",
               "mapfile -d '' -t add_dirs < <(rc=0; registry_add_dirs \"codex:$lane\" || rc=$?; "
               "printf '%s\\0' \"$rc\")"),)),
)
CALL_SITE_RE = r"roots\.(under_projects|projects_root_or_none)|registry_add_dirs"


class TC8ConsumerTable(RootsCase):
    """P3: each row runs in both states with no LANE1_REBASE_WORKTREES, no
    STALE_WORKTREE_ROOT (RootsCase.env strips both) and no patched constant."""

    def states(self, home: Path | None = None):
        for state, root in (("set", self.root), ("unset", None)):
            with self.subTest(state=state):
                self.declare(root, home=home)
                yield state

    def reason(self, proc: subprocess.CompletedProcess[str]) -> str:
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def test_row_fence_testplan(self) -> None:
        inside = str(self.root / "testplan" / "x.md")
        legacy = str(Path.home() / "Harmonic_Projects" / "testplan" / "x.md")
        for state in self.states():
            got = {p: self.decision(self.fence({"file_path": p, "content": "x"}))
                   for p in (inside, legacy)}
            denied = self.fence({"file_path": legacy, "content": "x"})
            if state == "set":
                self.assertEqual(got, {inside: "allow", legacy: "deny"})
                self.assertIn(f"{self.root}/testplan/", self.reason(denied))
            else:
                self.assertEqual(got, {inside: "deny", legacy: "deny"})
                self.assertIn("<projects root>/testplan", self.reason(denied))

    def test_row_fence_memory(self) -> None:
        lesson = str(self.root / "operator-memory" / "feedback_x.md")
        at_home = str(Path.home() / "feedback_x.md")
        at_root = str(self.root / "feedback_x.md")
        for state in self.states():
            got = {p: self.decision(self.fence({"file_path": p, "content": "x"}))
                   for p in (lesson, at_home, at_root)}
            self.assertEqual(got, {lesson: "allow" if state == "set" else "deny",
                                   at_home: "deny", at_root: "deny"})

    def test_row_fence_aliased_memory(self) -> None:
        """D63: testplan and operator-memory resolving to one directory, or the
        memory root inside testplan, never lets the testplan allowance admit
        MEMORY.md; a lesson and a testplan file outside the overlap still pass,
        and a testplan inside the memory root keeps working."""
        layouts = {}
        for name, real_tp, real_mem in (("same", "D", "D"), ("mem-in-tp", "D", "D/mem"),
                                        ("tp-in-mem", "D/tp", "D")):
            root = self.tmp / name
            for real in {real_tp, real_mem}:
                (root / real).mkdir(parents=True, exist_ok=True)
            (root / "testplan").symlink_to(root / real_tp)
            (root / "operator-memory").symlink_to(root / real_mem)
            layouts[name] = root
        # Real (resolved) paths: the fence requires the lexical form inside the
        # resolved root too, so a path spelled through the symlink is denied
        # before this guard is reached (K-P1 targets resolved paths the same way).
        want = {
            "same": {"D/MEMORY.md": "deny", "D/notes.md": "deny", "D/sub/x.md": "deny",
                     "D/feedback_x.md": "allow"},
            "mem-in-tp": {"D/mem/MEMORY.md": "deny", "D/mem/notes.md": "deny",
                          "D/x.md": "allow", "D/mem/feedback_x.md": "allow"},
            "tp-in-mem": {"D/MEMORY.md": "deny", "D/tp/x.md": "allow",
                          "D/feedback_x.md": "allow"},
        }
        for name, root in layouts.items():
            for state, declared in (("set", root), ("unset", None)):
                self.declare(declared)
                for rel, expected in want[name].items():
                    with self.subTest(layout=name, state=state, path=rel):
                        got = self.decision(self.fence({"file_path": str(root / rel),
                                                        "content": "x"}))
                        self.assertEqual(got, expected if state == "set" else "deny")

    def test_row_impl_worktree(self) -> None:
        path = str(self.root / ".worktrees" / "acme-12-impl")
        for state in self.states():
            proc = self.py(f"import worktree_issue as w; print(w.is_impl_worktree({path!r}))")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "True" if state == "set" else "False")

    def test_row_stale_worktrees_default_root(self) -> None:
        code = ("import stale_worktrees as s\n"
                "seen = []\n"
                "s.merged_repos = lambda *a, **k: ['o/r']\n"
                "s.main_head = lambda *a, **k: 'a' * 40\n"
                "def scan(repo, sha, root, deadline, runner):\n"
                "    seen.append(str(root))\n"
                "    return [], 0, 0\n"
                "s.stale_worktrees = scan\n"
                "print(s.warn('gh pr merge 1', None))\n"
                "print(seen)\n")
        for state in self.states():
            proc = self.py(code)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if state == "set":
                self.assertEqual(proc.stdout.split("\n")[:2],
                                 ["None", repr([str(self.root / ".worktrees")])], proc.stderr)
            else:
                self.assertEqual(proc.stdout.split("\n")[:2], ["None", "[]"])
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)
                self.assertIn("projects_root", proc.stderr)

    def test_row_lane1_rebase(self) -> None:
        repo = str(self.tmp / "acme")
        check = ("import lane1_rebase as l\n"
                 "l.remote_sha = lambda repo, ref: 'b' * 40 if ref.endswith('/main') else 'a' * 40\n"
                 "def pre(*a, **k):\n"
                 "    raise l.Exit(7, 'past the root')\n"
                 "l.check_preconditions = pre\n"
                 f"raise SystemExit(l.main(['--check', {repo!r}, '7', 'feat/7-thing']))\n")
        prep = (f"import lane1_rebase as l\n"
                f"try:\n    print(l.prep_path({repo!r}, 7))\n"
                "except l.Exit as exc:\n    print(exc.code)\n")
        expected = str(self.root / ".worktrees" / "acme-7-prep")
        for state in self.states():
            checked = self.py(check)
            prepped = self.py(prep)
            ran = self.py(_REBASE_RUN + f"go({repo!r})\n")
            if state == "set":
                self.assertEqual(checked.returncode, 7, checked.stdout + checked.stderr)
                self.assertIn("past the root", checked.stdout)
                self.assertEqual(prepped.stdout.strip(), expected, prepped.stderr)
                self.assertEqual(ran.stdout.split("\n")[:2], ["9", repr([expected])], ran.stderr)
            else:
                self.assertEqual(checked.returncode, 4, checked.stdout + checked.stderr)
                self.assertIn("projects_root", checked.stdout)
                self.assertEqual(prepped.stdout.strip(), "4", prepped.stderr)
                self.assertEqual(ran.stdout.split("\n")[:2], ["4", "[]"], ran.stderr)

    def test_row_launcher_codex3(self) -> None:
        if str(LANE) not in sys.path:
            sys.path.insert(0, str(LANE))
        import baseline_capture as bc  # noqa: PLC0415
        tree = (self.tmp / "launch").resolve()
        tree.mkdir()
        main, stub_bin = bc.build_fixture_tree(tree)
        home = tree / "home"
        bc.write_codex_hooks(home / "Harmonic_Projects" / "HRSE2")
        bc.write_lane_slots(home)
        forge = bc.link_forge_tools(home, TOOLS)
        for state in self.states(home=home):
            cell = bc.capture_cell(LANE, main, stub_bin, "3", [], env_overrides={
                "LANE_CLI": "codex", "HOME": str(home), "HARMONIC_FORGE_ROOT": str(forge)})
            if state == "set":
                self.assertTrue(cell["launched"], cell)
                args = cell["argv"][5:]
                pairs = list(zip(args, args[1:]))
                self.assertIn(("--add-dir", f"{self.root}/testplan"), pairs)
            else:
                self.assertFalse(cell["launched"], cell)
                self.assertIn("projects_root", cell["stderr"])

    def test_every_call_site_has_a_row(self) -> None:
        """A consumer added without a row fails here, so "every consumer is
        exercised through the configured path" is a standing check."""
        proc = subprocess.run(["git", "grep", "-n", "-E", CALL_SITE_RE, "--", "tools",
                               ":!tools/settings"], cwd=TOOLS.parent,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found = collections.Counter()
        for line in proc.stdout.splitlines():
            path, _, text = line.split(":", 2)
            found[(path, text.strip())] += 1
        declared = collections.Counter(site for row in CONSUMERS for site in row.sites)
        self.assertEqual(found, declared)
        tests = {name for cls in (TC8ConsumerTable, TC4Helpers) for name in dir(cls)}
        for row in CONSUMERS:
            with self.subTest(row=row.name):
                self.assertIn(row.test, tests)


class P1P2Kills(RootsCase):
    def test_symlinked_testplan_and_memory_allow_their_targets(self) -> None:
        """K-P1: under_projects(resolved=True) resolves the joined path, as main
        did, so a symlinked <root>/testplan or operator-memory admits its target
        and nothing outside it."""
        target = self.tmp / "D"
        (target / "tp").mkdir(parents=True)
        (target / "mem").mkdir()
        (self.root / "testplan").rmdir()
        (self.root / "testplan").symlink_to(target / "tp")
        (self.root / "operator-memory").symlink_to(target / "mem")
        self.declare(self.root)
        cases = {str(target / "tp" / "x.md"): "allow", str(target / "other.md"): "deny",
                 str(target / "mem" / "feedback_x.md"): "allow"}
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(self.decision(self.fence({"file_path": path, "content": "x"})), want)

    def test_one_run_reads_the_worktrees_root_once(self) -> None:
        """K-P2: a root that changes between two reads (A, then B) still puts the
        prep beside the lock, because run() reads it once and passes it down."""
        first, second = self.tmp / "A", self.tmp / "B"
        code = ("from unittest import mock\n"
                "import roots\n"
                f"m = mock.patch.object(roots, 'under_projects', side_effect=[{str(first)!r}, "
                f"{str(second)!r}]).start()\n"
                + _REBASE_RUN
                + f"go({str(self.tmp / 'acme')!r})\n"
                "print(m.call_count)\n")
        proc = self.py(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[:3],
                         ["9", repr([str(first / "acme-7-prep")]), "1"], proc.stderr)
        self.assertTrue((first / "acme-7-prep.lock").exists())


if __name__ == "__main__":
    unittest.main()
