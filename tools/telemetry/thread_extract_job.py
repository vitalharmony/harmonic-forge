#!/usr/bin/env python3
"""The scheduled thread extraction (harmonic-forge#907): `extract_threads.py`
over a window that starts at the last SUCCESSFUL run, never only at "now".

Why a watermark (preclose pass 1): a window computed from the wall clock loses
every event in a gap longer than the window -- the timer off for days, the
keyring locked, a revoked slot token -- because `--since` then filters the gap
out of the fetch, and `emit()`'s dedupe cannot recover what was never fetched.
So the window is `--since = min(watermark - OVERLAP, now - DEFAULT_WINDOW)`.
The watermark advances to this run's START only when the extractor exits 0
(it exits 1 when any repo errored), so a failed or partial run is retried in
full next time. With no watermark yet, the window is the default.

The watermark lives under `$XDG_STATE_HOME/harmonic-forge/` (default
`~/.local/state`), never in the event store.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
EXTRACTOR = HERE / "extract_threads.py"
DEFAULT_WINDOW = timedelta(days=3)
OVERLAP = timedelta(days=1)


def watermark_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "harmonic-forge" / "thread-extract.last"


def read_watermark(path: Path) -> Optional[datetime]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def window_start(now: datetime, watermark: Optional[datetime]) -> datetime:
    start = now - DEFAULT_WINDOW
    if watermark is not None:
        start = min(start, watermark - OVERLAP)
    return start


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(now: Optional[datetime] = None,
         runner: Callable[[list[str]], int] = lambda argv: subprocess.run(argv).returncode) -> int:
    started = now or datetime.now(timezone.utc)
    path = watermark_path()
    mark = read_watermark(path)
    since = window_start(started, mark)
    print(f"[thread-extract] window since {_iso(since)} "
          f"(watermark {_iso(mark) if mark else 'none'})", flush=True)
    code = runner([sys.executable, str(EXTRACTOR), "--since", _iso(since)])
    if code == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_iso(started) + "\n", encoding="utf-8")
        tmp.replace(path)
        print(f"[thread-extract] ok; watermark -> {_iso(started)}", flush=True)
    else:
        print(f"[thread-extract] extractor exited {code}; watermark unchanged, "
              "the next run re-covers this window", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
