#!/usr/bin/env python3
"""Measure the always-loaded context surface for a repo (harmonic-forge#497).

**What this counts, and why that set.** Every session pays for a fixed body of
text before its first prompt: the user's `CLAUDE.md`, the project's `CLAUDE.md`
and anything it `@`-imports, the unscoped rule files under `.claude/rules/`,
and the memory index. That is the surface a compacted session reliably still
holds — compaction discards the conversation, not the injected preamble — so
it is the only part of context whose size is a standing cost rather than a
transient one. It was unbudgeted and unmeasured.

Measured for an HRSE2 Lane 1 session on 2026-09-06: **77,396 bytes across 10
files, ~19,300 estimated tokens**, before any file is Read. Sessions then
compact repeatedly (160 times across all sessions since 2026-09-01) down to
~12k tokens, which means the always-loaded surface can exceed what a compacted
session has room to think in.

An earlier figure of 88,546 B circulated during this issue's own
implementation and was wrong in both directions at once: it counted
`.windsurfrules` (16,037 B), which the harness does not inject, and dropped two
`globs:`-carrying rule files (4,887 B), which it does. Both were corrected by
measuring headless sessions rather than reasoning from the frontmatter.

**Path-scoped rules are deliberately excluded.** `.claude/rules/*.md` files
that declare a path scope are injected only when a matching file is opened, so
they are not part of the standing cost. Counting them would inflate the figure
with text most sessions never load, and the budget would then be tuned against
a number nothing pays.

**Token estimates are estimates and are labelled as such.** No tokenizer is
available here without adding a dependency, so this uses bytes/4 — close
enough for a budget whose purpose is "is this surface growing", wrong enough
that it must never be quoted as a token count. The byte figure is the exact
one; the token column is the readable one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_lint import MAX_INDEX_BYTES, resolve_store  # noqa: E402

#: The default ceiling, in bytes, for the whole always-loaded surface.
#: Proposed at 60 KB against a measured 77.4 KB: a budget set above what is
#: already spent authorises the status quo and can never be exceeded, which is
#: the failure mode of most limits added after the fact. This one is meant to
#: be red on arrival and to come down.
DEFAULT_BUDGET_BYTES = 60_000

#: Bytes per token. See the module docstring — an estimate, never a count.
BYTES_PER_TOKEN = 4

#: `@path/to/file.md` import syntax in a CLAUDE.md.
_IMPORT_RE = re.compile(r"^\s*@([^\s]+)\s*$", re.M)

#: A rules file is path-scoped when its frontmatter declares where it applies.
#: Those are injected on a file match, not at session start.
#:
#: **`globs:` is deliberately NOT in this list.** It reads like a scope key and
#: was one here until it was measured: headless sessions in synthetic repos show
#: a `.claude/rules/*.md` carrying `globs:` is injected at session start
#: regardless of what file is open, exactly like one with no frontmatter at all.
#: Only `appliesTo:`, `paths:` and `scope:` actually suppress the injection.
#: Treating `globs:` as scoping dropped 4,887 B of always-paid text out of
#: HRSE2's report — under-reporting, which is the one failure mode this module
#: exists to avoid. Do not "restore" it without re-measuring.
_SCOPE_KEYS = ("appliesTo:", "paths:", "scope:")

#: How far to look for the closing frontmatter delimiter. Generous, because the
#: cost of not finding it is a file silently misclassified.
_FRONTMATTER_SCAN_BYTES = 8192


def _settings_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def user_claude_md() -> Path:
    return _settings_dir() / "CLAUDE.md"


def _is_path_scoped(path: Path) -> bool:
    """True when the rules file declares a path scope in its frontmatter.

    Read from the head only: a `globs:` mentioned in prose lower down is
    documentation about scoping, not a declaration of it.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:_FRONTMATTER_SCAN_BYTES]
    except OSError:
        return False
    if not head.startswith("---"):
        return False
    # The block must actually CLOSE within the scanned window. A 600-byte cap
    # was used here and had a false negative in the silent direction: a rules
    # file whose frontmatter is longer than the cap never reached its own scope
    # key, so a genuinely path-scoped file was counted as always-loaded. If no
    # closing delimiter is found, treat the file as unscoped — over-reporting
    # is visible in the table, under-reporting is not.
    if head.count("---") < 2:
        return False
    front = head.split("---", 2)[1]
    return any(key in front for key in _SCOPE_KEYS)


def _imports_of(claude_md: Path) -> list[Path]:
    """`@`-imported files, resolved relative to the importing file.

    One level only. Recursive imports are possible in principle and absent in
    practice in these repos; a depth limit that silently truncated a real
    chain would under-report the very number this tool exists to report, so
    the single level is stated rather than assumed.
    """
    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[Path] = []
    for match in _IMPORT_RE.finditer(text):
        target = (claude_md.parent / match.group(1)).resolve()
        if target.is_file():
            found.append(target)
    return found


def surface(repo: Path) -> list[tuple[str, Path, int]]:
    """`(category, path, bytes)` for everything injected at session start."""
    rows: list[tuple[str, Path, int]] = []

    def add(category: str, path: Path) -> None:
        if path.is_file():
            rows.append((category, path, path.stat().st_size))

    add("user CLAUDE.md", user_claude_md())
    for imported in _imports_of(user_claude_md()):
        add("user @import", imported)

    project_md = repo / "CLAUDE.md"
    add("project CLAUDE.md", project_md)
    for imported in _imports_of(project_md):
        add("project @import", imported)

    # `.windsurfrules` is NOT counted. HRSE2's CLAUDE.md names it the canonical
    # rules file, which is why it was included here at first, but the harness
    # does not inject it: a headless session in a repo containing one never
    # sees its body. At 16,037 B it was 18% of HRSE2's reported total and the
    # third-largest "contributor" the over-budget report told the operator to
    # cut -- text whose deletion would free zero session context, and which
    # could trip `--gate` on its own. Being named in a directive is not the
    # same as being loaded.

    rules_dir = repo / ".claude" / "rules"
    if rules_dir.is_dir():
        for rule in sorted(rules_dir.rglob("*.md")):
            if not _is_path_scoped(rule):
                add("unscoped rule", rule)

    store = resolve_store(repo)
    index = store / "MEMORY.md"
    if index.is_file():
        # Capped by `memory_lint`, so the cap is what a session can pay at
        # worst — but the ACTUAL size is what it pays today, and that is the
        # honest number for a budget.
        add("memory index", index)
    return rows


def report(repo: Path, budget: int) -> tuple[str, int]:
    """`(text, total_bytes)`."""
    rows = surface(repo)
    total = sum(size for _c, _p, size in rows)
    width = max((len(str(p)) for _c, p, _s in rows), default=20)
    lines = [f"context budget — {repo}", ""]
    lines.append(f"  {'file':<{min(width, 70)}}  {'bytes':>8}  {'~tokens':>8}  category")
    for category, path, size in sorted(rows, key=lambda r: -r[2]):
        shown = str(path)
        if len(shown) > 70:
            shown = "…" + shown[-69:]
        lines.append(f"  {shown:<{min(width, 70)}}  {size:>8}  "
                     f"{size // BYTES_PER_TOKEN:>8}  {category}")
    lines += ["", f"  {'TOTAL':<{min(width, 70)}}  {total:>8}  "
                  f"{total // BYTES_PER_TOKEN:>8}  "
                  f"({len(rows)} files, budget {budget})"]
    if total > budget:
        over = total - budget
        lines.append("")
        lines.append(f"OVER BUDGET by {over} bytes (~{over // BYTES_PER_TOKEN} tokens).")
        lines.append("Largest contributors:")
        for category, path, size in sorted(rows, key=lambda r: -r[2])[:3]:
            lines.append(f"  {size:>8}  {path.name}  ({category})")
    return "\n".join(lines), total


def summary_line(repo: Path, budget: int = DEFAULT_BUDGET_BYTES) -> str:
    """One line, for the SessionStart summary and `mise run hygiene`."""
    rows = surface(repo)
    total = sum(size for _c, _p, size in rows)
    state = "OVER" if total > budget else "ok"
    return (f"context: {total // 1000}KB / {budget // 1000}KB "
            f"(~{total // BYTES_PER_TOKEN} tokens, {len(rows)} files) {state}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path.cwd(),
                   help="Measure this repo's surface (default: cwd).")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET_BYTES,
                   help=f"Byte ceiling (default {DEFAULT_BUDGET_BYTES}).")
    p.add_argument("--gate", action="store_true",
                   help="Exit 1 when over budget. Without it the run is "
                        "report-only and exits 0.")
    p.add_argument("--summary", action="store_true",
                   help="One line instead of the table.")
    p.add_argument("--json", action="store_true", help="Machine-readable rows.")
    args = p.parse_args(argv)

    repo = args.repo.resolve()
    if args.json:
        rows = surface(repo)
        print(json.dumps({
            "repo": str(repo), "budget": args.budget,
            "total_bytes": sum(s for _c, _p, s in rows),
            "files": [{"category": c, "path": str(pth), "bytes": s}
                      for c, pth, s in rows]}, indent=2))
        total = sum(s for _c, _p, s in rows)
    elif args.summary:
        print(summary_line(repo, args.budget))
        total = sum(s for _c, _p, s in surface(repo))
    else:
        text, total = report(repo, args.budget)
        print(text)
    return 1 if (args.gate and total > args.budget) else 0


if __name__ == "__main__":
    sys.exit(main())
