#!/usr/bin/env python3
"""Memory-store lint (harmonic-forge#494), ported from `HRSE2/scripts/memory_lint.py`.

WHY THIS MOVED OUT OF HRSE2
---------------------------
The original derived the store path from its own repo root:

    repo_root = Path(__file__).parent.parent.resolve()
    encoded = str(repo_root).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"

Since `autoMemoryDirectory` landed (harmonic-forge#493 Phase 0) every repo
resolves to one shared store, so that derivation names a directory nothing
reads or writes. The lint ran, reported, and exited 0 against an inert copy.

EXIT-CODE CONTRACT
------------------
Default invocation is REPORT-ONLY and exits 0. `--gate` is what turns the cap
check and Checks 1/4/5 into exit-1 conditions.

That split is the whole design, and the reason is worth stating: the store is
grown by the harness, not by any branch. A live-pinned exit-1 check is one that
no commit can turn green — `MEMORY.md` rises on its own — so `--gate` runs
against a frozen fixture and the live store is reported on but never gated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

#: `MEMORY.md` is the index, not a memory; `README.md` documents the store.
#: Neither carries memory frontmatter and neither is linked from the index, so
#: without this every check that iterates memories reports both. harmonic-forge#493's
#: own `README.md` is why the gate would otherwise be red the day it lands.
NON_MEMORY_FILES = frozenset({"MEMORY.md", "README.md"})

#: `MEMORY.md` is loaded into every session's context, so its size is a real
#: budget rather than a style preference.
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
WARN_FRACTION = 0.80


# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def resolve_store(repo_root: Path | None = None) -> Path:
    """`autoMemoryDirectory` if set and present, else the legacy repo-derived path.

    Only the one key is read. The rest of `settings.json` is the operator's and
    must never be printed — see the module's callers, which print the resolved
    path and nothing else from this file.

    Project-level settings files are deliberately NOT consulted. The harness
    itself may merge them; if it ever does so for this key, this resolver will
    disagree with the harness and that disagreement will be silent. It is
    recorded here rather than guarded because no project-level file sets the key
    today, and a guard against a hypothetical is a guard nobody can test.
    """
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        configured = data.get("autoMemoryDirectory")
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError, ValueError):
        configured = None
    if configured:
        candidate = Path(str(configured)).expanduser()
        if candidate.is_dir():
            return candidate
    root = (repo_root or Path(__file__).resolve().parent.parent.parent)
    encoded = str(root).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


def memory_files(store: Path) -> list[Path]:
    return sorted(f for f in store.glob("*.md") if f.name not in NON_MEMORY_FILES)


# ---------------------------------------------------------------------------
# Frontmatter parsing (no PyYAML)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return result
    in_metadata_block = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if stripped == "metadata:":
            in_metadata_block = True
            continue
        if in_metadata_block:
            m = re.match(r"\s+type:\s*(.+)", line)
            if m:
                result["metadata.type"] = m.group(1).strip()
            continue
        m = re.match(r"^(name|description):\s*(.+)", stripped)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


def _body_text(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def _memory_links(text: str) -> list[str]:
    return re.findall(r"\(([^)]+\.md)\)", text)


def _index_text(store: Path) -> str:
    try:
        return (store / "MEMORY.md").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Check 1 — orphan files
# ---------------------------------------------------------------------------

def check_orphans(store: Path) -> list[str]:
    linked = set(_memory_links(_index_text(store)))
    return [f"{f.name}: not linked in MEMORY.md (orphan)"
            for f in memory_files(store) if f.name not in linked]


# ---------------------------------------------------------------------------
# Check 2 — dead MEMORY.md links
# ---------------------------------------------------------------------------

def check_dead_links(store: Path) -> list[str]:
    return [f"MEMORY.md → ({t}): file does not exist"
            for t in _memory_links(_index_text(store)) if not (store / t).exists()]


# ---------------------------------------------------------------------------
# Check 3 — stale project memories
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_STALE_KEYWORDS = re.compile(
    r"merge freeze|scheduled for|due|by \d{4}-\d{2}-\d{2}|deadline|expires", re.IGNORECASE)


def check_stale_projects(store: Path) -> list[str]:
    issues: list[str] = []
    today = date.today()
    for f in memory_files(store):
        text = f.read_text(encoding="utf-8")
        if _parse_frontmatter(text).get("metadata.type") != "project":
            continue
        body = _body_text(text)
        for m in _DATE_RE.finditer(body):
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if d < today:
                context = body[max(0, m.start() - 120):min(len(body), m.end() + 120)]
                if _STALE_KEYWORDS.search(context):
                    issues.append(f"{f.name}: date {m.group(1)} is in the past (possible stale deadline)")
                    break
    return issues


# ---------------------------------------------------------------------------
# Check 4 — broken [[slug]] references
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")

#: `[[hrse#1303]]` is a GitHub issue reference, not a memory link. Live example:
#: `user_ethical_employer_exclusions.md`. Reported in its own category and never
#: an exit-1 finding — a gate carrying a permanently unfixable finding is a gate
#: people learn to ignore.
_ISSUE_REF_RE = re.compile(r"^[\w.-]+#\d+$")


def _slug(s: str) -> str:
    """Normalize the two conventions the store actually uses.

    Filenames are `feedback_foo_bar.md`; the harness instructs sessions to write
    `name: <short-kebab-case-slug>`, so `name:` values are `feedback-foo-bar` —
    and sometimes a shortened variant of the stem. Collapsing `-` to `_` makes
    the two comparable.
    """
    return s.strip().lower().replace("-", "_")


def build_slug_index(store: Path) -> tuple[set[str], list[str]]:
    """`(known keys, collision findings)`.

    **This is an existence test against a set union, not a resolution to a
    unique file.** It never selects a file, so it cannot select the wrong one —
    which is what makes normalizing safe here and would not make it safe in a
    resolver. The collision guard exists anyway because the shortened-`name:`
    convention makes a genuine collision reachable, and a silent one would
    quietly weaken the check.
    """
    known: set[str] = set()
    owners: dict[str, list[str]] = {}
    for f in memory_files(store):
        fm = _parse_frontmatter(f.read_text(encoding="utf-8"))
        keys = {f.stem, _slug(f.stem)}
        if "name" in fm:
            keys.add(_slug(fm["name"]))
        known |= keys
        owners.setdefault(_slug(f.stem), []).append(f.name)
    collisions = [
        f"{' and '.join(sorted(names))}: normalize to the same key {key!r}"
        for key, names in sorted(owners.items()) if len(names) > 1
    ]
    return known, collisions


def check_broken_slugs(store: Path) -> tuple[list[str], list[str]]:
    """`(broken links, issue references)` — the second is report-only."""
    known, collisions = build_slug_index(store)
    broken: list[str] = list(collisions)
    issue_refs: list[str] = []
    for f in memory_files(store):
        for slug in _SLUG_RE.findall(_body_text(f.read_text(encoding="utf-8"))):
            if _ISSUE_REF_RE.match(slug.strip()):
                issue_refs.append(f"{f.name}: [[{slug}]] is an issue reference, not a memory link")
                continue
            if slug.strip() not in known and _slug(slug) not in known:
                broken.append(f"{f.name}: [[{slug}]] resolves to no memory file")
    return broken, issue_refs


# ---------------------------------------------------------------------------
# Check 5 — missing frontmatter
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("name", "description", "metadata.type")


def check_missing_frontmatter(store: Path) -> list[str]:
    issues: list[str] = []
    for f in memory_files(store):
        fm = _parse_frontmatter(f.read_text(encoding="utf-8"))
        missing = [x for x in _REQUIRED_FIELDS if x not in fm]
        if missing:
            issues.append(f"{f.name}: missing frontmatter field(s): {', '.join(missing)}")
    return issues


# ---------------------------------------------------------------------------
# Check 6 — index load cap
# ---------------------------------------------------------------------------

def index_size(store: Path) -> tuple[int, int]:
    text = _index_text(store)
    return len(text.splitlines()), len(text.encode("utf-8"))


def check_index_cap(store: Path) -> tuple[list[str], list[str]]:
    """`(over-cap findings, warnings)`. Over cap is exit-1 only under `--gate`."""
    lines, size = index_size(store)
    findings: list[str] = []
    warnings: list[str] = []
    if lines > MAX_INDEX_LINES:
        findings.append(f"MEMORY.md: {lines} lines exceeds the {MAX_INDEX_LINES}-line load cap")
    elif lines >= MAX_INDEX_LINES * WARN_FRACTION:
        warnings.append(f"MEMORY.md: {lines}/{MAX_INDEX_LINES} lines ({lines / MAX_INDEX_LINES:.0%})")
    if size > MAX_INDEX_BYTES:
        findings.append(f"MEMORY.md: {size} bytes exceeds the {MAX_INDEX_BYTES}-byte load cap")
    elif size >= MAX_INDEX_BYTES * WARN_FRACTION:
        warnings.append(f"MEMORY.md: {size}/{MAX_INDEX_BYTES} bytes ({size / MAX_INDEX_BYTES:.0%})")
    return findings, warnings


# ---------------------------------------------------------------------------
# Check 7 — staleness (report-only, never exit 1)
# ---------------------------------------------------------------------------

_MISE_TASK_RE = re.compile(r"mise run ([a-z0-9][\w-]*)")
_SCRIPT_PATH_RE = re.compile(r"\b((?:scripts|tools)/[\w/-]+\.py)\b")
_RULE_ID_RE = re.compile(r"\bR-\d{4}\b")
#: Both deleted from the boards in hrse#966.
_DEAD_BOARD_FIELDS = ("Estimate", "Priority")


def _repo_roots() -> list[Path]:
    forge = Path(__file__).resolve().parent.parent.parent
    return [forge, Path.home() / "Harmonic_Projects" / "HRSE2"]


def _corpus() -> tuple[str, set[str], list[Path]]:
    roots = _repo_roots()
    mise = "\n".join(
        (r / "mise.toml").read_text(encoding="utf-8", errors="replace")
        for r in roots if (r / "mise.toml").is_file()
    )
    registries = [Path.home() / "harmonic-forge/tools/rules/registry.toml",
                  Path.home() / "Harmonic_Projects/HRSE2/.claude/rules/registry.toml"]
    rule_ids: set[str] = set()
    for reg in registries:
        if reg.is_file():
            rule_ids |= set(_RULE_ID_RE.findall(reg.read_text(encoding="utf-8", errors="replace")))
    return mise, rule_ids, roots


def check_staleness(store: Path) -> list[str]:
    """Four literal shapes only. Best-effort and deliberately not generalized —
    a broader matcher would produce the 119/120 false-positive rate this issue
    exists to fix, in a new place."""
    mise_text, rule_ids, roots = _corpus()
    findings: list[str] = []
    for f in memory_files(store):
        body = _body_text(f.read_text(encoding="utf-8"))
        for task in set(_MISE_TASK_RE.findall(body)):
            if f"[tasks.{task}]" not in mise_text:
                findings.append(f"{f.name}: `mise run {task}` is in no mise.toml")
        for rel in set(_SCRIPT_PATH_RE.findall(body)):
            if not any((r / rel).is_file() for r in roots):
                findings.append(f"{f.name}: `{rel}` exists in neither repo")
        for rid in set(_RULE_ID_RE.findall(body)):
            if rule_ids and rid not in rule_ids:
                findings.append(f"{f.name}: `{rid}` is in neither registry")
        for field in _DEAD_BOARD_FIELDS:
            if re.search(rf"\b{field}\b field", body):
                findings.append(f"{f.name}: names the retired board field `{field}` (hrse#966)")
    return findings


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(store: Path, gate: bool) -> int:
    print(f"store: {store}")
    if not store.is_dir():
        print(f"ERROR: memory store not found: {store}", file=sys.stderr)
        return 2

    broken, issue_refs = check_broken_slugs(store)
    cap_findings, cap_warnings = check_index_cap(store)
    gating: list[tuple[str, list[str]]] = [
        ("Check 1 — orphan files", check_orphans(store)),
        ("Check 2 — dead MEMORY.md links", check_dead_links(store)),
        ("Check 4 — broken [[slug]] refs", broken),
        ("Check 5 — missing frontmatter", check_missing_frontmatter(store)),
        ("Check 6 — index load cap", cap_findings),
    ]
    advisory: list[tuple[str, list[str]]] = [
        ("Check 3 — stale project memories", check_stale_projects(store)),
        ("Check 6 — index load cap (warning)", cap_warnings),
        ("Check 4 — issue references (report-only)", issue_refs),
        ("Check 7 — staleness (report-only)", check_staleness(store)),
    ]

    for tag, group in (("FAIL", gating), ("note", advisory)):
        for label, items in group:
            if not items:
                print(f"  ok   {label}")
                continue
            print(f"  {tag} {label}")
            for item in items:
                print(f"         {item}")
    total = sum(len(items) for _, items in gating)

    lines, size = index_size(store)
    print(f"\nindex: {lines}/{MAX_INDEX_LINES} lines, {size}/{MAX_INDEX_BYTES} bytes"
          f" | memories: {len(memory_files(store))}")
    if total == 0:
        print("no gating findings.")
    else:
        print(f"{total} gating finding(s).")
    return 1 if (gate and total) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, default=None,
                   help="Lint this store instead of the resolved one (tests, fixtures).")
    p.add_argument("--gate", action="store_true",
                   help="Exit 1 on gating findings. Without it the run is report-only "
                        "and exits 0 — see the module docstring for why the live store "
                        "is never gated.")
    args = p.parse_args(argv)
    return run(args.store or resolve_store(), args.gate)


if __name__ == "__main__":
    sys.exit(main())
