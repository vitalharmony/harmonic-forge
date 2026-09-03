#!/usr/bin/env python3
"""Rule-registry drift check (harmonic-forge#447).

The registry (`registry.toml`) maps a stable ID to the rule text it names.
This check is what keeps that mapping honest, in the same spirit as
`mise run docs-check` for ontology drift: it fails loudly when a rule is
edited, moved, or deleted without its ID following.

WHY `text_sha` AND NOT `statement`
-----------------------------------
`statement` is a one-line human paraphrase. Rewording the underlying bullet
changes nothing a `statement` comparison could see, so "count violations of
*this* rule since it was written" would silently start counting a different
rule. `text_sha` is a hash over the **annotated span** in the source file,
so any edit to the rule's own text surfaces as drift and forces a
deliberate decision: same rule reworded (update the sha) or new rule (new
ID).

WHY THE INLINE ANNOTATION IS THE LOCATOR, NOT `anchor`
--------------------------------------------------------
`file`/`anchor` are descriptive only — never key on them. Section anchors
are coarse (`## Lane 2 — Muscle` spans ~15 bullets) and the classification
rule "one bullet may yield several IDs" means many rules legitimately share
one anchor. The actual locator is the paired marker in the source:

    <!-- R-0001 -->
    - Push to the remote only when the human operator explicitly asks.
    <!-- /R-0001 -->

THREE FAILURES, ALL DELIBERATE
--------------------------------
1. **Duplicate IDs.** The registry is its own allocator (`max(id)+1`), so
   two branches can independently allocate the same ID and each pass its
   own check. They collide at merge. Preventing that needs a central
   allocator, which is worse; detecting it is cheap, and the failure is
   then an ordinary merge conflict.
2. **Span drift.** `text_sha` no longer matches the annotated span.
3. **Orphans in either direction.** A registry row whose ID has no marker
   in any source file, or a marker with no registry row.
4. **An unrecognized registry field.** tomllib accepts any key, so a
   misspelled field is read by nothing and reported by nothing. See
   `_KNOWN_FIELDS`.

WHAT THIS SCHEME CANNOT EXPRESS
---------------------------------
`_OPEN`/`_CLOSE` are `^...$`-anchored, so a marker occupies its own line and
**a rule boundary falling mid-line cannot be marked**. `3-lane-protocol.md`
wraps at ~72 characters, so a second obligation inside a paragraph usually
begins mid-line. Six such obligations exist; relaxing the anchors and
reflowing the prose were both rejected (operator decision 2026-09-03), so
they are recorded on their containing rule as `folded_obligations` and
reported by `query_rules.py --folded`. The registry undercounts by that
amount, deliberately and visibly.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_REGISTRY = Path(__file__).resolve().parent / "registry.toml"

#: Files the registry annotates in this repo. HRSE2's half is a separate
#: registry pass in a separate PR (harmonic-forge#447 sequence step 6) —
#: and it must skip the three symlinked paths there, which resolve back
#: into this repo's `rules/` and would otherwise be annotated twice.
_ANNOTATED_GLOBS = ("rules/*.md", "3-lane-protocol.md")

#: Every field a registry row may carry. An unrecognized field is a hard
#: failure, not an ignored extra — see the check in `run_checks`.
_KNOWN_FIELDS = {
    "id", "file", "anchor", "statement", "text_sha", "hooks", "restates",
    "rationale_refs", "enforcement", "folded_obligations",
}

_OPEN = re.compile(r"^\s*<!--\s*(R-\d{4})\s*-->\s*$")
_CLOSE = re.compile(r"^\s*<!--\s*/(R-\d{4})\s*-->\s*$")


def span_sha(text: str) -> str:
    """Hash of an annotated span, whitespace-normalised per line.

    Normalised so a reflow (line rewrap, trailing-space cleanup) does not
    read as a rule change — those are the edits that would otherwise
    produce constant false drift on a prose corpus, and they genuinely do
    not change the obligation. A word change still surfaces.
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    normalised = "\n".join(line for line in lines if line)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def extract_spans(path: Path) -> dict[str, str]:
    """`{rule_id: span_text}` for every paired marker in one file.

    An unclosed or mismatched marker is a hard error, not a skip: a
    half-annotated span silently drops a rule out of every later count.
    """
    spans: dict[str, str] = {}
    open_id: str | None = None
    buffer: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        opening = _OPEN.match(line)
        closing = _CLOSE.match(line)
        if opening:
            if open_id is not None:
                raise ValueError(
                    f"{path}:{lineno}: {opening.group(1)} opened while "
                    f"{open_id} is still open — spans may not nest or overlap"
                )
            open_id = opening.group(1)
            buffer = []
        elif closing:
            if open_id is None:
                raise ValueError(f"{path}:{lineno}: closing {closing.group(1)} with nothing open")
            if closing.group(1) != open_id:
                raise ValueError(
                    f"{path}:{lineno}: closing {closing.group(1)} but {open_id} is open"
                )
            if open_id in spans:
                raise ValueError(f"{path}:{lineno}: {open_id} appears twice in this file")
            spans[open_id] = "\n".join(buffer)
            open_id = None
        elif open_id is not None:
            buffer.append(line)
    if open_id is not None:
        raise ValueError(f"{path}: {open_id} was never closed")
    return spans


def collect_source_spans(root: Path, globs: tuple[str, ...] = _ANNOTATED_GLOBS) -> dict[str, tuple[Path, str]]:
    found: dict[str, tuple[Path, str]] = {}
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            for rule_id, text in extract_spans(path).items():
                if rule_id in found:
                    raise ValueError(
                        f"{rule_id} is annotated in two files: "
                        f"{found[rule_id][0].name} and {path.name}"
                    )
                found[rule_id] = (path, text)
    return found


def load_registry(registry_path: Path) -> list[dict]:
    if not registry_path.exists():
        raise SystemExit(f"registry not found: {registry_path}")
    with registry_path.open("rb") as handle:
        return tomllib.load(handle).get("rule", [])


def check(root: Path, registry_path: Path,
          globs: tuple[str, ...] = _ANNOTATED_GLOBS) -> list[str]:
    """Every failure, collected — not the first. A partial report would
    have someone fix one drift and rerun to discover the next."""
    failures: list[str] = []
    rules = load_registry(registry_path)

    for index, rule in enumerate(rules):
        rule_id = rule.get("id", f"<row {index}>")
        unknown = sorted(set(rule) - _KNOWN_FIELDS)
        if unknown:
            failures.append(
                f"{rule_id}: unrecognized registry field(s) {unknown}. A misspelled field "
                f"is read by nothing and reported by nothing — `folded_obligations` typed as "
                f"`folded_obligation` would silently drop a known-folded rule from "
                f"`query_rules.py --folded`, which is exactly the invisible undercount that "
                f"field exists to prevent."
            )

    seen: dict[str, int] = {}
    for index, rule in enumerate(rules):
        rule_id = rule.get("id", f"<row {index}>")
        if rule_id in seen:
            failures.append(
                f"duplicate ID {rule_id}: rows {seen[rule_id]} and {index}. Two branches "
                f"likely allocated it independently; renumber one and rerun."
            )
        seen[rule_id] = index

    try:
        source = collect_source_spans(root, globs)
    except ValueError as exc:
        return failures + [str(exc)]

    for rule in rules:
        rule_id = rule.get("id")
        if rule_id not in source:
            failures.append(
                f"{rule_id}: in the registry but no `<!-- {rule_id} -->` span found in "
                f"{', '.join(globs)} — rule deleted or moved without its ID."
            )
            continue
        path, text = source[rule_id]
        actual = span_sha(text)
        expected = rule.get("text_sha", "")
        if actual != expected:
            failures.append(
                f"{rule_id} ({path.name}): span text changed — registry text_sha={expected!r}, "
                f"actual={actual!r}. Same rule reworded (update text_sha) or a different rule "
                f"(allocate a new ID)? That is a decision, not a refresh."
            )

    registry_ids = {rule.get("id") for rule in rules}
    for rule_id, (path, _) in source.items():
        if rule_id not in registry_ids:
            failures.append(
                f"{rule_id} ({path.name}): annotated in the source but absent from the "
                f"registry — every marker needs a row."
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=_PLATFORM_ROOT,
                        help="platform root containing rules/ and 3-lane-protocol.md")
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--glob", action="append", dest="globs", metavar="PATTERN",
                        help="annotated-file glob, relative to --root; repeatable. "
                             "Defaults to this repo's own corpus. HRSE2's pass supplies "
                             "its own (`.claude/rules/*.md`, `.windsurfrules`, ...) because "
                             "its corpus lives at different paths, NOT because it is a "
                             "different kind of corpus.")
    parser.add_argument("--next-id", action="store_true",
                        help="print the next free ID (the registry is its own allocator) and exit")
    args = parser.parse_args()

    if args.next_id:
        rules = load_registry(args.registry)
        highest = max((int(r["id"].split("-")[1]) for r in rules if r.get("id")), default=0)
        print(f"R-{highest + 1:04d}")
        return 0

    globs = tuple(args.globs) if args.globs else _ANNOTATED_GLOBS
    failures = check(args.root, args.registry, globs)
    if failures:
        print(f"rule-registry drift: {len(failures)} failure(s)", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    rules = load_registry(args.registry)
    print(f"rule registry: clean. {len(rules)} rule(s) annotated and in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
