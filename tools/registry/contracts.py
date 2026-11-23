#!/usr/bin/env python3
"""The contract index (harmonic-forge#1501): load, validate, render, check.

`tools/registry/contracts.toml` lists the shared behaviors of this repo and who
owns each, so an agent about to add or reimplement one can find the owner first.
It is a thin index: it points at owners, guards and callers and never restates
them. The effect registry (`effects.toml`) is linked, never duplicated, and no
grant or effect semantics enter it.

Every repo keeps one TOML at its root and renders it itself, because CI in a
consuming repo has no forge checkout. The field list below is the shared part.

    contracts.py validate    schema, unique ids, resolving paths
    contracts.py render      write docs/CONTRACTS.md from the TOML
    contracts.py --check     fail when docs/CONTRACTS.md differs (byte for byte) from a fresh render
    contracts.py resolve ID… resolved / unresolved / unmeasured per id; exit 0 only when all resolve

A contract id is `<project>.<name>`, `<project>` being a `name` in projects.toml.
An id in another project's namespace resolves where that checkout exists and
reports `unmeasured` otherwise, never green.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORGE_ROOT = HERE.parents[1]
TOML_PATH = HERE / "contracts.toml"
DOC_PATH = FORGE_ROOT / "docs" / "CONTRACTS.md"
OWN_PROJECT = "harmonic-forge"
STATUSES = ("canonical", "pending-adopter")
#: field -> "str" or "list" (a non-empty list of strings).
FIELDS = {
    "id": "str", "scope": "str", "owner": "str", "modules": "list", "authority": "str",
    "example_caller": "str", "guard": "list", "exceptions": "str", "scan_roots": "list",
    "blind_spots": "str", "status": "str",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")
RESOLVED, UNRESOLVED, UNMEASURED = "resolved", "unresolved", "unmeasured"


def _contract_list(data: dict) -> list[dict] | None:
    """The `[[contract]]` entries, or None when `contract` is not a list of tables
    (a single `[contract]` table, say)."""
    entries = data.get("contract", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return None
    return entries


def load(path: Path = TOML_PATH) -> list[dict]:
    try:
        entries = _contract_list(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"contracts: cannot read {path}: {exc}") from exc
    if entries is None:
        raise SystemExit(f"contracts: {path}: `contract` must be an array of tables ([[contract]])")
    return entries


def _path_part(ref: str) -> str:
    """`path` from `path`, `path:line` or `path:symbol`."""
    return ref.split(":", 1)[0].strip()


def link_problem(ref: str, root: Path, want: str) -> str | None:
    """Why one link does not resolve, or None. A link is a repo-relative path,
    optionally `:line` (that line must exist) or `:symbol`; `want` is "file" or
    "dir". An empty, absolute or escaping path never resolves."""
    path, _, suffix = ref.partition(":")
    path, suffix = path.strip(), suffix.strip()
    if not path or Path(path).is_absolute():
        return f"`{ref}` is not a repo-relative path"
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        return f"`{ref}` resolves outside the repository"
    if want == "dir" and not target.is_dir():
        return f"`{path}` is not a directory in the repository"
    if want == "file" and not target.is_file():
        return f"`{path}` is not a file in the repository"
    is_line = re.fullmatch(r"[0-9]+", suffix) is not None
    if want == "file" and suffix and not is_line:
        defined = _defined_names(target)
        if defined is None:
            return f"`{ref}` names symbols, but `{path}` is not Python this index can parse"
        names = [name for name in re.split(r"[,\s]+", suffix) if name]
        if not names:
            return f"`{ref}` has a symbol suffix that names no symbol"
        missing = [name for name in names if name not in defined]
        if missing:
            return f"`{ref}` names {', '.join(missing)}, not defined in `{path}`"
    if want == "file" and is_line:
        lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        if not 1 <= int(suffix) <= lines:
            return f"`{ref}` names line {suffix}, but the file has {lines} lines"
    return None


def _defined_names(path: Path) -> set[str] | None:
    """Module-level def, class and assignment names in a Python file, read with
    `ast` so text in a docstring or comment never counts; None when unparseable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            # An annotation with no value binds nothing at runtime.
            names.add(node.target.id)
    return names


def _string_list(value) -> list:
    """The list a list field holds, or [] when it is some other type (its type
    error is already reported by the schema check)."""
    return value if isinstance(value, list) else []


def problems(entries: list[dict], root: Path = FORGE_ROOT, project: str = OWN_PROJECT) -> list[str]:
    """Every reason the index is not valid; empty when it is."""
    if not entries:
        return ["the index has no entries (a scan of zero is never green)"]
    found: list[str] = []
    seen: set[str] = set()
    for number, entry in enumerate(entries, 1):
        label = str(entry.get("id") or f"entry {number}")
        for name, kind in FIELDS.items():
            value = entry.get(name)
            if kind == "str" and not (isinstance(value, str) and value.strip()):
                found.append(f"{label}: `{name}` must be a non-empty string")
            if kind == "list" and not (
                    isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value)):
                found.append(f"{label}: `{name}` must be a non-empty list of strings")
        unknown = sorted(set(entry) - set(FIELDS))
        if unknown:
            found.append(f"{label}: unknown field(s) {', '.join(unknown)}")
        contract_id = entry.get("id")
        if not isinstance(contract_id, str):
            continue
        if not _ID.match(contract_id):
            found.append(f"{label}: `id` must look like <project>.<name>")
        elif contract_id.split(".", 1)[0] != project:
            found.append(f"{label}: `id` must start with `{project}.` in this repo's index")
        if contract_id in seen:
            found.append(f"{label}: duplicate id")
        seen.add(contract_id)
        if entry.get("status") not in STATUSES:
            found.append(f"{label}: `status` must be one of {', '.join(STATUSES)}")
        files = [entry.get("owner"), entry.get("authority"), entry.get("example_caller"),
                 *_string_list(entry.get("modules")), *_string_list(entry.get("guard"))]
        links = [(ref, "file") for ref in files] + [(ref, "dir") for ref in _string_list(entry.get("scan_roots"))]
        for ref, want in links:
            if isinstance(ref, str) and ref.strip():
                problem = link_problem(ref, root, want)
                if problem:
                    found.append(f"{label}: {problem}")
    return found


def project_checkout(project: str, manifest: Path | None = None) -> Path | None:
    """The local checkout of a project named in projects.toml, or None when the
    project is unknown or its checkout is absent."""
    manifest = manifest or FORGE_ROOT / "projects.toml"
    try:
        projects = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", [])
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for item in projects:
        if item.get("name") == project and item.get("path"):
            path = Path(item["path"]).expanduser()
            return path if path.is_dir() else None
    return None


def resolve(contract_id: str, own: list[dict], project: str = OWN_PROJECT,
            checkout=None) -> str:
    """`resolved`, `unresolved` or `unmeasured` for one contract id. Another
    project's id is read from its checkout's contracts.toml, and `unmeasured`
    when that checkout (or its index) cannot be read: it never reads as green."""
    namespace = contract_id.split(".", 1)[0]
    if namespace == project:
        return RESOLVED if any(e.get("id") == contract_id for e in own) else UNRESOLVED
    base = (checkout or project_checkout)(namespace)
    if base is None:
        return UNMEASURED
    for name in ("contracts.toml", "tools/registry/contracts.toml"):
        index = base / name
        if index.is_file():
            try:
                entries = _contract_list(tomllib.loads(index.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return UNMEASURED
            # A malformed or invalid foreign index cannot vouch for an id.
            if entries is None or problems(entries, base, namespace):
                return UNMEASURED
            return RESOLVED if any(e.get("id") == contract_id for e in entries) else UNRESOLVED
    return UNMEASURED


def render(entries: list[dict]) -> str:
    lines = [
        "# Contract index", "",
        "<!-- Generated by `python3 tools/registry/contracts.py render` from "
        "`tools/registry/contracts.toml` (harmonic-forge#1501). Do not edit by hand. -->", "",
        "Before adding or reimplementing a shared behavior, find its entry here, search its consumers, "
        "and reuse or extend the owner.", "",
    ]
    for entry in entries:
        lines += [f"## `{entry['id']}` ({entry['status']})", "", entry["scope"], "",
                  f"- **Owner:** {entry['owner']}",
                  f"- **Modules:** {', '.join(f'`{m}`' for m in entry['modules'])}",
                  f"- **Authority:** `{entry['authority']}`",
                  f"- **Example caller:** `{entry['example_caller']}`",
                  f"- **Guard and tests:** {', '.join(f'`{g}`' for g in entry['guard'])}",
                  f"- **Exceptions:** {entry['exceptions']}",
                  f"- **Scan roots:** {', '.join(f'`{r}`' for r in entry['scan_roots'])}",
                  f"- **Blind spots:** {entry['blind_spots']}", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", choices=("validate", "render", "resolve"))
    parser.add_argument("ids", nargs="*", help="resolve: contract ids to look up")
    parser.add_argument("--check", action="store_true", help="fail when docs/CONTRACTS.md is not a fresh render")
    parser.add_argument("--index", type=Path, default=TOML_PATH)
    parser.add_argument("--doc", type=Path, default=DOC_PATH)
    parser.add_argument("--root", type=Path, default=FORGE_ROOT)
    args = parser.parse_args(argv)
    if args.check and args.command:
        parser.error("--check runs alone; it does not combine with a command")
    entries = load(args.index)
    errors = problems(entries, args.root)
    if errors:
        print("\n".join(f"contracts: {e}" for e in errors), file=sys.stderr)
        return 1
    if args.command == "resolve":
        # Exit 0 only when every id resolves; 1 when one is unresolved; 3 when
        # the rest resolve but one is unmeasured, so unmeasured is never green.
        states = {cid: resolve(cid, entries) for cid in args.ids}
        for cid, state in states.items():
            print(f"{cid}: {state}")
        if not states or UNRESOLVED in states.values():
            return 1
        return 3 if UNMEASURED in states.values() else 0
    if args.check:
        current = args.doc.read_bytes() if args.doc.is_file() else None
        if current != render(entries).encode("utf-8"):
            print(f"contracts: {args.doc} differs from a fresh render; run `contracts.py render`", file=sys.stderr)
            return 1
        print(f"contracts: {len(entries)} entries valid; {args.doc.name} is fresh")
        return 0
    if args.command == "render":
        args.doc.write_bytes(render(entries).encode("utf-8"))
        print(f"contracts: wrote {args.doc} ({len(entries)} entries)")
        return 0
    if args.command == "validate":
        print(f"contracts: {len(entries)} entries valid")
        return 0
    parser.error("give a command (validate | render) or --check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
