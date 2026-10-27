#!/usr/bin/env python3
"""Explicit attribution sidecars for LLM usage (harmonic-forge#1188 AC7/AC8).

One append-only file per session, `<ATTRIB_DIR>/<session_id>.jsonl`, lines
`{ts, session_id, issue, lane, run_id, trigger, ...}`. Written only by code
that already knows the issue it acts on (the lane-shorthand hook, the posters,
`cross_family_call.sh`, `te_run.py`); `llm_usage.py` joins each model call to
the newest line at or before it in the same session. Nothing here, or in the
join, infers an issue from message or tool text.

`append()` never raises: a sidecar is telemetry riding on a hook, a post or a
call, and a failure to write it must never fail the thing it rides on.

CLI (for `cross_family_call.sh`):
    attrib.py append --session-id ID [--issue KEY] [--lane L] [--run-id R]
                     [--trigger T] [--ts ISO] [--thread-id ID] [--parent-session-id ID]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ATTRIB_DIR_ENV = "HARMONIC_FORGE_ATTRIB_DIR"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_KEY = re.compile(r"^([A-Za-z])(\d{1,6})$")
_QUALIFIED = re.compile(r"^([\w.-]+/[\w.-]+)#(\d{1,7})$")
_SHORTHAND_DOC = Path(__file__).resolve().parents[2] / "rules" / "lane-shorthand.md"


def attrib_dir() -> Path:
    return Path(os.environ.get(ATTRIB_DIR_ENV) or Path.home() / ".claude" / "state" / "attrib").expanduser()


def under_test() -> bool:
    """The emit.py#907 test marker: a test process never writes real sidecars."""
    import sys
    return "unittest" in sys.modules or os.environ.get("HARMONIC_FORGE_TESTING") == "1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_issue(issue: Any) -> Optional[str]:
    """`owner/repo#N`, or None. Accepts that form, a prefix key (`F123`,
    resolved through `rules/lane-shorthand.md`'s prefix table), or None."""
    if issue is None or issue == "":
        return None
    text = str(issue).strip()
    if _QUALIFIED.match(text):
        return text
    key = _KEY.match(text)
    if not key:
        return None
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
    import expand_lane_shorthand as els  # noqa: E402 -- lazy: only for a bare key
    repo, _account = els.parse_repo_prefixes(_SHORTHAND_DOC.read_text(encoding="utf-8")).get(
        key.group(1).upper(), ("", ""))
    repo = els.STRIP_MARKDOWN.sub("", repo).strip()
    return f"{repo}#{key.group(2)}" if "/" in repo else None


def append(session_id: Any, issue: Any, lane: Any, run_id: Any, trigger: Any,
           ts: Optional[str] = None, **extra: Any) -> bool:
    """Append one sidecar line. Returns whether it was written; never raises."""
    try:
        sid = str(session_id or "")
        if not _SAFE_ID.match(sid):
            return False
        if not os.environ.get(ATTRIB_DIR_ENV) and under_test():
            return False
        line = {"ts": ts or _now(), "session_id": sid, "issue": normalize_issue(issue),
                "lane": None if lane in (None, "") else str(lane),
                "run_id": None if run_id in (None, "") else str(run_id),
                "trigger": None if trigger in (None, "") else str(trigger)[:80]}
        line.update({k: str(v) for k, v in extra.items() if v not in (None, "")})
        path = attrib_dir() / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except BaseException:  # noqa: BLE001 -- AC8: never fail the action this rides on
        return False


def read(session_id: str) -> list[dict]:
    """A session's sidecar lines in file order; malformed lines skipped."""
    if not _SAFE_ID.match(str(session_id or "")):
        return []
    path = attrib_dir() / f"{session_id}.jsonl"
    lines: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and isinstance(item.get("ts"), str):
                    lines.append(item)
    except OSError:
        return []
    return lines


def latest(session_id: str, at: Optional[str] = None) -> Optional[dict]:
    """The newest line at or before `at` (ISO), or the newest of all."""
    best = None
    for item in read(session_id):
        if at is not None and _ts(item["ts"]) > _ts(at):
            continue
        if best is None or _ts(item["ts"]) >= _ts(best["ts"]):
            best = item
    return best


def _ts(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)  # unparseable never joins
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        sub = parser.add_subparsers(dest="cmd", required=True)
        a = sub.add_parser("append")
        for flag in ("--session-id", "--issue", "--lane", "--run-id", "--trigger", "--ts",
                     "--thread-id", "--parent-session-id"):
            a.add_argument(flag)
        args = parser.parse_args(argv)
        issue = args.issue
        if not issue and args.parent_session_id:
            # The caller named no issue: carry the calling session's own
            # explicit line forward (a sidecar, never text).
            parent = latest(args.parent_session_id, args.ts)
            issue = (parent or {}).get("issue")
        append(args.session_id, issue, args.lane, args.run_id, args.trigger, ts=args.ts,
               thread_id=args.thread_id, parent_session_id=args.parent_session_id)
    except BaseException:  # noqa: BLE001 -- AC8, the CLI path too
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
