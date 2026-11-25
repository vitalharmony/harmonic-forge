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
    """Each simple `name = value` in the file (the last one wins)."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value
    return out


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
    # `from importlib import import_module as load` binds a loader under
    # another name (post-verdict cross-family finding 2).
    loaders = {n: n for n in ("import_module", "__import__", "spec_from_file_location")}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in loaders and a.asname:
                    loaders[a.asname] = a.name
        elif (isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute))
              and (node.value.id if isinstance(node.value, ast.Name) else node.value.attr) in loaders):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    loaders[t.id] = loaders[node.value.id if isinstance(node.value, ast.Name)
                                            else node.value.attr]
    found: list[list[str]] = []
    unresolved: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                found.append([parts[-1], "pkg:" + parts[0]])
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if parts:
                found.append([parts[-1], "pkg:" + parts[0]])
            if node.level or (parts and parts[-1] in dirs):
                found.extend([a.name] for a in node.names)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            name = loaders.get(name, name) if isinstance(fn, ast.Name) else name
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append([arg.value.split(".")[-1]])
                else:
                    unresolved.append(node.lineno)
            elif name == "spec_from_file_location":
                arg = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "location"), None)
                tail = _literal_tail(arg, assigns)
                if tail:
                    found.append(["file:" + tail])
                else:
                    unresolved.append(node.lineno)
    return found, unresolved


# A `source`/`.` in command position: line start, after a separator or pipe,
# or after a keyword that starts a command list.
SOURCE_CMD_RE = re.compile(
    r"(?:^|&&|\|\||[;|({!]|\b(?:then|do|else|elif|if|while|until))\s*"
    r"(?:(?:command|builtin|eval)\s+(?:--\s+)?)*(?:source|\.)\s+")


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


def shell_targets(lines: list[str]) -> tuple[list[list[str]], list[str]]:
    """(`file:` targets resolved by each sourced operand's trailing literal
    filename; the lines whose operand has no literal filename)."""
    found: list[list[str]] = []
    unresolved: list[str] = []
    for line in lines:
        for m in SOURCE_CMD_RE.finditer(line):
            word = _shell_word(line, m.end()).replace('"', "").replace("'", "")
            tail = word.rstrip("/").rsplit("/", 1)[-1]
            if tail and "$" not in tail and "(" not in tail and ")" not in tail:
                found.append(["file:" + tail])
            else:
                unresolved.append(line)
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
