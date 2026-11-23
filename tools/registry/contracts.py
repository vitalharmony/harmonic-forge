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
    contracts.py --check     fail when docs/CONTRACTS.md differs from a fresh render

A contract id is `<project>.<name>`, `<project>` being a `name` in projects.toml.
An id in another project's namespace resolves where that checkout exists and
reports `unmeasured` otherwise, never green.
"""
from __future__ import annotations

import argparse
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


def load(path: Path = TOML_PATH) -> list[dict]:
    try:
        return list(tomllib.loads(path.read_text(encoding="utf-8")).get("contract", []))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"contracts: cannot read {path}: {exc}") from exc


def _path_part(ref: str) -> str:
    """`path` from `path`, `path:line` or `path:symbol`."""
    return ref.split(":", 1)[0].strip()


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
        refs = [entry.get("authority"), entry.get("example_caller"), *entry.get("modules", []),
                *entry.get("guard", []), *entry.get("scan_roots", [])]
        for ref in refs:
            if isinstance(ref, str) and ref.strip() and not (root / _path_part(ref)).exists():
                found.append(f"{label}: `{_path_part(ref)}` does not exist")
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
            checkout=project_checkout) -> str:
    """`resolved`, `unresolved` or `unmeasured` for one contract id. Another
    project's id is read from its checkout's contracts.toml, and `unmeasured`
    when that checkout (or its index) cannot be read: it never reads as green."""
    namespace = contract_id.split(".", 1)[0]
    if namespace == project:
        return RESOLVED if any(e.get("id") == contract_id for e in own) else UNRESOLVED
    base = checkout(namespace)
    if base is None:
        return UNMEASURED
    for name in ("contracts.toml", "tools/registry/contracts.toml"):
        index = base / name
        if index.is_file():
            try:
                entries = tomllib.loads(index.read_text(encoding="utf-8")).get("contract", [])
            except (OSError, tomllib.TOMLDecodeError):
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
    parser.add_argument("command", nargs="?", choices=("validate", "render"))
    parser.add_argument("--check", action="store_true", help="fail when docs/CONTRACTS.md is not a fresh render")
    parser.add_argument("--index", type=Path, default=TOML_PATH)
    parser.add_argument("--doc", type=Path, default=DOC_PATH)
    parser.add_argument("--root", type=Path, default=FORGE_ROOT)
    args = parser.parse_args(argv)
    entries = load(args.index)
    errors = problems(entries, args.root)
    if errors:
        print("\n".join(f"contracts: {e}" for e in errors), file=sys.stderr)
        return 1
    if args.check:
        current = args.doc.read_text(encoding="utf-8") if args.doc.is_file() else None
        if current != render(entries):
            print(f"contracts: {args.doc} differs from a fresh render; run `contracts.py render`", file=sys.stderr)
            return 1
        print(f"contracts: {len(entries)} entries valid; {args.doc.name} is fresh")
        return 0
    if args.command == "render":
        args.doc.write_text(render(entries), encoding="utf-8")
        print(f"contracts: wrote {args.doc} ({len(entries)} entries)")
        return 0
    if args.command == "validate":
        print(f"contracts: {len(entries)} entries valid")
        return 0
    parser.error("give a command (validate | render) or --check")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
