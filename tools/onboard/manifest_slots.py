#!/usr/bin/env python3
"""Which gh credential slot an account runs on, from `projects.toml` (harmonic-forge#1528).

**Slot modes live in one top-level table, `[accounts.<name>]`.** Its one key is
`lane_slots` (bool, required): when true, inside lanes 1-3 the account runs on
`<slot root>/<name>-lane<N>` and that slot's file token, never the shared account
slot (harmonic-forge#1396/#1470); when false it runs on `<slot root>/<name>`.
The table is top-level on purpose: an app repo's own forge.toml may set only
`[project]` keys and refuses any other top-level key, so it can never reach a slot
mode. A `Project` field would be settable from there.

**Every account `projects.toml` declares is strict.** The launcher requires its
slot and refuses a login other than the account. The only ambient path is an
account declared nowhere in `projects.toml` (neither a `[[project]].account` nor
an `[accounts]` key) whose repo registers itself through its own checkout: at
launch (`launch_identity`), and at post time by the same rule outside a lane only
(`manifest_identity.apply_project_identity`, harmonic-forge#1579 ruling D11 as
amended by D14: inside a lane overlay-only posting refuses).
Such an account never reaches `selection` or `slot_dir`. An unregistered `<declared account>/<new repo>`
with its own forge.toml still refuses (pitch-inspection revision 1), and so does
one under the owner of any registered repo, whatever account that repo runs as
(`kenekted/<new>` is harmonicarchitect's; preclose round 1).

**A lane slot cannot be dropped silently.** The `[accounts]` lookup is
case-insensitive, a table without `lane_slots` is refused, and inside a lane an
account with no `[accounts]` table refuses rather than choosing a slot (sticky-wicket
verdict, preclose rounds 1-2): a missing declaration resolves to the deny side, so
deleting, misspelling or emptying the table fails closed and re-casing it still holds.

The repo-to-account lookup stays `manifest_identity.account_for_origin`, whose
two-call shape is the one place the per-checkout resolver is reached (R1).
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import manifest_identity
from manifest import ManifestError, load, manifest_path

#: The keys an `[accounts.<name>]` table may carry.
ACCOUNT_KEYS = ("lane_slots",)
LANES = ("1", "2", "3")


def accounts(path: Path | None = None) -> dict[str, dict]:
    """`{account: table}` from `projects.toml`'s `[accounts]`; `{}` when absent."""
    target = path or manifest_path()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {target}: {exc}") from exc
    table = raw.get("accounts", {})
    if not isinstance(table, dict):
        raise ManifestError(f"{target}: [accounts] must be a table of [accounts.<name>] tables")
    if len({name.casefold() for name in table}) != len(table):
        raise ManifestError(f"{target}: [accounts] names an account twice, ignoring case")
    for name, entry in table.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"{target}: [accounts.{name}] must be a table")
        unknown = sorted(set(entry) - set(ACCOUNT_KEYS))
        if unknown:
            raise ManifestError(f"{target}: [accounts.{name}] has unknown key(s): {', '.join(unknown)}")
        if "lane_slots" not in entry:
            raise ManifestError(f"{target}: [accounts.{name}] must set lane_slots = true or false")
        if not isinstance(entry["lane_slots"], bool):
            raise ManifestError(f"{target}: [accounts.{name}] lane_slots must be true or false")
    return table


def lane_slot(account: str, lane: str | None, path: Path | None = None) -> Path | None:
    """The account's own slot for `lane`, or None when it runs on the account slot.
    Inside a lane, refuses an account with no `[accounts]` table."""
    if lane not in LANES:
        return None
    table = {name.casefold(): entry for name, entry in accounts(path).items()}
    entry = table.get(account.casefold())
    if entry is None:
        raise ManifestError(
            f"account '{account}' is declared in projects.toml but has no "
            f"[accounts.{account}] table; a lane refuses rather than choosing a slot "
            "(harmonic-forge#1528)")
    if entry["lane_slots"]:
        return manifest_identity.slot_dir(f"{account}-lane{lane}")
    return None


def selection(account: str, lane: str | None, path: Path | None = None) -> tuple[Path, str | None]:
    """`(config_dir, token)`: a lane slot and its file token ('' when it has none),
    or the account slot and None. Shared by `slot_env` and `apply_project_identity`."""
    slot = lane_slot(account, lane, path)
    if slot is None:
        return manifest_identity.slot_dir(account), None
    return slot, manifest_identity._hosts_file_token(slot)


def declared(path: Path | None = None) -> set[str]:
    """Every account `projects.toml` names, plus the owner of every registered repo,
    casefolded: the strict set. An owner counts whatever account its repos run as."""
    projects = load(path)
    names = ({p.account for p in projects if p.account} | set(accounts(path))
             | {p.repo.split("/")[0] for p in projects if p.repo})
    return {name.casefold() for name in names}


def launch_identity(url: str, checkout: Path, lane: str | None) -> tuple[str, str, str, str]:
    """The launcher's decision for origin `url` in `checkout`, as
    `(mode, account, config_dir, token_file)`:

    - `lane`: a `lane_slots` account inside a lane; the token is read from the file;
    - `slot`: any other `projects.toml` account, which the launcher probes;
    - `ambient`: an account only the checkout's own forge.toml declares.

    Raises `ManifestError` for anything else, which the launcher refuses."""
    try:
        account = manifest_identity.account_for_origin(url)
    except ManifestError:
        account = None
    if account is not None:
        slot = lane_slot(account, lane)
        if slot is not None:
            return "lane", account, str(slot), str(slot / "hosts.yml")
        return "slot", account, str(manifest_identity.slot_dir(account)), ""
    account = manifest_identity.account_for_origin(url, checkout=Path(checkout))
    if account.casefold() in declared():
        raise ManifestError(
            f"{url} is registered only by its .claude/forge.toml, but its account "
            f"'{account}' is declared in projects.toml; register the repo there "
            "(harmonic-forge#1528)")
    return "ambient", account, "", ""


def main(argv: list[str]) -> int:
    """`launch <url> <checkout> <lane>`: one field per line, for `_gh_config_dir.sh`."""
    if len(argv) != 4 or argv[0] != "launch":
        raise SystemExit("usage: manifest_slots.py launch <url> <checkout> <lane>")
    fields = launch_identity(argv[1], Path(argv[2]), argv[3] or None)
    if any("\n" in field for field in fields):
        raise ManifestError("a launch field contains a newline")
    print("\n".join(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
