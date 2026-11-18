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


def projects_env(cmd: list[str], base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for one board command: in a lane, and only for a board
    `vitalharmony` owns, GH_TOKEN is the projects slot's token; otherwise
    `base` (default: this process's environment) unchanged."""
    env = dict(os.environ if base is None else base)
    if env.get("LANE") not in ("1", "2", "3"):
        return env
    # Account guards, both required: the session's own slot must be a
    # vitalharmony one (gh-as harmonicarchitect sets that account's slot), and
    # a command naming its owner must name vitalharmony. `item-edit` names only
    # a project id, so for it the slot is the guard.
    slot = Path(env.get("GH_CONFIG_DIR", "")).name
    if not slot.startswith(PROJECTS_OWNER):
        return env
    owner = board_owner(cmd)
    if owner is not None and owner != PROJECTS_OWNER:
        return env
    token = slot_token(PROJECTS_SLOT, env.get("GH_ACCT_HOME"))
    if token:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    return env
