#!/usr/bin/env python3
"""Refuse a hardcoded timezone in forge tooling (harmonic-forge#1505).

A local-time zone comes from `tools/settings/instance.py` `timezone()`, never
from a literal. This check parses every `*.py` under `tools/` and `skills/` with
`ast` and flags a call to `ZoneInfo` or `zoneinfo.ZoneInfo` whose first argument
or `key=` is a string literal, outside the accessor and the named exceptions.
It fails on any hit and on a scan of zero files, so a moved root never reads green.

    check_zone_literals.py [--root DIR]
"""
from __future__ import annotations

import argparse
import ast
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("tools", "skills")
#: Repo-relative paths allowed a literal zone, each with its reason.
EXCEPTIONS = {
    "tools/settings/instance.py": "the accessor itself holds the default",
}


def _is_zoneinfo(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "ZoneInfo"
    return isinstance(func, ast.Attribute) and func.attr == "ZoneInfo"


def _literal_zone(call: ast.Call) -> bool:
    args = list(call.args[:1]) + [kw.value for kw in call.keywords if kw.arg == "key"]
    return any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in args)


def scan(root: Path) -> tuple[int, list[str]]:
    """(files scanned, hits as `path:line`)."""
    files, hits = 0, []
    for directory in SCAN_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            files += 1
            if rel in EXCEPTIONS or path.name.startswith("test_"):
                continue
            try:
                with warnings.catch_warnings():
                    # Another file's invalid escape warns at parse time; not this check's concern.
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                hits.append(f"{rel}: cannot parse ({exc.__class__.__name__})")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_zoneinfo(node.func) and _literal_zone(node):
                    hits.append(f"{rel}:{node.lineno}")
    return files, hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    files, hits = scan(args.root)
    if files == 0:
        print("check-zone-literals: scanned zero files; a moved root is never green", file=sys.stderr)
        return 1
    if hits:
        print("check-zone-literals: a hardcoded zone; use tools/settings/instance.py timezone():", file=sys.stderr)
        print("\n".join(f"  {hit}" for hit in hits), file=sys.stderr)
        return 1
    print(f"check-zone-literals: {files} files, no hardcoded zone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
