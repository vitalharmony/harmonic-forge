#!/usr/bin/env python3
"""harmonic-forge#1500: no private gh close/merge recognizer outside the adapters.

Scans tracked non-test `tools/hooks/*.py` except the two adapters, from outside
that root so its own vocabulary never matches itself. A hit is keyed to its
enclosing top-level symbol; docstrings, bare strings and f-string parts are
never read. Rules G1, G1b, G2, G3 (direct) and G4 (re-export, to a fixpoint;
`from X import *` refused) are documented at `direct_hits` and `_g4`. A hit
whose (path, symbol) is an EXCEPTIONS entry is excepted; an entry that is
incomplete, outside the scan set, duplicated or stale, or a list above
CEILING, is refused. There is no write mode: growth is two hand edits.
Exit 0 clean; 1 a violation, a refusal, a moved scan root or an empty scan;
2 a file that cannot be read or parsed (UNMEASURED), never reported clean.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HOOKS = "tools/hooks"
EXCEPTIONS = "tools/rules/gh_recognizer_exceptions.toml"
FIELDS = ("path", "symbol", "reason", "contract")
#: Shrink-only, pinned equal to the entry count (TC5): a shrink lowers it.
CEILING = 6
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


def _strings(node: ast.AST) -> list[str]:
    """A string constant, or a tuple/list/set of only string constants, as a list; else []."""
    elts = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else [node]
    out = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return out if len(out) == len(elts) else []


def _recognizes(values: list[str]) -> bool:
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


def direct_hits(tree: ast.Module) -> list[tuple[int, str, str]]:
    """(line, symbol, rule) in one module. G1: a string constant (or a tuple/list/set
    of them) holding an adjacent issue/close or pr/merge pair or matching TEXT, as a
    Compare operand, a `re.<fn>` first argument or a startswith/endswith argument.
    G1b: a Compare of a Subscript with "close"/"merge", alone or in a sequence.
    G2: any other whitespace-free string constant matching TEXT. G3: a def, class,
    assignment, import, name or attribute naming a RETIRED recognizer."""
    skip, hits = _skipped(tree), set()
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
                        VERBS & set(_strings(o)) for o in operands if not isinstance(o, ast.Subscript)):
                    hits.add((node.lineno, symbol, "G1b"))
            elif isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
                func, first = node.func, node.args[0]
                is_re = func.attr in RE_FUNCS and isinstance(func.value, ast.Name) and func.value.id == "re"
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
    """G4 in one module: `from X import s`, or `alias.s` through `import X [as alias]`,
    with `s` flagged in scanned module X (an excepted symbol stays flagged). A
    top-level import is keyed to the name it binds. `from X import *` is a hit."""
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


def refusals(entries: object, scanned: list[str], hits: dict, unmeasured: set[str]) -> list[str]:
    """Why the exception list, or each of its entries, is refused; [] when valid."""
    if not isinstance(entries, list):
        return ["`exception` is not an array of tables"]
    out = [f"{len(entries)} entries exceed CEILING = {CEILING}"] if len(entries) > CEILING else []
    seen: set[tuple] = set()
    for i, e in enumerate(entries, 1):
        e = e if isinstance(e, dict) else {}
        key = (e.get("path"), e.get("symbol"))
        bad = [f"{'empty' if f in e else 'missing'} field {f!r}" for f in FIELDS
               if not (isinstance(e.get(f), str) and e[f].strip())]
        if not bad and key[0] not in scanned:
            bad = ["path is outside the scan set"]
        elif not bad and key in seen:
            bad = ["duplicates an earlier entry"]
        elif not bad and key[0] not in unmeasured and key[1] not in {h[1] for h in hits.get(key[0], ())}:
            bad = ["stale, matches no hit; delete it and lower CEILING"]
        seen.add(key)
        out += [f"entry {i} {key}: {why}" for why in bad]
    return out


def load_exceptions(root: Path) -> list | str:
    """The `[[exception]]` entries ([] when the file is absent), or why they cannot be read."""
    file = root / EXCEPTIONS
    try:
        return tomllib.loads(file.read_text(encoding="utf-8")).get("exception", []) if file.exists() else []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return f"unreadable: {exc}"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    tag = "[gh-recognizers]"
    try:
        root = Path(root) if root is not None else Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).resolve().parent,
            check=True, capture_output=True, text=True, timeout=30).stdout.strip())
        paths = scan_set(root)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{tag} {exc}; nothing checked", file=sys.stderr)
        return 1
    if not paths:
        print(f"{tag} 0 files scanned under {HOOKS}; nothing checked", file=sys.stderr)
        return 1
    unmeasured, trees = {}, {}
    for path in paths:
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            unmeasured[path] = f"{type(exc).__name__}: {exc}"
    hits, entries = all_hits(trees), load_exceptions(root)
    refused = [entries] if isinstance(entries, str) else refusals(entries, paths, hits, set(unmeasured))
    for reason in refused:
        print(f"REFUSED {EXCEPTIONS}: {reason}")
    if refused:
        return 1
    excepted = {(e["path"], e["symbol"]) for e in entries}
    violations = sorted((p, *h) for p, found in hits.items() for h in found if (p, h[1]) not in excepted)
    for path, line, symbol, rule in violations:
        print(f"VIOLATION {path}:{line}: {symbol} [{rule}] private gh close/merge recognizer; "
              "read the kind through gh_argv_adapters (harmonic-forge#1500)")
    for path, why in unmeasured.items():
        print(f"UNMEASURED {path}: {why}")
    if violations:
        print(f"{tag} {len(violations)} violation(s) in {len(paths)} files", file=sys.stderr)
        return 1
    if unmeasured:
        print(f"{tag} {len(unmeasured)} file(s) unmeasured; the scan is incomplete", file=sys.stderr)
        return 2
    loads = sum(dynamic_loads(tree) for tree in trees.values())
    print(f"{tag} clean: {len(paths)} files scanned, {len(excepted)} excepted symbols, 0 violations, "
          f"{loads} unmeasured dynamic loads")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
