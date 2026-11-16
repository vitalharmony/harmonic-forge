#!/usr/bin/env python3
"""Transaction-log view, rendered from git at read time (harmonic-forge#883).

There is no `transaction-log.md`. The view is a summary of git history, so it
is derived from git whenever it is read instead of being committed back into
git — a committed copy is stale the moment the next commit lands, and any two
branches that write it collide (README.md has the full history of why).

    render(repo, boundary, max_entries=None, max_chars=None) -> str

`boundary` picks where the view starts:
  - `recent:<N>`                 the last N first-parent commits on HEAD.
  - `version-minor:<relpath>`    every first-parent commit since the newest
                                 `vX.Y.0` tag reachable from HEAD (the current
                                 release), as `git describe --tags --match
                                 'v[0-9]*.[0-9]*.0' --abbrev=0 HEAD` finds it.
                                 <relpath> is accepted for compatibility and
                                 not read: the tag, not a file's history,
                                 marks the release (harmonic-forge#1372).
                                 Patch bumps never move the boundary; no such
                                 tag raises `ValueError`.

Usable as a library (a SessionStart hook imports it by path) and as a CLI
(a project's `mise run transaction-log` task shells out to it).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_MAX_ENTRIES = 40
DEFAULT_MAX_CHARS = 6000

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_MINOR_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.0$")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _minor_release_tag(repo: Path) -> tuple[str, str]:
    """(sha, tag) of the newest `vX.Y.0` tag reachable from HEAD.

    The tag, not the history of a version file, marks a release: `git log -S`
    on a version string takes the oldest commit that ever carried it, which in
    HRSE2 was a brief 3.0.0 months before the real 3.0.0 release, and rendered
    1,453 entries for a 118-commit release (harmonic-forge#1372).
    """
    try:
        # --exclude drops pre-release and suffixed tags (`v3.2.0-rc.0`,
        # `v3.1.4-pre-v3.2.0`) before describe picks, so a nearer one cannot
        # hide the release tag behind it.
        tag = _git(repo, "describe", "--tags", "--match", "v[0-9]*.[0-9]*.0",
                   "--exclude", "v*-*", "--abbrev=0", "HEAD").strip()
    except RuntimeError as exc:
        raise ValueError(f"no reachable vX.Y.0 tag: {exc}") from exc
    if not _MINOR_TAG_RE.match(tag):
        # Defense in depth behind --exclude: never take a non-release tag.
        raise ValueError(f"newest reachable tag {tag!r} is not exactly vX.Y.0")
    try:
        sha = _git(repo, "rev-parse", f"{tag}^{{commit}}").strip()
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve tag {tag}: {exc}") from exc
    return sha, tag


def resolve_boundary(repo: Path, boundary: str) -> tuple[list[str], str]:
    """Return (git log range args, human description) for <boundary>.

    Raises ValueError on an unknown or unresolvable boundary. Never falls back
    to full history.
    """
    kind, _, value = boundary.partition(":")
    if kind == "recent":
        if not value.isdigit() or int(value) < 1:
            raise ValueError(f"recent:<N> needs a positive integer, got {boundary!r}")
        return ["-n", value, "HEAD"], f"Last {value} first-parent commits on HEAD"
    if kind == "version-minor":
        if not value:
            raise ValueError("version-minor:<relpath> needs a path")
        sha, tag = _minor_release_tag(repo)
        return [f"{sha}..HEAD"], f"Commits on HEAD since the {tag[1:]} bump ({sha[:8]})"
    raise ValueError(f"unknown boundary {boundary!r} (expected recent:<N> or version-minor:<relpath>)")


def _entries(repo: Path, range_args: list[str]) -> list[str]:
    """One `git log` pass; each entry is `## <subject>` plus its shortstat line."""
    raw = _git(
        repo, "log", "--first-parent", "--diff-merges=first-parent", "--shortstat",
        f"--format={_RECORD_SEP}%H{_FIELD_SEP}%s", *range_args,
    )
    entries = []
    for record in raw.split(_RECORD_SEP)[1:]:
        head, _, rest = record.partition("\n")
        subject = head.split(_FIELD_SEP, 1)[1] if _FIELD_SEP in head else head
        stat = next((line.strip() for line in rest.splitlines() if line.strip()), "")
        entries.append(f"## {subject}\n- {stat or '(no file changes)'}")
    return entries


def render(repo: Path, boundary: str, max_entries: int | None = None,
           max_chars: int | None = None) -> str:
    """The view as markdown, newest first. Empty string when no commits."""
    range_args, _ = resolve_boundary(Path(repo), boundary)
    entries = _entries(Path(repo), range_args)
    kept: list[str] = []
    used = 0
    for entry in entries:
        if max_entries is not None and len(kept) >= max_entries:
            break
        cost = len(entry) + 1
        if max_chars is not None and used + cost > max_chars:
            break
        kept.append(entry)
        used += cost
    out = "\n".join(kept)
    omitted = len(entries) - len(kept)
    if omitted:
        out += (f"\n- … {omitted} older entries omitted; "
                "run mise run transaction-log --all")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the transaction-log view from git")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--boundary", required=True,
                        help="recent:<N> or version-minor:<relpath>")
    parser.add_argument("--all", action="store_true",
                        help=f"No caps (default {DEFAULT_MAX_ENTRIES} entries / {DEFAULT_MAX_CHARS} chars)")
    parser.add_argument("--out", type=Path, help="Write to this file instead of stdout")
    args = parser.parse_args()

    caps = {} if args.all else {"max_entries": DEFAULT_MAX_ENTRIES, "max_chars": DEFAULT_MAX_CHARS}
    try:
        _, description = resolve_boundary(args.project_root, args.boundary)
        body = render(args.project_root, args.boundary, **caps)
    except (ValueError, RuntimeError) as exc:
        print(f"transaction-log: {exc}", file=sys.stderr)
        return 1
    text = f"# Transaction log — {description}, newest first\n\n{body}\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
