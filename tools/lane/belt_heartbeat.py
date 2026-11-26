#!/usr/bin/env python3
"""The belt's heartbeat (harmonic-forge#1398): one file the watcher writes and
every suspenders tick reads.

The watcher (`tools/gh/watch_lane_posts.py`) writes it once at the end of
every poll. A tick (the `/loop 30m` cron) reads its age: a fresh heartbeat
means the Monitor is alive, a stale one means it died or expired and the tick
may re-arm it (belt skill step 4), but only when no Monitor task for the belt
is live either, so a tick landing in the normal expiry-to-re-arm gap never
double-arms.

The watcher writes it, and nothing else does: a session that only calls this
module's `read` cannot fake a fresh heartbeat without the watcher process
running. It is a liveness file and nothing more. It is never a lane action:
`record_lane_action` in `tools/hooks/afk_toggle.py`'s state directory owns that,
and a heartbeat that reset the afk idle clock would defeat the cutoff.

One file per (lane, workspace, scope). `scope` is the watcher's `--repo` value
or `all` (harmonic-forge#1199 lets two sessions of one lane split a workspace).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

STATE_RELDIR = Path(".claude") / "state" / "belt"
_MAX_KEY = 200  # leaves room for "heartbeat-" / "cutoff-" and ".json" under NAME_MAX 255


def state_dir() -> Path:
    return Path.home() / STATE_RELDIR


def _enc(value: str) -> str:
    """Reversible escaping, so two different values never share a name: percent-encode
    everything but letters, digits, `.` and `_`, and `-` too, because `-` joins the id's fields
    and `+` joins a scope's repos (a lossy slug let `o/repo` and `o/repo_` collide)."""
    return quote(value, safe="").replace("-", "%2D") or "%00"


def normalize_lane(lane: object) -> str:
    """`l1` from `1`, `l1` or `L1` (the session's `LANE` and the watcher's `--queue-for`
    spell it differently); anything else is escaped and lowercased."""
    text = str(lane or "").strip()
    match = re.fullmatch(r"[lL]?([123])", text)
    return f"l{match.group(1)}" if match else _enc(text.lower())


def scope_repos(scope: object = None) -> list[str]:
    """The repos a scope names: lowercased, sorted, deduplicated, `all` and blanks dropped.
    Takes a list or a string split on `+` or `,`, so `--repo`, `--scope-repo` and the tick's own
    `--scope` all name the same belt the same way. Idempotent on its own `+`-joined output."""
    if scope is None or scope == "":
        return []
    parts = [scope] if isinstance(scope, str) else list(scope)
    repos = {p.strip().lower() for part in parts for p in re.split(r"[+,]", str(part)) if p.strip()}
    return sorted(repos - {"all"})


def normalize_scope(scope: object = None) -> str:
    """`all` for no scope (or the CLI default `all`), else the repos each escaped and `+`-joined."""
    repos = scope_repos(scope)
    return "+".join(_enc(r) for r in repos) if repos else "all"


def key(lane: object, workspace: object = None, scope: object = None) -> str:
    """The one place a belt's identity is built (harmonic-forge#1398): `l<N>-<workspace>-<scope>`.
    The heartbeat file and the afk cutoff marker both name themselves from it and nothing else
    builds a `heartbeat-` or `cutoff-` name, so a writer and a reader can never spell a belt
    differently, and two belts of one lane in different workspaces never share a name."""
    name = f"{normalize_lane(lane)}-{_enc(str(workspace or 'none'))}-{normalize_scope(scope)}"
    if len(name) > _MAX_KEY:
        # A long escaped scope would pass the filesystem's 255-byte name limit; a digest of the
        # full identity keeps the name distinct and short (the prefix stays readable).
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        name = f"{normalize_lane(lane)}-{_enc(str(workspace or 'none'))[:40]}-h{digest}"
    return name


def path_for(lane: str, workspace: str, scope: object = "all") -> Path:
    return state_dir() / f"heartbeat-{key(lane, workspace, scope)}.json"


def write(lane: str, workspace: str, scope: object = "all", deadline: float | None = None,
          now: float | None = None) -> Path:
    """Record one poll. Atomic: a unique temp file renamed into place, so a
    reader never sees a half-written file."""
    path = path_for(lane, workspace, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"written_at": time.time() if now is None else now,
                       "deadline": deadline, "pid": os.getpid()})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read(lane: str, workspace: str, scope: object = "all", now: float | None = None) -> float | None:
    """The heartbeat's age in seconds, or None when there is no readable one.

    None is never "fresh": a missing, unparseable or future-dated file reads
    as no heartbeat, which is the direction that lets a tick re-arm."""
    try:
        written = float(json.loads(path_for(lane, workspace, scope).read_text(encoding="utf-8"))["written_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (time.time() if now is None else now) - written
    return age if age >= 0 else None


def is_stale(lane: str, workspace: str, scope: object = "all", max_age: float = 600.0,
             now: float | None = None) -> bool:
    """True when there is no heartbeat or it is older than `max_age` (two poll
    intervals of 300 s by default)."""
    age = read(lane, workspace, scope, now)
    return age is None or age > max_age


def _afk():
    """The afk idle cutoff's state module (harmonic-forge#1398, #1591), imported at call time so
    importing this module never loads the hook; read as attributes so patches reach it."""
    hooks = str(Path(__file__).resolve().parent.parent / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import afk_toggle  # noqa: PLC0415
    return afk_toggle


def _belt_key(lane: str, workspace: object = None, scope: object = "all") -> str | None:
    """The belt's identity from `key`, the ONE key builder; None when it cannot be built."""
    try:
        return key(lane, workspace, scope)
    except Exception:  # noqa: BLE001
        return None


def cutoff_marker_path(lane: str, workspace: object = None, scope: object = "all") -> Path | None:
    """One marker per belt (lane, workspace, scope), in afk's state dir, never this module's."""
    belt = _belt_key(lane, workspace, scope)
    return _afk().state_dir() / f"cutoff-{belt}.json" if belt else None


def idle_cutoff_due(lane: str, now: float | None = None, threshold: float | None = None,
                    workspace: object = None, scope: object = "all") -> str | None:
    """The episode id when this belt's lane has been idle `threshold` s (default
    `afk_toggle.IDLE_CUTOFF_SECONDS`) while afk is on and the cutoff is unannounced this episode.

    Idle time runs from the later of the lane's last action and `/afk on`. The marker is a file,
    not a flag: the watcher exits every 1800 s and is re-armed, so a flag would announce again."""
    afk = _afk()
    threshold = afk.IDLE_CUTOFF_SECONDS if threshold is None else threshold
    digit = afk._lane_digit(lane)
    episode = afk._afk_since() if afk.is_on() else None
    if digit is None or episode is None:
        return None
    since, since_at = episode
    current = time.time() if now is None else now
    marker = cutoff_marker_path(lane, workspace, scope)
    if marker is None:
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("since"), str):
            return None  # a marker without a readable episode id counts as announced
        marked = data["since"]
    except FileNotFoundError:
        marked = None
    except (OSError, ValueError, AttributeError):
        # An unreadable marker counts as announced: announcing twice in one episode is the failure
        # the marker prevents, and a corrupt one would otherwise be re-announced on every re-arm.
        return None
    if marked == since:
        return None
    age = afk.last_action_age(digit, current)
    idle = min(current - since_at, age) if age is not None else current - since_at
    return since if idle >= threshold else None


def mark_cutoff_announced(lane: str, since: str, workspace: object = None,
                          scope: object = "all") -> bool:
    """Record this episode's cutoff as announced for this belt, under afk's lock and writer. True
    only when the marker is on disk, so a failed write can never announce twice. Never raises."""
    try:
        afk = _afk()
        marker = cutoff_marker_path(lane, workspace, scope)
        if afk._lane_digit(lane) is None or marker is None:
            return False
        with afk.Locked():
            afk._atomic_write(marker, json.dumps({"since": since}))
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    """`belt_heartbeat.py read --lane l1 --workspace vh [--scope all] [--max-age 600]`.

    Prints the heartbeat's age and exits 0 when it is fresh, prints `none` or the
    stale age and exits 1 otherwise. A suspenders tick re-arms the Monitor only on
    exit 1 AND no live Monitor task for the belt."""
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("action", choices=["read"])
    parser.add_argument("--lane", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--scope", default="all")
    parser.add_argument("--max-age", type=float, default=600.0)
    args = parser.parse_args(argv)
    age = read(args.lane, args.workspace, args.scope)
    if age is None:
        print("none")
        return 1
    print(f"{age:.0f}s")
    return 0 if age <= args.max_age else 1


if __name__ == "__main__":
    raise SystemExit(main())
