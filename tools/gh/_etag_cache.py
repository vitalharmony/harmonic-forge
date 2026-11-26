"""Conditional GitHub reads with an on-disk ETag cache (harmonic-forge#1393).

Public surface (this docstring is the contract; CI slice 6 imports it):

    conditional_get(account, path, counter) -> Response(status, body, cached)
    prune(now=None, max_age=86400)          -> number of entries removed
    credential_identity(account, counter)   -> the login the cache keys on, or None

`conditional_get` runs one `GET` of `path` (a REST path, query included) through
`gh-as <account>`, sending `If-None-Match` when an entry exists. Only a real 304
returns the cached body. A 403 (primary or secondary limit), a 5xx, a timeout, a
missing status line, and a 404 or 410 all return `Response(status, None, False)`:
the cache never masks a failure, so a quota failure can never read as "no work"
(harmonic-forge#579's fail-closed rule). A 404 or 410 comes back with its status so
the caller's confirmed-missing path still runs.

The cache lives at `~/.claude/state/belt/etag/<account>/<hash>.json`, beside the
belt's watermarks, one file per (account, credential login, path with query). The
key carries the credential because GitHub varies on `Authorization`: an ETag minted
under one token does not validate under another (measured on harmonic-forge#1393:
the same URL under a second account has a different ETag and gets a 200, not a
304), so two lanes with their own tokens hold separate entries. If the login cannot
be read, the read is unconditional and nothing is cached. Entries are written to a
temp file in the same directory and `os.replace`d, so a reader never sees a torn
file; a corrupt entry is a miss that the next unconditional read rewrites; an
entry's mtime is touched on every use and `prune` removes entries unused for 24
hours, ignoring a file another process already deleted (every lane's belt shares
the directory).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional


PRUNE_AFTER_SECONDS = 24 * 3600
_ETAG = re.compile(r"(?im)^etag:\s*(.+?)\s*$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: `{account: login or None}` for the life of the process; one `GET /user` per account.
_IDENTITY: dict[str, Optional[str]] = {}


class Response(NamedTuple):
    status: Optional[int]
    body: Optional[str]
    cached: bool


def cache_root() -> Path:
    return Path.home() / ".claude" / "state" / "belt" / "etag"


def _account_dir(account: str) -> Path:
    return cache_root() / (_UNSAFE.sub("_", account).strip("_") or "x")


def _strip_headers(stdout: str) -> tuple[Optional[str], str]:
    """`(last header block, body)`. A followed redirect prints one block per
    response (watch_lane_posts.py `_http_status`), so blocks are stripped
    while the text starts with a status line, and the last one is the final
    response's. No separator: `(None, stdout)` unchanged."""
    last: Optional[str] = None
    rest = stdout
    while rest.startswith("HTTP/"):
        for sep in ("\r\n\r\n", "\n\n"):
            idx = rest.find(sep)
            if idx != -1:
                last, rest = rest[:idx], rest[idx + len(sep):]
                break
        else:
            return (None, stdout) if last is None else (last, rest)
    return last, rest


_STATUS_LINE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})\b")
_SECONDARY_LIMIT = re.compile(r"secondary rate limit", re.I)


@dataclass
class RawResponse:
    """One `gh api --include` response, whatever its status (harmonic-forge#1393).

    `status` is None when no status line could be read (a timeout, `gh-as` missing,
    garbled output): the caller treats that as a failure and never as "not
    modified". `body` is the text after the last header block."""

    status: Optional[int]
    headers: Optional[str]
    body: str
    stderr: str
    returncode: int


def gh_as_raw(
    account: str,
    argv: list[str],
    *,
    counter: Optional[object] = None,
    conditional: bool = False,
    timeout: int = 60,
) -> RawResponse:
    """The raw variant of `gh_as` (harmonic-forge#1393): `gh api --include` run
    through `gh-as <account>` that returns the status, headers and body and never
    raises on a non-zero exit. `gh` exits 1 on a 304, which `gh_as` would raise on.

    It counts like `gh_as` (one request through `counter.record`, rate-limit headers
    recorded from the final block, a 304 included, because the rate-limited-calls
    figure depends on it) and adds `calls_304`, `calls_conditional` (when the caller
    sent `If-None-Match`) and `calls_403_secondary`. A 403 or 429 naming a secondary
    rate limit is counted and named on stderr, never retried here. `argv` is the
    `gh` argument list starting with `api`; `--include` is added."""
    if counter is not None:
        counter.record(argv)
        if conditional:
            counter.calls_conditional += 1
    run_argv = ["api", "--include", *argv[1:]]
    try:
        proc = subprocess.run(["gh-as", account, "gh", *run_argv],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return RawResponse(None, None, "", f"{type(exc).__name__}: {exc}", -1)
    headers, body = _strip_headers(proc.stdout)
    status: Optional[int] = None
    if headers is not None:
        first = headers.splitlines()[0] if headers.splitlines() else ""
        match = _STATUS_LINE.match(first.strip())
        status = int(match.group(1)) if match else None
        if counter is not None:
            counter.record_headers(account, headers)
    if counter is not None and status == 304:
        counter.calls_304 += 1
    if status in (403, 429) and _SECONDARY_LIMIT.search(f"{body}\n{proc.stderr}"):
        if counter is not None:
            counter.calls_403_secondary += 1
        print(f"[belt_mechanics] secondary rate limit from {account} on "
              f"{' '.join(argv[1:3])}: not retried this tick", file=sys.stderr)
    return RawResponse(status, headers, body, proc.stderr, proc.returncode)


def credential_identity(account: str, counter: Optional[object] = None) -> Optional[str]:
    """The login behind `gh-as <account>`, read once per process, or None when it
    cannot be read (then nothing is cached for the account)."""
    if account in _IDENTITY:
        return _IDENTITY[account]
    login: Optional[str] = None
    resp = gh_as_raw(account, ["api", "-X", "GET", "user"], counter=counter)
    if resp.status == 200:
        try:
            value = json.loads(resp.body).get("login")
            login = str(value) if value else None
        except (ValueError, AttributeError):
            login = None
    _IDENTITY[account] = login
    return login


def _entry_path(account: str, login: str, path: str) -> Path:
    digest = hashlib.sha256(f"{account}\0{login}\0{path}".encode()).hexdigest()
    return _account_dir(account) / f"{digest}.json"


def _load(entry: Path, path: str, login: str) -> Optional[dict]:
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("path") != path or data.get("login") != login
            or not isinstance(data.get("etag"), str) or not isinstance(data.get("body"), str)
            or not _is_json(data["body"])):
        return None  # a miss: the next unconditional read rewrites it
    return data


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _store(entry: Path, path: str, login: str, etag: str, body: str) -> None:
    entry.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=entry.parent, prefix=entry.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"path": path, "login": login, "etag": etag, "body": body}, handle)
        os.replace(tmp, entry)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _touch(entry: Path) -> None:
    try:
        os.utime(entry)
    except OSError:
        pass


def conditional_get(account: str, path: str, counter: Optional[object] = None) -> Response:
    """One read of `path`, conditional when an entry exists. See the module docstring."""
    login = credential_identity(account, counter)
    entry = _entry_path(account, login, path) if login else None
    cached = _load(entry, path, login) if entry is not None else None
    argv = ["api", "-X", "GET", path]
    if cached is not None:
        argv += ["-H", f"If-None-Match: {cached['etag']}"]
    resp = gh_as_raw(account, argv, counter=counter, conditional=cached is not None)
    if resp.status == 304:
        if cached is None:
            return Response(304, None, False)  # nothing to serve: a failure, never "unchanged"
        _touch(entry)
        return Response(304, cached["body"], True)
    if resp.status == 200:
        etag = _ETAG.search(resp.headers or "")
        if entry is not None and etag and _is_json(resp.body):
            try:
                _store(entry, path, login, etag.group(1), resp.body)
            except OSError as exc:
                print(f"[_etag_cache] could not store {path}: {exc}", file=sys.stderr)
        return Response(200, resp.body, False)
    return Response(resp.status, None, False)


def prune(now: Optional[float] = None, max_age: float = PRUNE_AFTER_SECONDS) -> int:
    """Remove entries (and orphaned temp files) unused for `max_age` seconds.
    A file another process deleted or replaced under us is not an error."""
    cutoff = (time.time() if now is None else now) - max_age
    removed = 0
    root = cache_root()
    try:
        files = [p for d in root.iterdir() if d.is_dir() for p in d.iterdir()]
    except OSError:
        return 0
    for file in files:
        try:
            if file.stat().st_mtime < cutoff:
                file.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return removed
