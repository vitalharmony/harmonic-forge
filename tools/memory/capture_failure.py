#!/usr/bin/env python3
"""Capture one protocol failure as a structured, append-only record
(harmonic-forge#520).

Two fields carry the whole analytical value, and neither has ever been
recorded:

**`--caught-by`** — whether a mechanism caught the failure or a human did.
Every significant failure in the three lane belt dumps was caught by the
operator asking a question, not one by machinery. Nobody currently knows what
fraction the machinery catches, and the honest guess is close to zero. That is
a measurement, and it cannot be taken retroactively.

**`--rule-existed`** — whether a rule was already in force. This splits every
failure into two classes with opposite remedies: no rule is an *authoring*
problem, and a rule that was ignored is an *enforcement* problem. Writing
another rule in the second case is useless and actively harmful, because it
grows a corpus every session must load. `feedback_git_er_done_bias` is the
proof case: 7 instances, already promoted to R-0344, still recurring. The
promotion rule (2 instances or 14 days, harmonic-forge#493) treats promotion as
the end state; R-0344 shows it is not, and recurrence-after-promotion is the
signal that says words will not fix it.

Both are REQUIRED and both refuse rather than defaulting (AC3). A default would
be a guess recorded as data, and the generous-self-report is exactly the bias
this record exists to survive: `self` and `self_late` are separate values for
that reason, and the bright line between them is whether an artifact left the
session.

No network, no GitHub call, anywhere in this path (AC5). Telemetry that costs
quota or reaches the network reproduces the class of defect it exists to catch,
and a capture that can fail on a network error is a capture that gets skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ISO = "%Y-%m-%dT%H:%M:%SZ"

#: How the failure was caught. Ordered mechanism-first, because the point of
#: the field is to measure what fraction the machinery catches.
CAUGHT_BY = {
    "hook": "a PreToolUse/PostToolUse hook denied or flagged it",
    "gate": "a Lane 3 gate, a checker, or a test suite caught it",
    "another_lane": "a different lane read the work and found it",
    "operator": "the human noticed and said so — a DETECTION GAP",
    "self": "the acting session caught it before any artifact left the session",
    "self_late": "the acting session caught it AFTER an artifact left "
                 "(a posted comment, a commit, a pushed branch)",
}

#: Whether a rule was already in force, and what kind.
#: `rule`, `scoped_out` and `unpromoted` take a value; `none` and `prose` are
#: distinguished because "nothing written anywhere" and "written down but not
#: as a rule" have different remedies.
RULE_EXISTED = {
    "none": "no rule, no memory, nothing written — an AUTHORING problem",
    "rule": "a promoted R- rule was in force — an ENFORCEMENT problem",
    "scoped_out": "an R- rule exists but its scope excluded this case",
    "unpromoted": "a memory file exists but was never promoted to a rule",
    "prose": "written somewhere as prose (a doc, a handoff) but not as a rule",
}

#: Values of `--rule-existed` that require `--rule-ref`. `none` must not carry
#: one, so a typo cannot quietly become an unlabelled reference.
RULE_REF_REQUIRED = {"rule", "scoped_out", "unpromoted", "prose"}

#: Lane 3 may write only here (harmonic-forge#150 / the write-tier rules). Its
#: default lands inside, so the capture works from Lane 3 without a flag and
#: without tripping the guard that would otherwise deny it.
TESTPLAN_ROOT = Path.home() / "Harmonic_Projects" / "testplan"

#: Every other lane writes to the shared memory store, which is common across
#: all repos and lanes since 2026-09-06.
MEMORY_STORE = Path.home() / "Harmonic_Projects" / "operator-memory"

LOG_NAME = "protocol-failures.jsonl"


def default_log_path(lane: str | None) -> Path:
    """Per-lane default (AC4).

    A parameter with a default, not an assumption: Lane 3's restriction is
    honored by the default itself rather than by asking the invoker to
    remember it, because the invoker remembering is the failure mode this
    whole issue is about.
    """
    if lane == "3":
        return TESTPLAN_ROOT / LOG_NAME
    return MEMORY_STORE / LOG_NAME


def _enum_help(choices: dict[str, str]) -> str:
    return "  " + "\n  ".join(f"{key:<13} {text}" for key, text in choices.items())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture_failure.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("caught-by values:\n" + _enum_help(CAUGHT_BY)
                + "\n\nrule-existed values:\n" + _enum_help(RULE_EXISTED)),
    )
    parser.add_argument("--caught-by", required=True, choices=sorted(CAUGHT_BY),
                        help="REQUIRED. How the failure was caught.")
    parser.add_argument("--rule-existed", required=True, choices=sorted(RULE_EXISTED),
                        help="REQUIRED. Whether a rule was already in force.")
    parser.add_argument("--rule-ref", default=None,
                        help="The R- id, memory-file name, or doc path the "
                             "--rule-existed value refers to. Required for "
                             "every value except `none`; refused with `none`.")
    parser.add_argument("--expected", required=True,
                        help="What the protocol required.")
    parser.add_argument("--happened", required=True,
                        help="What actually happened.")
    parser.add_argument("--account", default=None,
                        help="Free-text account. The narrative, not the fields.")
    parser.add_argument("--lane", default=os.environ.get("LANE"),
                        help="Defaults to $LANE.")
    parser.add_argument("--repo", default=None, help="e.g. vitalharmony/hrse")
    parser.add_argument("--issue", default=None, help="Issue number, bare.")
    parser.add_argument("--session", default=None, help="Session id, if known.")
    parser.add_argument("--turns-lost", type=int, default=None,
                        help="Turns spent on the failure and its recovery.")
    parser.add_argument("--work-discarded", default=None,
                        help="What had to be thrown away, if anything.")
    parser.add_argument("--path", default=None,
                        help="Log path. Defaults per lane: Lane 3 writes to "
                             f"{TESTPLAN_ROOT}/{LOG_NAME}, every other lane to "
                             f"{MEMORY_STORE}/{LOG_NAME}.")
    return parser


def validate(args: argparse.Namespace) -> str | None:
    """Return a refusal message, or None when the record may be written.

    `argparse` already refuses a missing or out-of-enum value; this covers the
    cross-field rule it cannot express."""
    needs_ref = args.rule_existed in RULE_REF_REQUIRED
    if needs_ref and not args.rule_ref:
        return (f"--rule-existed {args.rule_existed!r} names something, so "
                f"--rule-ref is required (the R- id, memory-file name, or doc "
                f"path). Refusing rather than writing a record that asserts a "
                f"rule existed without saying which.")
    if not needs_ref and args.rule_ref:
        return ("--rule-existed none means nothing was written anywhere, so "
                "--rule-ref has nothing to refer to. Refusing rather than "
                "silently dropping it.")
    return None


def build_record(args: argparse.Namespace) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).strftime(_ISO),
        "caught_by": args.caught_by,
        "rule_existed": args.rule_existed,
        "rule_ref": args.rule_ref,
        "expected": args.expected,
        "happened": args.happened,
        "lane": args.lane,
        "repo": args.repo,
        "issue": args.issue,
        "session": args.session,
        "turns_lost": args.turns_lost,
        "work_discarded": args.work_discarded,
        "account": args.account,
    }
    return record


def append(record: dict, path: Path) -> Path:
    """Append one line. No rewrite path exists here, deliberately (AC2).

    Opening in `"a"` is the whole mechanism: there is no read-modify-write, so
    no code path in this file can overwrite an earlier record even by mistake.
    Write-time dedup is deliberately absent — deciding two records are the same
    event is an analysis-time judgment, and making it here would silently drop
    a real recurrence, which is the exact signal the log exists to preserve.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    refusal = validate(args)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    path = Path(args.path) if args.path else default_log_path(args.lane)
    record = build_record(args)
    append(record, path)
    print(f"captured: {path}")
    if args.rule_existed in ("rule", "scoped_out"):
        print("NOTE: a rule was already in force. This is an ENFORCEMENT "
              "finding, not an authoring one — the remedy is a hook, a "
              "refusal, or a mechanical gate. Writing another rule grows a "
              "corpus every session must load and will not fix this.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
