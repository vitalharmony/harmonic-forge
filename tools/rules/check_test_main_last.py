#!/usr/bin/env python3
"""harmonic-forge#1072: a test file's `if __name__ == "__main__":` block is last.

Run as a script (`python3 test_x.py`, the form lanes and handoffs use), a file
whose main block sits part-way through runs only the tests above it and skips
any `setUpModule` below it, and still reports OK on the partial suite.
harmonic-forge#1070 found 98 of 321 tests silently dropped that way.

Rule (sticky-wicket REFORGE, issuecomment-6279769936): the first top-level
statement that mentions `__name__` anywhere in it is the main block, and it
must be the file's last top-level statement. Nothing about the guard's
spelling or the later statements' kind is recognized, so no spelling (elif,
nested, reversed, computed) and no block type escapes it. That is stricter than
"no class, test function or setUpModule after it": any later statement fails.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def violations(source: str) -> list[tuple[int, int]]:
    """(main-block line, next top-level statement's line), or [] when the
    first statement naming `__name__` is the last one (or there is none)."""
    body = ast.parse(source).body
    for i, node in enumerate(body):
        if any(isinstance(n, ast.Name) and n.id == "__name__" for n in ast.walk(node)):
            return [(node.lineno, body[i + 1].lineno)] if i + 1 < len(body) else []
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="*", default=["tools", "skills"])
    parser.add_argument("--exclude", action="append", default=[], metavar="PATH",
                        help="a test file another open issue owns; name that issue beside it")
    args = parser.parse_args(argv)
    excluded = {Path(p) for p in args.exclude}
    bad = scanned = 0
    for root in args.roots:
        if not Path(root).is_dir():
            print(f"[main-block] root {root!r} is not a directory; nothing checked", file=sys.stderr)
            return 2
        for path in sorted(Path(root).rglob("test_*.py")):
            if path in excluded:
                continue
            scanned += 1
            for main_line, line in violations(path.read_text(encoding="utf-8")):
                print(f"{path}:{line}: code after the __main__ block at line {main_line}; "
                      "move the block to the end of the file (harmonic-forge#1072)")
                bad += 1
    if bad:
        print(f"[main-block] {bad} file(s) with code after the __main__ block", file=sys.stderr)
        return 1
    if not scanned:
        print("[main-block] no test_*.py found under the roots; nothing checked", file=sys.stderr)
        return 2
    print(f"[main-block] ok: every test file's __main__ block is last ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
