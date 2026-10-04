#!/usr/bin/env python3
"""Preclose panel-arm enrollment (harmonic-forge#890, reforged).

An issue's panel arm is **decided once and recorded**, never re-derived. Each
decision is one event appended to ``<repo>_<issue>.enrollment.jsonl`` beside
the receipts: ``{arm, assigned, source, reason, decided_at, decided_sha}``.
Every reader takes the newest event and nothing else. The file is separate from
the receipt, so no receipt rewrite (``--complete`` rebuilds the receipt from
``record()`` alone) can drop it.

- ``assigned`` is what the issue would get with no operator choice: the hash of
  ``repo#issue`` while the experiment is enrolling, else ``pre-experiment``.
- An event whose ``arm`` differs from its ``assigned`` is an override, and the
  issue leaves the comparison. Rejoining is another event whose arm equals its
  assignment, so "override cleared" and "never overridden" are both recorded.

Whether the experiment is enrolling is **declared**, never probed:
``experiment.json`` in the same store, written only by this script's
``--enroll``/``--stop`` (the operator's instruction, e.g. once
harmonic-forge#891's workflow is linked). The answer is the same from every
checkout. Absent or unreadable means not enrolling.

    preclose_enrollment.py --enroll --note "harmonic-forge#891 landed"
    preclose_enrollment.py --stop --note "<why>"
    preclose_enrollment.py --status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ARMS = ("manual", "workflow")
PRE_EXPERIMENT = "pre-experiment"
EXPERIMENT_FILE = "experiment.json"


def hashed_arm(repo: str, issue: int) -> str:
    """The arm an issue is assigned by: a pure function of `repo#issue`, so
    Lane 1 cannot steer a riskier diff to the arm it trusts."""
    digest = hashlib.sha256(f"{repo}#{issue}".encode()).hexdigest()
    return ARMS[int(digest[:8], 16) % 2]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enrolling(directory: Path) -> bool:
    """True only when `experiment.json` says so. Fails closed to False."""
    try:
        return json.loads((directory / EXPERIMENT_FILE).read_text(encoding="utf-8")).get("enrolling") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_experiment(directory: Path, on: bool, note: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / EXPERIMENT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enrolling": on, "since": _now(), "note": note}) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def events(path: Path) -> list[dict]:
    """Every recorded decision, oldest first. A line that will not decode is
    skipped and said aloud on stderr, never silently."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except ValueError:
            print(f"preclose-check: {path}:{number} is not a JSON enrollment event; skipped",
                  file=sys.stderr)
            continue
        if isinstance(event, dict) and event.get("arm"):
            out.append(event)
    return out


def current(path: Path) -> dict | None:
    found = events(path)
    return found[-1] if found else None


def append(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def overridden(event: dict) -> bool:
    return event.get("arm") != event.get("assigned")


def decide(path: Path, directory: Path, repo: str, issue: int, requested: str,
           re_enroll: str | None, reason: str, head_sha: str) -> tuple[dict, dict | None]:
    """(the issue's enrollment for this plan, the event to append or None).

    Nothing is written here: the plan appends the event only after every
    refusal has had its chance."""
    if reason and ("\n" in reason or len(reason) > 200):
        raise SystemExit("preclose-check: --arm-reason is one line of at most 200 characters.")
    on = enrolling(directory)
    existing = current(path)
    if existing and not re_enroll:
        if requested not in ("auto", existing["arm"]):
            raise SystemExit(
                f"preclose-check: {repo}#{issue} is enrolled as {existing['arm']}. Change it with "
                f"--re-enroll {requested} --arm-reason \"<why>\" (harmonic-forge#890).")
        return _require_runnable(existing, on), None
    assigned = (existing or {}).get("assigned") or (hashed_arm(repo, issue) if on else PRE_EXPERIMENT)
    arm = re_enroll or (assigned if requested == "auto" else requested)
    if assigned == PRE_EXPERIMENT and arm == "manual" and not re_enroll:
        arm = PRE_EXPERIMENT  # the manual panel IS the pre-experiment panel
    if (re_enroll or arm != assigned) and not reason:
        raise SystemExit(
            f"preclose-check: {repo}#{issue} is assigned the {assigned} arm. Choosing {arm} needs "
            "--arm-reason \"<why>\"; an issue on another arm is excluded from the comparison "
            "(harmonic-forge#890).")
    event = {"arm": arm, "assigned": assigned,
             "source": "operator" if (re_enroll or arm != assigned) else ("hash" if on else "not-enrolling"),
             "reason": reason or None, "decided_at": _now(), "decided_sha": head_sha}
    return _require_runnable(event, on), event


def _require_runnable(event: dict, on: bool) -> dict:
    if event["arm"] == "workflow" and not on:
        raise SystemExit(
            "preclose-check: the workflow arm runs only while the experiment is enrolling, and it "
            "is not (preclose_enrollment.py --status). Run the manual panel with --re-enroll "
            "manual --arm-reason \"<why>\".")
    return event


def note(event: dict) -> str:
    if overridden(event):
        return f"overridden: {event.get('reason')}"
    if event["arm"] == PRE_EXPERIMENT:
        return "pre-experiment: the experiment is not enrolling"
    return "assigned"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enroll", action="store_true", help="Start enrolling issues by hash.")
    action.add_argument("--stop", action="store_true", help="Stop enrolling new issues.")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--note", help="Why (required with --enroll/--stop).")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import preclose_check  # noqa: PLC0415
    directory = preclose_check.receipt_dir()
    if args.status:
        print(f"enrolling: {enrolling(directory)} ({directory / EXPERIMENT_FILE})")
        return
    if not (args.note or "").strip():
        parser.error("--enroll/--stop need --note \"<why>\"")
    print(f"recorded: {set_experiment(directory, args.enroll, args.note.strip())}")


if __name__ == "__main__":
    main()
