#!/usr/bin/env python3
"""harmonic-forge#1500: the private gh close/merge recognizer check.

Every plant runs against a temp git root holding a copy of the two adapter files
plus the planted modules, through the check's `main(root=...)`, never the
worktree.
"""
from __future__ import annotations

import contextlib
import io
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import check_gh_recognizers as check  # noqa: E402


class Root:
    """A temp git root: the two adapters copied in, `files` written and tracked."""

    def __init__(self, files: dict[str, str], adapters=check.ADAPTERS):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        subprocess.run(["git", "init", "-q", str(self.path)], check=True)
        for adapter in adapters:
            (self.path / adapter).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / adapter, self.path / adapter)
        for rel, body in files.items():
            (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.path / rel).write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.path), "add", "-A"], check=True)

    def run(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check.main([], root=self.path)
        return code, out.getvalue()

    def close(self):
        self.dir.cleanup()


def run_root(files: dict[str, str], **kw) -> tuple[int, str]:
    root = Root(files, **kw)
    try:
        return root.run()
    finally:
        root.close()


def hook(name: str) -> str:
    return f"tools/hooks/{name}.py"


EXCEPTIONS = "tools/rules/gh_recognizer_exceptions.toml"


def entry(path: str, symbol: str, reason: str = "a reason", contract: str = "a contract") -> str:
    return (f'[[exception]]\npath = "{path}"\nsymbol = "{symbol}"\n'
            f'reason = "{reason}"\ncontract = "{contract}"\n')


class TC1EachRuleFails(unittest.TestCase):
    """AC2: each plant fails the check and names its path, line and rule."""

    PLANTS = {
        "a": ("G1", "import re\ndef f(t):\n    return t[1:3] == [\"issue\", \"close\"]\n", 3),
        "b": ("G3", "import gh_argv_adapters as A\ndef _parse_issue_close(t):\n    return A.close_target(t)\n", 2),
        "c": ("G1b", "def f(t):\n    return t[2] == \"merge\"\n", 2),
        "d": ("G2", "def f(t):\n    return _has_field(t, \"state=closed\")\n", 2),
        "e": ("G1", "import re\nPAT = re.compile(r\"gh pr merge \\d+\")\n", 2),
    }

    def test_each_plant_fails_naming_path_line_and_rule(self):
        for key, (rule, body, line) in self.PLANTS.items():
            with self.subTest(plant=key):
                code, out = run_root({hook("zz_plant"): body})
                self.assertEqual(code, 1, out)
                self.assertIn(f"VIOLATION tools/hooks/zz_plant.py:{line}:", out)
                self.assertRegex(out, rf"zz_plant\.py:{line}: \S+ \[{rule}\]")


#: A home module holding one recognizer, as block_missing_preclose_inspection does.
HOME = 'def _canonical_cli_close(t):\n    return t[1:3] == ["issue", "close"]\n'


class TC2ReexportsFail(unittest.TestCase):
    """G4: a re-export of a flagged symbol fails, to a fixpoint."""

    def assert_violation(self, files, where, rule="G4"):
        code, out = run_root(files)
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"VIOLATION tools/hooks/{where}: \S+ \[{rule}\]")

    def test_from_import_of_an_excepted_symbol_still_fails(self):
        self.assert_violation({
            hook("block_missing_preclose_inspection"): HOME,
            hook("aa_shape"): "from block_missing_preclose_inspection import _canonical_cli_close as shape\n",
            EXCEPTIONS: entry("tools/hooks/block_missing_preclose_inspection.py", "_canonical_cli_close"),
        }, "aa_shape.py:1")

    def test_attribute_through_a_module_alias_fails(self):
        self.assert_violation({
            hook("batch_auth"): 'def _pinned_head(t):\n    return t[2] == "merge"\n',
            hook("aa_use"): "import batch_auth as b\nx = b._pinned_head\n",
        }, "aa_use.py:2")

    def test_a_two_hop_chain_fails(self):
        # Sorted so that one pass over the modules cannot reach aa_use.
        self.assert_violation({
            hook("zz_rec"): HOME,
            hook("mm_wrap"): "import zz_rec\ndef wrap(t):\n    return zz_rec._canonical_cli_close(t)\n",
            hook("aa_use"): "from mm_wrap import wrap\n",
        }, "aa_use.py:1")

    def test_a_star_import_fails_whatever_it_imports(self):
        self.assert_violation({hook("aa_star"): "from batch_auth import *\n"}, "aa_star.py:1", r"G4\*")


class TC3NeverGreenOnNothing(unittest.TestCase):
    """AC3: an empty scan, a moved scan root and an unparseable file are never clean."""

    def test_an_empty_root_exits_1(self):
        code, out = run_root({})
        self.assertEqual(code, 1, out)
        self.assertNotIn("clean", out)

    def test_a_root_without_the_adapter_exits_1(self):
        code, out = run_root({hook("ok"): "X = 1\n"}, adapters=(check.ADAPTERS[0],))
        self.assertEqual(code, 1, out)
        self.assertIn("scan root moved", out)

    def test_a_syntax_error_exits_2_and_is_not_clean(self):
        code, out = run_root({hook("ok"): "X = 1\n", hook("broken"): "def f(:\n"})
        self.assertEqual(code, 2, out)
        self.assertIn("UNMEASURED tools/hooks/broken.py", out)
        self.assertNotIn("clean", out)


#: Seven recognizers in one home module, so seven entries can all be valid.
SEVEN = "".join(f'def r{i}(t):\n    return t[{i}] == "close"\n' for i in range(1, 8))
HOME7 = "tools/hooks/home7.py"


class TC4ExceptionsAreRefused(unittest.TestCase):
    """AC4: an incomplete, empty, duplicate, stale or over-ceiling list is refused."""

    def run_entries(self, *entries: str) -> tuple[int, str]:
        return run_root({HOME7: SEVEN, EXCEPTIONS: "\n".join(entries)})

    def assert_refused(self, why: str, *entries: str):
        code, out = self.run_entries(*entries)
        self.assertEqual(code, 1, out)
        self.assertIn(why, out)

    def test_a_valid_list_excepts_its_symbols(self):
        code, out = self.run_entries(*(entry(HOME7, f"r{i}") for i in range(1, 7)))
        self.assertEqual(code, 1, out)  # r7 is not excepted
        self.assertIn("home7.py:14: r7 [G1b]", out)
        self.assertNotIn("REFUSED", out)
        self.assertNotRegex(out, r": r[1-6] \[")

    def test_a_missing_field_is_refused(self):
        for field in check.FIELDS:
            with self.subTest(field=field):
                body = "".join(line + "\n" for line in entry(HOME7, "r1").splitlines()
                               if not line.startswith(f"{field} ="))
                self.assert_refused(f"missing field {field!r}", body)

    def test_an_empty_field_is_refused(self):
        for field in check.FIELDS:
            with self.subTest(field=field):
                body = entry(HOME7, "r1").replace(f'{field} = "', f'{field} = "" #"')
                self.assert_refused(f"empty field {field!r}", body)

    def test_a_duplicate_is_refused(self):
        self.assert_refused("duplicates an earlier entry", entry(HOME7, "r1"), entry(HOME7, "r1"))

    def test_a_stale_entry_is_refused(self):
        self.assert_refused("stale, matches no hit", entry(HOME7, "no_such_symbol"))

    def test_a_path_outside_the_scan_set_is_refused(self):
        self.assert_refused("outside the scan set", entry(check.ADAPTERS[1], "classify"))

    def test_a_seventh_entry_over_the_ceiling_is_refused(self):
        self.assert_refused(f"7 entries exceed CEILING = {check.CEILING}",
                            *(entry(HOME7, f"r{i}") for i in range(1, 8)))


class TC5RealTree(unittest.TestCase):
    """AC1: the real tree is clean with exactly the pinned exceptions."""

    def test_the_real_tree_is_clean_with_six_entries(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check.main([], root=REPO)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("6 excepted symbols, 0 violations", out.getvalue())

    def test_the_ceiling_equals_the_entry_count(self):
        entries = check.load_exceptions(REPO)
        self.assertEqual(len(entries), 6)
        self.assertEqual(check.CEILING, len(entries))

    def test_ci_check_runs_the_check(self):
        import tomllib
        ci = tomllib.loads((REPO / "mise.toml").read_text(encoding="utf-8"))["tasks"]["ci-check"]["run"]
        self.assertIn("\npython3 tools/rules/check_gh_recognizers.py\n", ci)


class TC6NoFalsePositives(unittest.TestCase):
    """Prose, docstrings and action labels are never hits."""

    def test_prose_docstrings_and_labels_are_clean(self):
        body = (
            'def f(t):\n'
            '    """tokens[1:3] == ["issue","close"] and t[2] == "merge" and state=closed"""\n'
            '    pr, repo = 5, "o/r"\n'
            '    return f"gh pr merge {pr} --repo {repo} state=closed"\n'
            'def g(record):\n'
            '    return record.get("gh pr merge")\n'
        )
        code, out = run_root({hook("prose"): body})
        self.assertEqual(code, 0, out)
        self.assertIn("clean: 1 files scanned", out)


HOOKS = REPO / "tools" / "hooks"
sys.path.insert(0, str(HOOKS))

import batch_auth  # noqa: E402
import block_data_migration_close  # noqa: E402
import block_missing_preclose_inspection  # noqa: E402
import test_gh_argv_adapters as table  # noqa: E402

#: BATCH is read through decide() with no grant (classify_* return None on an
#: undecided call by design; only decide() asks), as the differential does.
NO_BATCH_STATE = HOOKS / "testdata" / "no-such-batch-state.json"
_DECIDE = {None: table.NOT_A_CLOSE, "ask": table.ASK, "allow": table.PASS}
_MAIN_KEPT = "main's recognition kept (block_data_migration_close.py:154-161)"
_PREFIX = "harmonic-forge#1577: gh behind an unmodeled prefix is not recognized"

#: Shrink-only: (consumer, row) -> (consumer's verdict, VERDICT's verdict, why).
RESIDUAL = {
    ("migration", "unprovable_env_unset"): (table.NOT_A_CLOSE, table.REFUSE, _MAIN_KEPT),
    ("migration", "unprovable_sudo"): (table.NOT_A_CLOSE, table.REFUSE, _MAIN_KEPT),
    ("migration", "unprovable_xargs"): (table.NOT_A_CLOSE, table.REFUSE, _MAIN_KEPT),
    ("preclose", "unprovable_sudo"): (table.NOT_A_CLOSE, table.REFUSE, _PREFIX),
    ("preclose", "unprovable_xargs"): (table.NOT_A_CLOSE, table.REFUSE, _PREFIX),
    ("batch", "unprovable_sudo"): (table.NOT_A_CLOSE, table.ASK, _PREFIX),
    ("batch", "unprovable_xargs"): (table.NOT_A_CLOSE, table.ASK, _PREFIX),
}


def consumer_verdict(consumer: str, command: str) -> str:
    """The verdict of the consumer's real entry point for one command."""
    if consumer == "batch":
        result = batch_auth.decide(command, state_path=NO_BATCH_STATE)
        return _DECIDE[result[0] if result else None]
    if consumer == "migration":
        return table.REFUSE if block_data_migration_close.find_close_targets(command) else table.NOT_A_CLOSE
    found = block_missing_preclose_inspection.find_gated_targets_exempt(command) or []
    if not found:
        return table.NOT_A_CLOSE
    return table.PASS if all(target[3] for target in found) else table.REFUSE


class TC7ConsumersMatchTheVerdictTable(unittest.TestCase):
    """Issue TC4: each shared row, through each consumer's real entry point, gives
    that consumer's VERDICT column; every exception is a pinned RESIDUAL row."""

    def test_every_row_matches_or_is_a_pinned_residual(self):
        found = {}
        for name, tokens in table.ROWS.items():
            command = shlex.join(tokens)
            for consumer in table.CONSUMERS:
                got = consumer_verdict(consumer, command)
                want = table.verdict(consumer, table.A.classify(tokens, {}))
                if got != want:
                    found[(consumer, name)] = (got, want)
        pinned = {key: value[:2] for key, value in RESIDUAL.items()}
        self.assertEqual({k: v for k, v in found.items() if pinned.get(k) != v}, {},
                         "a consumer differs from VERDICT and no RESIDUAL row pins it")
        self.assertEqual(sorted(set(pinned) - set(found)), [],
                         "a RESIDUAL row no longer differs: delete it (shrink-only)")

    def test_every_residual_names_its_reason(self):
        for key, (_, _, why) in RESIDUAL.items():
            with self.subTest(residual=key):
                self.assertRegex(why, r"harmonic-forge#\d+|\.py:\d+")


if __name__ == "__main__":
    unittest.main()
