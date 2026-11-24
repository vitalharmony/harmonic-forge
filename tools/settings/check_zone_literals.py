#!/usr/bin/env python3
"""Refuse a hardcoded timezone in forge tooling (harmonic-forge#1505).

A local-time zone comes from `tools/settings/instance.py` `timezone()`, never
from a literal. This check parses every `*.py` under `tools/` and `skills/` with
`ast` and flags a call that constructs a `ZoneInfo` with a string literal
anywhere in ANY of its arguments, starred and keyword ones included:
`ZoneInfo(...)`, `zoneinfo.ZoneInfo(...)`, an
import alias of either, and the `no_cache` constructor. Only the files named in
EXCEPTIONS are exempt, and only files actually parsed count toward the scan, so
a tree of exempt files, like an empty one, never reads green.

A tripwire, not a proof. Caught: a direct call, `zoneinfo.ZoneInfo`, an import
alias of either, `no_cache`, and a zone argument containing a string literal or
an f-string (so a concatenation that includes a literal). Not caught, by design: a zone held in a
variable or constant first (`TZ = "Europe/Paris"; ZoneInfo(TZ)`), an assignment
alias (`Z = ZoneInfo`), `**kwargs`, `from_file`, and a string built before the
call. Closing those needs dataflow analysis; review catches them.

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
    "tools/settings/test_instance.py": "the accessor's own tests construct the zones they compare against",
}
_CONSTRUCTORS = ("no_cache",)


def _zoneinfo_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(names bound to the ZoneInfo class, names bound to the zoneinfo module)."""
    classes, modules = {"ZoneInfo"}, {"zoneinfo"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "zoneinfo":
            classes.update(a.asname or a.name for a in node.names if a.name == "ZoneInfo")
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "zoneinfo")
    return classes, modules


def _is_class(node: ast.AST, classes: set[str], modules: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in classes
    return (isinstance(node, ast.Attribute) and node.attr == "ZoneInfo"
            and isinstance(node.value, ast.Name) and node.value.id in modules)


def _is_zone_call(func: ast.AST, classes: set[str], modules: set[str]) -> bool:
    if _is_class(func, classes, modules):
        return True
    return isinstance(func, ast.Attribute) and func.attr in _CONSTRUCTORS and _is_class(func.value, classes, modules)


def _has_literal(call: ast.Call) -> bool:
    # Every argument, starred ones included (post-verdict survivor: an empty
    # `*[]` first shifted the zone out of a first-argument-only scan).
    args = list(call.args) + [kw.value for kw in call.keywords]
    return any(isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, str))
               for arg in args for n in ast.walk(arg))


def scan(root: Path) -> tuple[int, list[str]]:
    """(files parsed, hits as `path:line`). Exempt files are not counted."""
    files, hits = 0, []
    for directory in SCAN_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in EXCEPTIONS:
                continue
            try:
                with warnings.catch_warnings():
                    # Another file's invalid escape warns at parse time; not this check's concern.
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                hits.append(f"{rel}: cannot parse ({exc.__class__.__name__})")
                continue
            files += 1
            classes, modules = _zoneinfo_names(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_zone_call(node.func, classes, modules) and _has_literal(node):
                    hits.append(f"{rel}:{node.lineno}")
    return files, hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    files, hits = scan(args.root)
    if hits:
        print("check-zone-literals: a hardcoded zone; use tools/settings/instance.py timezone():", file=sys.stderr)
        print("\n".join(f"  {hit}" for hit in hits), file=sys.stderr)
        return 1
    if files == 0:
        print("check-zone-literals: parsed zero files; a moved root is never green", file=sys.stderr)
        return 1
    print(f"check-zone-literals: {files} files, no hardcoded zone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
