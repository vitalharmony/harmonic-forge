"""Which waiting heavy job starts first (harmonic-forge#1634).

`heavy_slot.py` admits the head of its queue. This module decides who the head
is: the highest effective class, then the ticket name, which is arrival order.

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
a non-git directory.

**The ticket name is the only carrier.** A ticket is named
`<arrival ns, 20 digits>-<pid>-<class>.ticket` and has no body: nothing writes
one and nothing reads one. Within a class the order is the name order, the same
order origin/main and every old-code reader use, so mixed versions never wait on
each other. A name whose third part is not a known class (an old-format
`<ns>-<pid>.ticket`, or any other shape) is `normal`, and a name whose arrival
does not parse never ages, so neither can jump the queue. Only `_admit` creates
live tickets, and it always writes a 20-digit prefix.
"""
from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

PRIORITY_RANK = {"normal": 0, "high": 1}
ENV = "HEAVY_SLOT_PRIORITY"
DEFAULT_AGING_SECONDS = 1200
GIT_TIMEOUT_SECONDS = 2.0
#: Variables that would make `git -C <dir>` answer for another repo or stop
#: discovery early (as tools/onboard/manifest_git.py strips the first three;
#: a discovery ceiling inside the forge tree would rank a forge job high).
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES")
_FORGE_CHECKOUT = Path(__file__).resolve().parents[2]


def _common_dir(directory: str | Path, environ: Mapping[str, str]) -> str | None:
    """The realpath'd git common dir, or None. Output is decoded with the file
    system encoding (surrogateescape), so a non-UTF-8 path can never raise."""
    env = {k: v for k, v in environ.items() if k not in _GIT_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS, check=False)
        out = os.fsdecode(result.stdout).strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not out:
        return None
    return os.path.realpath(out)


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
        mine = _common_dir(here, env)
        forge = _common_dir(forge_checkout, env)
    except Exception:  # noqa: BLE001 -- every undecided class is high, never an escape to slot()'s unmanaged path
        return "high"
    return "normal" if mine is not None and mine == forge else "high"


def parse_ticket(name: str) -> tuple[str, float | None]:
    """`(class, arrived)` from a ticket name alone. Total and pure: any name
    yields a class (`normal` unless the third part is a known class), and an
    arrival that does not parse is None."""
    parts = name.removesuffix(".ticket").split("-")
    klass = parts[2] if len(parts) == 3 and parts[2] in PRIORITY_RANK else "normal"
    try:
        arrived = int(parts[0]) / 1e9
    except (ValueError, OverflowError):
        arrived = None
    return klass, arrived


def effective_class(klass: str, arrived: float | None, now: float, aging_seconds: float) -> str:
    if klass == "normal" and arrived is not None and now - arrived >= aging_seconds:
        return "high"
    return klass


def rank_key(path: Path, now: float, aging_seconds: float) -> tuple[int, str]:
    """Highest effective class first, then the name: arrival order, as on main."""
    klass, arrived = parse_ticket(path.name)
    effective = effective_class(klass, arrived, now, aging_seconds)
    return (-PRIORITY_RANK[effective], path.name)


def aging_seconds(config: Mapping) -> float:
    """The configured bound when it is a finite, non-negative number (not a
    bool); anything else, TOML's `inf`/`nan` or a string included, is the default."""
    limits = config.get("limits", {})
    value = limits.get("aging_seconds", DEFAULT_AGING_SECONDS) if isinstance(limits, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_AGING_SECONDS
    if isinstance(value, float) and not math.isfinite(value):
        return DEFAULT_AGING_SECONDS
    return value if value >= 0 else DEFAULT_AGING_SECONDS
