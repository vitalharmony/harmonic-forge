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
"""
from __future__ import annotations

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

    def declare(self, root: Path | None) -> None:
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
        proc = self.py(f"import worktree_issue as w; print(any(str({str(path)!r}).startswith(r + '/') "
                       "for r in w._IMPL_ROOTS))")
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
                 "/srv/a\nb/hrse2-7-impl"]
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


if __name__ == "__main__":
    unittest.main()
