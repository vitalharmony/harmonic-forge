#!/usr/bin/env python3
"""Create the board `Path` single-select once (harmonic-forge#1232).

    create_path_field.py --board OWNER/N [--read-only]

Reads the board's fields first and prints them. Only when `Path` is absent does
it create one, with the options `1`, `2`, `3`; when `Path` exists it prints that
and exits 0 without a write. `--read-only` stops after the read and says what
the create would do, so the read can be pasted on the issue before any write.

It only ever creates. Changing the options of an existing single-select wipes
every assignment on the board (harmonic-forge#300), so no code path here
touches an existing field.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable, Optional

FIELD = "Path"
OPTIONS = ("1", "2", "3")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def split_board(board: str) -> tuple[str, str]:
    owner, _, number = board.strip().rpartition("/")
    if not owner or not number.isdigit():
        raise ValueError(f"--board must be OWNER/N, got {board!r}")
    return owner, number


def read_fields(owner: str, number: str, run: Callable = _run) -> list[dict]:
    result = run(["gh", "project", "field-list", number, "--owner", owner, "--format", "json"])
    if result.returncode != 0:
        raise RuntimeError(f"field-list failed for {owner}/{number}: {result.stderr.strip()}")
    return json.loads(result.stdout)["fields"]


def ensure_path_field(board: str, *, read_only: bool = False, run: Callable = _run,
                      out: Callable[[str], None] = print) -> str:
    """Returns 'present', 'would-create' (read-only) or 'created'."""
    owner, number = split_board(board)
    fields = read_fields(owner, number, run)
    out(f"[path-field] {board}: " + ", ".join(
        f"{f.get('name')} ({f.get('type', '?')})" for f in fields))
    existing = next((f for f in fields if f.get("name") == FIELD), None)
    if existing is not None:
        options = [o.get("name") for o in existing.get("options") or []]
        out(f"[path-field] {board}: {FIELD} already exists with options {options}; no change")
        return "present"
    if read_only:
        out(f"[path-field] {board}: {FIELD} is absent; would create it with options {list(OPTIONS)}")
        return "would-create"
    result = run(["gh", "project", "field-create", number, "--owner", owner, "--name", FIELD,
                  "--data-type", "SINGLE_SELECT", "--single-select-options", ",".join(OPTIONS)])
    if result.returncode != 0:
        raise RuntimeError(f"field-create failed for {board}: {result.stderr.strip()}")
    out(f"[path-field] {board}: created {FIELD} with options {list(OPTIONS)}")
    return "created"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--board", required=True, help="OWNER/N, for example vitalharmony/3")
    parser.add_argument("--read-only", action="store_true", help="read and report; never create")
    args = parser.parse_args(argv)
    try:
        ensure_path_field(args.board, read_only=args.read_only)
    except (ValueError, RuntimeError) as exc:
        print(f"[path-field] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
