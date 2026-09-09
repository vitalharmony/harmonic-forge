#!/usr/bin/env python3
"""Report platform-link drift from a repo's own verification gate (harmonic-forge#543).

**Why not `tools/lane/_lane_args.sh`.** #543 rules that out with four reasons,
and they are all still true: it is a pure argument parser whose caller contract
declares `in $@` / `out lane_agent_requested, lane_ack_stale,
lane_passthrough`; all three launchers `set -euo pipefail` before sourcing it,
so a non-zero check there aborts the launcher with no message; it is sourced
before `lane3`'s own staleness and `backend/.env` refusals, so a drift message
would print ahead of both; and it would break the captured launch-tuple
baseline. The issue's own preferred surface is each repo's `mise run check`,
following `check_rule_drift.py` — no launcher latency, no AC5 risk. This is
that surface.

**Lane 3's zero-mutation guarantee is preserved by construction.** This calls
`sync_rules.py`'s `verify_project`, which never links, never repairs, and never
creates a directory — `link_project` calls `mkdir(parents=True)`, the verify
path deliberately does not (asserted by `test_verify_creates_nothing`). A gate
that repaired its own preconditions could not report on them.

## Why drift fails the gate and "undeclared" does not

#541 gave `--verify` three-valued exits precisely so a caller can tell a
finding from a state it should not act on:

  * **1, drift** — a link this repo DECLARED is missing, or points somewhere
    that is not the platform. That is unambiguously wrong wherever it is seen,
    so it fails the gate.
  * **2, undeclared** — no manifest in this tree. Usually an older branch,
    forked before the manifest landed, and the fix is a checkout away. Failing
    the gate on it would block work on every such branch for a condition the
    branch cannot fix, which is how a loud check becomes a check people route
    around. Reported loudly, non-fatally, naming `mise run hooks-install`.

Neither case is silent, which is the whole point of harmonic-forge#540: the
defect was never that the links were wrong, it was that nothing said so.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_sync_rules():
    spec = importlib.util.spec_from_file_location(
        "sync_rules", _ROOT / "sync_rules.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_ROOT / 'sync_rules.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sync_rules", module)
    spec.loader.exec_module(module)
    return module


def report(project_root: Path, sync_rules=None) -> int:
    """Gate exit code: 0 verified or undeclared, 1 drift, 2 could not run."""
    sync_rules = sync_rules or _load_sync_rules()

    if not project_root.is_dir():
        print(f"[platform-links] not a directory: {project_root}", file=sys.stderr)
        return 2

    code = sync_rules.verify_project(project_root)

    if code == sync_rules.EXIT_OK:
        return 0

    if code == sync_rules.EXIT_CANNOT_RUN:
        print(
            "[platform-links] This checkout does not declare which platform "
            "skills it consumes, so nothing could be compared. Usually a branch "
            "forked before the manifest landed. Run `mise run hooks-install` "
            "(once per clone) or switch to a branch carrying "
            ".claude/platform-skills.toml. Not failing the gate — a branch "
            "cannot fix a file it does not have.",
            file=sys.stderr,
        )
        return 0

    print(
        "[platform-links] DRIFT — a platform link this repo declares is missing "
        "or points outside the platform. The paths are named above. This session "
        "is running without content it declared it consumes; run "
        "`mise run hooks-install` to repair.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report platform-link drift for a checkout (harmonic-forge#543)")
    parser.add_argument("--project", default=".", help="Checkout to inspect")
    args = parser.parse_args(argv)
    return report(Path(args.project).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
