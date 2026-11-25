#!/usr/bin/env python3
"""Regenerate `migration_main_178ba5f.json`: what origin/main's data-migration
close hook (harmonic-forge#1498's base, 178ba5f) reported for every command the
differential replays.

harmonic-forge#1498 reforged pass 1 survivor 3: the differential's migration
baseline must be main's recognizer, never the code under test, or it can never
report a loosening against main. Main's hook is read from `git archive 178ba5f`
into a temporary directory and run there in an isolated interpreter with a
scrubbed environment, so nothing from this checkout or the caller's shell leaks
into the baseline.

Run from the repo root after adding a command to the migration hook's tests:
    python3 tools/hooks/testdata/migration_main_table.py
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

BASE = "178ba5f76eca2c3f900617af15f503873ef257c0"
HERE = Path(__file__).resolve().parent
HOOKS = HERE.parent
OUT = HERE / "migration_main_178ba5f.json"

_EVAL = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import block_data_migration_close as old
commands = json.load(sys.stdin)
out = {}
for command in commands:
    found = old.find_close_targets(command)
    out[command] = None if found is None else sorted([r, str(n)] for r, n in found)
json.dump(out, sys.stdout)
"""


def commands() -> list[str]:
    sys.path.insert(0, str(HOOKS))
    import block_data_migration_close  # noqa: PLC0415
    import test_gh_argv_adapters_differential as differential  # noqa: PLC0415
    recorded = differential.recorded(
        "test_block_data_migration_close", [(block_data_migration_close, "find_close_targets")])
    found = [entry for entry in recorded if isinstance(entry, str)]
    found += [shlex.join(tokens) for tokens in [*differential.ROWS.values(), differential.PLANTED]]
    return sorted(set(found))


def main_outputs(cmds: list[str]) -> dict:
    archive = subprocess.run(["git", "-C", str(HOOKS.parent.parent), "archive", BASE, "tools"],
                             capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        done = subprocess.run([sys.executable, "-I", "-c", _EVAL, str(Path(tmp) / "tools" / "hooks")],
                              input=json.dumps(cmds), capture_output=True, text=True, check=True,
                              cwd=tmp, env={"PATH": "/usr/bin:/bin", "HOME": tmp})
    return json.loads(done.stdout)


if __name__ == "__main__":
    table = main_outputs(commands())
    OUT.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{OUT.name}: {len(table)} commands from {BASE[:7]}")
