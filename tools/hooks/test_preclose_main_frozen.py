#!/usr/bin/env python3
"""The preclose hook against main's frozen 178ba5f table (harmonic-forge#1499 sticky-wicket rules 7-8).

Two claims the differential cannot make on its own:

- The table is main's output. `test_the_table_is_mains_output` reruns
  `preclose_main_table`'s evaluator on `git archive 178ba5f` over every key and
  byte-compares the result with the file, so an edited row fails, and pins the
  key count and a digest of the sorted keys, so a deleted row fails. It never
  calls `commands()` or `_write()`, which rewrite the file it checks. Main's id is
  this file's own literal, never a value the branch can edit; the test skips only
  when git reports a shallow clone (CI's checkout, a stated residual), so the
  local `mise run check` is the run that counts.
- No row is looser than main, whatever its classification. For every key the
  hook's targets are at least as strict as main's (`at_least_as_strict`): a
  loosening is a bug, never a class.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "testdata"))

import block_missing_preclose_inspection as hook  # noqa: E402
import migration_main_table  # noqa: E402
import preclose_main_table  # noqa: E402

#: This file's own literal, never migration_main_table's: the guard must not read
#: a value the branch edits (post-verdict survivor 1). Full id: a forged commit
#: can make a short prefix ambiguous.
MAIN = "178ba5f76eca2c3f900617af15f503873ef257c0"
#: Post-verdict survivor 2: the regeneration compares only the table's own keys,
#: so the key set itself is pinned. Adding a key updates both on purpose.
KEY_COUNT = 843
KEYS_SHA256 = "5e975889855a8bad10a5be86a81c5e24411e5319964182d6d98f3a99df6efaa5"
UNDECIDED = (None, "", "issue", False)


def at_least_as_strict(old: list | None, new: list | None) -> bool:
    """Pitch change 2: new holds an undecided target, or every old (repo,
    number, kind) is in new with an exempt bit no looser than old's. Undecided is
    any empty number, which `decide_targets` denies before any read (an unbound
    `gh pr merge` keeps its own text). None (untokenizable, fail-open) is no targets."""
    new_targets = [tuple(t) for t in new or []]
    if any(not t[1] for t in new_targets):
        return True
    return all(any(t[:3] == (r, str(n), k) and (not t[3] or e) for t in new_targets)
               for r, n, k, e in old or [])


def _table() -> dict:
    return json.loads(preclose_main_table.OUT.read_text(encoding="utf-8"))


class NeverLooserThanMain(unittest.TestCase):
    def test_every_frozen_key_is_at_least_as_strict_as_main(self) -> None:
        looser = {command: (old, hook.find_gated_targets_exempt(command)) for command, old in _table().items()
                  if not at_least_as_strict(old, hook.find_gated_targets_exempt(command))}
        self.assertEqual(looser, {}, "a loosening against main 178ba5f is a bug, never a classification")

    def test_the_order_is_over_whole_target_lists(self) -> None:
        """Kill check (pitch change 2): a max-strength comparison misses these."""
        two = [["o/r", "5", "issue", False], ["o/r", "6", "issue", False]]
        for old, new in ((two, [("o/r", "5", "issue", False)]),                      # drops the second
                         ([["a/b", "5", "issue", False]], [("c/d", "5", "issue", False)]),  # swaps the repo
                         ([["o/r", "5", "issue", False]], []),                       # drops the only one
                         ([["o/r", "5", "issue", False]], [("o/r", "5", "issue", True)]),   # newly exempt
                         ([["o/r", "7", "pr", False]], [("o/r", "7", "issue", False)])):    # another kind
            with self.subTest(old=old, new=new):
                self.assertFalse(at_least_as_strict(old, new))
        for old, new in ((two, [UNDECIDED]), (two, [(None, "", "pr", False)]), (None, []), ([], [("o/r", "5", "issue", False)]),
                         ([[None, "5", "issue", True]], [(None, "5", "issue", False)])):
            with self.subTest(old=old, new=new):
                self.assertTrue(at_least_as_strict(old, new))


class TableIsMainsOutput(unittest.TestCase):
    def test_the_archive_reads_main_by_its_full_id(self) -> None:
        # The archive reads migration_main_table.BASE; it must be this file's literal.
        self.assertRegex(MAIN, r"^[0-9a-f]{40}$")
        self.assertEqual(migration_main_table.BASE, MAIN)

    def test_no_frozen_key_is_deleted(self) -> None:
        keys = sorted(_table())
        self.assertEqual(len(keys), KEY_COUNT)
        self.assertEqual(hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest(), KEYS_SHA256)

    def test_the_table_is_mains_output(self) -> None:
        self.assertEqual(migration_main_table.BASE, MAIN)
        present = subprocess.run(["git", "-C", str(HERE), "cat-file", "-e", f"{MAIN}^{{commit}}"],
                                 capture_output=True, check=False)
        if present.returncode:
            shallow = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--is-shallow-repository"],
                                     capture_output=True, text=True, check=False)
            if shallow.returncode == 0 and shallow.stdout.strip() == "true":
                self.skipTest(f"{MAIN} is absent from a shallow clone; the local full-clone run is binding")
            self.fail(f"{MAIN} is not a commit in this full clone")
        table = _table()
        result = preclose_main_table.main_outputs(sorted(table), preclose_main_table._EVAL)
        self.assertEqual(json.dumps(result, indent=1, sort_keys=True) + "\n",
                         preclose_main_table.OUT.read_text(encoding="utf-8"),
                         f"preclose_main_178ba5f.json is not main {MAIN}'s output for its own keys")


if __name__ == "__main__":
    unittest.main()
