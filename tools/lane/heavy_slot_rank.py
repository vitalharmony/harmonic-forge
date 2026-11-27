"""Which waiting heavy job starts first (harmonic-forge#1634).

`heavy_slot.py` admits the head of its queue. This module decides who the head
is: the highest effective class, then arrival order.

**The class comes from the repo, not the session.** A job taken in the forge's
own repository (its checkout, or any worktree of it) is `normal`; a job taken in
any other repository, or outside one, is `high`. The repo is named by its git
common dir, compared with the common dir of the forge checkout this file runs
from, so no repo name is written here and a newly onboarded client repo is
`high` with no configuration. `HEAVY_SLOT_PRIORITY=high|normal`, set by the
operator only, overrides it.

**Aging, so nothing starves.** A `normal` ticket that has waited
`aging_seconds` ranks as `high`, by its original arrival. The operator set the
bound above `max_wait_seconds` (D32), so a later `high` job's overdue start comes
before an earlier `normal` job ages.

Every failure is `high`: an unreadable working directory, git missing or slow,
a non-git directory. An unknown or missing class in a ticket body is `normal`,
so an old-format ticket never jumps the queue.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

PRIORITY_RANK = {"normal": 0, "high": 1}
ENV = "HEAVY_SLOT_PRIORITY"
DEFAULT_AGING_SECONDS = 1200
GIT_TIMEOUT_SECONDS = 2.0
#: Location variables that would make `git -C <dir>` answer for another repo
#: (as tools/onboard/manifest_git.py strips them).
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
_FORGE_CHECKOUT = Path(__file__).resolve().parents[2]


def _common_dir(directory: str | Path, environ: Mapping[str, str]) -> str | None:
    env = {k: v for k, v in environ.items() if k not in _GIT_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, env=env, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return os.path.realpath(result.stdout.strip())


def override(environ: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """`(class, None)` for a valid override, `(None, value)` for an invalid one
    (the caller logs it once), `(None, None)` when unset."""
    value = (os.environ if environ is None else environ).get(ENV, "")
    if not value:
        return None, None
    return (value, None) if value in PRIORITY_RANK else (None, value)


def priority_class(cwd: str | Path | None = None,
                   environ: Mapping[str, str] | None = None,
                   forge_checkout: Path = _FORGE_CHECKOUT) -> str:
    env = os.environ if environ is None else environ
    chosen, _invalid = override(env)
    if chosen:
        return chosen
    try:
        here = cwd if cwd is not None else os.getcwd()
    except OSError:
        return "high"
    mine = _common_dir(here, env)
    forge = _common_dir(forge_checkout, env)
    return "normal" if mine is not None and mine == forge else "high"


def ticket_body(klass: str, arrived: float) -> str:
    return json.dumps({"class": klass, "arrived": arrived})


def read_ticket(path: Path) -> tuple[str, float]:
    """`(class, arrived)`; an unreadable body is `normal`, arrived from the
    ticket name's `time_ns` prefix."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        klass = data.get("class")
        arrived = float(data["arrived"])
        return (klass if klass in PRIORITY_RANK else "normal"), arrived
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        try:
            return "normal", int(path.name.split("-", 1)[0]) / 1e9
        except ValueError:
            return "normal", 0.0


def effective_class(klass: str, arrived: float, now: float, aging_seconds: float) -> str:
    if klass == "normal" and now - arrived >= aging_seconds:
        return "high"
    return klass


def rank_key(path: Path, now: float, aging_seconds: float) -> tuple[int, float, str]:
    klass, arrived = read_ticket(path)
    effective = effective_class(klass, arrived, now, aging_seconds)
    return (-PRIORITY_RANK[effective], arrived, path.name)


def aging_seconds(config: dict) -> float:
    return float(config.get("limits", {}).get("aging_seconds", DEFAULT_AGING_SECONDS))
