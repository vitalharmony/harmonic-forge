#!/usr/bin/env python3
"""BATCH against main's frozen 178ba5f table (harmonic-forge#1497 sticky-wicket
round 2, issuecomment-7184370067, patch step 3; test_preclose_main_frozen.py's
pattern, harmonic-forge#1499 rules 7-8).

Two claims the differential cannot make on its own:

- The table is main's output. `test_the_table_is_mains_output` reruns
  `batch_main_table`'s evaluator on `git archive 178ba5f` over every key and
  byte-compares the result with the file, so an edited row fails, and pins the
  key count and a digest of the sorted keys, so a deleted row fails. It never
  calls `commands()` or `_write()`, which rewrite the file it checks. Main's id is
  this file's own literal, never a value the branch can edit; the test skips only
  when git reports a shallow clone (CI's checkout, a stated residual), so the
  local `mise run check` is the run that counts.
- No row is looser than main, whatever its classification. For every key,
  `decide()` with no grant is at least as strict as main's: `ask` is the
  strictest verdict, and both `allow` and silent None are looser than it. A
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

import batch_auth  # noqa: E402
import batch_main_table  # noqa: E402
import migration_main_table  # noqa: E402

#: This file's own literal, never migration_main_table's: the guard must not read
#: a value the branch edits. Full id: a forged commit can make a short prefix ambiguous.
MAIN = "178ba5f76eca2c3f900617af15f503873ef257c0"
#: The regeneration compares only the table's own keys, so the key set itself is
#: pinned. Adding a key updates both on purpose.
KEY_COUNT = 179
KEYS_SHA256 = "3e257f13fd88ccfc8c6da228320ffc5ca4985d5c0e5996912c7c4407de8fe010"
#: No grant, so `decide()` never reads CI (the table's own evaluator does the same).
NO_STATE = HERE / "testdata" / "no-such-batch-state.json"


def at_least_as_strict(old: str | None, new: str | None) -> bool:
    """`ask` is never looser than anything; any other verdict is only as strict
    as itself. So `allow` and silent None are each looser than `ask`, and
    neither stands in for the other."""
    return new == "ask" or new == old


def _verdict(command: str) -> str | None:
    result = batch_auth.decide(command, state_path=NO_STATE)
    return None if result is None else result[0]


def _table() -> dict:
    return json.loads(batch_main_table.OUT.read_text(encoding="utf-8"))


class NeverLooserThanMain(unittest.TestCase):
    def test_every_frozen_key_is_at_least_as_strict_as_main(self) -> None:
        looser = {command: (old["decide"], _verdict(command)) for command, old in _table().items()
                  if not at_least_as_strict(old["decide"], _verdict(command))}
        self.assertEqual(looser, {}, "a loosening against main 178ba5f is a bug, never a classification")

    def test_the_order(self) -> None:
        """Kill check: allow and silent None are each looser than ask, and
        neither stands in for the other."""
        for old, new in (("ask", None), ("ask", "allow"), ("allow", None), (None, "allow")):
            with self.subTest(old=old, new=new):
                self.assertFalse(at_least_as_strict(old, new))
        for old, new in (("ask", "ask"), (None, "ask"), ("allow", "ask"), (None, None), ("allow", "allow")):
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
        result = migration_main_table.main_outputs(sorted(table), batch_main_table._EVAL)
        self.assertEqual(json.dumps(result, indent=1, sort_keys=True) + "\n",
                         batch_main_table.OUT.read_text(encoding="utf-8"),
                         f"batch_main_178ba5f.json is not main {MAIN}'s output for its own keys")


if __name__ == "__main__":
    unittest.main()
