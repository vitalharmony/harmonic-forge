#!/usr/bin/env python3
"""The sanctioned reader of the precheck gate's `decisions.jsonl`
(harmonic-forge#1448 AC3/AC4).

Counts only: `<tool>\\t<mode>\\t<class>\\t<outcome>\\t<count>` over a window,
plus a totals line. Never a command, argv, fp, tuid, session or ts. Tool, mode
and class are printed only when the registry names them (an outside family's
name for a `guarded-elsewhere` row too); anything else prints
`<unregistered>`, so a corrupt or hostile row cannot inject text.

New cases (AC3): every `ran-failed` whose joined gate row (same session and
tuid) is `allowed-receipt`, every `refused-backstop`, every `error`, and every
tool|mode pair never seen before. `--new-cases` prints one `protocol-failure`
capture line per case (AC4); `--mark-seen` records the printed cases and the
pairs in `<store>/report-seen.json` so the next tick does not print them
again. The first run seeds the pairs from all history without flagging them.

The store is `gate_receipt.store_dir()`, outside forge (AC5); no network.

    python3 ~/harmonic-forge/tools/precheck/gate_report.py [--since 7d|24h] [--new-cases] [--mark-seen]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "tools" / "registry"))

import gate_receipt  # noqa: E402

MASK = "<unregistered>"
CAPTURE = "python3 ~/harmonic-forge/tools/memory/capture_failure.py"
WINDOW = re.compile(r"^(\d+)([dh])$")


def window_seconds(text: str) -> int:
    found = WINDOW.match(text)
    if not found:
        raise argparse.ArgumentTypeError(f"--since takes <N>d or <N>h, not {text!r}")
    return int(found.group(1)) * (86400 if found.group(2) == "d" else 3600)


def names() -> tuple[set[str], set[str], set[str]]:
    """Every tool, mode and class name the registry declares."""
    reg = gate_receipt.registry()
    tools, modes, classes = set(), set(), set()
    for tool in reg.get("tools", []):
        tools.add(tool["path"])
        for mode in tool.get("modes", []):
            modes.add(mode.get("name"))
            if mode.get("class"):
                classes.add(mode["class"])
    for family in reg.get("outside", []):
        if isinstance(family, dict):
            for key in ("family", "name"):
                if isinstance(family.get(key), str):
                    tools.add(family[key])
            if isinstance(family.get("class"), str):
                classes.add(family["class"])
    return tools, modes, classes


def rows() -> list[dict]:
    path = gate_receipt.store_dir() / "decisions.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
            out.append(data)
    return out


def seen_path() -> Path:
    return gate_receipt.store_dir() / "report-seen.json"


def read_seen() -> dict | None:
    try:
        data = json.loads(seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {"pairs": [p for p in data.get("pairs", []) if isinstance(p, str)],
            "cases": [c for c in data.get("cases", []) if isinstance(c, str)]}


def write_seen(seen: dict) -> None:
    path = seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".report-seen.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pairs": sorted(set(seen["pairs"])), "cases": sorted(set(seen["cases"]))}, fh)
    os.replace(tmp, path)


def case_key(row: dict) -> str:
    raw = f"{row.get('session')}|{row.get('tuid')}|{row.get('outcome')}|{row.get('ts')!r}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _pair(row: dict) -> str:
    return f"{row.get('tool')}|{row.get('mode')}"


class Masker:
    def __init__(self) -> None:
        self.tools, self.modes, self.classes = names()

    def __call__(self, row: dict) -> tuple[str, str, str]:
        tool, mode, klass = row.get("tool"), row.get("mode"), row.get("class")
        return (tool if isinstance(tool, str) and tool in self.tools else ("-" if tool is None else MASK),
                mode if isinstance(mode, str) and mode in self.modes else ("-" if mode is None else MASK),
                klass if isinstance(klass, str) and klass in self.classes else ("-" if klass is None else MASK))


OUTCOMES = {"precheck", "allowed-receipt", "allowed-override", "refused", "guarded-elsewhere",
            "error", "ran-ok", "ran-failed", "refused-backstop"}


def _outcome(row: dict) -> str:
    value = row.get("outcome")
    return value if value in OUTCOMES else MASK


def new_cases(all_rows: list[dict], recent: list[dict], seen: dict | None) -> tuple[list[dict], set[str]]:
    """The new-case rows in `recent`, and every pair in all history."""
    every_pair = {_pair(r) for r in all_rows if r.get("mode")}
    known_pairs = set(every_pair) if seen is None else set(seen["pairs"])
    known_cases = set() if seen is None else set(seen["cases"])
    gate_by_join = {(r.get("session"), r.get("tuid")): r.get("outcome") for r in all_rows
                    if r.get("tuid") and r.get("outcome") not in ("ran-ok", "ran-failed", "refused-backstop")}
    cases, flagged_pairs = [], set()
    for r in recent:
        outcome = r.get("outcome")
        hit = (outcome == "ran-failed"
               and gate_by_join.get((r.get("session"), r.get("tuid"))) == "allowed-receipt") \
            or outcome in ("refused-backstop", "error")
        pair = _pair(r)
        if not hit and r.get("mode") and pair not in known_pairs and pair not in flagged_pairs:
            hit = True
            flagged_pairs.add(pair)
        if hit and case_key(r) not in known_cases:
            cases.append(r)
    return cases, every_pair


def capture_line(row: dict, mask: Masker) -> str:
    tool, mode, klass = mask(row)
    outcome = _outcome(row)
    return (f"tool={tool} mode={mode} outcome={outcome} -> {CAPTURE} --tool {tool} --class {klass} "
            f'--probe "<fill in>" --caught-by hook --rule-existed rule --expected "<fill in>" '
            f'--happened "precheck gate {outcome} on {tool} {mode}"')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Counts-only report of the precheck gate's decisions.")
    parser.add_argument("--since", type=window_seconds, default=window_seconds("7d"))
    parser.add_argument("--new-cases", action="store_true", help="print only the new cases")
    parser.add_argument("--mark-seen", action="store_true", help="remember the printed cases")
    args = parser.parse_args(argv)
    all_rows = rows()
    cutoff = time.time() - args.since
    recent = [r for r in all_rows if r["ts"] >= cutoff]
    mask = Masker()
    seen = read_seen()
    cases, every_pair = new_cases(all_rows, recent, seen)
    if not args.new_cases:
        counts = Counter((*mask(r), _outcome(r)) for r in recent)
        for key in sorted(counts):
            print("\t".join([*key, str(counts[key])]))
        print(f"total\t{len(recent)}")
        if cases:
            print(f"new cases\t{len(cases)}")
    for r in cases:
        if args.new_cases:
            print(capture_line(r, mask))
    if args.mark_seen:
        state = seen or {"pairs": [], "cases": []}
        state["pairs"] = sorted(set(state["pairs"]) | every_pair)
        state["cases"] = sorted(set(state["cases"]) | {case_key(r) for r in cases})
        try:
            write_seen(state)
        except OSError as exc:
            print(f"gate_report: seen-state not written ({exc.strerror})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
