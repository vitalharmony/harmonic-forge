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
shell, TOML, JSON, and extensionless files with a python or shell shebang),
minus test files (`test_*`, `*.test.ts`, `testdata/`, `fixtures/`) and minus
the globs in `tools/rules/operator_paths.toml`. Operator files are never
scanned and never baselined.

No account, org or project name is written here: those patterns, the forge
root and the projects root are built at scan time from `projects.toml`
through `tools/onboard/manifest.py`. The fixed patterns below are spelled so
that their own source text does not match them.

Imports resolve by basename across tracked non-test files, with no `sys.path`
evaluation; the scan refuses when two such Python files share a basename,
which keeps that resolution exact. A module loaded by `spec_from_file_location`
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
    if suffix == ".toml":
        return "hash"
    if suffix == ".json":
        return "plain"
    if suffix == "":
        try:
            with open(root / path, "rb") as fh:
                first = fh.readline(200)
        except OSError:
            return None
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


def code_lines(source: str, file_kind: str) -> list[str]:
    """The file's lines with comments (and Python docstrings) blanked."""
    lines = source.splitlines()
    if file_kind == "plain":
        return lines
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

def python_targets(source: str) -> tuple[list[str], int, list[str]]:
    """(imported names or file basenames, count of computed path loads, the
    computed-load line descriptions)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], 0, []
    found: list[str] = []
    dynamic: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module.split(".")[0])
            if node.level:
                found.extend(a.name for a in node.names)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value.split(".")[-1])
            elif name == "spec_from_file_location":
                arg = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "location"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append("file:" + arg.value.rsplit("/", 1)[-1])
                else:
                    dynamic.append(f"line {node.lineno}")
    return found, len(dynamic), dynamic


SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s+[\"']?([^\s\"';]+)")


def shell_targets(lines: list[str]) -> list[str]:
    return ["file:" + m.group(1).rsplit("/", 1)[-1]
            for m in (SOURCE_RE.match(l) for l in lines) if m]


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
        try:
            source = (root / p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = code_lines(source, k)
        for line in lines:
            text = normalize(line)
            if not text:
                continue
            for name, rx in rules:
                if rx.search(text):
                    rows[(name, p, text)] += 1
        if k == "python":
            targets, _, dyn = python_targets(source)
            dynamic.extend(f"{p} {d}" for d in dyn)
        elif k == "shell":
            targets = shell_targets(lines)
        else:
            targets = []
        for t in targets:
            hits = by_file.get(t[5:], []) if t.startswith("file:") else by_module.get(t, [])
            for target in hits:
                if target != p and is_operator(target, globs):
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
                    else f"{path}: {text}")
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
          f"in {len(rows)} row(s), none new; {len(dynamic)} computed file-path "
          "load(s) out of scope (--list-dynamic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
