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
again. The first run seeds the pairs from all history without flagging them;
every later run judges cases from the last marked run (`last_run`) or the
`--since` window, whichever is older, and marks seen only the pairs in it.

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


#: A log line longer than this is skipped unparsed (harmonic-forge#1448 pass 2).
LINE_MAX_BYTES = 64 * 1024


def _lines(path: Path) -> list[bytes]:
    """Every line of `path` up to LINE_MAX_BYTES; a longer line is consumed in
    bounded chunks and dropped, never held whole."""
    out: list[bytes] = []
    with path.open("rb") as fh:
        while True:
            line = fh.readline(LINE_MAX_BYTES + 1)
            if not line:
                return out
            if len(line) > LINE_MAX_BYTES and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = fh.readline(LINE_MAX_BYTES + 1)
                continue
            out.append(line)


def rows() -> list[dict]:
    path = gate_receipt.store_dir() / "decisions.jsonl"
    try:
        lines = _lines(path)
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            data = json.loads(line.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 -- RecursionError, MemoryError: a bad line is skipped
            continue
        # A row is untrusted data: a wrong-typed join key (unhashable) or ts
        # is skipped rather than crashing the belt tick.
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)) \
                and not isinstance(data.get("ts"), bool) \
                and all(data.get(k) is None or isinstance(data.get(k), str)
                        for k in ("session", "tuid", "tool", "mode", "outcome")):
            out.append(data)
    return out


def seen_path() -> Path:
    return gate_receipt.store_dir() / "report-seen.json"


class SeenUnreadable(Exception):
    """report-seen.json exists but cannot be read or parsed."""


def read_seen() -> dict | None:
    """None only when the file is missing (a genuine first run); a present but
    unreadable or corrupt file raises SeenUnreadable, never reads as first run."""
    try:
        text = seen_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SeenUnreadable(str(exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SeenUnreadable(str(exc)) from exc
    if not isinstance(data, dict):
        raise SeenUnreadable("not a JSON object")
    for key in ("pairs", "cases"):
        if key in data and not isinstance(data[key], list):
            raise SeenUnreadable(f"{key} is not a list")
    last = data.get("last_run")
    return {"pairs": [p for p in data.get("pairs", []) if isinstance(p, str)],
            "cases": [c for c in data.get("cases", []) if isinstance(c, str)],
            "last_run": last if isinstance(last, (int, float)) and not isinstance(last, bool) else None}


def write_seen(seen: dict) -> None:
    path = seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".report-seen.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pairs": sorted(set(seen["pairs"])), "cases": sorted(set(seen["cases"])),
                   "last_run": seen.get("last_run")}, fh)
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
    """The new-case rows in `recent`, and the pairs to mark seen: all history
    on a first run (silent seeding), otherwise only the pairs in `recent`, the
    same window the cases were judged in, so a pair is never marked seen
    without having been reported."""
    every_pair = {_pair(r) for r in (all_rows if seen is None else recent) if r.get("mode")}
    known_pairs = set(every_pair) if seen is None else set(seen["pairs"])
    known_cases = set() if seen is None else set(seen["cases"])
    # Every gate row counts: one command can log several under one tuid.
    receipted = {(r.get("session"), r.get("tuid")) for r in all_rows
                 if r.get("tuid") and r.get("outcome") == "allowed-receipt"}
    cases, flagged_pairs = [], set()
    for r in recent:
        outcome = r.get("outcome")
        hit = (outcome == "ran-failed" and (r.get("session"), r.get("tuid")) in receipted) \
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
    started = time.time()
    cutoff = started - args.since
    recent = [r for r in all_rows if r["ts"] >= cutoff]
    mask = Masker()
    notices = []
    try:
        try:
            seen = read_seen()
        except SeenUnreadable:
            raise
        except Exception as exc:  # noqa: BLE001 -- a belt tick never crashes on seen-state
            raise SeenUnreadable(str(exc)) from exc
    except SeenUnreadable:
        # Never re-seed from a corrupt file: treat no pair as seen.
        seen = {"pairs": [], "cases": []}
        notices.append("gate_report: seen-state unreadable (report-seen.json); no pair treated as seen")
    for notice in notices:
        print(notice)
    # Cases are judged since the last marked run when that is older than the
    # --since window, so a gap between ticks never hides a case or a pair.
    last_run = seen.get("last_run") if seen else None
    since_last = recent if last_run is None or last_run >= cutoff \
        else [r for r in all_rows if r["ts"] >= last_run]
    cases, every_pair = new_cases(all_rows, since_last, seen)
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
        state["last_run"] = started
        try:
            write_seen(state)
        except OSError as exc:
            print(f"gate_report: seen-state not written ({exc.strerror or exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
