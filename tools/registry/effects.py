#!/usr/bin/env python3
"""The effect registry (harmonic-forge#1290): load, validate, extend, render.

`tools/registry/effects.toml` declares every script under `tools/gh` and
`tools/lane`: each tool's modes with their effect class (decision 2), and for
shared state or above the precheck, the guard or the gap (decisions 3 and 6);
the libraries and data files; and the outside-command table (decision 4). A
consuming repo adds its own scripts in `<repo>/.claude/effects.toml`, which
may hold `[[tool]]` and `[[library]]` entries only (decision 7).

Nothing here allows or denies a call: harmonic-forge#1291's gate is the first
runtime reader. This module reads local files only and makes no external call.

    effects.py validate [--repo-root DIR]   schema, citations and declarations
    effects.py inventory                    tools/gh/_preflight.py's table
    effects.py gaps                         the decision 6 gap list
    effects.py layerset                     tools/precheck/layersets/registry-v1.json
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORGE_ROOT = HERE.parents[1]
BASE = HERE / "effects.toml"
EXTENSION = Path(".claude") / "effects.toml"
DESIGN_LAYER_SET = FORGE_ROOT / "tools" / "precheck" / "layersets" / "design-v1.json"
CLASSES_FILE = FORGE_ROOT / "tools" / "memory" / "failure_classes.json"
SCANNED_DIRS = ("tools/gh", "tools/lane")

sys.path.insert(0, str(FORGE_ROOT / "tools" / "hooks"))
import batch_auth  # noqa: E402
import deny_advisory_subagent_gh_writes as gh_hook  # noqa: E402

#: Decision 2's order. A mode that does two things takes the higher.
CLASSES = ("read", "local-write", "shared-state", "external-publish", "irreversible")
RANK = {name: index for index, name in enumerate(CLASSES)}
GATED = RANK["shared-state"]
GUARD_KINDS = ("hook", "operator-grant")
#: What a precheck's receipt key binds (harmonic-forge#1291 decision 4). An
#: empty list means #1291's 30-minute ceiling.
BINDS = ("head_sha", "origin_branch_sha", "origin_main_sha", "checks_file", "body_content", "args")
#: design-v1's `lint` and `provenance` layers bar these; the registry never claims them.
NEVER_CLAIMED = ("shell", "provenance")
READ_SHAPES = ("none", "gh-permitted", "flag-allowlist", "first-token")
RECOGNIZERS = {"classify_pr_merge": batch_auth.classify_pr_merge,
               "classify_issue_close": batch_auth.classify_issue_close}

TOP_KEYS = {"data", "tool", "library", "outside"}
EXTENSION_KEYS = {"tool", "library"}
TOOL_KEYS = {"path", "replay_tool", "wrapper", "alias_of", "mode", "note"}
MODE_KEYS = {"name", "match", "exact", "class", "cite", "note", "precheck", "guard_kind", "guard",
             "gap", "binds", "binds_cite", "duration_s", "guard_note", "validates"}
LIBRARY_KEYS = {"path", "sourced_by"}
OUTSIDE_KEYS = {"family", "programs", "match", "class", "read_shape", "flags", "first_token",
                "guard_kind", "guard", "gap", "guard_note", "replay_tool", "validates"}
OUTSIDE_MATCH_KEYS = {"any_token", "adjacent", "recognizer"}
VALIDATES_KEYS = {"class", "cite"}
CITE = re.compile(r"^[A-Za-z0-9_./-]+:(\d+)(?:-(\d+))?$")
MAIN_BLOCK = re.compile(r"^if __name__ == ['\"]__main__['\"]", re.MULTILINE)


class RegistryError(ValueError):
    """The registry, or an extension of it, refuses."""


def failure_classes() -> tuple[set[str], set[str]]:
    data = json.loads(CLASSES_FILE.read_text(encoding="utf-8"))
    return set(data["versions"][str(data["current"])]), set(data["interim_tools"])


# --------------------------------------------------------------------------
# Load and validate.

def _text(value, where: str, field: str) -> str:
    # Every text field renders into a markdown table cell.
    if not isinstance(value, str) or not value.strip() or "|" in value or "\n" in value:
        raise RegistryError(f"{where}: {field} must be a non-empty one-line string with no '|'")
    return value


def _keys(table, allowed: set[str], where: str) -> dict:
    if not isinstance(table, dict):
        raise RegistryError(f"{where} must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise RegistryError(f"{where}: unknown key(s) {sorted(unknown)}")
    return table


def _cite(value, where: str, roots: list[Path]) -> str:
    match = CITE.match(value) if isinstance(value, str) else None
    if not match:
        raise RegistryError(f"{where}: citation {value!r} is not path:line or path:line-line")
    path = value.split(":", 1)[0]
    last = int(match.group(2) or match.group(1))
    for root in roots:
        target = root / path
        if target.is_file():
            if int(match.group(1)) < 1 or last > _line_count(target):
                raise RegistryError(f"{where}: citation {value} is past the end of {path}")
            return value
    raise RegistryError(f"{where}: citation {value} names no file")


@functools.lru_cache(maxsize=None)
def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _replay_tools(entry: dict, where: str, interim: set[str]) -> list[str]:
    value = entry.get("replay_tool")
    if value is None:
        return []
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise RegistryError(f"{where}: replay_tool must be a name or a list of names")
    unknown = [n for n in names if n not in interim]
    if unknown:
        raise RegistryError(f"{where}: replay_tool {unknown} is not in failure_classes.json interim_tools")
    return names


def _validates(entry: dict, where: str, classes: set[str], roots: list[Path]) -> list[dict]:
    value = entry.get("validates", [])
    if not isinstance(value, list):
        raise RegistryError(f"{where}: validates must be a list")
    out = []
    for item in value:
        _keys(item, VALIDATES_KEYS, f"{where} validates")
        klass = item.get("class")
        if klass not in classes or klass in NEVER_CLAIMED:
            raise RegistryError(f"{where}: validates class {klass!r} is unknown or never claimed here")
        if "cite" not in item:
            raise RegistryError(f"{where}: validates {klass} has no citation")
        out.append({"class": klass, "cite": _cite(item["cite"], f"{where} validates", roots)})
    return out


def _guard_fields(entry: dict, where: str) -> None:
    if ("guard_kind" in entry) != ("guard" in entry):
        raise RegistryError(f"{where}: guard_kind and guard come together")
    if "guard_kind" in entry:
        if entry["guard_kind"] not in GUARD_KINDS:
            raise RegistryError(f"{where}: unknown guard_kind {entry['guard_kind']!r}")
        _text(entry["guard"], where, "guard")


def _mode(raw, where: str, classes: set[str], roots: list[Path]) -> dict:
    mode = dict(_keys(raw, MODE_KEYS, where))
    _text(mode.get("name"), where, "name")
    where = f"{where} ({mode['name']})"
    match = mode.get("match")
    if not isinstance(match, list) or not all(isinstance(t, str) and t for t in match):
        raise RegistryError(f"{where}: match must be a list of tokens")
    if not isinstance(mode.get("exact", False), bool):
        raise RegistryError(f"{where}: exact must be true or false")
    if mode.get("class") not in RANK:
        raise RegistryError(f"{where}: unknown class {mode.get('class')!r}")
    cites = mode.get("cite")
    if not isinstance(cites, list) or not cites:
        raise RegistryError(f"{where}: needs at least one citation")
    for cite in cites:
        _cite(cite, where, roots)
    for field in ("note", "gap", "guard_note", "precheck"):
        if field in mode:
            _text(mode[field], where, field)
    _guard_fields(mode, where)
    has_gap, has_cover = "gap" in mode, "precheck" in mode or "guard" in mode
    if RANK[mode["class"]] >= GATED:
        # Decision 6: shared state or above names its precheck, its guard, or the gap.
        if has_gap == has_cover:
            raise RegistryError(f"{where}: shared state or above needs exactly one of precheck/guard or gap")
    elif has_gap or has_cover:
        raise RegistryError(f"{where}: a {mode['class']} mode takes no precheck, guard or gap")
    if "precheck" in mode:
        # Receipt binding: required, never defaulted.
        if "binds" not in mode:
            raise RegistryError(f"{where}: a precheck mode needs binds (an empty list is the 30-minute ceiling)")
        binds = mode["binds"]
        if not isinstance(binds, list) or any(b not in BINDS for b in binds):
            raise RegistryError(f"{where}: binds must be a list from {BINDS}")
        if binds and "binds_cite" not in mode:
            raise RegistryError(f"{where}: a non-empty binds needs binds_cite")
        if "binds_cite" in mode:
            _cite(mode["binds_cite"], where, roots)
    elif "binds" in mode or "binds_cite" in mode:
        raise RegistryError(f"{where}: binds belongs to a precheck mode")
    if "duration_s" in mode and (not isinstance(mode["duration_s"], int) or mode["duration_s"] <= 0):
        raise RegistryError(f"{where}: duration_s must be a positive integer")
    mode["validates"] = _validates(mode, where, classes, roots)
    if mode["validates"] and has_gap:
        raise RegistryError(f"{where}: a gap validates nothing")
    return mode


def _tool(raw, where: str, classes: set[str], interim: set[str], roots: list[Path]) -> dict:
    tool = dict(_keys(raw, TOOL_KEYS, where))
    path = tool.get("path")
    if not isinstance(path, str) or not path:
        raise RegistryError(f"{where}: needs a path")
    where = f"tool {path}"
    tool["replay_tools"] = _replay_tools(tool, where, interim)
    if not isinstance(tool.get("wrapper", False), bool):
        raise RegistryError(f"{where}: wrapper must be true or false")
    if "alias_of" in tool:
        if "mode" in tool or tool["replay_tools"]:
            raise RegistryError(f"{where}: an alias has no modes or replay_tool of its own")
        tool["modes"] = []
        return tool
    raw_modes = tool.pop("mode", None)
    if not isinstance(raw_modes, list) or not raw_modes:
        raise RegistryError(f"{where}: needs at least one mode")
    tool["modes"] = [_mode(m, where, classes, roots) for m in raw_modes]
    names = [m["name"] for m in tool["modes"]]
    if len(set(names)) != len(names):
        raise RegistryError(f"{where}: a mode name appears twice")
    for index, mode in enumerate(tool["modes"]):
        if not mode["match"] and index != len(tool["modes"]) - 1:
            raise RegistryError(f"{where}: mode {mode['name']!r} matches every call, so it must be last")
        if "precheck" in mode and (mode["precheck"] == mode["name"] or mode["precheck"] not in names):
            raise RegistryError(f"{where}: precheck {mode['precheck']!r} names no other mode of this tool")
    return tool


def _library(raw, where: str, roots: list[Path]) -> dict:
    library = dict(_keys(raw, LIBRARY_KEYS, where))
    path = library.get("path")
    if not isinstance(path, str) or not path:
        raise RegistryError(f"{where}: needs a path")
    if path.endswith(".sh"):
        if "sourced_by" not in library:
            raise RegistryError(f"library {path}: a .sh library names one source site (sourced_by)")
        _cite(library["sourced_by"], f"library {path}", roots)
    elif "sourced_by" in library:
        raise RegistryError(f"library {path}: sourced_by is for .sh libraries")
    return library


def _outside(raw, where: str, classes: set[str], interim: set[str], roots: list[Path]) -> dict:
    entry = dict(_keys(raw, OUTSIDE_KEYS, where))
    _text(entry.get("family"), where, "family")
    where = f"outside {entry['family']}"
    programs = entry.get("programs")
    if not isinstance(programs, list) or not programs or not all(isinstance(p, str) and p for p in programs):
        raise RegistryError(f"{where}: needs programs")
    if entry.get("class") not in RANK or RANK[entry["class"]] < GATED:
        raise RegistryError(f"{where}: an outside family's class is shared state or above")
    if "match" in entry:
        match = _keys(entry["match"], OUTSIDE_MATCH_KEYS, f"{where} match")
        if not match:
            raise RegistryError(f"{where}: an empty match")
        if "any_token" in match and not isinstance(match["any_token"], str):
            raise RegistryError(f"{where}: any_token is one token")
        if "adjacent" in match and (not isinstance(match["adjacent"], list) or len(match["adjacent"]) != 2):
            raise RegistryError(f"{where}: adjacent is a pair of tokens")
        if "recognizer" in match and match["recognizer"] not in RECOGNIZERS:
            raise RegistryError(f"{where}: unknown recognizer {match['recognizer']!r}")
    shape = entry.get("read_shape")
    if shape not in READ_SHAPES:
        raise RegistryError(f"{where}: unknown read_shape {shape!r}")
    if shape == "flag-allowlist":
        flags = entry.get("flags")
        if not isinstance(flags, dict) or set(flags) != set(programs):
            raise RegistryError(f"{where}: flag-allowlist needs flags for each program")
        for program, lists in flags.items():
            _keys(lists, {"no_value", "with_value"}, f"{where} flags.{program}")
            if not all(isinstance(lists.get(k), list) for k in ("no_value", "with_value")):
                raise RegistryError(f"{where}: flags.{program} needs no_value and with_value lists")
    elif "flags" in entry:
        raise RegistryError(f"{where}: flags belong to flag-allowlist")
    if shape == "first-token":
        if not isinstance(entry.get("first_token"), list) or not entry["first_token"]:
            raise RegistryError(f"{where}: first-token needs its list")
    elif "first_token" in entry:
        raise RegistryError(f"{where}: first_token belongs to first-token")
    for field in ("gap", "guard_note"):
        if field in entry:
            _text(entry[field], where, field)
    _guard_fields(entry, where)
    if ("gap" in entry) == ("guard" in entry):
        raise RegistryError(f"{where}: needs exactly one of guard or gap")
    entry["replay_tools"] = _replay_tools(entry, where, interim)
    entry["validates"] = _validates(entry, where, classes, roots)
    if entry["validates"] and "gap" in entry:
        raise RegistryError(f"{where}: a gap validates nothing")
    return entry


def _parse(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"{path} is unreadable: {exc}") from exc


def load(repo_root: Path | str | None = None, *, base: Path = BASE, forge_root: Path = FORGE_ROOT) -> dict:
    """The forge base, plus `<repo_root>/.claude/effects.toml` when present.
    An extension adds `[[tool]]` and `[[library]]` entries; redefining a forge
    path, or any other table, refuses."""
    classes, interim = failure_classes()
    data = _parse(Path(base))
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise RegistryError(f"{base}: unknown table(s) {sorted(unknown)}")
    roots = [Path(forge_root)]
    registry = {
        "forge_root": str(forge_root),
        "data": list(data.get("data", [])),
        "tools": [dict(_tool(t, "tool", classes, interim, roots), root=str(forge_root))
                  for t in data.get("tool", [])],
        "libraries": [dict(_library(lib, "library", roots), root=str(forge_root))
                      for lib in data.get("library", [])],
        "outside": [_outside(o, "outside", classes, interim, roots) for o in data.get("outside", [])],
    }
    if not all(isinstance(p, str) and p for p in registry["data"]):
        raise RegistryError(f"{base}: data is a list of paths")
    if repo_root is not None:
        ext_path = Path(repo_root) / EXTENSION
        if ext_path.is_file():
            ext = _parse(ext_path)
            extra = set(ext) - EXTENSION_KEYS
            if extra:
                raise RegistryError(f"{ext_path}: an extension holds [[tool]] and [[library]] only, "
                                    f"not {sorted(extra)}")
            ext_roots = [Path(repo_root), Path(forge_root)]
            registry["tools"] += [dict(_tool(t, "tool", classes, interim, ext_roots), root=str(repo_root))
                                  for t in ext.get("tool", [])]
            registry["libraries"] += [dict(_library(lib, "library", ext_roots), root=str(repo_root))
                                      for lib in ext.get("library", [])]
    _check_unique(registry)
    return registry


def _check_unique(registry: dict) -> None:
    seen: set[str] = set()
    for path in ([t["path"] for t in registry["tools"]] + [lib["path"] for lib in registry["libraries"]]
                 + registry["data"]):
        if path in seen:
            raise RegistryError(f"{path} is declared twice (an extension may not redefine a forge entry)")
        seen.add(path)
    tools = {t["path"] for t in registry["tools"]}
    for tool in registry["tools"]:
        if "alias_of" in tool and (tool["alias_of"] not in tools or tool["alias_of"] == tool["path"]):
            raise RegistryError(f"tool {tool['path']}: alias_of names no other tool")


# --------------------------------------------------------------------------
# Declarations (decision 4): every script is a tool, a library or data.

def _scripts(root: Path, dirs=SCANNED_DIRS) -> list[str]:
    found = []
    for directory in dirs:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(root)
            parts = rel.parts
            if (not path.is_file() or any(p.startswith(".") or p in ("__pycache__", "testdata") for p in parts)
                    or path.name.startswith("test_") or path.name in ("README.md", "conftest.py")
                    or path.suffix == ".pyc"):
                continue
            found.append(rel.as_posix())
    return found


def declaration_problems(registry: dict, root: Path = FORGE_ROOT, dirs=SCANNED_DIRS) -> list[str]:
    """Each undeclared script, each declared path that is missing, and each
    library that is really a tool (a `__main__` block, or extensionless)."""
    root = Path(root)
    data = set(registry["data"]) if str(root) == registry["forge_root"] else set()
    declared = ({t["path"] for t in registry["tools"] if t["root"] == str(root)}
                | {lib["path"] for lib in registry["libraries"] if lib["root"] == str(root)}
                | data)
    problems = [f"undeclared script: {p}" for p in _scripts(root, dirs) if p not in declared]
    for entry in registry["tools"] + registry["libraries"]:
        if entry["root"] == str(root) and not (root / entry["path"]).is_file():
            problems.append(f"declared but missing: {entry['path']}")
    for path in sorted(data):
        if not (root / path).is_file():
            problems.append(f"declared but missing: {path}")
    for library in registry["libraries"]:
        if library["root"] != str(root):
            continue
        path = root / library["path"]
        if not path.suffix:
            problems.append(f"library {library['path']} is extensionless, so it is run as a command: a tool")
        elif path.suffix == ".py" and path.is_file() and MAIN_BLOCK.search(path.read_text(encoding="utf-8")):
            problems.append(f"library {library['path']} has a __main__ block: a tool")
    return problems


# --------------------------------------------------------------------------
# Modes (decision 3).

def _token_matches(want: str, argv: list[str]) -> bool:
    return any(a == want or (want.startswith("--") and a.startswith(want + "=")) for a in argv)


def find_tool(registry: dict, path: str) -> dict:
    for tool in registry["tools"]:
        if tool["path"] == path:
            return next(t for t in registry["tools"] if t["path"] == tool["alias_of"]) \
                if "alias_of" in tool else tool
    raise KeyError(path)


def resolve_mode(registry: dict, path: str, argv: list[str]) -> dict:
    """The first mode whose match holds; a call no mode matches resolves to the
    tool's highest-class mode (decision 3), wrappers included."""
    tool = find_tool(registry, path)
    for mode in tool["modes"]:
        if mode.get("exact"):
            if list(argv) == mode["match"]:
                return mode
        elif all(_token_matches(t, argv) for t in mode["match"]):
            return mode
    top = max(RANK[m["class"]] for m in tool["modes"])
    return next(m for m in tool["modes"] if RANK[m["class"]] == top)


# --------------------------------------------------------------------------
# Outside commands (decision 4), fail-closed.

@dataclasses.dataclass(frozen=True)
class Outside:
    family: str
    klass: str
    read: bool
    guard_kind: str | None
    guard: str | None
    gap: str | None


def _verdict(entry: dict, read: bool) -> Outside:
    return Outside(entry["family"], "read" if read else entry["class"], read,
                   entry.get("guard_kind"), entry.get("guard"), entry.get("gap"))


def _strip(tokens: list[str]) -> list[str]:
    while True:
        tokens = gh_hook._resolve_wrapper_prefix(list(tokens))
        if tokens and Path(tokens[0]).name == "gha":
            tokens = ["gh", *tokens[2:]] if len(tokens) >= 2 else []
            continue
        return tokens


def _family_matches(entry: dict, tokens: list[str]) -> bool:
    match = entry.get("match")
    if match is None:
        return True
    rest = tokens[1:]
    if "any_token" in match and match["any_token"] in rest:
        return True
    pair = match.get("adjacent")
    if pair and any(rest[i:i + 2] == pair for i in range(len(rest) - 1)):
        return True
    recognizer = match.get("recognizer")
    return bool(recognizer and RECOGNIZERS[recognizer](list(tokens)) is not None)


def _flag_read(args: list[str], flags: dict) -> bool:
    no_value, with_value = set(flags["no_value"]), set(flags["with_value"])
    positional, index = 0, 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            positional += 1
        elif token in no_value:
            pass
        elif token in with_value:
            if index + 1 >= len(args):
                return False
            index += 1
        elif token.startswith("--") and "=" in token and token.split("=", 1)[0] in with_value:
            pass
        elif (not token.startswith("--") and len(token) > 2
              and all(f"-{letter}" in no_value for letter in token[1:])):
            pass  # a short cluster of listed no-value flags only
        else:
            return False  # any flag off the list, known to the program or not
        index += 1
    return positional == 1


def _is_read(entry: dict, tokens: list[str]) -> bool:
    shape, program = entry["read_shape"], Path(tokens[0]).name
    if shape == "gh-permitted":
        return gh_hook._gh_permitted(tokens[1:])
    if shape == "flag-allowlist":
        return _flag_read(tokens[1:], entry["flags"][program])
    if shape == "first-token":
        return len(tokens) > 1 and tokens[1] in entry["first_token"]
    return False


def _write_entry(registry: dict, program: str) -> dict:
    """The family a program's name inside a shell-escape token falls to: the
    program's highest-class family, so `bash -c '... gh ...'` is never classed
    below a merge it may hold (fail-closed; the cost is that every gh call
    inside a shell escape is classed as `gh pr merge`)."""
    entries = [e for e in registry["outside"] if program in e["programs"]]
    top = max(RANK[e["class"]] for e in entries)
    return next(e for e in entries if RANK[e["class"]] == top)


def classify_outside(argv: list[str], registry: dict | None = None) -> Outside | None:
    """Class one command segment's tokens, or None when no family applies
    (ungated). Never reads a family from a token's position; reads no grant."""
    registry = registry if registry is not None else load()
    tokens = _strip(list(argv))
    if not tokens:
        return None
    program = Path(tokens[0]).name
    if program in gh_hook._SHELL_ESCAPE_COMMANDS:
        candidates: list[Outside] = []
        inner = [tokens[1:]]
        for token in tokens[1:]:
            try:
                inner.append(shlex.split(token))
            except ValueError:
                pass
        for segment in inner:
            found = classify_outside(segment, registry) if segment else None
            if found is not None:
                candidates.append(found)
        programs = {p for e in registry["outside"] for p in e["programs"]}
        for name in sorted(programs):
            if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", t) for t in tokens[1:]):
                candidates.append(_verdict(_write_entry(registry, name), read=False))
        if not candidates:
            return None
        top = max(RANK[c.klass] for c in candidates)
        return next(c for c in candidates if RANK[c.klass] == top)
    for entry in registry["outside"]:
        if program in entry["programs"] and _family_matches(entry, tokens):
            return _verdict(entry, _is_read(entry, tokens))
    return None


# --------------------------------------------------------------------------
# Renderings.

def _cover(entry: dict) -> str:
    parts = []
    if "precheck" in entry:
        parts.append(f"precheck: `{entry['precheck']}`")
    if "guard" in entry:
        parts.append(f"guard ({entry['guard_kind'].replace('-', ' ')}): {entry['guard']}")
    if "gap" in entry:
        parts.append(f"gap: {entry['gap']}")
    return "; ".join(parts)


def _gated_rows(registry: dict):
    for tool in registry["tools"]:
        for mode in tool["modes"]:
            if RANK[mode["class"]] >= GATED:
                yield f"`{Path(tool['path']).name}`", mode["name"], mode
    for entry in registry["outside"]:
        yield f"`{entry['family']}`", "outside command", entry


def inventory(registry: dict | None = None) -> str:
    """tools/gh/_preflight.py's table: every shared-state-or-above mode and
    outside family, with its precheck, guard or gap."""
    registry = registry if registry is not None else load()
    lines = ["| Tool | Mode | Class | Precheck, guard or gap |", "|---|---|---|---|"]
    for name, mode_name, entry in _gated_rows(registry):
        lines.append(f"| {name} | {mode_name} | {entry['class']} | {_cover(entry)} |")
    return "\n".join(lines)


def gaps(registry: dict | None = None) -> str:
    """The decision 6 gap list: each gated mode and family with neither a
    precheck nor a guard."""
    registry = registry if registry is not None else load()
    lines = ["| Tool or family | Mode | Class | Gap |", "|---|---|---|---|"]
    for name, mode_name, entry in _gated_rows(registry):
        if "gap" in entry:
            lines.append(f"| {name} | {mode_name} | {entry['class']} | {entry['gap']} |")
    return "\n".join(lines)


def claims(registry: dict) -> list[tuple[str, str, str]]:
    """`(replay_tool, class, cite)` for every cited validates class."""
    out = []
    entries = [m | {"replay_tools": t["replay_tools"]} for t in registry["tools"] for m in t["modes"]]
    for entry in entries + registry["outside"]:
        for tool in entry["replay_tools"]:
            for item in entry["validates"]:
                out.append((tool, item["class"], item["cite"]))
    return out


def layerset(registry: dict | None = None, design: Path = DESIGN_LAYER_SET) -> dict:
    """One `registry` layer: validated claims only, design-v1's gate bar verbatim."""
    registry = registry if registry is not None else load()
    gate = next(layer for layer in json.loads(design.read_text(encoding="utf-8"))["layers"]
                if layer["name"] == "gate")
    claimed = sorted({f"{tool}|{klass}" for tool, klass, cite in claims(registry)
                      if cite and klass not in NEVER_CLAIMED})
    return {"name": "registry", "claims": claimed, "claims_basis": "validated", "module": None,
            "bar": gate["bar"]}


def layer_set_document(registry: dict | None = None, design: Path = DESIGN_LAYER_SET) -> str:
    """registry-v1.json: design-v1 with its gate layer replaced, at the same
    index, by the registry layer."""
    document = json.loads(design.read_text(encoding="utf-8"))
    index = next(i for i, layer in enumerate(document["layers"]) if layer["name"] == "gate")
    document["layers"][index] = layerset(registry, design)
    return json.dumps(document, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="schema, citations and declarations")
    check.add_argument("--repo-root", type=Path, default=None,
                       help="a consuming repo whose .claude/effects.toml is merged")
    sub.add_parser("inventory", help="print tools/gh/_preflight.py's table")
    sub.add_parser("gaps", help="print the decision 6 gap list")
    sub.add_parser("layerset", help="print registry-v1.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            registry = load(args.repo_root)
            problems = declaration_problems(registry)
            if args.repo_root is not None and Path(args.repo_root).resolve() != FORGE_ROOT:
                problems += declaration_problems(registry, args.repo_root, dirs=())
            for problem in problems:
                print(f"effects: {problem}", file=sys.stderr)
            if problems:
                return 1
            tools = sum(1 for t in registry["tools"] if "alias_of" not in t)
            modes = sum(len(t["modes"]) for t in registry["tools"])
            print(f"effects: valid -- {tools} tools, {modes} modes, {len(registry['libraries'])} libraries, "
                  f"{len(registry['data'])} data files, {len(registry['outside'])} outside families")
        elif args.command == "inventory":
            print(inventory())
        elif args.command == "gaps":
            print(gaps())
        else:
            sys.stdout.write(layer_set_document())
    except RegistryError as exc:
        print(f"effects: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
