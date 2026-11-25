"""harmonic-forge#1524: the shrink-only hardcode baseline and import-edge check."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hardcode_scan as hc  # noqa: E402

MANIFEST = '''[[project]]
name = "widgetco"
repo = "acmeorg/widgetco"
path = "~/Harmonic_Projects/widgetco"
prefix = "W"
account = "acmeorg"
'''

OPERATOR = '''globs = ["tools/telemetry/*", "tools/memory/*"]
'''

CORE = '''import os


def run():
    return os.getcwd()
'''

PLANT = 'ROOT = Path.home() / "Harmonic_Projects"\n'

TELEMETRY_IMPORT = '''import sys
from pathlib import Path


def record():
    telemetry = str(Path(__file__).resolve().parent.parent / "telemetry")
    if telemetry not in sys.path:
        sys.path.insert(0, telemetry)
    import emit  # noqa: PLC0415
    return emit
'''


class ScratchRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write("projects.toml", MANIFEST)
        self.write("tools/rules/operator_paths.toml", OPERATOR)
        self.write("tools/hooks/core_hook.py", CORE)
        self.write("tools/telemetry/emit.py", "ROOT = '~/Harmonic_Projects'\n")
        self.git("init", "-q")
        self.track()

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True,
                       capture_output=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def track(self):
        self.git("add", "--", ".")

    def run_scan(self, *mode):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hc.main([*mode, "--root", str(self.root)])
        return rc, out.getvalue() + err.getvalue()

    def baseline_now(self):
        rc, text = self.run_scan("--print")
        self.assertEqual(rc, 0, text)
        (self.root / hc.BASELINE).write_text(text, encoding="utf-8")

    def baseline_rows(self):
        return hc.read_baseline(self.root / hc.BASELINE)


class TestFindings(ScratchRepo):
    def test_new_finding_fails(self):
        self.baseline_now()
        self.assertEqual(self.run_scan("--check")[0], 0)
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("[projects-root] tools/hooks/core_hook.py: "
                      'ROOT = Path.home() / "Harmonic_Projects"', out)

    def test_count_rise_fails(self):
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        self.baseline_now()
        self.write("tools/hooks/core_hook.py", CORE + PLANT + PLANT)
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("count 2 > baseline 1", out)

    def test_moving_line_passes(self):
        self.write("tools/hooks/core_hook.py", PLANT + CORE)
        self.baseline_now()
        self.write("tools/hooks/core_hook.py", CORE + "\n\n" + PLANT.replace(" / ", "  /  "))
        self.assertEqual(self.run_scan("--check")[0], 0)

    def test_comment_and_docstring_pass(self):
        self.baseline_now()
        self.write("tools/hooks/core_hook.py",
                   '"""Lives under ~/Harmonic_Projects."""\n' + CORE
                   + "# ROOT = Path.home() / 'Harmonic_Projects'\n"
                   + 'def f():\n    """acmeorg widgetco Harmonic_Projects"""\n    return 1\n')
        self.write("tools/hooks/run.sh", "#!/bin/sh\n# cd ~/Harmonic_Projects\necho ok\n")
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 0, out)

    def test_names_read_from_manifest(self):
        self.baseline_now()
        self.write("tools/hooks/core_hook.py", CORE + 'OWNER = "acmeorg/widgetco"\n'
                   'REF = "widgetco#12"\n')
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("[org]", out)
        self.assertIn("[project-name]", out)
        self.assertNotIn('REF = "widgetco#12"', out)

    def test_operator_and_test_files_not_reported(self):
        self.write("tools/memory/store.py", PLANT)
        self.write("tools/hooks/test_core_hook.py", PLANT)
        self.write("tools/hooks/testdata/x.json", '"~/Harmonic_Projects"\n')
        self.track()
        rc, text = self.run_scan("--print")
        self.assertEqual(rc, 0)
        self.assertNotIn("tools/memory/", text)
        self.assertNotIn("tools/telemetry/", text.replace('text = "tools/telemetry', ""))
        self.assertNotIn("test_core_hook", text)
        self.assertNotIn("testdata", text)

    def test_operator_paths_never_baselined(self):
        self.write("tools/memory/store.py", PLANT)
        self.track()
        self.baseline_now()
        self.assertFalse(any(path.startswith(("tools/memory/", "tools/telemetry/"))
                             for _, path, _ in self.baseline_rows()))


class TestBaseline(ScratchRepo):
    def test_stale_rows_fail_then_update_removes(self):
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        self.baseline_now()
        before = self.baseline_rows()
        self.write("tools/hooks/core_hook.py", CORE)
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("remove 1 stale rows", out)
        rc, out = self.run_scan("--update")
        self.assertEqual(rc, 0, out)
        after = self.baseline_rows()
        self.assertTrue(set(after) < set(before))
        self.assertTrue(all(after[k] <= before[k] for k in after))
        self.assertEqual(self.run_scan("--check")[0], 0)

    def test_update_cannot_add(self):
        self.baseline_now()
        before = (self.root / hc.BASELINE).read_text(encoding="utf-8")
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        rc, out = self.run_scan("--update")
        self.assertEqual(rc, 1)
        self.assertIn("only removes or lowers rows", out)
        self.assertEqual((self.root / hc.BASELINE).read_text(encoding="utf-8"), before)


class TestImports(ScratchRepo):
    def test_import_edge_fails(self):
        self.baseline_now()
        self.write("tools/gh/closer.py", TELEMETRY_IMPORT)
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("[import] tools/gh/closer.py imports operator file "
                      "tools/telemetry/emit.py", out)

    def test_existing_edge_passes_until_cut(self):
        self.write("tools/gh/closer.py", TELEMETRY_IMPORT)
        self.track()
        self.baseline_now()
        self.assertEqual(self.run_scan("--check")[0], 0)
        self.write("tools/gh/closer.py", CORE)
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("remove 1 stale rows", out)

    def test_shell_source_edge_fails(self):
        self.write("tools/telemetry/env.sh", "X=1\n")
        self.track()
        self.baseline_now()
        self.write("tools/lane/launch.sh", '#!/bin/sh\n. "$DIR/../telemetry/env.sh"\n')
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("tools/lane/launch.sh imports operator file tools/telemetry/env.sh", out)

    def test_duplicate_basename_fails(self):
        self.baseline_now()
        self.write("tools/gh/core_hook.py", CORE)
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("duplicate Python basename", out)
        self.assertIn("core_hook", out)


class TestReviewFixes(ScratchRepo):
    """harmonic-forge#1524 preclose pass 1."""

    def test_package_qualified_imports_resolve(self):
        self.write("tools/memory/memory_lint.py", "X = 1\n")
        self.track()
        self.baseline_now()
        for i, stmt in enumerate(("from telemetry import emit\n",
                                  "import telemetry.emit\n",
                                  "import memory.memory_lint\n")):
            self.write(f"tools/gh/user{i}.py", stmt)
        self.write("tools/gh/user3.py", "import telemetry\n")
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("tools/gh/user0.py imports operator file tools/telemetry/emit.py", out)
        self.assertIn("tools/gh/user1.py imports operator file tools/telemetry/emit.py", out)
        self.assertIn("tools/gh/user2.py imports operator file tools/memory/memory_lint.py", out)
        self.assertIn("tools/gh/user3.py imports operator file tools/telemetry/", out)

    def test_non_utf8_file_scanned_or_fails(self):
        self.baseline_now()
        p = self.root / "tools/hooks/latin.py"
        p.write_bytes(b'S = "caf\xe9"\n' + PLANT.encode())
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("[projects-root] tools/hooks/latin.py", out)
        self.baseline_now()
        p.write_bytes(b"# -*- coding: no-such-codec -*-\nS = '\xe9'\n")
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("tools/hooks/latin.py: unreadable", out)
        self.assertNotIn("stale", out)

    def test_typescript_scanned(self):
        self.baseline_now()
        self.write("tools/routing/mod/a.ts",
                   "// ~/Harmonic_Projects in a comment\n"
                   "const r = `${home}/Harmonic_Projects`;\n")
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        self.assertIn("[projects-root] tools/routing/mod/a.ts: const r", out)
        self.assertNotIn("in a comment", out)

    def test_shell_source_after_operator_found(self):
        self.write("tools/telemetry/env.sh", "X=1\n")
        self.track()
        self.baseline_now()
        self.write("tools/lane/a.sh", '#!/bin/sh\ncd "$D" && . ../telemetry/env.sh\n')
        self.write("tools/lane/b.sh", '#!/bin/sh\ntrue; source "$D/env.sh"\n')
        self.write("tools/lane/c.sh", '#!/bin/sh\nfalse || source env.sh\n')
        self.track()
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1)
        for f in "abc":
            self.assertIn(f"tools/lane/{f}.sh imports operator file tools/telemetry/env.sh", out)


class TestTwoPassPatch(ScratchRepo):
    """harmonic-forge#1524 two-pass sticky-wicket PATCH: a load the scanner
    cannot resolve is a baselined row, never clean; the ratchet's count and
    --update paths are pinned."""

    def setUp(self):
        super().setUp()
        self.write("tools/telemetry/env.sh", "X=1\n")
        self.track()
        self.baseline_now()

    def check_fails_naming(self, *needles):
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1, out)
        for n in needles:
            self.assertIn(n, out)

    def test_self_locating_source_is_an_edge(self):
        self.write("tools/lane/a.sh", '#!/bin/bash\nsource "$(dirname "$(readlink -f '
                   '"${BASH_SOURCE[0]}")")/../telemetry/env.sh"\n')
        self.track()
        self.check_fails_naming("tools/lane/a.sh imports operator file tools/telemetry/env.sh")

    def test_source_after_a_keyword_is_an_edge(self):
        self.write("tools/lane/b.sh", '#!/bin/sh\nif [ -f "$D/env.sh" ]; then source "$D/env.sh"; fi\n')
        self.write("tools/lane/c.sh", '#!/bin/sh\nfor f in x; do . "$D/env.sh"; done\n')
        self.track()
        self.check_fails_naming("tools/lane/b.sh imports operator file tools/telemetry/env.sh",
                                "tools/lane/c.sh imports operator file tools/telemetry/env.sh")

    def test_a_source_with_no_literal_filename_is_an_unresolved_row(self):
        self.write("tools/lane/d.sh", '#!/bin/sh\nsource "$CONF"\n')
        self.track()
        self.check_fails_naming("[import-unresolved] tools/lane/d.sh has a load whose target "
                                'cannot be resolved: source "$CONF"')

    def test_a_by_path_load_resolves_its_literal_tail(self):
        self.write("tools/telemetry/emit.py", "X = 1\n")
        self.write("tools/hooks/byp.py", 'import importlib.util\nfrom pathlib import Path\n'
                   '_P = Path(__file__).parent.parent / "telemetry" / "emit.py"\n'
                   'spec = importlib.util.spec_from_file_location("emit", _P)\n')
        self.track()
        self.check_fails_naming("tools/hooks/byp.py imports operator file tools/telemetry/emit.py")

    def test_an_unresolvable_load_is_an_unresolved_row(self):
        self.write("tools/hooks/dyn.py", "import importlib\n\n\ndef load(name):\n"
                   "    return importlib.import_module(name)\n")
        self.track()
        self.check_fails_naming("[import-unresolved] tools/hooks/dyn.py")

    def test_a_python_file_that_does_not_parse_fails_the_scan(self):
        self.write("tools/hooks/broken.py", "def f(:\n    import emit\n")
        self.track()
        self.check_fails_naming("tools/hooks/broken.py: does not parse as Python")

    def test_a_lowered_count_is_stale_and_update_lowers_it(self):
        self.write("tools/hooks/core_hook.py", CORE + PLANT + PLANT)
        self.track()
        self.baseline_now()
        key = ("projects-root", "tools/hooks/core_hook.py", 'ROOT = Path.home() / "Harmonic_Projects"')
        self.assertEqual(self.baseline_rows()[key], 2)
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        self.check_fails_naming("remove 1 stale rows", "(baseline 2, scan 1)")
        self.assertEqual(self.run_scan("--update")[0], 0)
        self.assertEqual(self.baseline_rows()[key], 1)

    def test_update_keeps_a_still_live_row(self):
        self.write("tools/hooks/core_hook.py", CORE + PLANT)
        self.write("tools/hooks/other.py", 'P = "~/Harmonic_Projects/x"\n')
        self.track()
        self.baseline_now()
        live = [k for k in self.baseline_rows() if k[1] == "tools/hooks/other.py"]
        self.assertTrue(live)
        self.write("tools/hooks/core_hook.py", CORE)
        self.assertEqual(self.run_scan("--update")[0], 0)
        after = self.baseline_rows()
        for k in live:
            self.assertIn(k, after)
        self.assertEqual(self.run_scan("--check")[0], 0)


class TestWiring(unittest.TestCase):
    def test_ci_check_runs_scan(self):
        mise = (HERE.parents[1] / "mise.toml").read_text(encoding="utf-8")
        block = mise.split("[tasks.ci-check]", 1)[1].split("\n[tasks.", 1)[0]
        self.assertIn("python3 tools/rules/hardcode_scan.py --check", block)


if __name__ == "__main__":
    unittest.main()
