#!/usr/bin/env python3
"""Call one declared gate-adapter capability and print its finding (ADR-008 AC4).

ADR-008 names three adapter call sites. Two of them are instructions inside
`lane3-gate` rather than lines of code — "immediately after the verdict, before
the report is posted, run `residue_sweep` if declared". An instruction with no
runnable command behind it is the shape that produced harmonic-forge#540:
complete in the repo that wrote it, absent everywhere it is used. This is that
command.

    python3 tools/gate/run_adapter.py residue_sweep --repo /path/to/repo
    python3 tools/gate/run_adapter.py lease:check_owner --worktree /path/to/wt

Exit codes are the finding, so a gate script can branch on them without
parsing: 0 pass, 1 fail, 2 blocked. **BLOCKED is not success and not failure**
— a step that could not run must be visible as such in the gate report, never
folded into either (ADR-008 AC4, `rules/testing-gate.md` rule 8).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adapter  # noqa: E402

#: The entrypoint key used when the capability is named without one.
DEFAULT_ENTRYPOINT = {"residue_sweep": "entrypoint", "lease": "check_owner"}

EXIT = {adapter.PASS: 0, adapter.FAIL: 1, adapter.BLOCKED: 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capability",
                        help="`residue_sweep`, `lease:check_owner`, "
                             "`lease:acquire`, `lease:release`")
    parser.add_argument("--repo", type=Path, default=None,
                        help="repo whose manifest declares it (default: cwd)")
    parser.add_argument("--issue", default=None)
    parser.add_argument("--target-sha", default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--report-only", action="store_true",
                        help="read-only form; write entrypoints refuse under it")
    parser.add_argument("--json", action="store_true",
                        help="print the raw finding instead of the report lines")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    capability, _, entrypoint = args.capability.partition(":")
    entrypoint = entrypoint or DEFAULT_ENTRYPOINT.get(capability, "entrypoint")

    result = adapter.call(
        capability, entrypoint, cwd=args.repo,
        issue=args.issue, target_sha=args.target_sha,
        report_only=args.report_only, worktree=args.worktree,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{capability}.{entrypoint}: {result['status'].upper()}")
        for line in result["evidence"]:
            print(f"  {line}")
    return EXIT.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
