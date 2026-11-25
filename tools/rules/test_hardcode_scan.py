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

    def test_a_source_behind_command_or_builtin_is_an_edge(self):
        """Post-verdict cross-family finding 1."""
        self.write("tools/lane/e.sh", '#!/bin/bash\ncommand source "$(dirname -- '
                   '"${BASH_SOURCE[0]}")/../telemetry/env.sh"\n')
        self.write("tools/lane/f.sh", '#!/bin/bash\nbuiltin . "$D/env.sh"\n')
        self.track()
        self.check_fails_naming("tools/lane/e.sh imports operator file tools/telemetry/env.sh",
                                "tools/lane/f.sh imports operator file tools/telemetry/env.sh")

    def test_an_aliased_loader_is_still_a_load(self):
        """Post-verdict cross-family finding 2."""
        self.write("tools/hooks/al.py", "from importlib import import_module as load\n\n\n"
                   "def go(name):\n    return load(name)\n")
        self.write("tools/hooks/al2.py", "import importlib\nloader = importlib.import_module\n\n\n"
                   "def go(name):\n    return loader(name)\n")
        self.track()
        self.check_fails_naming("[import-unresolved] tools/hooks/al.py",
                                "[import-unresolved] tools/hooks/al2.py")

    def test_an_interpolated_filename_is_unresolved_not_its_extension(self):
        """Post-verdict cross-family finding 3."""
        self.write("tools/hooks/fs.py", "from importlib.util import spec_from_file_location\n\n\n"
                   "def go(stem):\n"
                   '    return spec_from_file_location("p", f"../telemetry/{stem}.py")\n')
        self.write("tools/telemetry/emit.py", "X = 1\n")
        self.write("tools/hooks/fs2.py", "from importlib.util import spec_from_file_location\n\n\n"
                   "def go(root):\n"
                   '    return spec_from_file_location("p", f"{root}/telemetry/emit.py")\n')
        self.track()
        self.check_fails_naming("[import-unresolved] tools/hooks/fs.py",
                                "tools/hooks/fs2.py imports operator file tools/telemetry/emit.py")

    def test_post_verdict_2_shell_forms_are_edges_or_unresolved(self):
        """Operator ruling (a): `source --`, an assignment prefix, a quoted
        eval and a glob operand are never clean."""
        self.write("tools/lane/g.sh", '#!/bin/bash\nsource -- "../telemetry/env.sh"\n')
        self.write("tools/lane/h.sh", '#!/bin/bash\nLC_ALL=C source "../telemetry/env.sh"\n')
        self.write("tools/lane/i.sh", "#!/bin/bash\neval 'source \"../telemetry/env.sh\"'\n")
        self.write("tools/lane/j.sh", "#!/bin/bash\nsource ../telemetry/en?.sh\n")
        self.track()
        self.check_fails_naming(
            "tools/lane/g.sh imports operator file tools/telemetry/env.sh",
            "tools/lane/h.sh imports operator file tools/telemetry/env.sh",
            "[import-unresolved] tools/lane/i.sh", "[import-unresolved] tools/lane/j.sh")

    def test_post_verdict_2_python_forms_are_edges_or_unresolved(self):
        """Operator ruling (a): a reassigned path, an annotated alias,
        runpy.run_path and a package named to import_module or by a dotted
        import are never clean."""
        self.write("tools/telemetry/emit.py", "X = 1\n")
        self.write("tools/hooks/k.py", "from importlib.util import spec_from_file_location\n"
                   'path = "../telemetry/emit.py"\nspec_from_file_location("e", path)\n'
                   'path = "core.py"\n')
        self.write("tools/hooks/l.py", "import importlib\nload: object = importlib.import_module\n")
        self.write("tools/hooks/m.py", 'import runpy\nrunpy.run_path("../telemetry/emit.py")\n')
        self.write("tools/hooks/n.py", 'import importlib\nimportlib.import_module("telemetry")\n')
        self.write("tools/hooks/o.py", "import tools.telemetry\n")
        self.track()
        self.check_fails_naming(
            "[import-unresolved] tools/hooks/k.py", "[import-unresolved] tools/hooks/l.py",
            "tools/hooks/m.py imports operator file tools/telemetry/emit.py",
            "tools/hooks/n.py imports operator file tools/telemetry/",
            "tools/hooks/o.py imports operator file tools/telemetry/")

    def test_post_verdict_3_forms_are_never_clean(self):
        """Post-verdict 3: a parameter rebinding, an annotated alias call,
        a quoted or continued source, an eval with no source word."""
        self.write("tools/telemetry/emit.py", "X = 1\n")
        self.write("tools/hooks/q.py", "import runpy\np = 'core.py'\n\n\ndef load(p):\n"
                   "    return runpy.run_path(p)\n")
        self.write("tools/hooks/r.py", "import runpy\nfrom typing import Callable\n"
                   "load: Callable = runpy.run_path\nload('../telemetry/emit.py')\n")
        self.write("tools/lane/s.sh", "#!/bin/bash\n'source' ../telemetry/env.sh\n")
        self.write("tools/lane/t.sh", "#!/bin/bash\nsource \\\n  ../telemetry/env.sh\n")
        self.write("tools/lane/u.sh", '#!/bin/bash\neval "$CMD"\n')
        self.track()
        self.check_fails_naming(
            "[import-unresolved] tools/hooks/q.py", "[import-unresolved] tools/hooks/r.py",
            "tools/lane/s.sh imports operator file tools/telemetry/env.sh",
            "tools/lane/t.sh imports operator file tools/telemetry/env.sh",
            "[import-unresolved] tools/lane/u.sh")

    def test_two_loader_references_on_one_line_count_twice(self):
        self.write("tools/hooks/v.py", "from importlib import import_module as load\n"
                   "exec(code); load(name)\n")
        self.track()
        rc, text = self.run_scan("--print")
        self.assertIn('text = "exec(code); load(name)"\ncount = 2', text)

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


ENV = "../telemetry/env.sh"
EMIT = "../telemetry/emit.py"


class TestF1556Residuals(ScratchRepo):
    """harmonic-forge#1556: the five residual load forms, each an edge or an
    unresolved row, never clean."""

    def setUp(self):
        super().setUp()
        self.write("tools/telemetry/env.sh", "X=1\n")
        self.write("tools/telemetry/emit.py", "X = 1\n")
        self.track()
        self.baseline_now()
        self.n = 0

    def plant(self, ext, text):
        """Write `text` to a new tracked core file; its path."""
        self.n += 1
        rel = (f"tools/lane/p{self.n}.sh" if ext == "sh" else f"tools/hooks/p{self.n}.py")
        self.write(rel, ("#!/bin/bash\n" if ext == "sh" else "") + text)
        self.track()
        return rel

    def check(self):
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 1, out)
        return out

    def assert_edge(self, ext, text, target):
        rel = self.plant(ext, text)
        self.assertIn(f"{rel} imports operator file {target}", self.check())

    def assert_unresolved_call(self, text, call):
        """`call` is an unresolved row and the file has no import edge."""
        rel = self.plant("py", text)
        out = self.check()
        self.assertIn(f"[import-unresolved] {rel} has a load whose target cannot be "
                      f"resolved: {call}", out)
        self.assertNotIn(f"{rel} imports operator file", out)

    def test_quoted_dot_is_an_edge(self):
        for line in (f"'.' {ENV}", f"FOO=1 '.' {ENV}", f"FOO=1 s'ource' {ENV}",
                     f"case $x in a) '.' {ENV};; esac"):
            with self.subTest(line=line):
                self.assert_edge("sh", line + "\n", "tools/telemetry/env.sh")

    def test_quoted_dot_survives_update(self):
        """AC2: `--update` cannot ratchet a still-live quoted-dot load out."""
        rel = self.plant("sh", f". {ENV}\n")
        self.baseline_now()
        key = ("import", rel, "tools/telemetry/env.sh")
        self.assertEqual(self.baseline_rows()[key], 1)
        self.write(rel, f"#!/bin/bash\n'.' {ENV}\n")
        rc, out = self.run_scan("--update")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_scan("--check")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.baseline_rows()[key], 1)

    def test_quoted_eval_is_unresolved(self):
        rel = self.plant("sh", "'eval' \"$CMD\"\n")
        self.assertIn(f"[import-unresolved] {rel} has a load whose target cannot be "
                      "resolved: 'eval' \"$CMD\"", self.check())

    def test_continued_command_word_is_an_edge(self):
        for text in (f"sou\\\nrce {ENV}\n", f"FOO=1 sou\\\nrce {ENV}\n",
                     f"case $x in a) sou\\\nrce {ENV};; esac\n", f"FOO=1 '.' \\\n{ENV}\n"):
            with self.subTest(text=text):
                self.assert_edge("sh", text, "tools/telemetry/env.sh")

    def test_source_on_eval_line_keeps_its_edge(self):
        """AC3 pin: the eval line is one unresolved row AND the `source` on it
        keeps its own import row."""
        line = f'eval ":"; source {ENV}'
        rel = self.plant("sh", line + "\n")
        self.baseline_now()
        rows = {k: v for k, v in self.baseline_rows().items() if k[1] == rel}
        self.assertEqual(rows, {("import-unresolved", rel, line): 1,
                                ("import", rel, "tools/telemetry/env.sh"): 1})

    def test_qualified_from_import_star_is_an_edge(self):
        for text in ("from tools.telemetry import *\n", "from tools import telemetry\n"):
            with self.subTest(text=text):
                self.assert_edge("py", text, "tools/telemetry/")

    def test_chained_alias_call_is_an_edge(self):
        self.assert_edge("py", "import runpy\nif True:\n    first = runpy.run_path\n"
                         f'second = first\nsecond("{EMIT}")\n', "tools/telemetry/emit.py")

    def test_rebound_alias_call_is_unresolved(self):
        """Bound twice, by an expression or by bare references only: the
        second form is decided by the exact-alias rule alone, since a bare
        reference is never a loose binding."""
        for rebind in ("load = runpy.run_path if use_path else runpy.run_module\n",
                       "if use_path:\n    load = runpy.run_path\n"):
            with self.subTest(rebind=rebind):
                self.assert_unresolved_call(
                    f"import runpy\nload = runpy.run_module\n{rebind}"
                    f'load("{EMIT}")\n', f'load("{EMIT}")')

    def test_import_as_rebinding_is_unresolved(self):
        a = "load = runpy.run_path\n"
        b = "from importlib import import_module as load\n"
        for body in (a + b, b + a):
            with self.subTest(body=body):
                self.assert_unresolved_call(f'import runpy\n{body}load("tools.telemetry")\n',
                                            'load("tools.telemetry")')

    def test_def_or_class_rebinding_is_unresolved(self):
        with self.subTest("class"):
            self.assert_unresolved_call("import runpy\nload = runpy.run_path\nclass load:\n"
                                        f'    pass\nload("{EMIT}")\n', f'load("{EMIT}")')
        with self.subTest("def"):
            self.assert_unresolved_call("from importlib import import_module as load\n"
                                        "def load(p):\n    return p\n"
                                        'load("tools.telemetry")\n', 'load("tools.telemetry")')

    def test_match_or_type_param_rebinding_is_unresolved(self):
        head = "import runpy\nload = runpy.run_path\n"
        call = 'load("tools.telemetry")'
        for text in (
                "import runpy, importlib\nload = runpy.run_path\n"
                "match importlib.import_module:\n    case load:\n        pass\n",
                head + "match 1:\n    case int() as load:\n        pass\n",
                head + "match [1]:\n    case [*load]:\n        pass\n",
                head + "class C[load]:\n    pass\n"):
            with self.subTest(text=text):
                self.assert_unresolved_call(text + call + "\n", call)

    def assert_loose_alias_call(self, binding):
        """A loader alias bound once by `binding` is unresolved at the call
        after it, not only on the binding line (operator widening of
        harmonic-forge#1556 to residual d, 2026-11-25)."""
        call = f'load("{EMIT}")'
        self.assert_unresolved_call(f"import runpy\n{binding}{call}\n", call)

    def test_walrus_alias_call_is_unresolved(self):
        self.assert_loose_alias_call("if (load := runpy.run_path):\n    pass\n")

    def test_for_target_alias_call_is_unresolved(self):
        self.assert_loose_alias_call("for load in (runpy.run_path,):\n    pass\n")

    def test_with_target_alias_call_is_unresolved(self):
        self.assert_loose_alias_call("from contextlib import nullcontext\n"
                                     "with nullcontext(runpy.run_path) as load:\n    pass\n")

    def test_tuple_unpacking_alias_call_is_unresolved(self):
        for binding in ("load, x = runpy.run_path, 1\n", "[x, (load, y)] = 1, (runpy.run_path, 2)\n"):
            with self.subTest(binding=binding):
                self.assert_loose_alias_call(binding)

    def test_augmented_assignment_alias_call_is_unresolved(self):
        self.assert_loose_alias_call("load = None\nload += runpy.run_path\n")

    def test_parameter_default_alias_call_is_unresolved(self):
        """A def default, positional or keyword-only, is a loose binding: the
        call in the body, on its own line, is a row."""
        call = f'return load("{EMIT}")'
        for head in ("def f(load=runpy.run_path):\n", "def f(x, *, load=runpy.run_path):\n"):
            with self.subTest(head=head):
                self.assert_unresolved_call(f"import runpy\n{head}    {call}\n", call)

    def test_container_then_for_alias_call_is_unresolved(self):
        """A plain name holding a container of loaders taints a later for target."""
        self.assert_loose_alias_call("fns = (runpy.run_path,)\nfor load in fns:\n    pass\n")

    def test_plain_assign_of_a_partial_alias_call_is_unresolved(self):
        self.assert_loose_alias_call("import functools\nload = functools.partial(runpy.run_path)\n")

    def test_match_capture_alias_call_is_unresolved(self):
        """A single-bound `case load:` over a loader subject."""
        self.assert_loose_alias_call("match runpy.run_path:\n    case load:\n        pass\n")

    def test_comprehension_target_alias_call_is_unresolved(self):
        """A comprehension target called in the element, on its own line."""
        call = f'load("{EMIT}")'
        self.assert_unresolved_call(
            f"import runpy\nout = [\n    {call}\n    for load in (runpy.run_path,)\n]\n", call)

    def test_keyword_argument_alias_call_is_unresolved(self):
        self.assert_loose_alias_call(
            "from contextlib import nullcontext\n"
            "with nullcontext(enter_result=runpy.run_path) as load:\n    pass\n")

    def test_loose_chain_alias_call_is_unresolved(self):
        """A loose binding over a name that is itself only ambiguous."""
        self.assert_loose_alias_call("x = (runpy.run_path, 1)\ny = x\nfor load in y:\n    pass\n")

    def test_long_chain_alias_call_is_unresolved(self):
        """The fixpoint converges on a chain in the worst source order: the
        chain's head is an alias of a rebound loader name, so the call is a
        row, not an edge. s=3 is pitch inspection r3's counterexample; s=6 is
        long enough that the old `len(binds) + len(loose) + 1` bound runs out."""
        for s in (3, 6):
            with self.subTest(s=s):
                chain = "".join(f"X{i} = X{i - 1}\n" for i in range(s, 1, -1))
                call = f'X{s}("tools.telemetry")'
                self.assert_unresolved_call(
                    f"import_module = Z\nZ = X{s} if c else q\n{chain}"
                    f"X1 = import_module\n{call}\n", call)

    def test_loose_bind_of_a_loader_call_result_stays_clean(self):
        """A loader's return value is not a loader: `(spec := spec_from_file_location(...))`
        taints nothing, so a later `spec.loader` use is no row."""
        rel = self.plant("py", "from importlib.util import spec_from_file_location\n"
                         f'if (spec := spec_from_file_location("m", "{EMIT}")):\n'
                         "    print(spec.name)\n")
        out = self.check()
        self.assertIn(f"{rel} imports operator file tools/telemetry/emit.py", out)
        self.assertNotIn("print(spec.name)", out)

    def test_loader_name_rebinding_is_unresolved(self):
        for text, call in (
                ("import runpy\nfrom importlib import import_module\n"
                 "import_module = runpy.run_path\n", f'import_module("{EMIT}")'),
                ("from importlib import import_module\nfrom runpy import run_path\n"
                 "run_path = import_module\n", 'run_path("tools.telemetry")'),
                ("from runpy import run_path as import_module\n", f'import_module("{EMIT}")')):
            with self.subTest(call=call):
                self.assert_unresolved_call(text + call + "\n", call)


class TestWiring(unittest.TestCase):
    def test_ci_check_runs_scan(self):
        mise = (HERE.parents[1] / "mise.toml").read_text(encoding="utf-8")
        block = mise.split("[tasks.ci-check]", 1)[1].split("\n[tasks.", 1)[0]
        self.assertIn("python3 tools/rules/hardcode_scan.py --check", block)


if __name__ == "__main__":
    unittest.main()
