"""The Projects-board credential for lane sessions (harmonic-forge#1396).

A lane's fine-grained token cannot read or write the user-owned Projects
board, so board calls carry the classic token stored in the
`vitalharmony-projects` slot (operator option A, 2026-11-18: scopes `repo`,
`project`, `read:org`). That token is admin-capable, so it is handed out here,
by the board callers' own code for their own fixed board queries, and never
chosen from a command's text (preclose pass 1: routing by argv substring let a
lane run any GraphQL mutation on it).

Outside a lane, or when the slot has no token, the caller's environment is
returned unchanged: the call then runs on whatever credential the session has,
and a lane without the projects token fails visibly rather than escalating.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROJECTS_SLOT = "vitalharmony-projects"


def slot_token(slot: str, home: str | None = None) -> str:
    """A slot's file token: its bare `token` file, else hosts.yml's oauth_token."""
    root = Path(home or os.environ.get("GH_ACCT_HOME") or Path.home() / ".config" / "gh-accounts")
    try:
        bare = (root / slot / "token").read_text(encoding="utf-8").strip()
        if bare:
            return bare
    except OSError:
        pass
    try:
        for line in (root / slot / "hosts.yml").read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*oauth_token:\s*(\S+)", line)
            if match:
                return match.group(1)
    except OSError:
        return ""
    return ""


#: The one account whose board this token serves. Another account's board call
#: keeps the caller's own credential (preclose pass 1: an account-blind override
#: replaced a harmonicarchitect token gh-as had already verified).
PROJECTS_OWNER = "vitalharmony"


def board_owner(cmd: list[str]) -> str | None:
    """The board owner a board command names: `--owner X` (gh project) or
    `-F owner=X` / `-f owner=X` (gh api graphql). These commands are built by
    the board callers' own code, never passed through from a caller."""
    for i, arg in enumerate(cmd):
        if arg == "--owner" and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith("--owner="):
            return arg.split("=", 1)[1]
        if arg in ("-F", "-f", "--field", "--raw-field") and i + 1 < len(cmd) \
                and cmd[i + 1].startswith("owner="):
            return cmd[i + 1].split("=", 1)[1]
    return None


def projects_env(cmd: list[str], base: dict[str, str] | None = None,
                 owner: str | None = None) -> dict[str, str]:
    """The environment for one board command: in a lane, and only for a board
    `vitalharmony` owns, GH_TOKEN is the projects slot's token; otherwise
    `base` (default: this process's environment) with any inherited projects
    token removed (harmonic-forge#1463 AC1).

    An empty projects slot grants nothing: the read then runs as the lane slot,
    which cannot read a user-owned board, so it fails visibly (AC4, as
    harmonic-forge#1456 left it)."""
    env = dict(os.environ if base is None else base)
    # harmonic-forge#1463 AC2: identity comes from the process, not only from
    # the caller-built base: the process's own accounts root (preclose pass 1),
    # lane and slot.
    home = str(_accounts_root(None))
    lane = env.get("LANE")
    if lane not in ("1", "2", "3") or os.environ.get("LANE") != lane:
        return withhold_projects_token(env)
    # Account guards, both required: the base's slot must be the process's own
    # GH_CONFIG_DIR, an allowlisted vitalharmony slot directly under the
    # process's accounts root (gh-as harmonicarchitect sets that account's
    # slot), and a command naming its owner must name vitalharmony. `item-edit`
    # names only a project id, so for it the slot is the guard.
    slot_dir = _resolved(env.get("GH_CONFIG_DIR"))
    if (slot_dir is None or slot_dir != _resolved(os.environ.get("GH_CONFIG_DIR"))
            or slot_dir.name not in GRANTABLE_SLOTS or slot_dir.parent != _resolved(home)
            or not slot_dir.is_dir()):
        return withhold_projects_token(env)
    # An explicit board owner wins over the command's: an issue-field query
    # names the issue's repo owner, which need not own the board (pass 2).
    owner = owner if owner is not None else board_owner(cmd)
    if owner is not None and owner != PROJECTS_OWNER:
        return withhold_projects_token(env)
    token = slot_token(PROJECTS_SLOT, home)
    if token:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
        return env
    return withhold_projects_token(env)


#: harmonic-forge#1463 AC2: the base slots that may receive the projects token.
#: Never the projects slot itself, and never the bare `vitalharmony` slot: it
#: has no token file, so its credential is the shared keyring's admin one, and a
#: probe that strips the projects token must fall back to a lane's own
#: credential (sticky-wicket PATCH, preclose pass 2).
GRANTABLE_SLOTS = frozenset({"vitalharmony-lane1", "vitalharmony-lane2", "vitalharmony-lane3"})


def _accounts_root(home: str | None) -> Path:
    """The accounts root, `~` expanded (preclose pass 1: a tilde GH_ACCT_HOME
    must not lose the token)."""
    return Path(home or os.environ.get("GH_ACCT_HOME") or Path.home() / ".config" / "gh-accounts").expanduser()


def _resolved(path: str | Path | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def withhold_projects_token(env: dict[str, str]) -> dict[str, str]:
    """`env` without the projects slot's token in GH_TOKEN or GITHUB_TOKEN
    (harmonic-forge#1463 AC1, AC3). A value comparison: no marker, no state.
    The token is read from the process's own accounts root, never the base's
    (preclose pass 1)."""
    root = str(_accounts_root(None))
    token = slot_token(PROJECTS_SLOT, root)
    if token:
        stripped = False
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if env.get(key) == token:
                env.pop(key)
                stripped = True
        if stripped:
            _pin_lane_token(env, root)
    else:
        # Fail closed (sticky-wicket PATCH, preclose pass 2): when the projects
        # token cannot be read it cannot be recognized, so a lane-slot env
        # carries only that lane slot's own token.
        slot = _resolved(env.get("GH_CONFIG_DIR"))
        if slot is not None and slot.name in GRANTABLE_SLOTS:
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            _pin_lane_token(env, root)
    return _require_lane_token(env, root)


class NoLaneToken(RuntimeError):
    """In a lane, no lane slot token could be read, so the gh call must not run
    (harmonic-forge#1466, operator ruling 1a): gh treats a missing or empty
    GH_TOKEN as unset and falls back to the shared keyring's admin entry."""


def _require_lane_token(env: dict[str, str], root: str) -> dict[str, str]:
    """harmonic-forge#1466: in a lane (the process's own LANE is 1-3), an env
    on a grantable lane slot always leaves with that slot's own token; an env
    on the bare vitalharmony slot, or with no GH_CONFIG_DIR, cannot run gh.
    Another account's slot (harmonicarchitect, gh-as) is left unchanged, and
    outside a lane nothing changes (AC2)."""
    if os.environ.get("LANE") not in ("1", "2", "3"):
        return env
    slot = _resolved(env.get("GH_CONFIG_DIR"))
    if slot is None or slot.name == PROJECTS_OWNER:
        raise NoLaneToken(f"lane {os.environ['LANE']}: no lane slot for this gh call "
                          f"(GH_CONFIG_DIR={env.get('GH_CONFIG_DIR') or 'unset'}); refusing to run gh "
                          f"on the shared keyring")
    if slot.name not in GRANTABLE_SLOTS:
        return env  # another account's slot keeps its own credential
    if slot.parent == _resolved(root):
        lane_token = slot_token(slot.name, root)
        if lane_token:
            env["GH_TOKEN"] = lane_token
            env.pop("GITHUB_TOKEN", None)
            return env
    raise NoLaneToken(f"lane {os.environ['LANE']}: no readable token for slot {slot.name}; "
                      f"refusing to run gh on the shared keyring")


def _pin_lane_token(env: dict[str, str], root: str) -> None:
    """Set GH_TOKEN to the env's lane slot's own file token (operator ruling
    1a on the post-verdict, harmonic-forge#1463): with no GH_TOKEN, gh can
    prefer the shared keyring's admin entry over the slot's hosts.yml. Only a
    grantable lane slot under the process's accounts root is pinned."""
    slot = _resolved(env.get("GH_CONFIG_DIR"))
    if slot is None or slot.name not in GRANTABLE_SLOTS or slot.parent != _resolved(root):
        return
    lane_token = slot_token(slot.name, root)
    if lane_token:
        env["GH_TOKEN"] = lane_token
