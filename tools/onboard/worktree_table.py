#!/usr/bin/env python3
"""Worktree classification for `forge_onboard.py --install` (harmonic-forge#1532
reforge, operator D86).

Two pieces, each specified in the issue's reforge design amendment:

1. `parse_listing`: a strict parser over the exact stdout BYTES of
   `git worktree list --porcelain -z`. Anything it cannot read whole is a
   `ListingError` (the caller's preclose `InstallError`), never an empty or
   partial listing.
2. `classify`: one explicit ordered outcome table, `ROWS`. The first row
   whose `when` holds decides; `DEFAULT_DENY` (row 12) catches the rest.

Git is reached only through the `run(args, cwd) -> CompletedProcess[bytes]`
callable the caller passes (`forge_onboard_install._git_raw`): bytes in, so
no newline translation can rewrite a path before it is compared.
"""
from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

Run = Callable[[list[str], Path], subprocess.CompletedProcess]

#: The keys git-worktree(1) "Porcelain Format" documents, and each one's
#: form: `flag` takes no value, `value` needs one, `optional` takes either.
#: An unknown key is NOT refused here (git has added keys before: `prunable`
#: in 2.31); it is recorded, and a lane-path record carrying one is row 12.
KEYS = {"worktree": "value", "HEAD": "value", "branch": "value", "detached": "flag",
        "bare": "flag", "locked": "optional", "prunable": "optional"}


class ListingError(Exception):
    """The listing (or the repo's common dir) cannot be read whole."""


@dataclass(frozen=True)
class Record:
    """One whole `git worktree list --porcelain -z` record. `path` is as git
    printed it (decoded, never resolved here); `unknown` names any key git
    printed that `KEYS` does not document."""
    path: Path
    is_main: bool
    bare: bool = False
    locked: bool = False
    prunable: bool = False
    unknown: tuple[str, ...] = ()


def _record(record: bytes, is_main: bool) -> Record:
    fields = record.split(b"\0")
    if fields[0].partition(b" ")[0] != b"worktree":
        raise ListingError(f"porcelain record does not start with `worktree`: {record!r}")
    seen: dict[str, bytes | None] = {}
    for item in fields:
        if not item:
            raise ListingError(f"porcelain record has an empty field (a stray terminator): {record!r}")
        key_raw, sep, value = item.partition(b" ")
        key = key_raw.decode("utf-8", "backslashreplace")
        if key in seen:
            raise ListingError(f"porcelain record repeats `{key}` (a record without its "
                               f"terminator?): {record!r}")
        form = KEYS.get(key)
        if (sep and not value) or (form == "flag" and sep) or (form == "value" and not sep):
            raise ListingError(f"porcelain attribute {item!r} is not in git's documented form")
        seen[key] = value if sep else None
    raw_path = seen["worktree"] or b""
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise ListingError(f"worktree path {raw_path!r} is not UTF-8; --install cannot represent "
                           f"it -- rename it") from None
    return Record(path=Path(path), is_main=is_main, bare="bare" in seen, locked="locked" in seen,
                  prunable="prunable" in seen, unknown=tuple(k for k in seen if k not in KEYS))


def parse_listing(raw: bytes) -> tuple[Record, ...]:
    """The byte grammar: `raw` ends in `\\0\\0`; the records are
    `raw[:-2].split(b"\\0\\0")`; a record's fields are `split(b"\\0")`. So a
    path holding a newline, a CR or a quote is read whole. The first record
    is the main worktree (git-worktree(1))."""
    if not raw:
        raise ListingError("git worktree list printed nothing (git always lists the main worktree)")
    if not raw.endswith(b"\0\0"):
        raise ListingError(f"porcelain output ends mid-record (a record without its terminator): "
                           f"{raw[-80:]!r}")
    records: list[Record] = []
    for record in raw[:-2].split(b"\0\0"):
        records.append(_record(record, is_main=not records))
    return tuple(records)


def _why(proc: subprocess.CompletedProcess) -> str:
    text = proc.stderr or proc.stdout or b""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text.strip() or f"exit {proc.returncode}"


def read_listing(repo: Path, run: Run) -> tuple[Record, ...]:
    """A failed or malformed listing is ListingError, never an empty one."""
    proc = run(["worktree", "list", "--porcelain", "-z"], repo)
    if proc.returncode != 0:
        raise ListingError(_why(proc))
    return parse_listing(proc.stdout)


def _one_path(proc: subprocess.CompletedProcess) -> str | None:
    """Exactly one strict-UTF-8 line ending in exactly one `\\n` (only that
    one removed); None when git failed or printed anything else."""
    out = proc.stdout
    if proc.returncode != 0 or len(out) < 2 or not out.endswith(b"\n"):
        return None
    try:
        return out[:-1].decode("utf-8")
    except UnicodeDecodeError:
        return None


def common_dir(repo: Path, run: Run) -> Path:
    proc = run(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    found = _one_path(proc)
    if found is None:
        raise ListingError(f"cannot read {repo}'s git common dir (git printed {proc.stdout!r}: "
                           f"{_why(proc)})")
    return Path(found).resolve()


@dataclass(frozen=True)
class Identity:
    git_dir: Path
    common_dir: Path
    toplevel: Path


def probe_identity(path: Path, run: Run) -> Identity | str:
    """`path`'s own git dir, common dir and toplevel, one `rev-parse` each so
    a newline in a parent directory's name cannot split the output; or why
    it could not be read."""
    found = []
    for flag in ("--git-dir", "--git-common-dir", "--show-toplevel"):
        proc = run(["rev-parse", "--path-format=absolute", flag], path)
        value = _one_path(proc)
        if value is None:
            return f"git rev-parse {flag}: {_why(proc) if proc.returncode else repr(proc.stdout)}"
        found.append(Path(value).resolve())
    return Identity(*found)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


# --- The outcome table ------------------------------------------------------

CREATE, PRESENT, FAIL, MAIN = "create", "present", "fail", "main"


@dataclass
class Facts:
    path: Path
    repo: Path
    common: Path
    disk: str  # "absent" | "symlink" | "present"
    matches: tuple[Record, ...]
    run: Run
    _identity: Identity | str | None = field(default=None, repr=False)

    @property
    def record(self) -> Record | None:
        """THE record: exactly one match, carrying no unknown key."""
        if len(self.matches) == 1 and not self.matches[0].unknown:
            return self.matches[0]
        return None

    def identity(self) -> Identity | str:
        """Probed once, and only by the rows that read it (8-11)."""
        if self._identity is None:
            self._identity = probe_identity(self.path, self.run)
        return self._identity

    def identity_ok(self) -> bool:
        ident = self.identity()
        return (isinstance(ident, Identity) and ident.common_dir == self.common
                and ident.toplevel == self.path.resolve())


@dataclass(frozen=True)
class Row:
    number: int
    name: str
    when: Callable[[Facts], bool]
    kind: str
    message: Callable[[Facts], str]


@dataclass(frozen=True)
class Outcome:
    row: int
    kind: str
    detail: str


def _prune(f: Facts) -> str:
    return f"git -C {f.repo} worktree prune"


def _main_message(f: Facts) -> str:
    return (f"{f.path} is {f.repo}'s main checkout, not a lane worktree -- rename the checkout "
            f"or run from elsewhere; nothing was written")


def _listed_present(f: Facts) -> bool:
    return f.disk == "present" and f.record is not None and not f.record.prunable


def _linked(f: Facts) -> bool:
    ident = f.identity()
    return isinstance(ident, Identity) and ident.git_dir != ident.common_dir


ROWS: tuple[Row, ...] = (
    Row(1, "symlink", lambda f: f.disk == "symlink", FAIL,
        lambda f: f"{f.path} is a symlink (-> {os.readlink(f.path)}), not a worktree of {f.repo} "
                  f"-- left alone; inspect it, then remove it and re-run"),
    Row(2, "main checkout", lambda f: f.disk == "present" and f.record is not None
        and (f.record.is_main or f.record.bare), MAIN, _main_message),
    Row(3, "absent, locked", lambda f: f.disk == "absent" and f.record is not None and f.record.locked,
        FAIL, lambda f: f"{f.path} is gone but still registered as a locked worktree of {f.repo} "
                        f"-- not unlocked or pruned automatically; inspect, then run: "
                        f"git -C {f.repo} worktree unlock {f.path} && {_prune(f)}"),
    Row(4, "absent, prunable", lambda f: f.disk == "absent" and f.record is not None and f.record.prunable,
        FAIL, lambda f: f"{f.path} is gone but still registered as a worktree of {f.repo} "
                        f"-- not pruned automatically; inspect, then run: {_prune(f)}"),
    Row(5, "absent, unlisted", lambda f: f.disk == "absent" and not f.matches, CREATE,
        lambda f: f"create {f.path}"),
    Row(6, "present, prunable", lambda f: f.disk == "present" and f.record is not None and f.record.prunable,
        FAIL, lambda f: f"{f.path} exists but git lists it as a prunable worktree of {f.repo} (its "
                        f".git link is broken) -- left alone; inspect it, move it aside, then "
                        f"run: {_prune(f)}"),
    Row(7, "present, unlisted", lambda f: f.disk == "present" and not f.matches, FAIL,
        lambda f: f"{f.path} exists but is not a worktree of {f.repo} -- left alone; inspect "
                  f"it, then remove it and re-run"),
    Row(8, "listed, unreadable", lambda f: _listed_present(f) and not isinstance(f.identity(), Identity),
        FAIL, lambda f: f"{f.path} is listed as a worktree of {f.repo} but git cannot read it "
                        f"({f.identity()}) -- left alone; inspect it"),
    Row(9, "identity mismatch", lambda f: _listed_present(f) and isinstance(f.identity(), Identity)
        and not f.identity_ok(), FAIL,
        lambda f: f"{f.path} is listed as a worktree of {f.repo} but is not one (its git "
                  f"repository is {f.identity().common_dir}, not {f.common}) -- left alone; inspect it"),
    Row(10, "main checkout (git-dir backstop)", lambda f: _listed_present(f) and f.identity_ok()
        and not _linked(f), MAIN, _main_message),
    Row(11, "linked, identity ok", lambda f: _listed_present(f) and f.identity_ok() and _linked(f),
        PRESENT, lambda f: f"already present ({f.path})"),
)

DEFAULT_DENY = Row(12, "default-deny", lambda f: True, FAIL,
                   lambda f: f"{f.path} is in a state --install does not handle ({f.disk}; git lists "
                             f"{list(f.matches)}) -- left alone; inspect: "
                             f"git -C {f.repo} worktree list --porcelain")


def classify(path: Path, repo: Path, common: Path, records: tuple[Record, ...], run: Run) -> Outcome:
    """Reads only. The first row of `ROWS` whose `when` holds, else row 12.
    Record paths are resolved here, only to compare with `path`. An OSError
    (or a pre-3.13 symlink-loop RuntimeError) propagates to the caller's
    per-item wrap."""
    st = _lstat(path)
    if st is None:
        disk = "absent"
    elif stat.S_ISLNK(st.st_mode):
        disk = "symlink"
    else:
        disk = "present"
    matches: tuple[Record, ...] = ()
    if disk != "symlink":
        key = path.resolve()
        matches = tuple(r for r in records if r.path.resolve() == key)
    facts = Facts(path=path, repo=repo, common=common, disk=disk, matches=matches, run=run)
    for row in (*ROWS, DEFAULT_DENY):
        if row.when(facts):
            return Outcome(row.number, row.kind, row.message(facts))
    raise AssertionError("unreachable: DEFAULT_DENY always holds")


def listed(path: Path, records: tuple[Record, ...]) -> bool:
    """Whether any record resolves to `path` (for a write-phase message)."""
    key = path.resolve()
    return any(r.path.resolve() == key for r in records)
