"""A failed background `l1_post --dry-run` is recorded so the next prompt shows
it (harmonic-forge#1544).

`run(main)` wraps `l1_post`'s entry point. Only under `--dry-run`, any non-zero
exit is recorded once to `~/.claude/state/l1post/dry-run-failed/
<owner_repo>#<issue>-<kind>.json` as `{text, sha, at, shown}`, then re-raised
unchanged; a zero exit records nothing. `clear()` (called on the clean-dry-run
line) removes the entry for that issue and kind. `SystemExit(WAIT_EXIT)` is a
wait for CI, not a refusal, so it records nothing.

The text: a `fail()` exit carries it in the exception; an int-code exit
(argparse, rc=2) has printed it to stderr, so `run()` keeps the last 2 KB of
stderr through a tee and records that. A state error never changes the exit.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

WAIT_EXIT = 3  # _carry_forward.WAIT_EXIT: a wait for CI, not a refusal
_TAIL_BYTES = 2048


def state_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".claude" / "state" / "l1post" / "dry-run-failed"


def _entry(repo: str, issue: str, kind: str) -> Path:
    return state_dir() / f"{repo.replace('/', '_')}#{issue}-{kind}.json"


def record(repo: str, issue: str, kind: str, sha: str, text: str) -> None:
    path = _entry(repo, issue, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repo": repo, "issue": issue, "kind": kind, "sha": sha,
                               "text": text, "at": int(time.time()), "shown": False}))
    os.replace(tmp, path)


def clear(repo: str, issue: str | int, kind: str) -> None:
    try:
        _entry(repo, str(issue), kind).unlink(missing_ok=True)
    except OSError:
        pass


def pending() -> list[tuple[Path, dict]]:
    """Unshown entries, oldest first; a malformed entry is skipped."""
    out = []
    try:
        paths = sorted(state_dir().glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and not data.get("shown") and data.get("text"):
            out.append((path, data))
    return out


def mark_shown(path: Path, data: dict) -> None:
    data = {**data, "shown": True}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


class _Tee(io.TextIOBase):
    """Passes writes through to stderr and keeps the last _TAIL_BYTES."""

    def __init__(self, real):
        self._real, self._buf = real, ""

    def write(self, s: str) -> int:
        self._buf = (self._buf + s)[-_TAIL_BYTES:]
        return self._real.write(s)

    def flush(self) -> None:
        self._real.flush()

    @property
    def tail(self) -> str:
        return self._buf


def _target(argv: list[str]) -> tuple[str, str, str, str] | None:
    parser = argparse.ArgumentParser(add_help=False)
    for flag in ("--repo", "--issue", "--kind", "--sha"):
        parser.add_argument(flag, default="")
    try:
        ns, _ = parser.parse_known_args(argv)
    except SystemExit:
        return None
    if not (ns.repo and ns.issue and ns.kind):
        return None
    return ns.repo, ns.issue, ns.kind, ns.sha


def run(main, argv: list[str] | None = None) -> None:
    """Call `main()`; under `--dry-run`, record a non-zero exit, then re-raise."""
    argv = sys.argv[1:] if argv is None else argv
    if "--dry-run" not in argv:
        main()
        return
    tee = _Tee(sys.stderr)
    sys.stderr = tee
    try:
        main()
        _safe_clear(argv)  # a dry run that returns is a clean one
    except SystemExit as exc:
        if exc.code in (None, 0):
            _safe_clear(argv)
        code = exc.code
        if code not in (None, 0, WAIT_EXIT):
            text = code if isinstance(code, str) else (tee.tail.strip() or f"exit {code}")
            _safe_record(argv, str(text))
        raise
    except BaseException as exc:  # a traceback is a failed dry run too
        _safe_record(argv, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        sys.stderr = tee._real


def _safe_clear(argv: list[str]) -> None:
    try:
        target = _target(argv)
        if target:
            clear(target[0], target[1], target[2])
    except Exception:  # noqa: BLE001
        pass


def _safe_record(argv: list[str], text: str) -> None:
    try:
        target = _target(argv)
        if target:
            repo, issue, kind, sha = target
            record(repo, issue, kind, sha, text.strip())
    except Exception:  # noqa: BLE001 - recording never changes the exit
        pass
