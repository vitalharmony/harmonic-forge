#!/usr/bin/env python3
"""Shadow-measure the self-referential claim class (harmonic-forge#902).

The class: a lane's final message claims tests pass AND names, in backticks or
double quotes, the command of a background task that is still running. The
message then reports a result that is not in yet, in its own words. The
hrse#2056 slice 3a incident: "all its tests pass. The verification gate
(`mise run check`) is running in the background."

This hook only records. It never emits a `decision`, `reason` or
`systemMessage`, never spawns a process, and never fails a stop. Every Stop in
a lane session bumps the heartbeat counters, so "zero fires" can be told apart
from "never ran". A match appends one line of six fields to the shadow log.
`report` prints the log for hand labelling; `--replay <glob>` runs the same
match over transcripts.

**Precision only, never recall.** `TESTS_PASS_RE` is an open-grammar
recognizer that missed 85 of 129 in-class finals in #898's measurement. It is
acceptable here only because this is a measurement instrument whose decidable
half rests on the task's own `status`. Promotion to an advisory is a separate,
measured decision on a labelled sample of n >= 15.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import glob
import json
import os
import re
import sys
from pathlib import Path

# Copied from harmonic-forge#898 (`23ea596:tools/hooks/block_unevidenced_claim_stop.py:35`).
# #898 was cut, so it never merged; the handoff's fallback is to copy and cite.
TESTS_PASS_RE = re.compile(r"\btests?\s+(?:now\s+|all\s+)?(?:pass(?:es|ed)?|are\s+(?:all\s+|now\s+)?passing)\b"
                           r"|\btest\s+suite\s+(?:now\s+)?pass(?:es|ed)?\b", re.I)
# Origin: `784e2b6:tools/hooks/block_unevidenced_claim_stop.py:76` (#898).
ENDED_STATUSES = ("completed", "complete", "done", "failed", "killed", "stopped", "cancelled", "canceled",
                  "succeeded", "error", "exited")
_SPAN_RE = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"")
MIN_SPAN_TOKENS = 2
MIN_SAMPLE = 15

STATE_DIR = Path.home() / ".claude" / "state" / "claim-stop"
SHADOW = "shadow.jsonl"
HEARTBEAT = "heartbeat.json"
COUNTERS = ("stops_seen", "background_tasks_present", "background_tasks_nonempty", "tests_pass_matched", "fired")


def _tokens(text: str) -> list[str]:
    return text.split()


def _contains(command: list[str], span: list[str]) -> bool:
    n = len(span)
    return any(command[i:i + n] == span for i in range(len(command) - n + 1))


def running_tasks(tasks: object) -> list[dict]:
    """Entries of the Stop payload's `background_tasks` whose status is not
    terminal. A dict of arrays is flattened; anything malformed is skipped."""
    if isinstance(tasks, dict):
        tasks = [t for value in tasks.values() if isinstance(value, list) for t in value]
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if isinstance(t, dict)
            and str(t.get("status") or "").strip().lower() not in ENDED_STATUSES]


def selfref_match(text: object, tasks: object) -> tuple[str, str, str] | None:
    """`(claim, command span, task status)` for the first backticked or quoted
    span of two or more tokens that equals, or is a contiguous token run of, a
    running task's command, in a message carrying a tests-pass claim."""
    if not isinstance(text, str):
        return None
    claim = TESTS_PASS_RE.search(text)
    if not claim:
        return None
    running = running_tasks(tasks)
    for found in _SPAN_RE.finditer(text):
        span = _tokens(found.group(1) or found.group(2) or "")
        if len(span) < MIN_SPAN_TOKENS:
            continue
        for task in running:
            command = task.get("command")
            if isinstance(command, str) and command.strip() and _contains(_tokens(command), span):
                return claim.group(0), " ".join(span), str(task.get("status") or "")
    return None


@contextlib.contextmanager
def _locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def heartbeat(payload: dict, matched_claim: bool, fired: bool, directory: Path | None = None) -> None:
    """Bump the counters on every lane Stop. Never raises."""
    directory = STATE_DIR if directory is None else directory
    tasks = payload.get("background_tasks")
    bumps = {"stops_seen": True, "background_tasks_present": tasks is not None,
             "background_tasks_nonempty": bool(tasks), "tests_pass_matched": matched_claim, "fired": fired}
    try:
        with _locked(directory):
            path = directory / HEARTBEAT
            try:
                counts = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(counts, dict):
                    counts = {}
            except (OSError, ValueError):
                counts = {}
            for key in COUNTERS:
                counts[key] = int(counts.get(key) or 0) + (1 if bumps[key] else 0)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(counts), encoding="utf-8")
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - a measurement never fails a stop
        pass


def record(lane: str, prompt_id: object, match: tuple[str, str, str], directory: Path | None = None) -> None:
    """Append one line of exactly six fields. Never raises."""
    directory = STATE_DIR if directory is None else directory
    claim, command, status = match
    line = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "lane": lane,
            "prompt_id": str(prompt_id or ""), "claim": claim, "command": command, "status": status}
    try:
        with _locked(directory), (directory / SHADOW).open("a", encoding="utf-8") as out:
            out.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001
        pass


def decide(payload: object, env: dict | None = None, directory: Path | None = None) -> None:
    """Record, never decide: always None."""
    env = os.environ if env is None else env
    lane = str(env.get("LANE") or "").strip()
    if not lane or not isinstance(payload, dict):
        return None
    text = payload.get("last_assistant_message")
    match = selfref_match(text, payload.get("background_tasks"))
    heartbeat(payload, isinstance(text, str) and bool(TESTS_PASS_RE.search(text)), match is not None, directory)
    if match:
        record(lane, payload.get("prompt_id"), match, directory)
    return None


# --- measurement ----------------------------------------------------------------

def _pointer(prompt_id: str, roots: list[Path]) -> str:
    """`transcript:record` of the newest record carrying this prompt id."""
    if not prompt_id:
        return "(no prompt id)"
    needle = f'"promptId":"{prompt_id}"'
    best: tuple[float, str] | None = None
    for root in roots:
        for path in root.glob("*/*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            hits = [n for n, line in enumerate(lines, 1) if needle in line]
            if hits and (best is None or path.stat().st_mtime > best[0]):
                best = (path.stat().st_mtime, f"{path}:{hits[-1]}")
    return best[1] if best else f"(prompt {prompt_id} not found in transcripts)"


def report(directory: Path | None = None, roots: list[Path] | None = None) -> int:
    """Print the shadow log for hand labelling. Reads two files by name;
    writes nothing."""
    directory = STATE_DIR if directory is None else directory
    roots = [Path.home() / ".claude" / "projects"] if roots is None else roots
    lines = []
    try:
        for raw in (directory / SHADOW).read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(raw))
            except ValueError:
                continue
    except OSError:
        pass
    try:
        counts = json.loads((directory / HEARTBEAT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        counts = {}
    print(f"claim-selfref shadow: {len(lines)} fired, "
          f"{len({str(l.get('prompt_id')) for l in lines})} distinct prompt(s)")
    print("heartbeat: " + ", ".join(f"{k}={int(counts.get(k) or 0)}" for k in COUNTERS))
    for line in lines:
        print(f"- {line.get('ts')} LANE={line.get('lane')} {line.get('claim')!r} / `{line.get('command')}` "
              f"({line.get('status')}) -> {_pointer(str(line.get('prompt_id') or ''), roots)}")
    print(f"sample: n={len(lines)} (source: live); promotion needs n >= {MIN_SAMPLE}, "
          "labelled by hand, combined with --replay")
    return 0


_BG_STARTED_RE = re.compile(r"running in background with ID: ([A-Za-z0-9_-]+)")
_GAP = r"(?:\s|\\n)*"
# Origin: `784e2b6:tools/hooks/block_unevidenced_claim_stop.py:415-451` (#898).
_BG_ENDED_RE = re.compile(rf"<task-id>([A-Za-z0-9_-]+)</task-id>{_GAP}(?:<tool-use-id>[^<]*</tool-use-id>{_GAP})?"
                          rf"(?:<output-file>[^<]*</output-file>{_GAP})?<status>(?:{'|'.join(ENDED_STATUSES)})")


def replay_file(path: Path) -> list[tuple[int, tuple[str, str, str]]]:
    """Fires over one transcript: each assistant text block is matched against
    the background runs still in flight at that record, reconstructed from
    launch acknowledgements and completion notifications."""
    running: dict[str, str] = {}
    pending_cmd: dict[str, str] = {}
    fires = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return fires
    for number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        if '"run_in_background": true' in line or '"run_in_background":true' in line:
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending_cmd[block.get("id") or ""] = str((block.get("input") or {}).get("command") or "")
        if "running in background with ID" in line:
            use_ids = [b.get("tool_use_id") for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            for task_id in _BG_STARTED_RE.findall(line):
                running[task_id] = next((pending_cmd[u] for u in use_ids if u in pending_cmd), "")
        if "<task-id>" in line:
            for task_id in _BG_ENDED_RE.findall(line):
                running.pop(task_id, None)
        if entry.get("type") == "assistant" and isinstance(content, list):
            tasks = [{"status": "running", "command": c} for c in running.values()]
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    match = selfref_match(block.get("text"), tasks)
                    if match:
                        fires.append((number, match))
    return fires


def replay(pattern: str) -> int:
    paths = sorted(Path(p) for p in glob.glob(os.path.expanduser(pattern)))
    total = 0
    for path in paths:
        for number, (claim, command, status) in replay_file(path):
            total += 1
            print(f"- {path}:{number} {claim!r} / `{command}` ({status})")
    print(f"replay: {total} fire(s) over {len(paths)} transcript(s); sample: n={total} (source: replay)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
        parser.add_argument("command", nargs="?", choices=["report"])
        parser.add_argument("--replay", metavar="GLOB")
        args = parser.parse_args(argv)
        return replay(args.replay) if args.replay else report()
    try:
        payload = json.load(sys.stdin)
        decide(payload)
    except Exception:  # noqa: BLE001 - a Stop hook never wedges a session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
