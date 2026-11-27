"""harmonic-forge#1566 TC3 and TC5: every generated lane task resolves the
forge root at run time (HARMONIC_FORGE_ROOT, else instance.toml forge_root,
else a refusal), with no home default, and wrapper_parity still discovers the
platform scripts the block invokes."""
from __future__ import annotations

import os
import shutil
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
         "lane-comment": {"usage_args": ""},
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
            # Reports the root it ran from and the HARMONIC_FORGE_ROOT it
            # inherited, which the prelude never writes (sticky-wicket e2p2 (a)).
            path.write_text("import os, sys, pathlib\n"
                            "print('RAN', pathlib.Path(__file__).resolve().parents[2])\n"
                            "print('ENV', os.environ.get('HARMONIC_FORGE_ROOT', '<unset>'))\n"
                            "sys.exit(7)\n", encoding="utf-8")

    def real_forge(self, root: Path, hooks: bool = True) -> None:
        """A stub forge that `instance.resolve_forge_root` accepts: the stub
        scripts, a copy of the real `tools/settings/instance.py` (stdlib-only)
        and, unless `hooks` is False, a `tools/hooks/` directory."""
        self.stub_forge(root)
        settings = root / "tools" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE.parents[1] / "tools" / "settings" / "instance.py", settings)
        if hooks:
            (root / "tools" / "hooks").mkdir(parents=True, exist_ok=True)

    def write_instance(self, text: str) -> None:
        cfg = self.home / ".config" / "harmonic-forge"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "instance.toml").write_text(text, encoding="utf-8")

    def assert_refused(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("RAN", proc.stdout)
        self.assertEqual(proc.stderr.strip().splitlines(), [proc.stderr.strip()])
        self.assertIn(REFUSAL, proc.stderr)

    def run_task(self, name: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {"PATH": os.environ["PATH"], "HOME": str(self.home), **TASKS[name], **env}
        return subprocess.run(["sh", "-c", "-o", "errexit", bodies()[name]], env=base,
                              cwd=self.repo, capture_output=True, text=True, timeout=60)

    def test_no_backslash_in_any_body(self) -> None:
        for name, body in bodies().items():
            self.assertNotIn("\\", body, name)

    def test_the_env_root_wins(self) -> None:
        root = self.home / "env forge"
        self.real_forge(root)
        for name in TASKS:
            with self.subTest(name):
                proc = self.run_task(name, HARMONIC_FORGE_ROOT=str(root))
                self.assertIn(f"RAN {root.resolve()}", proc.stdout)

    def test_instance_toml_forge_root_with_tilde(self) -> None:
        self.real_forge(self.home / "x")
        self.write_instance('forge_root = "~/x"\n')
        for name in TASKS:
            with self.subTest(name):
                proc = self.run_task(name, HARMONIC_FORGE_ROOT="")
                self.assertIn(f"RAN {(self.home / 'x').resolve()}", proc.stdout)

    def test_a_relative_env_root_refuses_by_name(self) -> None:
        """Pitch N1: a set env value passes `launch_view`'s validation too. A
        relative one is refused, never run against the consumer's cwd, even
        with a runnable forge at that relative path and a valid
        instance.toml behind it."""
        # Beside the task's cwd (self.repo), not in it: gate-checkout refuses a
        # dirty tree before its prelude runs.
        self.real_forge(self.home / "rel forge")
        declared = self.home / "declared forge"
        self.real_forge(declared)
        self.write_instance(f'forge_root = "{declared}"\n')
        for name in TASKS:
            with self.subTest(name):
                self.assert_refused(self.run_task(name, HARMONIC_FORGE_ROOT="../rel forge"))

    def test_an_env_root_without_tools_hooks_refuses_by_name(self) -> None:
        """Pitch N1: an env value naming a forge with `instance.py` but no
        `tools/hooks/` reaches `launch_view` and is refused by it."""
        root = self.home / "hookless env forge"
        self.real_forge(root, hooks=False)
        for name in TASKS:
            with self.subTest(name):
                self.assert_refused(self.run_task(name, HARMONIC_FORGE_ROOT=str(root)))

    def test_neither_refuses_and_invokes_nothing(self) -> None:
        # A forge the validator would accept, so a refusal can only mean there
        # is no home default (sticky-wicket e2p2 (b)).
        self.real_forge(self.home / "harmonic-forge")
        for name in TASKS:
            with self.subTest(name):
                self.assert_refused(self.run_task(name))

    def test_the_prelude_never_propagates_a_root(self) -> None:
        """Sticky-wicket e2p2 (a): the prelude resolves the root into a private
        shell variable and never writes or exports HARMONIC_FORGE_ROOT, so a
        child sees the caller's value unchanged: unset stays unset, an empty
        value stays empty (an inherited variable stays exported after a
        reassignment), and a symlink spelling is passed through as given."""
        declared = self.home / "declared forge"
        self.real_forge(declared)
        self.write_instance(f'forge_root = "{declared}"\n')
        link = self.home / "link forge"
        link.symlink_to(declared)
        ran = f"RAN {declared.resolve()}"
        for name in TASKS:
            with self.subTest(name, case="unset"):
                out = self.run_task(name).stdout.splitlines()
                self.assertIn(ran, out)
                self.assertIn("ENV <unset>", out)
            with self.subTest(name, case="empty"):
                out = self.run_task(name, HARMONIC_FORGE_ROOT="").stdout.splitlines()
                self.assertIn(ran, out)
                self.assertIn("ENV ", out)
            with self.subTest(name, case="symlink"):
                out = self.run_task(name, HARMONIC_FORGE_ROOT=str(link)).stdout.splitlines()
                self.assertIn(ran, out)
                self.assertIn(f"ENV {link}", out)

    def test_a_forge_root_with_a_trailing_newline_refuses_by_name(self) -> None:
        """Sticky-wicket S2: `$(...)` strips a trailing newline, so an
        unvalidated reader turned `"<root>\\n"` into a root that ran."""
        root = self.home / "nl forge"
        self.real_forge(root)
        self.write_instance(f'forge_root = "{root}\\n"\n')
        for name in TASKS:
            with self.subTest(name):
                self.assert_refused(self.run_task(name))

    def test_a_forge_root_without_tools_hooks_refuses_by_name(self) -> None:
        """Sticky-wicket S2 / dismissed finding 5: `launch_view` refuses a root
        with no `tools/hooks/`, and the generated tasks now agree."""
        root = self.home / "hookless forge"
        self.real_forge(root, hooks=False)
        self.write_instance(f'forge_root = "{root}"\n')
        for name in TASKS:
            with self.subTest(name):
                self.assert_refused(self.run_task(name))

    def test_an_unparseable_instance_toml_still_refuses_by_name(self) -> None:
        cfg = self.home / ".config" / "harmonic-forge"
        cfg.mkdir(parents=True)
        (cfg / "instance.toml").write_text("forge_root = [\n", encoding="utf-8")
        proc = self.run_task("gate-checkout")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(REFUSAL, proc.stderr)


class TC5WrapperParity(unittest.TestCase):
    def parity(self, *args: str) -> subprocess.CompletedProcess[str]:
        forge = HERE.parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            mise = Path(tmp) / "mise.toml"
            mise.write_text(lane_tasks.render(), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(forge / "tools" / "gh" / "wrapper_parity.py"),
                 "--mise-toml", str(mise), *args],
                env={**os.environ, "HARMONIC_FORGE_ROOT": str(forge)},
                capture_output=True, text=True, timeout=120)

    def test_the_handoff_shape_command_passes(self) -> None:
        """The handoff's TC5, run as written (sticky-wicket S1)."""
        proc = self.parity("--shape", "--expect", "l1-post,lane-comment")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[wrapper-parity] tools/gh wrappers forward verbatim: OK", proc.stdout)

    def test_a_rendered_block_is_discovered(self) -> None:
        forge = HERE.parents[1]
        proc = self.parity("--discover", "--expect", "l1-post,lane-comment")
        # Pass-1 survivor: the exit code and the discovered scripts decide, not the
        # absence of one message.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found = dict(line.split("\t", 1) for line in proc.stdout.splitlines() if "\t" in line)
        self.assertEqual(found.get("l1-post"), str(forge / "tools" / "gh" / "l1_post.py"))
        self.assertEqual(found.get("lane-comment"),
                         str(forge / "tools" / "gh" / "post_lane_discussion.py"))


if __name__ == "__main__":
    unittest.main()
