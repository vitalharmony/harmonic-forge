"""harmonic-forge#1566 TC3 and TC5: every generated lane task resolves the
forge root at run time (HARMONIC_FORGE_ROOT, else instance.toml forge_root,
else a refusal), with no home default, and wrapper_parity still discovers the
platform scripts the block invokes."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lane_tasks  # noqa: E402

SCRIPTS = ("gh/l1_post.py", "gh/post_lane_discussion.py", "worktree/check_worktree_busy.py")
REFUSAL = "lane task: no forge root"
#: The tasks that invoke a platform script, and the env each body reads.
TASKS = {"l1-post": {"usage_args": ""},
         "lane-comment": {"usage_issue": "1", "usage_file": "f", "usage_kind": "discussion"},
         "gate-checkout": {"usage_branch": "main"}}


def bodies() -> dict[str, str]:
    block = lane_tasks.render()
    inner = block.split(lane_tasks.BEGIN_MARKER, 1)[1].split(lane_tasks.END_MARKER, 1)[0]
    return {name: task["run"] for name, task in tomllib.loads(inner)["tasks"].items()}


class TC3RunTimeRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="home with space "))
        self.repo = self.home / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.home)], check=False)

    def stub_forge(self, root: Path) -> None:
        for rel in SCRIPTS:
            path = root / "tools" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("import os, sys\nprint('RAN', os.environ['HARMONIC_FORGE_ROOT'])\n"
                            "sys.exit(7)\n", encoding="utf-8")

    def run_task(self, name: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {"PATH": os.environ["PATH"], "HOME": str(self.home), **TASKS[name], **env}
        return subprocess.run(["sh", "-c", "-o", "errexit", bodies()[name]], env=base,
                              cwd=self.repo, capture_output=True, text=True, timeout=60)

    def test_no_backslash_in_any_body(self) -> None:
        for name, body in bodies().items():
            self.assertNotIn("\\", body, name)

    def test_the_env_root_wins(self) -> None:
        root = self.home / "env forge"
        self.stub_forge(root)
        for name in TASKS:
            with self.subTest(name):
                proc = self.run_task(name, HARMONIC_FORGE_ROOT=str(root))
                self.assertIn(f"RAN {root}", proc.stdout)

    def test_instance_toml_forge_root_with_tilde(self) -> None:
        self.stub_forge(self.home / "x")
        cfg = self.home / ".config" / "harmonic-forge"
        cfg.mkdir(parents=True)
        (cfg / "instance.toml").write_text('forge_root = "~/x"\n', encoding="utf-8")
        for name in TASKS:
            with self.subTest(name):
                proc = self.run_task(name, HARMONIC_FORGE_ROOT="")
                self.assertIn(f"RAN {self.home}/x", proc.stdout)

    def test_neither_refuses_and_invokes_nothing(self) -> None:
        self.stub_forge(self.home / "harmonic-forge")  # the old default must not be used
        for name in TASKS:
            with self.subTest(name):
                proc = self.run_task(name)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("RAN", proc.stdout)
                self.assertEqual(proc.stderr.strip().splitlines(), [proc.stderr.strip()])
                self.assertIn(REFUSAL, proc.stderr)

    def test_an_unparseable_instance_toml_still_refuses_by_name(self) -> None:
        cfg = self.home / ".config" / "harmonic-forge"
        cfg.mkdir(parents=True)
        (cfg / "instance.toml").write_text("forge_root = [\n", encoding="utf-8")
        proc = self.run_task("gate-checkout")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(REFUSAL, proc.stderr)


class TC5WrapperParity(unittest.TestCase):
    def test_a_rendered_block_is_discovered(self) -> None:
        forge = HERE.parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            mise = Path(tmp) / "mise.toml"
            mise.write_text(lane_tasks.render(), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(forge / "tools" / "gh" / "wrapper_parity.py"),
                 "--mise-toml", str(mise), "--discover", "--expect", "l1-post,lane-comment"],
                env={**os.environ, "HARMONIC_FORGE_ROOT": str(forge)},
                capture_output=True, text=True, timeout=120)
        self.assertNotIn("not discovered", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
