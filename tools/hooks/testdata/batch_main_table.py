#!/usr/bin/env python3
"""Regenerate `batch_main_178ba5f.json`: what origin/main's BATCH readers
(`batch_auth.classify_issue_close`, `classify_pr_merge` and `decide` at 178ba5f,
unchanged on main until harmonic-forge#1497) reported for every command the
differential replays.

harmonic-forge#1497 sticky-wicket PATCH (issuecomment-7182907859) step 6, the
F1498/F1499 pattern: the BATCH differential is measured against main's frozen
readers, never the code under test, so a loosening against main shows. Each
entry is `{"issue": [repo, number] | null, "pr": [repo, number] | null,
"decide": "ask" | "allow" | null}`: the two classifiers on the command's
`shlex.split` tokens, and `decide()` on the command with an empty BATCH state
(no grant, so no CI read). Main's module runs from `git archive` exactly as
migration_main_table.py runs it.

Run from the repo root after adding a command to test_batch_auth.py:
    python3 tools/hooks/testdata/batch_main_table.py
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

OUT = HERE / "batch_main_178ba5f.json"

_EVAL = r"""
import json, shlex, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import batch_auth as old
state = Path(sys.argv[1]) / "no-such-batch-state.json"
out = {}
for command in json.load(sys.stdin):
    tokens = shlex.split(command)
    close, merge = old.classify_issue_close(tokens), old.classify_pr_merge(tokens)
    verdict = old.decide(command, state_path=state)
    out[command] = {"issue": None if close is None else [close[0], close[1]],
                    "pr": None if merge is None else [merge[0], None if merge[1] is None else str(merge[1])],
                    "decide": None if verdict is None else verdict[0]}
json.dump(out, sys.stdout)
"""


def _write(table: dict) -> None:
    OUT.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def commands() -> list[str]:
    import batch_auth  # noqa: PLC0415
    import test_gh_argv_adapters_differential as differential  # noqa: PLC0415
    recorded = differential.recorded("test_batch_auth", [
        (batch_auth, "classify_issue_close"), (batch_auth, "classify_pr_merge")])
    found = [shlex.join(entry) for entry in recorded if isinstance(entry, list)]
    found += [shlex.join(tokens) for tokens in [*differential.ROWS.values(), differential.PLANTED]]
    return sorted(set(found))


if __name__ == "__main__":
    # A row is only ever added: an existing row stays though no test replays it.
    kept = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    table = main_outputs(sorted(set(commands()) | set(kept)), _EVAL)
    _write(table)
    print(f"{OUT.name}: {len(table)} commands from 178ba5f")
