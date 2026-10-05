#!/usr/bin/env python3
"""The scheduled thread extraction (harmonic-forge#907): `extract_threads.py`
run once per manifest repo, each over a window that starts at THAT repo's last
successful run.

Why a watermark (preclose pass 1): a window computed from the wall clock loses
every event in a gap longer than the window, because `--since` filters the gap
out of the fetch and `emit()`'s dedupe cannot recover what was never fetched.

Why one per repo (preclose pass 2, sticky-wicket PATCH): the extractor's unit
of identity, fetching and failure is the repo, but its process exit code
collapses eight repos into one bit. A single global watermark therefore never
advanced while any one repo kept failing -- a cross-account slot with a stale
token is enough -- and the gap protection was inert for all of them. Each repo
now has its own watermark, advanced only when its own invocation exits 0.

Window per repo: `--since = max(min(watermark - OVERLAP, now - DEFAULT_WINDOW),
now - MAX_LOOKBACK)`. The `MAX_LOOKBACK` clamp bounds a recovery run's quota
after a long outage, and says so loudly when it clamps: the loss is then
reported, not silent.

Watermarks live under `$XDG_STATE_HOME/harmonic-forge/thread-extract/`
(default `~/.local/state`), never in the event store.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "onboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXTRACTOR = HERE / "extract_threads.py"
DEFAULT_WINDOW = timedelta(days=3)
OVERLAP = timedelta(days=1)
MAX_LOOKBACK = timedelta(days=30)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "harmonic-forge" / "thread-extract"


def watermark_path(repo: str) -> Path:
    return state_dir() / (repo.replace("/", "__") + ".last")


def read_watermark(path: Path) -> Optional[datetime]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def window_start(now: datetime, watermark: Optional[datetime]) -> tuple[datetime, bool]:
    """The window's start, and whether MAX_LOOKBACK clamped it."""
    start = now - DEFAULT_WINDOW
    if watermark is not None:
        start = min(start, watermark - OVERLAP)
    floor = now - MAX_LOOKBACK
    return (floor, True) if start < floor else (start, False)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_repos(loader: Optional[Callable[[], list]] = None) -> list[str]:
    """Every manifest project with a repo. A manifest that cannot be read
    raises: running zero repos and exiting 0 would advance nothing while
    reporting success."""
    if loader is None:
        import manifest  # noqa: PLC0415

        def loader():  # noqa: E306
            return manifest.load(None)
    return [p.repo for p in loader() if getattr(p, "repo", None)]


def _write_watermark(path: Path, stamp: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_iso(stamp) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(now: Optional[datetime] = None,
         runner: Callable[[list[str]], int] = lambda argv: subprocess.run(argv).returncode,
         repos: Optional[list[str]] = None) -> int:
    started = now or datetime.now(timezone.utc)
    try:
        targets = repos if repos is not None else manifest_repos()
    except Exception as exc:  # noqa: BLE001 -- ManifestError and any read failure
        print(f"[thread-extract] cannot read the manifest: {exc}; nothing run", flush=True)
        return 2
    if not targets:
        print("[thread-extract] the manifest names no repos; nothing run", flush=True)
        return 2
    failed = []
    for repo in targets:
        path = watermark_path(repo)
        mark = read_watermark(path)
        since, clamped = window_start(started, mark)
        if clamped:
            print(f"[thread-extract] {repo}: watermark {_iso(mark)} is older than "
                  f"{MAX_LOOKBACK.days} days; window CLAMPED to {_iso(since)}, "
                  "events before it are not re-fetched", flush=True)
        code = runner([sys.executable, str(EXTRACTOR), "--repo", repo, "--since", _iso(since)])
        if code == 0:
            _write_watermark(path, started)
            print(f"[thread-extract] {repo}: since {_iso(since)} ok; watermark -> {_iso(started)}",
                  flush=True)
        else:
            failed.append(repo)
            print(f"[thread-extract] {repo}: since {_iso(since)} exited {code}; watermark "
                  f"unchanged ({_iso(mark) if mark else 'none'}), so this repo's next run "
                  "starts from it", flush=True)
    if failed:
        print(f"[thread-extract] {len(failed)} of {len(targets)} repos failed: {', '.join(failed)}",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
