#!/usr/bin/env python3
"""harmonic-forge#1072: a test file's `if __name__ == "__main__":` block is last.

Run as a script (`python3 test_x.py`, the form lanes and handoffs use), a file
whose main block sits part-way through runs only the tests above it and skips
any `setUpModule` below it, and still reports OK on the partial suite.
harmonic-forge#1070 found 98 of 321 tests silently dropped that way.

Rule (sticky-wicket REFORGE, issuecomment-6279769936): the first top-level
statement that compares `__name__` anywhere in it is the main block, and it
must be the file's last top-level statement. Only the comparison is required,
not a spelling, so elif, nested, reversed and computed guards all match, while
a plain use such as `getLogger(__name__)` does not (reforge pass 1). No later
statement's kind is inspected: any statement after the block fails, which is
stricter than "no class, test function or setUpModule after it".
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path


def _compares_name(node: ast.stmt) -> bool:
    """True when `__name__` is an operand of a comparison anywhere in `node`,
    so a guard in any spelling matches and `getLogger(__name__)` does not."""
    return any(isinstance(c, ast.Compare)
               and any(isinstance(n, ast.Name) and n.id == "__name__" for n in ast.walk(c))
               for c in ast.walk(node))


def violations(source: str) -> list[tuple[int, int]]:
    """(main-block line, next top-level statement's line), or [] when the
    first statement comparing `__name__` is the last one (or there is none)."""
    body = ast.parse(source).body
    for i, node in enumerate(body):
        if _compares_name(node):
            return [(node.lineno, body[i + 1].lineno)] if i + 1 < len(body) else []
    return []


def _tracked_test_files() -> list[Path]:
    """Every tracked test_*.py in the repo (preclose reforge pass 2: a fixed
    root list missed scripts/). Raises when git cannot list them."""
    out = subprocess.run(["git", "ls-files", "-z", "--", "*test_*.py"], check=True,
                         capture_output=True, text=True, timeout=30).stdout
    return sorted(Path(p) for p in out.split("\0") if p and Path(p).name.startswith("test_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="*",
                        help="directories to scan; default: every tracked test_*.py in the repo")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATH",
                        help="a test file another open issue owns; name that issue beside it")
    args = parser.parse_args(argv)
    excluded = {Path(p) for p in args.exclude}
    paths: list[Path] = []
    if not args.roots:
        try:
            paths = _tracked_test_files()
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[main-block] cannot list tracked test files ({exc}); nothing checked", file=sys.stderr)
            return 2
    for root in args.roots:
        if not Path(root).is_dir():
            print(f"[main-block] root {root!r} is not a directory; nothing checked", file=sys.stderr)
            return 2
        paths.extend(sorted(Path(root).rglob("test_*.py")))
    bad = scanned = 0
    for path in paths:
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
