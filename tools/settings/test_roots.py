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

    def test_the_run_fixture_works_under_a_tmpdir_inside_a_checkout(self) -> None:
        """Pass-1 survivor: a Codex lane's TMPDIR has a .git ancestor, where
        instance.py reads no config; the run says so once and goes on -- no
        crash, and its temp root is still removed on exit."""
        checkout = self.tmp / "lane tmp"
        (checkout / ".git").mkdir(parents=True)
        code = ("import run_tests\n"
                "with run_tests.contained_tempdir() as root:\n"
                "    print('entered')\n")
        proc = subprocess.run([sys.executable, "-c", f"import sys; sys.path[:0] = [{str(TOOLS)!r}, "
                               f"{str(TOOLS / 'settings')!r}]\n" + code],
                              env={**self.env(), "TMPDIR": str(checkout)},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "entered")
        self.assertIn("per-run instance.toml skipped", proc.stderr)
        self.assertEqual([p.name for p in checkout.iterdir()], [".git"])

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
