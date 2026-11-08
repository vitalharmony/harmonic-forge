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

**The failure key** (harmonic-forge#1288): `--tool`, `--class` and `--probe`
name the tool, the missing-precondition class and the check that would have
caught the slip. All three are required under a precheck rule
(`PRECHECK_RULES`) or when a hook or gate caught it, a partial key is always
refused, and each flag may be given once: one slip per record. Every record
carries a session and a turn id; an unresolved one refuses. The command that
failed is not stored: it is recovered from the session transcript through
`(session, turn)`. `--probe` is one line of at most 200 characters and passes a
blunt, exemption-free secret scan (`secret_scan.probe_findings`), and a target
inside a harmonic-forge checkout is refused, because failure data never enters
the public repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secret_scan  # noqa: E402

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

#: The versioned class list, the interim closed tool list and the pseudo-tools.
CLASSES_FILE = Path(__file__).resolve().parent / "failure_classes.json"

#: Rules whose records must carry the full failure key. The registry has no
#: family field; R-0419 is its only precheck rule (harmonic-forge#1288).
PRECHECK_RULES = {"R-0419"}

#: `--caught-by` values whose records must carry the full failure key.
KEY_REQUIRED_CAUGHT_BY = {"hook", "gate"}

KEY_FLAGS = ("tool", "class", "probe")

#: A reasoning error no tool or probe would have caught: the one class
#: `--tool none` may carry (operator ruling 2026-11-08).
REASONING_CLASS = "other:reasoning"
NO_TOOL = "none"

_OTHER_CLASS = re.compile(r"^other:[a-z][a-z0-9-]*$")
_RULE_ID = re.compile(r"R-\d{4}")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")

#: Claude Code's per-session transcripts: `<root>/<project-slug>/<session>.jsonl`.
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

#: How much of a transcript's tail is read for the newest prompt id.
TAIL_BYTES = 2 * 1024 * 1024


def load_classes(path: Path | None = None) -> dict:
    return json.loads((path or CLASSES_FILE).read_text(encoding="utf-8"))


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


def resolve_turn(session: str | None, root: Path | None = None) -> str | None:
    """The newest `promptId` in the session's transcript, or None.

    Reads at most the last `TAIL_BYTES` of the newest `*/<session>.jsonl`
    under `root`, drops the partial first line after the seek, and returns the
    `promptId` of the last top-level, non-sidechain `type == "user"` record. A
    `promptId` that only appears inside a record's text never counts, because
    every line is parsed as JSON and only the top-level key is read."""
    if not session or not _SESSION_ID.match(session):
        return None
    base = root if root is not None else TRANSCRIPT_ROOT
    try:
        candidates = [p for p in base.glob(f"*/{session}.jsonl") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        with newest.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").split("\n")
    if start > 0:
        lines = lines[1:]
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        if record.get("isSidechain"):
            continue
        prompt = record.get("promptId")
        if isinstance(prompt, str) and prompt:
            return prompt
    return None


def inside_forge(path: Path) -> bool:
    """True when `path`, after resolving symlinks, lies inside a harmonic-forge
    checkout or worktree: an ancestor holding `.git` (a folder, or a
    worktree's file) and `tools/memory/capture_failure.py`."""
    resolved = Path(path).expanduser().resolve()
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists() and (ancestor / "tools" / "memory" / "capture_failure.py").exists():
            return True
    return False


def forge_refusal(path: Path, flag: str) -> str | None:
    if inside_forge(path):
        return (f"{flag} {path} is inside a harmonic-forge checkout. harmonic-forge "
                "is public and failure data never enters it; write under "
                f"{MEMORY_STORE} or {TESTPLAN_ROOT}.")
    return None


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
    parser.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                        help="Session id. Defaults to $CLAUDE_CODE_SESSION_ID; "
                             "refused when neither resolves.")
    parser.add_argument("--turn", default=None,
                        help="Turn id. Defaults to the newest promptId in the "
                             "session's transcript; refused when neither resolves.")
    parser.add_argument("--tool", action="append", default=None,
                        help="Failure key: the tool whose precondition was missed "
                             "(interim closed list, or a pseudo-tool). `none` only "
                             f"with --class {REASONING_CLASS}. Once per record.")
    parser.add_argument("--class", dest="klass", metavar="CLASS", action="append", default=None,
                        help="Failure key: the missing-precondition class from the "
                             "current class-list version, or other:<name>. Once "
                             "per record.")
    parser.add_argument("--probe", action="append", default=None,
                        help="Failure key: the check that would have caught it, "
                             "described in one line of at most "
                             f"{secret_scan.MAX_PROBE} characters. No credential "
                             "keyword, @, :// or token shape. Once per record.")
    parser.add_argument("--turns-lost", type=int, default=None,
                        help="Turns spent on the failure and its recovery.")
    parser.add_argument("--work-discarded", default=None,
                        help="What had to be thrown away, if anything.")
    parser.add_argument("--path", default=None,
                        help="Log path. Defaults per lane: Lane 3 writes to "
                             f"{TESTPLAN_ROOT}/{LOG_NAME}, every other lane to "
                             f"{MEMORY_STORE}/{LOG_NAME}.")
    return parser


def _single(args: argparse.Namespace) -> str | None:
    """Collapse each key flag to one value, refusing a repeat. argparse would
    otherwise keep the last one silently, and a bundled event is two slips.

    A blank or whitespace-only value counts as absent, so the partial-key and
    missing-key refusals apply to it rather than storing an empty key field."""
    for flag in KEY_FLAGS:
        attr = "klass" if flag == "class" else flag
        values = getattr(args, attr)
        if values is not None and len(values) > 1:
            return (f"--{flag} was given {len(values)} times. One slip per record: "
                    "capture each slip with its own call (the calls share the "
                    "session and turn ids).")
        value = values[0] if values else None
        if flag == "probe":
            # The form checks run on the raw value, before a blank probe is
            # normalized to absent: 201 spaces or a lone newline still refuses.
            kinds = secret_scan.form_findings(value)
            if kinds:
                return _probe_refusal(kinds)
        setattr(args, attr, value if value is not None and value.strip() else None)
    return None


def key_required(args: argparse.Namespace) -> bool:
    cited = set(_RULE_ID.findall(args.rule_ref or ""))
    return bool(cited & PRECHECK_RULES) or args.caught_by in KEY_REQUIRED_CAUGHT_BY


def validate_key(args: argparse.Namespace, classes: dict) -> str | None:
    given = {flag: getattr(args, "klass" if flag == "class" else flag) for flag in KEY_FLAGS}
    present = [flag for flag, value in given.items() if value is not None and value.strip()]
    if present and len(present) < len(KEY_FLAGS):
        missing = ", ".join(f"--{f}" for f in KEY_FLAGS if f not in present)
        return (f"a partial failure key is refused: missing {missing}. Give all "
                "three of --tool, --class and --probe, or none.")
    if not present:
        if key_required(args):
            return ("this record needs the failure key (--tool, --class, --probe): "
                    f"its rule is a precheck rule ({', '.join(sorted(PRECHECK_RULES))}) "
                    "or a hook or gate caught it.")
        return None
    current = classes["versions"][str(classes["current"])]
    klass = args.klass
    if klass not in current:
        if not _OTHER_CLASS.match(klass) or klass.split(":", 1)[1] in current:
            return (f"--class {klass!r} is not in class-list version "
                    f"{classes['current']} ({', '.join(current)}) and is not "
                    "other:<name> with a new [a-z][a-z0-9-]* name.")
    tool = args.tool
    if tool == NO_TOOL:
        if klass != REASONING_CLASS:
            return (f"--tool none is accepted only with --class {REASONING_CLASS}, "
                    "for a reasoning error no tool or probe would have caught.")
    elif tool not in classes["interim_tools"] and tool not in classes["pseudo_tools"]:
        return (f"--tool {tool!r} is not on the interim tool list or a pseudo-tool "
                f"({', '.join(classes['pseudo_tools'])}). See {CLASSES_FILE.name}.")
    return None


def validate_ids(args: argparse.Namespace) -> str | None:
    if not args.session:
        return ("no session id: $CLAUDE_CODE_SESSION_ID is unset and no "
                "--session was given. A Codex caller or a background subagent "
                "passes --session and --turn itself.")
    if not args.turn:
        args.turn = resolve_turn(args.session, TRANSCRIPT_ROOT)
    if not args.turn:
        return (f"no turn id: no promptId resolved from session {args.session}'s "
                "transcript and no --turn was given.")
    return None


def _probe_refusal(kinds: list[str]) -> str:
    return (f"--probe is refused ({', '.join(kinds)}). Nothing was written; "
            "the text is not repeated here. Describe the check in one line of "
            f"at most {secret_scan.MAX_PROBE} characters, with no credential "
            "keyword, @, :// or token, and capture again. The command itself "
            "is recovered from the transcript through (session, turn).")


def validate_secrets(args: argparse.Namespace) -> str | None:
    kinds = secret_scan.probe_findings(args.probe)
    return _probe_refusal(kinds) if kinds else None


def validate(args: argparse.Namespace) -> str | None:
    """Return a refusal message, or None when the record may be written.

    `argparse` already refuses a missing or out-of-enum value; this covers the
    cross-field rules it cannot express."""
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


def refusal_for(args: argparse.Namespace, path: Path) -> str | None:
    """Every refusal, in order, before anything is written."""
    for check in (lambda: _single(args), lambda: validate(args),
                  lambda: validate_key(args, load_classes()), lambda: validate_ids(args),
                  lambda: validate_secrets(args), lambda: forge_refusal(path, "--path")):
        refusal = check()
        if refusal:
            return refusal
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
        "turn": args.turn,
        "tool": args.tool,
        "class": args.klass,
        "probe": args.probe,
        "class_list_version": load_classes()["current"],
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
    # Expanded once, here: the forge guard and the writer use this one path.
    path = Path(args.path).expanduser() if args.path else default_log_path(args.lane)
    refusal = refusal_for(args, path)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
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
