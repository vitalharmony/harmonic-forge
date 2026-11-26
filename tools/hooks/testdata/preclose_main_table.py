#!/usr/bin/env python3
"""Regenerate `preclose_main_178ba5f.json`: what origin/main's preclose hook
(`block_missing_preclose_inspection.find_gated_targets_exempt` at 178ba5f, the
last change to it before harmonic-forge#1499) reported for every command the
differential replays.

harmonic-forge#1499 design amendment, change 4: the preclose differential is
measured against main's frozen recognizer, never the code under test, so a
loosening against main shows. Each entry is main's full 4-tuple list
`[repo, number, kind, exempt]` in main's order, or None when main could not
tokenize the command. Main's hook runs from `git archive` exactly as
migration_main_table.py runs it.

Run from the repo root after adding a command to the preclose hook's tests:
    python3 tools/hooks/testdata/preclose_main_table.py
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from migration_main_table import main_outputs  # noqa: E402

OUT = HERE / "preclose_main_178ba5f.json"
MODULES = "test_block_missing_preclose_inspection,test_block_missing_preclose_inspection_adapter"

_EVAL = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import block_missing_preclose_inspection as old
out = {}
for command in json.load(sys.stdin):
    found = old.find_gated_targets_exempt(command)
    out[command] = None if found is None else [[r, str(n), k, bool(e)] for r, n, k, e in found]
json.dump(out, sys.stdout)
"""


def _write(table: dict) -> None:
    OUT.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def commands() -> list[str]:
    import block_missing_preclose_inspection as hook  # noqa: PLC0415
    import test_block_missing_preclose_inspection_adapter as tests  # noqa: PLC0415
    # The grid first: its test fails, and so stops the recording run, until the
    # table holds main's output for every grid command.
    current = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    _write({**current, **main_outputs(tests.grid(), _EVAL)})
    import test_gh_argv_adapters_differential as differential  # noqa: PLC0415
    recorded = differential.recorded(MODULES, [
        (hook, "find_gated_targets"), (hook, "find_gated_targets_exempt")])
    found = [entry for entry in recorded if isinstance(entry, str)] + tests.grid()
    found += [shlex.join(tokens) for tokens in [*differential.ROWS.values(), differential.PLANTED]]
    return sorted(set(found))


if __name__ == "__main__":
    # A row is only ever added (harmonic-forge#1499 pitch change 4): an existing
    # row stays though no test replays it, and the regeneration test proves all.
    kept = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    table = main_outputs(sorted(set(commands()) | set(kept)), _EVAL)
    _write(table)
    print(f"{OUT.name}: {len(table)} commands from 178ba5f")
