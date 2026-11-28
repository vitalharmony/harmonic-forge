#!/usr/bin/env python3
"""harmonic-forge#1663 (operator D90, D93): the two grant-writing hook
entrypoints arm `manifest_overlay.refuse_reads` as the first statement of
`main`, so no grant decision they make can read an app repo's overlay (R1
enforced in code). AC numbers are the handoff's.

Every end-to-end case runs the hook in a fresh interpreter from inside an
overlay-only checkout, with `HOME` at a scratch dir (so every grant store is a
scratch one), `FORGE_PROJECTS_MANIFEST` at a scratch base and a failing fake
`gh` first on `PATH`. A mutant, patched in the child before `main` runs, makes a
grant path read the overlay the way a good-faith fallback would; the control
run disarms the refusal to show the same mutant reads it there.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path[:0] = [str(HERE), str(TOOLS / "onboard"), str(TOOLS / "gh")]

from test_manifest_overlay import overlay_only_fixture  # noqa: E402

ENTRYPOINTS = {"expand_lane_shorthand.main": HERE / "expand_lane_shorthand.py",
               "batch_gate.main": HERE / "batch_gate.py"}
HEAD = "f" * 40
PATHS = (f"HOOKS, ONBOARD, GH = {str(HERE)!r}, {str(TOOLS / 'onboard')!r}, "
         f"{str(TOOLS / 'gh')!r}\nimport sys\nsys.path[:0] = [HOOKS, ONBOARD, GH]\n")


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo, self.base = overlay_only_fixture(self.root)
        self.home = self.root / "home"
        self.home.mkdir()
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text("#!/bin/sh\necho 'fake gh: refused' >&2\nexit 1\n")
        (bindir / "gh").chmod(0o755)
        self.env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(self.home), "LANE": "1",
                    "FORGE_PROJECTS_MANIFEST": str(self.base), "HARMONIC_FORGE_TESTING": "1"}
        self.mark = self.root / "mutant-ran"

    def child(self, code: str, stdin: str = "") -> subprocess.CompletedProcess:
        prelude = PATHS + f"MARK = {str(self.mark)!r}\n"
        return subprocess.run([sys.executable, "-B", "-c", prelude + code], cwd=self.repo,
                              env=self.env, input=stdin, capture_output=True, text=True,
                              timeout=120, check=False)

    def written(self) -> list[str]:
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*") if p.is_file())


class AC2ExpandRefusesEndToEnd(Scratch):
    """`BATCH F5 IMC F5` in a LANE=1 session; the mutant is KD5's shape: the
    credential slot falls back to `account_for_origin(..., checkout=cwd)`, which
    IMC's label read reaches (the FORCE path never does; pitch change 1)."""

    CODE = (
        "from pathlib import Path\n"
        "import manifest_identity, manifest_overlay\n"
        "def mutant(repo, path=None):\n"
        "    Path(MARK).write_text(str(repo))\n"
        "    acct = manifest_identity.account_for_origin('https://github.com/acme/app.git',\n"
        "                                                checkout=Path.cwd())\n"
        "    Path(MARK).write_text(f'{repo} -> {acct}')\n"
        "    return None\n"
        "manifest_identity.slot_env_or_none = mutant\n"
        "if DISARM:\n"
        "    manifest_overlay.refuse_reads = lambda reason: None\n"
        "import expand_lane_shorthand as m\n"
        "m.main()\n")

    def run_hook(self, disarm: bool) -> subprocess.CompletedProcess:
        payload = json.dumps({"prompt": "BATCH F5 IMC F5", "session_id": "s-1663"})
        return self.child(f"DISARM = {disarm}\n" + self.CODE, payload)

    def test_ac2_an_overlay_fallback_on_the_imc_path_is_refused_and_exits_non_zero(self) -> None:
        result = self.run_hook(disarm=False)
        self.assertTrue(self.mark.exists(), "the mutant never ran, so this case proves nothing")
        self.assertEqual(self.mark.read_text(), "acme/forge", "the mutant's overlay read returned")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("OverlayReadRefused", result.stderr)
        self.assertIn("expand_lane_shorthand.main", result.stderr)
        self.assertIn("harmonic-forge#1663", result.stderr)
        self.assertEqual(result.stdout, "", "a receipt was emitted past the refusal")
        # The exact on-disk set: BATCH (the step before IMC) wrote its grant;
        # IMC wrote no waiver (residual 2: the earlier grant stays).
        self.assertEqual(self.written(), [".claude/state/batch-authorized.json",
                                          ".claude/state/batch-authorized.json.lock"])
        grants = json.loads((self.home / ".claude/state/batch-authorized.json").read_text())
        self.assertEqual(sorted(grants), ["F5"])
        self.assertFalse(list(self.home.rglob("imc")), "an IMC state dir was created")

    def test_control_the_same_mutant_reads_the_overlay_when_disarmed(self) -> None:
        result = self.run_hook(disarm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mark.read_text(), "acme/forge -> acme",
                         "unarmed, the mutant resolves the overlay-only repo's account")
        self.assertIn("BATCH: authorized F5", result.stdout)


class AC2bBatchGateAsks(Scratch):
    """D93: `batch_gate.main` catches the refusal and emits `ask`. The command
    is a BATCH-authorized, PR-linked merge on green CI, which `allow`s unarmed,
    so `ask` here can only come from the refusal."""

    CODE = (
        "import io, json, os, runpy\n"
        "from pathlib import Path\n"
        "os.isatty = lambda fd: True\n"
        "BLOCK_IMPORT and sys.modules.__setitem__('manifest_overlay', None)\n"
        "import batch_auth\n"
        "batch_auth.authorize(['F5'])\n"
        "batch_auth.link_pr('F5', 'acme/forge', 5)\n"
        "def mutant(repo, number, run=None):\n"
        "    Path(MARK).write_text('ran')\n"
        "    if not BLOCK_IMPORT:\n"
        "        import manifest_overlay\n"
        "        manifest_overlay.project_for_checkout(Path.cwd())\n"
        f"    return 'green', 'faked', {HEAD!r}\n"
        "batch_auth.merge_ci_state = mutant\n"
        "if DISARM:\n"
        "    import manifest_overlay\n"
        "    manifest_overlay.refuse_reads = lambda reason: None\n"
        "runpy.run_path(str(Path(HOOKS) / 'batch_gate.py'), run_name='__main__')\n")

    def run_gate(self, disarm: bool = False, block_import: bool = False):
        command = f"gh pr merge 5 --match-head-commit {HEAD} --repo acme/forge"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        result = self.child(f"DISARM = {disarm}\nBLOCK_IMPORT = {block_import}\n" + self.CODE,
                            payload)
        decision = json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None
        return result, decision

    def test_ac2b_a_refused_read_inside_decide_emits_ask_naming_1663(self) -> None:
        result, decision = self.run_gate()
        self.assertEqual(self.mark.read_text(), "ran", "the mutant never ran")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(decision, "no decision: the call would fall to the permission mode")
        self.assertEqual(decision["permissionDecision"], "ask")
        reason = decision["permissionDecisionReason"]
        self.assertIn("harmonic-forge#1663", reason)
        self.assertIn("batch_gate.main", reason)

    def test_control_the_same_merge_is_allowed_when_disarmed(self) -> None:
        result, decision = self.run_gate(disarm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mark.read_text(), "ran")
        self.assertEqual(decision["permissionDecision"], "allow")

    def test_an_overlay_module_that_cannot_be_armed_asks_on_a_covered_command(self) -> None:
        result, decision = self.run_gate(block_import=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("could not be imported or armed", decision["permissionDecisionReason"])
        self.assertIn("harmonic-forge#1663", decision["permissionDecisionReason"])

    def test_an_arming_call_that_raises_asks_on_a_covered_command(self) -> None:
        """Pass-1 survivor 2: the module imports but `refuse_reads` raises (or
        is missing, as at origin/main). D93: ask, never a silent exit 1."""
        code = ("import manifest_overlay\n"
                "def boom(reason):\n"
                "    raise AttributeError('no refuse_reads')\n"
                "manifest_overlay.refuse_reads = boom\n")
        command = f"gh pr merge 5 --match-head-commit {HEAD} --repo acme/forge"
        for cmd, covered in ((command, True), ("ls", False)):
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
            result = self.child(code + "import runpy\nfrom pathlib import Path\n"
                                "runpy.run_path(str(Path(HOOKS) / 'batch_gate.py'), "
                                "run_name='__main__')\n", payload)
            with self.subTest(command=cmd):
                self.assertEqual(result.returncode, 0, result.stderr)
                if not covered:
                    self.assertEqual(result.stdout, "")
                    continue
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "ask")
                self.assertIn("could not be imported or armed (AttributeError: no refuse_reads)",
                              decision["permissionDecisionReason"])

    def test_an_unarmable_gate_stays_silent_on_an_uncovered_command(self) -> None:
        code = ("sys.modules['manifest_overlay'] = None\nimport runpy\nfrom pathlib import Path\n"
                "runpy.run_path(str(Path(HOOKS) / 'batch_gate.py'), run_name='__main__')\n")
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        result = self.child(code, payload)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)


class ExpandWithoutTheOverlayModule(Scratch):
    """Pass-1 survivor 1: an overlay module that cannot import leaves nothing to
    read the overlay through, so the hook expands as origin/main does (exit 0)
    instead of exiting 1 on every prompt; with it, `main` runs armed."""

    CODE = ("BLOCK_IMPORT and sys.modules.__setitem__('manifest_overlay', None)\n"
            "import expand_lane_shorthand as m\n"
            "m.main()\n"
            "if not BLOCK_IMPORT:\n"
            "    import manifest_overlay\n"
            "    print('ARMED=' + str(manifest_overlay._REFUSED))\n")

    def run_hook(self, block_import: bool) -> subprocess.CompletedProcess:
        payload = json.dumps({"prompt": "L2D F5 please review", "session_id": "s-1663"})
        return self.child(f"BLOCK_IMPORT = {block_import}\n" + self.CODE, payload)

    def test_an_unimportable_overlay_module_still_expands_and_exits_zero(self) -> None:
        result = self.run_hook(block_import=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("L2D [Lane 2 done", result.stdout)
        self.assertNotIn("ARMED=", result.stdout)

    def test_the_normal_path_expands_armed(self) -> None:
        result = self.run_hook(block_import=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("L2D [Lane 2 done", result.stdout)
        self.assertIn("ARMED=expand_lane_shorthand.main", result.stdout)


class AC3ArmingIsTheFirstStatement(unittest.TestCase):
    """The one accepted spelling: `manifest_overlay.refuse_reads(...)` as an
    expression statement, first in `main`. `from manifest_overlay import
    refuse_reads` fails this by design (pitch change 6)."""

    def test_ac3_each_main_arms_first(self) -> None:
        for entry, path in ENTRYPOINTS.items():
            with self.subTest(entry=entry):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                main = next(n for n in tree.body
                            if isinstance(n, ast.FunctionDef) and n.name == "main")
                first = main.body[0]
                if isinstance(first, ast.Try):  # batch_gate arms inside its ask handler (D93)
                    first = first.body[0]
                armed = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                         and isinstance(first.value.func, ast.Attribute)
                         and first.value.func.attr == "refuse_reads"
                         and isinstance(first.value.func.value, ast.Name)
                         and first.value.func.value.id == "manifest_overlay")
                self.assertTrue(armed, f"{entry}: the first statement of main is not "
                                       "`manifest_overlay.refuse_reads(...)` (harmonic-forge#1663)")
                arg = first.value.args[0] if first.value.args else None
                self.assertEqual(getattr(arg, "value", None), entry,
                                 f"{entry}: the arming reason must name the entrypoint")

    def test_the_hooks_name_only_the_arming_api_of_the_overlay_module(self) -> None:
        for entry, path in ENTRYPOINTS.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == "manifest_overlay"}
            with self.subTest(entry=entry):
                self.assertLessEqual(used, {"refuse_reads", "OverlayReadRefused", "error"}, entry)


class AC4RequireCiPlanStaysUnarmed(Scratch):
    """`require_ci_plan` is a legitimate overlay consumer (through
    `ci_plan.resolve_project`); arming it would flip an overlay-only repo's deny
    into its fail-open allow (design alternative 1)."""

    def test_ac4_an_overlay_only_checkout_with_no_receipt_is_still_denied(self) -> None:
        self.env["LANE"] = "2"
        self.env["HARMONIC_FORGE_CI_PLAN_DIR"] = str(self.root / "receipts")
        payload = {"tool_name": "Edit", "session_id": "s-1663", "cwd": str(self.repo),
                   "tool_input": {"file_path": str(self.repo / "a.py")}}
        code = (f"import json, os\nimport require_ci_plan\n"
                f"print(json.dumps(require_ci_plan.decide({payload!r}, os.environ['LANE'])))\n")
        result = self.child(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        reason = json.loads(result.stdout)
        self.assertIsNotNone(reason, "an overlay-only checkout with no receipt was allowed")
        self.assertIn("Read the gate first (harmonic-forge#918)", reason)


class AC6ArmingNeverLeaksIntoTheSuite(unittest.TestCase):
    """`tools/run_tests.py` runs every directory in one process, so an
    in-process `main()` would arm every later test (pitch change 4). Every test
    function that calls either entrypoint's `main()` in-process restores
    `manifest_overlay._REFUSED` around it (`mock.patch.object(...,
    "_REFUSED", None)`)."""

    MODULES = {"expand_lane_shorthand", "batch_gate"}

    def test_every_in_process_main_call_restores_the_flag(self) -> None:
        checked = 0
        for path in sorted(TOOLS.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                       for a in n.names if a.name in self.MODULES}
            if not aliases:
                continue
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
                calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "main"
                         and isinstance(n.func.value, ast.Name) and n.func.value.id in aliases]
                if not calls:
                    continue
                checked += 1
                restores = any(isinstance(n, ast.Constant) and n.value == "_REFUSED"
                               for n in ast.walk(fn))
                self.assertTrue(restores, f"{path.relative_to(TOOLS)}:{fn.lineno} {fn.name} calls "
                                          "main() in-process without restoring _REFUSED")
        self.assertGreaterEqual(checked, 9, "the scan found fewer in-process callers than exist")

    def test_each_in_process_caller_leaves_the_flag_unset(self) -> None:
        """Pass-1 survivor 3: the check above is bound to the spelling
        `"_REFUSED"`, which `contextlib.nullcontext("_REFUSED")` satisfies.
        This runs, for every in-process caller, one test that reaches it, and
        checks that `main` armed and that the flag is unset afterwards."""
        import importlib  # noqa: PLC0415
        import manifest_overlay  # noqa: PLC0415

        real = manifest_overlay.refuse_reads
        ran = 0
        for path in sorted(TOOLS.rglob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                       for a in n.names if a.name in self.MODULES}
            if not aliases or path.resolve() == Path(__file__).resolve():
                continue
            tests = [(c.name, f) for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
                     for f in c.body if isinstance(f, ast.FunctionDef)
                     and f.name.startswith("test")]
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
                if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                           and n.func.attr == "main" and isinstance(n.func.value, ast.Name)
                           and n.func.value.id in aliases for n in ast.walk(fn)):
                    continue
                name = next((f"{cls}.{t.name}" for cls, t in tests if t is fn or any(
                    isinstance(n, ast.Call) and fn.name == getattr(n.func, "attr", None)
                    or isinstance(n, ast.Call) and fn.name == getattr(n.func, "id", None)
                    for n in ast.walk(t))), None)
                where = f"{path.relative_to(TOOLS)}:{fn.lineno} {fn.name}"
                self.assertIsNotNone(name, f"{where}: no test reaches this caller")
                sys.path.insert(0, str(path.parent))
                module = importlib.import_module(path.stem)
                arms: list[str] = []

                def spy(reason: str) -> None:
                    real(reason)
                    arms.append(reason)

                result = unittest.TestResult()
                try:
                    with mock.patch.object(manifest_overlay, "refuse_reads", spy):
                        unittest.defaultTestLoader.loadTestsFromName(name, module).run(result)
                    left = manifest_overlay._REFUSED
                finally:
                    manifest_overlay._REFUSED = None
                ran += 1
                with self.subTest(caller=where, test=name):
                    self.assertTrue(arms, f"{name} never reached main(): "
                                          f"{result.errors + result.failures}")
                    self.assertIsNone(left, f"{name} left the overlay refusal armed")
        self.assertGreaterEqual(ran, 9, "fewer in-process callers ran than exist")

    def test_the_flag_is_unset_in_this_process(self) -> None:
        import manifest_overlay  # noqa: PLC0415
        self.assertIsNone(manifest_overlay._REFUSED)


if __name__ == "__main__":
    unittest.main()
