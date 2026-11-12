#!/usr/bin/env python3
"""One ntfy POST, shared (harmonic-forge#1338).

`notify()` moved here from `orphan_reap.py`, behavior unchanged; `orphan_reap`
imports it and keeps its own topic file.

`push --key <K> --text <T>` sends one afk push to the operator's reminder
topic (`~/.local/share/ai-playlist-curator/ntfy-topic`), at most one per key
per hour, rate-limited through `~/.claude/state/afk/push.json`. The topic is
never printed. Exit 0 on a sent push or a rate-limited skip; non-zero on an
HTTP failure, naming the status (never the topic).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

REMINDER_TOPIC_RELPATH = Path(".local") / "share" / "ai-playlist-curator" / "ntfy-topic"
PUSH_STATE_RELPATH = Path(".claude") / "state" / "afk" / "push.json"
RATE_SECONDS = 60 * 60

#: (url, body, headers) -> HTTP status. Tests pass a fake; nothing else does.
Transport = Callable[[str, bytes, dict], int]


def _urllib_transport(url: str, body: bytes, headers: dict) -> int:
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def post(topic: str, text: str, title: str, transport: Transport | None = None) -> int:
    return (transport or _urllib_transport)(f"https://ntfy.sh/{topic}", text.encode(), {"Title": title})


def notify(summary: str, topic_file: str | None, title: str = "orphan-reap",
           transport: Transport | None = None, label: str = "") -> None:
    """One ntfy POST; the topic is read from its file and never printed. A
    failure is reported on stderr and never raised."""
    if not topic_file:
        print(f"{label}ntfy not sent: no topic file", file=sys.stderr)
        return
    try:
        topic = Path(topic_file).read_text().strip()
        if topic:
            post(topic, summary, title, transport)
    except Exception as exc:  # noqa: BLE001 - a missed alert never fails the run
        print(f"{label}ntfy not sent: {type(exc).__name__}", file=sys.stderr)


def _push_state() -> Path:
    return Path.home() / PUSH_STATE_RELPATH


def afk_push(key: str, text: str, transport: Transport | None = None,
             now: float | None = None, topic_file: Path | None = None) -> tuple[int, str]:
    """(exit code, message). One push per key per hour."""
    now = time.time() if now is None else now
    state = _push_state()
    state.parent.mkdir(parents=True, exist_ok=True)
    with (state.parent / "push.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            sent = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        except ValueError:
            sent = {}
        last = sent.get(key)
        if isinstance(last, (int, float)) and now - last < RATE_SECONDS:
            return 0, f"push skipped: {key} already pushed within the hour"
        topic_file = topic_file or (Path.home() / REMINDER_TOPIC_RELPATH)
        try:
            topic = topic_file.read_text().strip()
        except OSError as exc:
            return 2, f"push not sent: topic file unreadable ({type(exc).__name__})"
        if not topic:
            return 2, "push not sent: topic file empty"
        try:
            status = post(topic, text, key, transport)
        except Exception as exc:  # noqa: BLE001
            return 1, f"push not sent: {type(exc).__name__}"
        if not 200 <= status < 300:
            return 1, f"push not sent: HTTP {status}"
        sent[key] = now
        fd, tmp = tempfile.mkstemp(dir=state.parent, prefix="push.json.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(sent, sort_keys=True) + "\n")
        os.replace(tmp, state)
        return 0, f"push sent: {key}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    push = sub.add_parser("push", help="one afk push, at most one per key per hour")
    push.add_argument("--key", required=True)
    push.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    code, message = afk_push(args.key, args.text)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
