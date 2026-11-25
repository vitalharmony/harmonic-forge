#!/usr/bin/env python3
"""harmonic-forge#1524: a shrink-only baseline of operator hardcodes and of
core-to-operator import edges.

A finding is `(rule, path, text, count)`: `text` is a code line with comments
(and, in Python, docstrings) removed and whitespace collapsed, so moving a line
inside its file changes nothing; `count` is how often that text occurs in that
file under that rule. An import edge is a finding of rule `import` whose text
is the operator-side file a core file imports.

`--check` fails on a new row or a count above the baseline, and fails when the
scan is below the baseline ("remove N stale rows"), so the baseline shrinks in
the same change that removes a finding. `--update` only removes or lowers rows
and refuses while the scan holds a row the baseline lacks: a new row is a hand
edit review sees. `--print` renders the whole scan to stdout and writes nothing.

Scope: tracked files under `tools/`, `scripts/` and `sync_rules.py` (Python,
shell, TypeScript (hardcode rules only, no import edges), TOML, JSON, and
extensionless files with a python or shell shebang),
minus test files (`test_*`, `*.test.ts`, `testdata/`, `fixtures/`) and minus
the globs in `tools/rules/operator_paths.toml`. Operator files are never
scanned and never baselined.

No account, org or project name is written here: those patterns, the forge
root and the projects root are built at scan time from `projects.toml`
through `tools/onboard/manifest.py`. The fixed patterns below are spelled so
that their own source text does not match them.

A file is read as UTF-8, else by its PEP 263 coding declaration, else as
latin-1; a file that cannot be read fails the scan by name, never a skip.

Imports resolve by basename across tracked non-test files, with no `sys.path`
evaluation; the scan refuses when two such Python files share a basename,
which keeps that resolution exact. A dotted import resolves its last
component; `from pkg import name` also resolves each imported name when `pkg`
is a tracked directory, and an import of a package directory under `tools/`
whose files are operator-side is an edge to that directory. A module loaded by `spec_from_file_location`
resolves from a literal path argument; a computed path is out of scope and
counted in the output (`--list-dynamic` names each one).

harmonic-forge#1556. Every dotted part of a `from` module, and every name a
`from` import binds, may be the operator package, as for `import`. A loader
alias resolves only when its name is bound exactly once in the file, by any
form, and that binding is a bare reference to one loader or exact alias
(chains resolve by a bounded fixpoint); any other loader-bound name is
ambiguous, and each use of it, a call included, is an `import-unresolved`
row. A loader's own name rebound that way is ambiguous too, never remapped.
A name bound by a walrus, a plain assignment whose value is not a bare
reference (`load = functools.partial(runpy.run_path)`, `fns = (loader,)`), a
tuple or list unpacking, an augmented assignment, a def or lambda parameter
default, a for, comprehension or with target, or a match capture is never an
exact alias: when the bound value (the iterable, context expression or
subject; anywhere inside it, a call's arguments included but not its callee)
references a loader or ambiguous name, every target it binds is ambiguous,
so a call through it is a row at every use, not only on the binding line.
The fixpoint's bound covers every remap and every turn to ambiguous, so a
chain converges in any source order.

In shell, a backslash-newline is removed with nothing inserted, as bash
does. Each joined line is read by two recognizers: the raw-text regexes
below, unchanged, and a second one at every shell word start that compares
the word after quote and backslash removal with `source`, `.` and `eval`.
Their loads are a multiset union, so a load either sees counts once and an
eval no longer hides a `source` on its line.

Accepted residuals:
- (a) The union counts each tail as the larger of the two recognizers'
  counts, and the unresolved count as their max. A line where the two see
  different sites with the same target undercounts. Unresolved rows are
  keyed by line text, so this matters only for identical repeated lines.
- (b) A command word produced by an expansion is not seen: a variable
  (`S=source; $S file`), ANSI-C quoting (`$'\\56' file`,
  `so$'\\x75'rce file`) or brace expansion (`{.,} file`). Only quote-removal
  and backslash spellings count.
- (c) A loader bound or looked up by string key is not seen
  (`getattr(importlib, "import_module")(...)`, `globals()['load'] = ...`,
  `setattr(...)`), and an alias rebound that way stays resolved.
- (e) A loader stored on an attribute and called through it
  (`self.load = runpy.run_path`, then `self.load(p)`) is a row on the binding
  line only: names are tracked, attribute targets are not.
(b), (c) and (e) are out of harmonic-forge#1556's scope by Lane 1's decision.

Over-approximated edges fail closed: the scan can emit an `import` edge no
runtime load makes (`from opentelemetry.telemetry import api` ->
`tools/telemetry/`; `jq '.' ../telemetry/env.sh` -> `tools/telemetry/env.sh`).
Such an edge only adds a row; there is no "no false edges" guarantee.

A new quoted `.` argument in scanned core shell (`jq -c '.' "$f"`,
`jq "." "$f"`) is a new `import-unresolved` row by design: `--check` fails
until its author adds that row to the baseline by hand.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "hardcode.baseline.toml"
OPERATOR_PATHS = "tools/rules/operator_paths.toml"
SCOPE = ("tools", "scripts", "sync_rules.py")

# Fixed rules. Each regex is written so that it does not match its own source.
FIXED_RULES = (
    ("systemd", r"system(?:ctl|d-run)\b|\.(?:service|timer)\b"),
    ("codex-path", r"\.co[d]ex/"),
    ("push-notify", r"\bnt[f]y\b"),
    ("user-home", r"/hom[e]/[a-z_][\w.-]*|/User[s]/[A-Za-z]"),
    ("timezone", r"\b(?:America|Europe|Asia|Australia|Africa)/[A-Z][a-z]+"),
    ("gh-slot", r"gh-ac[c]ounts|\.config/gh-[a-z]|-lane(?:\$\{?LANE\}?|\{lane\}|[123])\b"),
)


def _word(value: str) -> str:
    return rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"


def manifest_rules(manifest: Path | None) -> list[tuple[str, str]]:
    """Org, account, project-name, forge-root and projects-root rules, from
    `projects.toml` (harmonic-forge#1524 AC5)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
    import manifest as onboard_manifest  # noqa: PLC0415
    projects = onboard_manifest.load(manifest)
    orgs: set[str] = set()
    accounts: set[str] = set()
    names: set[str] = set()
    roots: set[str] = set()
    checkouts: set[str] = set()
    for p in projects:
        names.add(p.name)
        if p.repo and "/" in p.repo:
            orgs.add(p.repo.split("/")[0])
        if p.board_owner:
            orgs.add(p.board_owner)
        if p.account:
            accounts.add(p.account)
        for declared in (p.path, p.worktree_dir):
            if declared and declared.startswith("~/"):
                parts = declared[2:].split("/")
                if len(parts) == 1 and declared == p.path:
                    checkouts.add(parts[0])
                else:
                    roots.add(parts[0])
    rules: list[tuple[str, str]] = []
    home = r"(?:~/|\$\{?HOME\}?/|home\(\)\s*/\s*[\"'])"
    for c in sorted(checkouts - roots):
        rules.append(("forge-root", home + re.escape(c) + r"(?![\w-])"))
    for r in sorted(roots):
        rules.append(("projects-root", _word(r)))
    for o in sorted(orgs):
        rules.append(("org", _word(o)))
    for a in sorted(accounts - orgs):
        rules.append(("account", _word(a)))
    for n in sorted(names):
        # `name#123` is an issue reference, not a hardcode.
        rules.append(("project-name", _word(n) + r"(?!#\d)"))
    return rules


def compile_rules(manifest: Path | None) -> list[tuple[str, re.Pattern[str]]]:
    return [(name, re.compile(rx, re.IGNORECASE if name in
                              ("org", "account", "project-name") else 0))
            for name, rx in (*FIXED_RULES, *manifest_rules(manifest))]


# --- file classification ----------------------------------------------------

def is_test(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    parts = path.split("/")
    return (base.startswith("test_") or base.endswith(".test.ts")
            or base == "conftest.py" or "testdata" in parts or "fixtures" in parts)


def load_operator_globs(root: Path) -> list[str]:
    data = tomllib.loads((root / OPERATOR_PATHS).read_text(encoding="utf-8"))
    globs = data.get("globs")
    if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
        raise SystemExit(f"{OPERATOR_PATHS}: `globs` must be a list of strings")
    return globs


def is_operator(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *SCOPE], cwd=root,
                         capture_output=True, check=True).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def kind(root: Path, path: str) -> str | None:
    suffix = Path(path).suffix
    if suffix == ".py":
        return "python"
    if suffix in (".sh", ".bash"):
        return "shell"
    if suffix == ".ts":
        return "ts"
    if suffix == ".toml":
        return "hash"
    if suffix == ".json":
        return "plain"
    if suffix == "":
        try:
            with open(root / path, "rb") as fh:
                first = fh.readline(200)
        except OSError as exc:
            raise ScanError(f"{path}: unreadable ({exc})") from exc
        if first.startswith(b"#!"):
            if b"python" in first:
                return "python"
            if re.search(rb"\b(?:ba)?sh\b", first):
                return "shell"
    return None


# --- comment and docstring stripping ---------------------------------------

def _strip_hash(line: str) -> str:
    """Drop a `#` comment that starts at line start or after whitespace,
    outside quotes."""
    quote = None
    escaped = False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _strip_ts(source: str) -> list[str]:
    """Blank `//` and `/* */` comments outside string and template literals."""
    out: list[str] = []
    quote = None
    block = False
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if block:
            if ch == "*" and nxt == "/":
                block = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
        elif quote:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == quote or (ch == "\n" and quote != "`"):
                quote = None
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        elif ch == "/" and nxt == "*":
            block = True
            out.append("  ")
            i += 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out).splitlines()


def code_lines(source: str, file_kind: str) -> list[str]:
    """The file's lines with comments (and Python docstrings) blanked."""
    lines = source.splitlines()
    if file_kind == "plain":
        return lines
    if file_kind == "ts":
        return _strip_ts(source)
    if file_kind != "python":
        return [_strip_hash(l) for l in lines]
    try:
        tree = ast.parse(source)
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return [_strip_hash(l) for l in lines]
    grid = [list(l) for l in lines]

    def blank(sl: int, sc: int, el: int, ec: int) -> None:
        for ln in range(sl, el + 1):
            if ln - 1 >= len(grid):
                break
            row = grid[ln - 1]
            start = sc if ln == sl else 0
            end = ec if ln == el else len(row)
            for i in range(start, min(end, len(row))):
                row[i] = " "

    for t in toks:
        if t.type == tokenize.COMMENT:
            blank(t.start[0], t.start[1], t.end[0], t.end[1])
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt in body:
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)):
                blank(stmt.lineno, stmt.col_offset, stmt.end_lineno or stmt.lineno,
                      stmt.end_col_offset or 0)
    return ["".join(r) for r in grid]


def normalize(line: str) -> str:
    return " ".join(line.split())


# --- import edges ------------------------------------------------------------

def _assigns(tree: ast.AST) -> dict[str, ast.AST]:
    """Each name bound exactly once in the file by a simple `name = value`. A
    name bound more than once, or by an annotated, augmented, loop or `with`
    target, is left out, so a path through it never resolves (operator ruling
    (a): a later rebinding can no longer hide the load it follows)."""
    out: dict[str, ast.AST] = {}
    seen: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            seen[node.id] = seen.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):  # a parameter rebinds the name too
            seen[node.arg] = seen.get(node.arg, 0) + 1
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value
    return {k: v for k, v in out.items() if seen.get(k) == 1}


def _literal_tail(node: ast.AST | None, assigns: dict[str, ast.AST], depth: int = 0) -> str | None:
    """The literal trailing filename of a path expression, or None: a string,
    `x / "f.py"`, `os.path.join(..., "f.py")`, `Path(..., "f.py")`, an f-string
    ending in a literal, or one name followed to its assignment."""
    if node is None or depth > 2:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        tail = node.value.rstrip("/").rsplit("/", 1)[-1]
        return tail or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _literal_tail(node.right, assigns, depth)
    if isinstance(node, ast.JoinedStr) and node.values:
        # Only a last literal piece that carries a "/" (or is the whole string)
        # names a whole filename; `f"{stem}.py"` names only an extension, so
        # it is unresolved (post-verdict cross-family finding 3).
        last = node.values[-1]
        if not (isinstance(last, ast.Constant) and isinstance(last.value, str)):
            return None
        if len(node.values) > 1 and "/" not in last.value:
            return None
        return _literal_tail(last, assigns, depth)
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name in ("join", "Path", "PurePath", "joinpath"):
            return _literal_tail(node.args[-1], assigns, depth)
        return None
    if isinstance(node, ast.Name) and node.id in assigns:
        return _literal_tail(assigns[node.id], assigns, depth + 1)
    return None


#: Operator ruling (a): every function that loads code by name or path.
LOADER_NAMES = frozenset({"import_module", "__import__", "spec_from_file_location", "run_path",
                          "run_module", "load_source", "SourceFileLoader", "exec", "execfile",
                          "load_module"})
#: The path-taking loaders and the index of their path argument.
PATH_LOADERS = {"spec_from_file_location": 1, "run_path": 0, "load_source": 1,
                "SourceFileLoader": 1}


def _binding_counts(tree: ast.AST) -> Counter:
    """How many times each name is bound in the file, by any form: a `Store`
    name (assignment, augmented, walrus, for/with/comprehension target), a
    parameter, an import alias, a def or class, an `except ... as`, a match
    capture and a type parameter (harmonic-forge#1556)."""
    stores: Counter = Counter()
    for node in ast.walk(tree):
        names: list[str | None] = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.append(node.id)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.extend(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.ExceptHandler):
            names.append(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar,
                               ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping):
            names.append(node.rest)
        stores.update(n for n in names if n)
    return stores


def _ref_name(node: ast.AST) -> str | None:
    """The name a bare reference names: `x` or the attribute of `a.x`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _value_refs(value: ast.AST) -> list[str]:
    """The names in reference position of an assigned value: the value
    itself, each `IfExp` branch and each `BoolOp` operand -- never anything
    inside a call, so `spec = spec_from_file_location(...)` taints nothing."""
    if isinstance(value, ast.IfExp):
        return _value_refs(value.body) + _value_refs(value.orelse)
    if isinstance(value, ast.BoolOp):
        return [r for v in value.values for r in _value_refs(v)]
    name = _ref_name(value)
    return [name] if name else []


def _loose_refs(value: ast.AST) -> list[str]:
    """Every name referenced anywhere in the value of a `_loose_binds`
    binding, a call's arguments included but never its callee: the value of
    `nullcontext(runpy.run_path)` is the loader, the value of
    `spec_from_file_location(...)` is not (harmonic-forge#1556, operator
    widening 2026-11-25)."""
    refs: list[str] = []
    stack = [value]
    while stack:
        node = stack.pop()
        name = _ref_name(node)
        if name:
            refs.append(name)
        if isinstance(node, ast.Call):
            stack.extend(node.args)
            stack.extend(k.value for k in node.keywords)
        else:
            stack.extend(ast.iter_child_nodes(node))
    return refs


def _store_names(target: ast.AST) -> list[str]:
    """Each name a binding target stores (`a`, `a, (b, *c)`)."""
    return [n.id for n in ast.walk(target)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _loose_binds(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """(name, value) for each binding that is not a plain `name = <bare
    reference>`: a walrus, a plain or annotated assignment whose value is not
    a bare reference (a call, container, subscript or other expression), a
    tuple or list unpacking target, an augmented assignment, a def or lambda
    parameter default (positional or keyword-only), a for or comprehension
    target (value: the iterable), a with target (value: the context
    expression) and a match capture (value: the subject). Such a name is
    never an exact alias; one bound to a loader is ambiguous
    (harmonic-forge#1556, operator widening 2026-11-25)."""
    loose: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.NamedExpr):
            loose.append((node.target.id, node.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            bare = _ref_name(node.value) is not None
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, (ast.Tuple, ast.List)):
                    loose.extend((n, node.value) for n in _store_names(t))
                elif isinstance(t, ast.Name) and not bare:
                    loose.append((t.id, node.value))
        elif isinstance(node, ast.AugAssign):
            loose.extend((n, node.value) for n in _store_names(node.target))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            pos = a.posonlyargs + a.args
            loose.extend((arg.arg, d) for arg, d in
                         zip(pos[len(pos) - len(a.defaults):], a.defaults))
            loose.extend((arg.arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults)
                         if d is not None)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            loose.extend((n, node.iter) for n in _store_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            loose.extend((n, item.context_expr) for item in node.items
                         if item.optional_vars is not None
                         for n in _store_names(item.optional_vars))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for p in ast.walk(case.pattern):
                    if isinstance(p, (ast.MatchAs, ast.MatchStar)) and p.name:
                        loose.append((p.name, node.subject))
                    elif isinstance(p, ast.MatchMapping) and p.rest:
                        loose.append((p.rest, node.subject))
    return loose


def _loader_aliases(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """(each name provably bound to exactly one loader -> that loader; the
    names bound to a loader in any other way). A name is an exact alias only
    when it is bound once in the whole file and that binding is a bare
    reference to one loader or exact alias; chains resolve by a bounded
    fixpoint. Every other loader-bound name is ambiguous: unresolved at every
    use, a call through it included (harmonic-forge#1556 form 5). A name in
    LOADER_NAMES is never remapped, and can itself be ambiguous. A target of a
    `_loose_binds` binding whose value references a loader or ambiguous name
    is ambiguous too (harmonic-forge#1556, operator widening 2026-11-25)."""
    stores = _binding_counts(tree)
    loaders = {n: n for n in LOADER_NAMES}
    ambiguous: set[str] = set()
    binds: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from importlib import import_module as load` binds a loader
            # under another name (post-verdict cross-family finding 2).
            for a in node.names:
                if a.name in LOADER_NAMES and a.asname and a.asname != a.name:
                    if stores[a.asname] == 1 and a.asname not in LOADER_NAMES:
                        loaders[a.asname] = a.name
                    else:
                        ambiguous.add(a.asname)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    binds.append((t.id, node.value))
    loose = [(name, _loose_refs(value)) for name, value in _loose_binds(tree)]
    # Each name is remapped at most once and turns ambiguous at most once, and
    # a sweep that changes anything spends one of those events, so this bound
    # always reaches the fixpoint, even for a chain in the worst source order.
    for _ in range(2 * len(binds) + len(loose) + 1):
        changed = False
        for name, value in binds:
            refs = _value_refs(value)
            hit = {loaders[r] for r in refs if r in loaders and r not in ambiguous}
            amb_ref = any(r in ambiguous for r in refs)
            if not hit and not amb_ref:
                continue
            exact = (_ref_name(value) is not None and len(hit) == 1 and not amb_ref
                     and stores[name] == 1)
            if exact:
                target = next(iter(hit))
                if name not in LOADER_NAMES and loaders.get(name) != target:
                    loaders[name] = target
                    changed = True
            elif name not in ambiguous:
                ambiguous.add(name)
                loaders.pop(name, None)
                changed = True
        for name, refs in loose:
            if name not in ambiguous and any(r in loaders or r in ambiguous for r in refs):
                ambiguous.add(name)
                loaders.pop(name, None)
                changed = True
        if not changed:
            break
    return loaders, ambiguous


def python_targets(source: str, dirs: frozenset[str] = frozenset()
                   ) -> tuple[list[list[str]], list[int]]:
    """(one candidate group per import: module names, `file:` basenames or
    `pkg:` package names; the line numbers of loads whose target cannot be
    resolved to a literal name). `dirs` names the tracked directories, so that
    `from pkg import name` resolves `name` only when `pkg` is one. A file that
    does not parse raises SyntaxError: it is never read as importing nothing
    (harmonic-forge#1524 two-pass sticky-wicket PATCH)."""
    tree = ast.parse(source)
    assigns = _assigns(tree)
    loaders, ambiguous = _loader_aliases(tree)
    found: list[list[str]] = []
    unresolved: list[int] = []
    resolved_funcs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                # every dotted part may be the operator package
                # (`import tools.telemetry`, operator ruling (a))
                found.append([parts[-1]] + ["pkg:" + part for part in parts])
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if parts:
                # every dotted part may be the operator package
                # (`from tools.telemetry import *`, harmonic-forge#1556 form 4)
                found.append([parts[-1]] + ["pkg:" + part for part in parts])
            if node.level or (parts and parts[-1] in dirs):
                # an imported name may itself be the package (`from tools import telemetry`)
                found.extend([a.name, "pkg:" + a.name] for a in node.names)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if isinstance(fn, ast.Name) and name in ambiguous:
                # Never resolved: an ambiguous LOADER_NAMES name is popped from
                # `loaders`, so the fallback below would resolve it as itself.
                continue
            name = loaders.get(name, name) if isinstance(fn, ast.Name) else name
            if name in ("import_module", "__import__", "run_module"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    parts = arg.value.split(".")
                    found.append([parts[-1]] + ["pkg:" + part for part in parts])
                    resolved_funcs.add(id(fn))
            elif name in PATH_LOADERS:
                index = PATH_LOADERS[name]
                arg = node.args[index] if len(node.args) > index else next(
                    (k.value for k in node.keywords if k.arg in ("location", "path", "path_name")),
                    None)
                tail = _literal_tail(arg, assigns)
                if tail:
                    found.append(["file:" + tail])
                    resolved_funcs.add(id(fn))
    # Operator ruling (a): every other reference to a loader -- an unresolved
    # call, an alias, an annotated or passed-around reference, exec -- is an
    # unresolved row. Detection over-approximates; resolution never guesses.
    for node in ast.walk(tree):
        if id(node) in resolved_funcs:
            continue
        name = (node.id if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                else node.attr if isinstance(node, ast.Attribute) else None)
        if name is not None and (name in LOADER_NAMES or loaders.get(name) in LOADER_NAMES
                                 or (isinstance(node, ast.Name) and name in ambiguous)):
            unresolved.append(node.lineno)
    return found, sorted(unresolved)  # every reference counts, two on a line are two


# Operator ruling (a): a `source` or `.` word anywhere on a line is a load
# (after any separator, keyword, prefix, assignment or quote), resolved by its
# operand's trailing literal filename. Over-approximate on purpose. These raw
# regexes are kept as they are; `CMD_POS_RE` below runs beside them
# (harmonic-forge#1556), and an eval no longer short-circuits the line.
SOURCE_CMD_RE = re.compile(r"""(?:^|(?<=[\s;&|({!`'"]))(?:source['"]?|\.)\s+(?:--\s+)?""")
#: A line that evaluates text can load without a visible `source`.
EVAL_RE = re.compile(r"""(?:^|(?<=[\s;&|({!`'"]))eval\s""")


def _shell_word(text: str, i: int) -> str:
    """The one shell word starting at `i`, quotes and `$( )` nesting respected."""
    out, depth, quote = [], 0, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
            out.append(c)
        elif c in "\"'":
            quote = c
            out.append(c)
        elif text.startswith("$(", i):
            depth += 1
            out.append("$(")
            i += 2
            continue
        elif c == ")" and depth:
            depth -= 1
            out.append(c)
        elif depth == 0 and (c.isspace() or c in ";&|)"):
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


#: harmonic-forge#1556: every shell word start -- line start, or after
#: whitespace, a separator, `(`, `{`, `!`, a backtick, a `case` arm's `)` or an
#: assignment's `=`. No reserved-word or prefix list: command position is never
#: decided, so no position can be missed.
CMD_POS_RE = re.compile(r"""(?:^|(?<=[\s;&|({!`)=]))(?=\S)""")


def _operand_tail(word: str) -> str | None:
    """A sourced operand's trailing literal filename, or None when it has
    none (empty, or carrying an expansion or glob character)."""
    word = word.replace('"', "").replace("'", "")
    tail = word.rstrip("/").rsplit("/", 1)[-1]
    if tail and not any(c in tail for c in "$()`*?[]{}~"):
        return tail
    return None


def _raw_sites(text: str) -> tuple[Counter, int]:
    """(literal tails, unresolved count) from the raw-text regexes: an eval
    anywhere on the line counts once, and each `SOURCE_CMD_RE` match counts
    its operand."""
    tails: Counter = Counter()
    unres = 1 if EVAL_RE.search(text) else 0
    for m in SOURCE_CMD_RE.finditer(text):
        tail = _operand_tail(_shell_word(text, m.end()))
        if tail:
            tails[tail] += 1
        else:
            unres += 1
    return tails, unres


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _command_sites(text: str) -> tuple[Counter, int]:
    """(literal tails, unresolved count) from every shell word start whose
    word, after quote and backslash removal, is `source`, `.` or `eval`
    (`'.'`, `s'ource'`, `'eval'`, harmonic-forge#1556 forms 1 and 2a). A
    quoted `.` argument (`jq -c '.' "$f"`) reads as a load too, on purpose."""
    tails: Counter = Counter()
    unres = 0
    for m in CMD_POS_RE.finditer(text):
        word = _shell_word(text, m.end())
        bare = re.sub(r"""['"\\]""", "", word)
        if bare == "eval":
            unres += 1
        elif bare in ("source", "."):
            i = _skip_space(text, m.end() + len(word))
            op = _shell_word(text, i)
            if op.replace('"', "").replace("'", "") == "--":
                i = _skip_space(text, i + len(op))
                op = _shell_word(text, i)
            if not op.replace('"', "").replace("'", ""):
                continue  # a `.` with no operand loads nothing
            tail = _operand_tail(op)
            if tail:
                tails[tail] += 1
            else:
                unres += 1
    return tails, unres


def shell_targets(lines: list[str]) -> tuple[list[list[str]], list[str]]:
    """(`file:` targets resolved by each sourced operand's trailing literal
    filename; the lines whose operand has no literal filename). Both
    recognizers run on every joined line and their loads are a multiset
    union, so a load either sees counts once and an eval never hides a
    `source` on its line (harmonic-forge#1556 form 3)."""
    found: list[list[str]] = []
    unresolved: list[str] = []
    joined: list[str] = []
    for line in lines:  # a continuation joins its next line, so the operand is seen
        if joined and joined[-1].endswith("\\"):
            # bash removes backslash-newline outright: `sou\` + `rce` is `source`
            joined[-1] = joined[-1][:-1] + line
        else:
            joined.append(line)
    for line in joined:
        raw_t, raw_u = _raw_sites(line)
        cmd_t, cmd_u = _command_sites(line)
        tails = raw_t + (cmd_t - raw_t)
        found.extend(["file:" + t] for t in tails.elements())
        unresolved.extend([line] * max(raw_u, cmd_u))
    return found, unresolved


def read_source(root: Path, path: str) -> str:
    """UTF-8, else the PEP 263 declared coding, else latin-1; an unreadable
    file raises ScanError naming it."""
    try:
        data = (root / path).read_bytes()
    except OSError as exc:
        raise ScanError(f"{path}: unreadable ({exc})") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for line in data.splitlines()[:2]:
        m = re.match(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", line)
        if m:
            codec = m.group(1).decode("ascii")
            try:
                return data.decode(codec)
            except (LookupError, UnicodeDecodeError) as exc:
                raise ScanError(f"{path}: unreadable as declared coding "
                                f"{codec} ({exc})") from exc
    return data.decode("latin-1")


# --- scan --------------------------------------------------------------------

class ScanError(Exception):
    pass


def scan(root: Path, manifest: Path | None = None) -> tuple[Counter, list[str]]:
    rules = compile_rules(manifest)
    globs = load_operator_globs(root)
    files = [p for p in tracked(root) if not is_test(p)]
    by_module: dict[str, list[str]] = {}
    by_file: dict[str, list[str]] = {}
    for p in files:
        base = p.rsplit("/", 1)[-1]
        by_file.setdefault(base, []).append(p)
        if p.endswith(".py"):
            by_module.setdefault(base[:-3], []).append(p)
    dirs = frozenset(d for p in files for d in p.split("/")[:-1])
    pkg_dirs: dict[str, str] = {}
    for p in files:
        parts = p.split("/")
        if (len(parts) > 2 and parts[0] == "tools"
                and is_operator(f"tools/{parts[1]}/__init__.py", globs)):
            pkg_dirs.setdefault(parts[1], f"tools/{parts[1]}/")
    dupes = {m: ps for m, ps in by_module.items() if len(ps) > 1}
    if dupes:
        raise ScanError("duplicate Python basename(s), import resolution is "
                        "ambiguous: " + "; ".join(
                            f"{m}: {', '.join(ps)}" for m, ps in sorted(dupes.items())))
    rows: Counter = Counter()
    dynamic: list[str] = []
    for p in files:
        if is_operator(p, globs):
            continue
        k = kind(root, p)
        if k is None:
            continue
        source = read_source(root, p)
        lines = code_lines(source, k)
        for line in lines:
            text = normalize(line)
            if not text:
                continue
            for name, rx in rules:
                if rx.search(text):
                    rows[(name, p, text)] += 1
        unresolved: list[str] = []
        if k == "python":
            try:
                targets, linenos = python_targets(source, dirs)
            except SyntaxError as exc:
                raise ScanError(f"{p}: does not parse as Python ({exc.msg}, line "
                                f"{exc.lineno}); a file that does not parse is never "
                                "read as importing nothing") from exc
            src_lines = source.splitlines()
            unresolved = [src_lines[n - 1] for n in linenos if 0 < n <= len(src_lines)]
        elif k == "shell":
            targets, unresolved = shell_targets(lines)
        else:
            targets = []
        # harmonic-forge#1524 two-pass PATCH: a load the scanner cannot resolve
        # is a baselined, shrink-only row, never an unchecked count.
        for line in unresolved:
            text = normalize(line)
            rows[("import-unresolved", p, text)] += 1
            dynamic.append(f"{p}: {text}")
        for group in targets:
            edges = []
            for t in group:
                if t.startswith("pkg:"):
                    continue
                hits = by_file.get(t[5:], []) if t.startswith("file:") else by_module.get(t, [])
                edges += [h for h in hits if h != p and is_operator(h, globs)]
            if not edges:
                edges = [pkg_dirs[t[4:]] for t in group
                         if t.startswith("pkg:") and t[4:] in pkg_dirs]
            for target in dict.fromkeys(edges):
                rows[("import", p, target)] += 1
    return rows, dynamic


# --- baseline ----------------------------------------------------------------

def read_baseline(path: Path) -> Counter:
    if not path.exists():
        return Counter()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: Counter = Counter()
    for row in data.get("finding", []):
        out[(row["rule"], row["path"], row["text"])] = int(row["count"])
    return out


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(rows: Counter) -> str:
    """Hand-rendered TOML (no TOML writer in the toolchain), sorted."""
    out = ["# harmonic-forge#1524: shrink-only baseline of operator hardcodes and",
           "# core-to-operator import edges. Regenerate downward with",
           "# `python3 tools/rules/hardcode_scan.py --update`; a new row is a hand edit.",
           ""]
    for (rule, path, text), count in sorted(rows.items()):
        if count <= 0:
            continue
        out += ["[[finding]]", f"rule = {_q(rule)}", f"path = {_q(path)}",
                f"text = {_q(text)}", f"count = {count}", ""]
    return "\n".join(out)


def compare(scan_rows: Counter, base: Counter) -> tuple[list[str], list[str]]:
    """(new-or-risen messages, stale messages)."""
    new, stale = [], []
    for key, n in sorted(scan_rows.items()):
        b = base.get(key, 0)
        if n > b:
            rule, path, text = key
            what = (f"{path} imports operator file {text}" if rule == "import"
                    else f"{path} has a load whose target cannot be resolved: {text}"
                    if rule == "import-unresolved" else f"{path}: {text}")
            new.append(f"[{rule}] {what} (count {n} > baseline {b})")
    for key, b in sorted(base.items()):
        n = scan_rows.get(key, 0)
        if n < b:
            rule, path, text = key
            stale.append(f"[{rule}] {path}: {text} (baseline {b}, scan {n})")
    return new, stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    mode.add_argument("--print", action="store_true", dest="print_")
    mode.add_argument("--list-dynamic", action="store_true")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--manifest", type=Path, default=None)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    manifest = args.manifest or (root / "projects.toml")
    try:
        rows, dynamic = scan(root, manifest)
    except ScanError as exc:
        print(f"hardcode_scan: {exc}", file=sys.stderr)
        return 1
    if args.print_:
        sys.stdout.write(render(rows))
        return 0
    if args.list_dynamic:
        print("\n".join(dynamic))
        return 0
    baseline_path = root / BASELINE
    base = read_baseline(baseline_path)
    new, stale = compare(rows, base)
    if new:
        print(f"hardcode_scan: {len(new)} new finding(s) or count rise(s):", file=sys.stderr)
        for line in new:
            print(f"  {line}", file=sys.stderr)
        if args.update:
            print("hardcode_scan: --update only removes or lowers rows; a new row "
                  "is a hand edit to hardcode.baseline.toml", file=sys.stderr)
        return 1
    if args.update:
        if stale:
            baseline_path.write_text(render(rows), encoding="utf-8")
        print(f"hardcode_scan: baseline updated, {len(stale)} row(s) removed or lowered")
        return 0
    if stale:
        print(f"hardcode_scan: remove {len(stale)} stale rows "
              "(run `python3 tools/rules/hardcode_scan.py --update`):", file=sys.stderr)
        for line in stale:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"[CHECK] hardcode_scan: {sum(rows.values())} baselined finding(s) "
          f"in {len(rows)} row(s), none new; {len(dynamic)} unresolved load(s), "
          "each a baselined row (--list-dynamic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
