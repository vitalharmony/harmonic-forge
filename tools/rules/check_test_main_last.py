#!/usr/bin/env python3
"""harmonic-forge#1072: a test file's `if __name__ == "__main__":` block is last.

Run as a script (`python3 test_x.py`, the form lanes and handoffs use), a file
whose main block sits part-way through runs only the tests above it and skips
any `setUpModule` below it, and still reports OK on the partial suite.
harmonic-forge#1070 found 98 of 321 tests silently dropped that way.

Fails when any `test_*.py` under the given roots defines a class, a `test_*`
function or `setUpModule` after its top-level main block, naming file and line.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _is_main_guard(node: ast.stmt) -> bool:
    """Any top-level `if` whose test names `__name__` and the string
    `"__main__"`, whatever the spelling: reversed, or joined with `and`."""
    if not isinstance(node, ast.If):
        return False
    parts = list(ast.walk(node.test))
    return (any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts)
            and any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts))


def _is_test_definition(node: ast.stmt) -> bool:
    if isinstance(node, ast.ClassDef):
        return True
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (node.name.startswith("test_") or node.name == "setUpModule"))


def _definitions(nodes: list[ast.stmt]):
    """Test definitions in `nodes`, including those inside top-level
    if/try/with blocks, but never inside a class or function body."""
    for node in nodes:
        if _is_test_definition(node):
            yield node
        elif not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                yield from _definitions(getattr(node, field, []) or [])


def violations(source: str) -> list[tuple[int, int]]:
    """(main-block line, offending definition line) for each definition after it."""
    body = ast.parse(source).body
    found = []
    for i, node in enumerate(body):
        if _is_main_guard(node):
            found += [(node.lineno, later.lineno) for later in _definitions(body[i + 1:])]
            break
    return found


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
                print(f"{path}:{line}: defined after the __main__ block at line {main_line}; "
                      "move the block to the end of the file (harmonic-forge#1072)")
                bad += 1
    if bad:
        print(f"[main-block] {bad} definition(s) after a __main__ block", file=sys.stderr)
        return 1
    if not scanned:
        print("[main-block] no test_*.py found under the roots; nothing checked", file=sys.stderr)
        return 2
    print(f"[main-block] ok: every test file's __main__ block is last ({scanned} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
