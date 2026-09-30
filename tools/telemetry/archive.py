#!/usr/bin/env python3
"""Append-only telemetry archive: keep what a bounded writer would drop (harmonic-forge#826).

Every always-deleting state writer in the lane tooling (a record cap, a TTL
prune, a marker unlink) calls `archive()` with what it is about to remove, and
removes it only when `archive()` reports the full count written. The live
files keep their bounds; nothing is thrown away. This is the loss-stopping
half of harmonic-forge#637, not its store: records land as gzip JSONL that the
F637 store (design.md §95, append-only files + DuckDB views) can ingest as-is.

Layout, partitioned by account and org (OPERATOR-RULINGS 2026-09-29) so a
client or venture partition can be excluded, exported or deleted as a unit:

    <root>/<account>/<org>/<source>/<YYYY-MM>.jsonl.gz
    <root>/unresolved/unresolved/<source>/<YYYY-MM>.jsonl.gz

Each line is a v1 envelope: `{schema_version, source, archived_at, account,
org, repo, record}`. `org` is the owner segment of the registry's normalized
(lowercase) `repo`; GitHub owner names are case-insensitive and `Project` has
no `org` field, so this is canonical rather than a lossy guess.

**Registry-driven, never a repo list.** Origins resolve against
`projects.toml` through `tools/onboard/manifest.py`; onboarding a repo is the
only step that brings it under this archive. A record that resolves to no
registry entry is kept under `unresolved/`, never dropped.

**Resolution encodes forward, never decodes.** Claude Code names a session's
transcript directory by replacing every non-alphanumeric character of the
cwd with `-`, so `/` and `-` collide and a directory name cannot be decoded.
Each registry path is resolved (symlinks included) and encoded, then compared;
the longest match wins, so `HRSE2-lane2` resolves to the repo at `HRSE2`.

`archive()` never raises: a telemetry failure must not break the hook or
lane tool that called it. It returns the number of records durably appended,
and 0 on any failure -- which the caller reads as "do not delete".
"""
from __future__ import annotations

import fcntl
import hashlib
import gzip
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
ROOT_ENV = "HARMONIC_FORGE_TELEMETRY_ARCHIVE"
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ONBOARD = Path(__file__).resolve().parent.parent / "onboard"


@dataclass(frozen=True)
class Origin:
    account: str
    org: str
    repo: Optional[str]


UNRESOLVED = Origin("unresolved", "unresolved", None)


#: A test root must carry this file, or the override is refused. An env var is
#: inherited by every child process, so an unguarded override would let any
#: process that happened to inherit it archive into a throwaway directory while
#: every prune site, seeing a full count, deleted the only live copy. Refusing
#: fails toward retention: `archive()` returns 0 and nothing is deleted.
TEST_ROOT_SENTINEL = ".hf-telemetry-test-root"


class ArchiveRootRefused(RuntimeError):
    pass


def archive_root() -> Path:
    override = os.environ.get(ROOT_ENV)
    if override:
        root = Path(override).expanduser()
        if not (root / TEST_ROOT_SENTINEL).exists():
            raise ArchiveRootRefused(
                f"{ROOT_ENV}={override} has no {TEST_ROOT_SENTINEL}; refusing to archive there")
        return root
    return Path.home() / ".local/share/harmonic-forge/telemetry/archive"


def make_test_root(path: Path) -> Path:
    """Mark a directory as a legitimate test archive root (tests only)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / TEST_ROOT_SENTINEL).write_text("test archive root\n", encoding="utf-8")
    return path


def failure_log() -> Path:
    """Where archive failures are recorded -- outside the archive root, so a
    broken root cannot also swallow the evidence that it is broken."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") \
        / "harmonic-forge" / "telemetry-archive-failures.jsonl"


def record_failure(source: str, reason: str, *, forced_loss: int = 0) -> None:
    """One line per failure. `forced_loss` > 0 means a caller hit its hard
    ceiling and trimmed without an archive -- the one case data is lost."""
    try:
        path = failure_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(), "source": source,
                "reason": reason[:300], "forced_loss": forced_loss,
            }) + "\n")
    except Exception:
        pass


def encode_cwd(path: Path) -> str:
    """Claude Code's transcript-directory name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


# (resolved absolute path, encoded form, origin), longest path first.
_REGISTRY: Optional[list[tuple[Path, str, Origin]]] = None


def _load_registry(manifest_file: Optional[Path] = None) -> list[tuple[Path, str, Origin]]:
    if str(_ONBOARD) not in sys.path:
        sys.path.insert(0, str(_ONBOARD))
    import manifest  # noqa: PLC0415 -- bare-name import, as the rest of tools/ does

    entries: list[tuple[Path, str, Origin]] = []
    for project in manifest.load(manifest_file):
        if not project.repo or not project.path:
            continue
        resolved = Path(project.path).expanduser().resolve()
        origin = Origin(project.account or "unresolved", project.repo.split("/", 1)[0], project.repo)
        entries.append((resolved, encode_cwd(resolved), origin))
    entries.sort(key=lambda e: len(str(e[0])), reverse=True)
    return entries


def registry(manifest_file: Optional[Path] = None) -> list[tuple[Path, str, Origin]]:
    """Memoized at module scope: `batch_auth._prune` calls into this while
    holding its state lock, so the TOML parse must happen at most once."""
    global _REGISTRY
    if manifest_file is not None:
        return _load_registry(manifest_file)
    if _REGISTRY is None:
        _REGISTRY = _load_registry()
    return _REGISTRY


def reset_registry_cache() -> None:
    global _REGISTRY
    _REGISTRY = None


def resolve(where: Optional[Path | str], manifest_file: Optional[Path] = None) -> Origin:
    """The registry origin of a checkout path, a cwd, or an encoded transcript
    directory name. Unmatched (or `None`) is `UNRESOLVED`, never an error."""
    if where is None:
        return UNRESOLVED
    try:
        entries = registry(manifest_file)
    except Exception:
        return UNRESOLVED
    text = str(where)
    if "/" not in text:  # an encoded transcript directory name
        for _path, encoded, origin in entries:
            if text == encoded or text.startswith(encoded + "-"):
                return origin
        return UNRESOLVED
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return UNRESOLVED
    for path, encoded, origin in entries:
        if candidate == path or path in candidate.parents:
            return origin
        # A sibling lane worktree (`HRSE2-lane2`) shares the checkout's prefix.
        if encode_cwd(candidate).startswith(encoded + "-"):
            return origin
    return UNRESOLVED


def origin_for_repo(repo: Optional[str], manifest_file: Optional[Path] = None) -> Origin:
    """The registry origin of an `owner/name` (case-insensitive)."""
    if not repo:
        return UNRESOLVED
    try:
        wanted = repo.strip().lower()
        for _path, _encoded, origin in registry(manifest_file):
            if origin.repo == wanted:
                return origin
    except Exception:
        pass
    return UNRESOLVED


_PREFIX_REPOS: Optional[dict[str, str]] = None


def origin_for_key(key: Optional[str]) -> Origin:
    """The origin of a repo-prefixed issue key (`H1816`, `F826`), via the
    registry's own prefix map -- never a hardcoded letter table."""
    global _PREFIX_REPOS
    if not key:
        return UNRESOLVED
    try:
        if _PREFIX_REPOS is None:
            if str(_ONBOARD) not in sys.path:
                sys.path.insert(0, str(_ONBOARD))
            import manifest  # noqa: PLC0415
            _PREFIX_REPOS = manifest.prefix_repos()
        return origin_for_repo(_PREFIX_REPOS.get(key[:1].lower()))
    except Exception:
        return UNRESOLVED


def partition(origin: Origin, source: str, when: datetime) -> Path:
    return archive_root() / origin.account / origin.org / source / f"{when:%Y-%m}.jsonl.gz"


def archive(
    source: str,
    records: Iterable[dict[str, Any]],
    *,
    where: Optional[Path | str] = None,
    origin: Optional[Origin] = None,
    now: Optional[datetime] = None,
) -> int:
    """Append `records` under `source`; return how many were written (0 on failure).

    `origin` wins over `where` when both are given. One exclusive lock per
    partition file spans the whole append, so two writers never interleave
    a gzip member.
    """
    try:
        if not _SOURCE_RE.match(source):
            return 0
        batch = list(records)
        if not batch:
            return 0
        stamp = now or datetime.now(timezone.utc)
        who = origin or resolve(where)
        target = partition(who, source, stamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            json.dumps({
                "schema_version": SCHEMA_VERSION,
                "source": source,
                "archived_at": stamp.isoformat(),
                "account": who.account,
                "org": who.org,
                "repo": who.repo,
                # Dedupe key for at-least-once delivery: a partial multi-origin
                # failure re-archives the groups that already succeeded.
                "record_hash": hashlib.sha256(
                    json.dumps(record, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
                "record": record,
            }, sort_keys=True, default=str) + "\n"
            for record in batch
        ).encode("utf-8")
        lock = target.with_name(target.name + ".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                size = target.stat().st_size if target.exists() else 0
                try:
                    with target.open("ab") as raw:
                        with gzip.GzipFile(fileobj=raw, mode="ab") as gz:
                            gz.write(lines)
                        raw.flush()
                        os.fsync(raw.fileno())
                except BaseException:
                    # A torn gzip member would make the whole partition
                    # unreadable after the next good append: roll it back.
                    with target.open("r+b") as raw:
                        raw.truncate(size)
                    raise
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return len(batch)
    except Exception as exc:
        record_failure(source, f"{type(exc).__name__}: {exc}")
        return 0


def archive_by_origin(
    source: str,
    records: Iterable[dict[str, Any]],
    where_of: Any,
    *,
    now: Optional[datetime] = None,
    origin_of: Any = None,
) -> int:
    """`archive()` for a batch whose records come from different checkouts
    (a machine-wide log such as `belt-wakeup-fires.jsonl` carries each
    record's own `cwd`). `where_of(record)` names each record's location;
    records are grouped by resolved origin and each group appended to its
    own partition. Returns the total written; a caller deletes only when it
    equals the batch size."""
    try:
        groups: dict[Origin, list[dict[str, Any]]] = {}
        for record in records:
            try:
                origin = origin_of(record) if origin_of else resolve(where_of(record))
            except Exception:
                origin = UNRESOLVED
            groups.setdefault(origin, []).append(record)
        return sum(archive(source, batch, origin=origin, now=now) for origin, batch in groups.items())
    except Exception:
        return 0


def archive_file(source: str, path: Path, *, reason: str) -> int:
    """Archive one state file's content (JSON, else text) before its unlink.

    The record carries `path`, `reason` and the file's content; its origin is
    the file's own `cwd`/`repo` field when it has one, else `unresolved`.
    Returns 1 when written, 0 otherwise -- the caller decides whether its
    unlink is housekeeping (gate it on this) or semantics (a consumed grant
    must go regardless)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        content: Any = json.loads(text)
    except ValueError:
        content = text
    record = {"path": str(path), "reason": reason, "content": content}
    where = content.get("cwd") if isinstance(content, dict) else None
    repo = content.get("repo") if isinstance(content, dict) else None
    origin = origin_for_repo(repo) if repo else resolve(where)
    return archive(source, [record], origin=origin)


#: A bounded writer whose archive keeps failing holds its overflow rather than
#: drop it -- but only up to this many times its bound. Past that it trims
#: anyway and records the forced loss, so a broken archive can never turn a
#: hook that fires on every tool call into an unbounded, ever-slower read.
HARD_CEILING_FACTOR = 10


def over_hard_ceiling(total: int, bound: int) -> bool:
    return total > bound * HARD_CEILING_FACTOR


def read(path: Path) -> list[dict[str, Any]]:
    """Every envelope in one partition file (multi-member gzip)."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
