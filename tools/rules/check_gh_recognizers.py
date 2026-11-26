#!/usr/bin/env python3
"""harmonic-forge#1500: no private gh close/merge recognizer outside the adapters.

Every tracked non-test `tools/hooks/*.py` except `gh_argv.py` and
`gh_argv_adapters.py` is parsed and searched for close/merge kind recognition.
A hit is keyed to the top-level symbol that encloses it (a def, a class, an
assignment target, else `<module>`). Docstrings, bare expression strings and
f-string parts are never inspected.

- G1: a string constant, or a tuple/list/set of them, holding an adjacent
  ("issue","close")/("pr","merge") pair or matching TEXT, used as a Compare
  operand, the first argument of a `re` function, or the argument of
  startswith/endswith.
- G1b: a Compare with a Subscript on one side and "close"/"merge" (alone or in a
  sequence) on the other.
- G2: any other string constant without whitespace that matches TEXT.
- G3: a def, class, assignment, import, name or attribute naming a retired
  recognizer.
- G4: `from X import s` or `alias.s` (through `import X [as alias]`) where `s` is
  a flagged symbol of another scanned module X flags the enclosing symbol in
  turn, iterated to a fixpoint. An excepted symbol stays flagged, so a
  re-export of it still fails. `from X import *` is a violation in itself
  (G4 cannot resolve it).

Exit 0 is clean; 1 is a violation, a moved scan root or an empty scan; 2 is a
file that could not be read or parsed (UNMEASURED), never reported clean.
Lives in tools/rules/, outside its own scan root, so the vocabulary it holds as
data never matches itself.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

HOOKS = "tools/hooks"
ADAPTERS = (f"{HOOKS}/gh_argv.py", f"{HOOKS}/gh_argv_adapters.py")
TEXT = re.compile(r"issue(\s|\\s[+*]?)+close|pr(\s|\\s[+*]?)+merge|state=closed|/merge\b")
PAIRS = {("issue", "close"), ("pr", "merge")}
VERBS = {"close", "merge"}
RE_FUNCS = {"compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "split"}
DYNAMIC = {"spec_from_file_location", "import_module", "__import__"}
#: F1497/F1498/F1499 removed these; naming one again is a reintroduction.
RETIRED = {"_parse_issue_close", "_parse_pr_merge", "_parse_api_close", "_parse_api_merge",
           "_parse_positional_target", "_method_is", "_gh_words", "API_ISSUE_PATH",
           "API_MERGE_PATH", "_ISSUE_CLOSE_VALUE_FLAGS", "_PR_MERGE_VALUE_FLAGS"}


def _strings(node: ast.AST) -> list[str] | None:
    """A string constant, or a tuple/list/set of only string constants, as a list."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return out if len(out) == len(node.elts) else None
    return None


def _recognizes(values: list[str] | None) -> bool:
    if not values:
        return False
    return any(pair in PAIRS for pair in zip(values, values[1:])) or any(TEXT.search(v) for v in values)


def _skipped(tree: ast.AST) -> set[int]:
    """ids of docstrings, bare expression strings and every f-string part."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in ast.walk(node))
    return skip


def _symbol(top: ast.stmt) -> str:
    if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return top.name
    target = top.targets[0] if isinstance(top, ast.Assign) else getattr(top, "target", None)
    if isinstance(target, (ast.Tuple, ast.List)) and target.elts:
        target = target.elts[0]
    return target.id if isinstance(target, ast.Name) else "<module>"


def _re_names(tree: ast.AST) -> set[str]:
    """`re` and every name `import re as X` binds in the module."""
    names = {"re"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.asname for a in node.names if a.name == "re" and a.asname)
    return names


def direct_hits(tree: ast.Module) -> list[tuple[int, str, str]]:
    """(line, symbol, rule) for G1, G1b, G2 and G3 in one parsed module."""
    skip, re_names, hits = _skipped(tree), _re_names(tree), set()
    for top in tree.body:
        symbol, used = _symbol(top), set()
        for node in ast.walk(top):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                for operand in operands:
                    if id(operand) not in skip and _recognizes(_strings(operand)):
                        hits.add((node.lineno, symbol, "G1"))
                        used.add(id(operand))
                if any(isinstance(o, ast.Subscript) for o in operands) and any(
                        VERBS & set(_strings(o) or ()) for o in operands if not isinstance(o, ast.Subscript)):
                    hits.add((node.lineno, symbol, "G1b"))
            elif isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
                func, first = node.func, node.args[0]
                is_re = func.attr in RE_FUNCS and isinstance(func.value, ast.Name) and func.value.id in re_names
                if (is_re or func.attr in ("startswith", "endswith")) and id(first) not in skip \
                        and _recognizes(_strings(first)):
                    hits.add((node.lineno, symbol, "G1"))
                    used.add(id(first))
        for node in ast.walk(top):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                    and id(node) not in used and not re.search(r"\s", node.value) and TEXT.search(node.value)):
                hits.add((node.lineno, symbol, "G2"))
            names = [getattr(node, "name", None), getattr(node, "id", None), getattr(node, "attr", None)]
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names += [a.name.rsplit(".", 1)[-1] for a in node.names] + [a.asname for a in node.names]
            if RETIRED & {n for n in names if isinstance(n, str)}:
                hits.add((node.lineno, symbol, "G3"))
    return sorted(hits)


def _g4(tree: ast.Module, modules: set[str], flagged: dict[str, set[str]]) -> set[tuple[int, str, str]]:
    """G4 hits in one module against the current flagged symbols of the others."""
    aliases = {a.asname or a.name: a.name.rsplit(".", 1)[-1] for n in ast.walk(tree)
               if isinstance(n, ast.Import) for a in n.names}
    hits = set()
    for top in tree.body:
        symbol = _symbol(top)
        for node in ast.walk(top):
            if isinstance(node, ast.ImportFrom):
                source = (node.module or "").rsplit(".", 1)[-1]
                for a in node.names:
                    if a.name == "*":
                        hits.add((node.lineno, symbol, "G4*"))
                    elif source in modules and a.name in flagged[source]:
                        hits.add((node.lineno, (a.asname or a.name) if node is top else symbol, "G4"))
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                source = aliases.get(node.value.id)
                if source in modules and node.attr in flagged[source]:
                    hits.add((node.lineno, symbol, "G4"))
    return hits


def all_hits(trees: dict[str, ast.Module]) -> dict[str, set[tuple[int, str, str]]]:
    """Direct hits per path, then G4 re-exports iterated until no symbol is newly flagged."""
    stem = {path: Path(path).stem for path in trees}
    hits = {path: set(direct_hits(tree)) for path, tree in trees.items()}
    flagged = {stem[p]: {symbol for _, symbol, _ in found} for p, found in hits.items()}
    changed = True
    while changed:
        changed = False
        for path, tree in trees.items():
            for hit in _g4(tree, set(flagged), flagged) - hits[path]:
                hits[path].add(hit)
                changed = changed or hit[1] not in flagged[stem[path]]
                flagged[stem[path]].add(hit[1])
    return hits


def dynamic_loads(tree: ast.Module) -> int:
    """Dynamic module loads: counted as unmeasured, never followed."""
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
               and (getattr(n.func, "attr", None) in DYNAMIC or getattr(n.func, "id", None) in DYNAMIC))


def scan_set(root: Path) -> list[str]:
    """Tracked non-test hook modules, the two adapters excluded (raises when git cannot list)."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", HOOKS], check=True,
                         capture_output=True, text=True, timeout=30).stdout
    tracked = [p for p in out.split("\0") if p]
    missing = [a for a in ADAPTERS if a not in tracked or not (root / a).is_file()]
    if missing:
        raise FileNotFoundError(f"scan root moved: {', '.join(missing)} not found")
    return sorted(p for p in tracked if p.endswith(".py") and p not in ADAPTERS
                  and not Path(p).name.startswith("test_")
                  and not {"testdata", "fixtures"} & set(Path(p).parts))


def _top() -> Path:
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).resolve().parent,
                               check=True, capture_output=True, text=True, timeout=30).stdout.strip())


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    tag = "[gh-recognizers]"
    try:
        root = Path(root) if root is not None else _top()
        paths = scan_set(root)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{tag} {exc}; nothing checked", file=sys.stderr)
        return 1
    if not paths:
        print(f"{tag} 0 files scanned under {HOOKS}; nothing checked", file=sys.stderr)
        return 1
    unmeasured, trees = [], {}
    for path in paths:
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            unmeasured.append(f"UNMEASURED {path}: {type(exc).__name__}: {exc}")
    loads = sum(dynamic_loads(tree) for tree in trees.values())
    violations = sorted((path, *hit) for path, found in all_hits(trees).items() for hit in found)
    for path, line, symbol, rule in violations:
        print(f"VIOLATION {path}:{line}: {symbol} [{rule}] private gh close/merge recognizer; "
              "read the kind through gh_argv_adapters (harmonic-forge#1500)")
    for line in unmeasured:
        print(line)
    if violations:
        print(f"{tag} {len(violations)} violation(s) in {len(paths)} files", file=sys.stderr)
        return 1
    if unmeasured:
        print(f"{tag} {len(unmeasured)} file(s) unmeasured; the scan is incomplete", file=sys.stderr)
        return 2
    print(f"{tag} clean: {len(paths)} files scanned, 0 violations, {loads} unmeasured dynamic loads")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
