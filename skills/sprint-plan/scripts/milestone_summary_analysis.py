"""Deterministic dependency, file-scope, and cache analysis."""
from __future__ import annotations

import json
import re

from milestone_summary_source import cache_dir

TOOLING_LABEL = "tooling-exception"
PATH_TOKEN = re.compile(r"`((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]+|\.[a-z]+rules|[A-Za-z0-9_-]+/)`")
#: hrse#1543: the continuation used to require each subsequent ref to follow
#: a comma immediately, so a parenthetical between refs (e.g. "#1434 (G2-e,
#: go/no-go), #1439 (...)") truncated the capture to the first ref alone.
#: `[^\n]*` alone (tried first, reverted per preclose finding on this same
#: issue) fixed the enumeration but deleted the terminator the old pattern
#: was also providing: a markdown "line" is a whole soft-wrapped paragraph,
#: so capturing to end-of-line pulled in a clause's own children (a "depends
#: on #12" inside one child's parenthetical swept in every SIBLING #N later
#: in the same paragraph, live on harmonic-forge#11) and reversed-direction
#: refs ("Blocked on #9. This unblocks #12." on one line). The actual
#: boundary this needs is the *clause*, not the line: `_dependency_span`
#: below walks forward from the trigger consuming only refs, parentheticals,
#: and list connectives (comma / "and"), and stops at the first token that
#: is none of those -- in particular at sentence-ending punctuation, which
#: is exactly where a dependency statement ends in every body surveyed.
LINKED_DEP = re.compile(r"(?:blocked\s+(?:by|on)|depends\s+on)\b([^\n]*)", re.I)
INFERRED_DEP = re.compile(r"(?:requires|prerequisite|gated\s+on|waits?\s+on|depends\s+upon|must\s+land\s+(?:before|after))\b.{0,80}?((?:[a-z-]+)?#\d+(?:\s*,\s*(?:[a-z-]+)?#\d+)*)", re.I)
ISSUE_REF = re.compile(r"(?:([a-z][a-z0-9-]*)#|#)(\d+)")
_DEP_SPAN_LEADING_FILLER = re.compile(r"^[\s:*]*")
#: No `\A` anchor: `.match(string, pos)` already anchors the attempt at
#: `pos`, and `\A` would additionally require `pos == 0`, silently failing
#: every iteration past the first.
_DEP_SPAN_ITEM = re.compile(r"\s*,?\s*(?:and\s+)?(?:\([^()]*\)|(?:[a-z][a-z0-9-]*)?#\d+)", re.I)


def _dependency_span(remainder: str) -> str:
    """Trim a `LINKED_DEP` match's rest-of-line capture to the clause the
    trigger phrase actually introduced: a run of refs, parentheticals, and
    list connectives, stopping at the first token that is none of those.
    """
    pos = _DEP_SPAN_LEADING_FILLER.match(remainder).end()
    while True:
        match = _DEP_SPAN_ITEM.match(remainder, pos)
        if not match:
            break
        pos = match.end()
    return remainder[:pos]
#: hrse#1523: a third dependency source. Neither `LINKED_DEP` nor
#: `INFERRED_DEP` matches this repo's own most common convention for
#: stating a hard dependency -- a `## Dependencies` heading followed by
#: bulleted, bolded issue refs (e.g. hrse#199: "**#192 (BACKLOG-052 Phase
#: 1)** -- ... it cannot ship before them.") -- because the prose after the
#: bold ref varies and was never a fixed phrase to match on. The heading
#: itself IS the declaration; the bullet's leading bold ref(s) are read
#: structurally rather than by phrase, catching this repo's own idiom
#: without weakening either phrase-based pattern.
#: hrse#1523 preclose finding: the terminator must stop at ANY heading
#: level, not only another `##` -- `(?=\n##\s|\Z)` cannot match `\n### `
#: (the character after `\n##` is `#`, not whitespace), so a `### Notes`
#: subheading's own bullets were silently pulled into the Dependencies
#: section and read as real blockers.
_DEPENDENCIES_SECTION = re.compile(
    r"^##\s+Dependencies\s*\n(?P<body>.*?)(?=\n#{1,6}\s|\Z)", re.I | re.M | re.S)
_SECTION_BULLET_REFS = re.compile(
    r"^\s*-\s*\*\*((?:(?:[a-z][a-z0-9-]*)?#\d+(?:\s*[/,]\s*(?:[a-z][a-z0-9-]*)?#\d+)*)"
    r")", re.M | re.I)
#: hrse#1523 preclose finding: an issue body quoting a `## Dependencies`
#: example inside a fenced code block (this file's own docstrings do
#: exactly that) must not have that example read as a real declaration.
#: Stripped before the section search only -- `LINKED_DEP`/`INFERRED_DEP`
#: predate this issue and are unchanged.
_FENCE = re.compile(r"```.*?```", re.S)
TOOLING_HINT = re.compile(r"(^|/)(\.claude/|\.codex/|scripts/|tools/|\.github/|docs/|mise\.toml)")
PRODUCT_SURFACE = re.compile(r"(^|/)(backend/app/|frontend/src/)")


def dependencies(repo: str, issue: dict) -> list[tuple[str, str]]:
    body = issue.get("body") or ""
    self_key = f"{repo.split('/')[-1]}#{issue['number']}"
    edges, seen = [], set()

    def _add(target: str, provenance: str) -> None:
        if target != self_key and target not in seen:
            seen.add(target)
            edges.append((target, provenance))

    for pattern, provenance in ((LINKED_DEP, "linked"), (INFERRED_DEP, "inferred")):
        for match in pattern.finditer(body):
            captured = match.group(1)
            if pattern is LINKED_DEP:
                captured = _dependency_span(captured)
            for prefix, number in ISSUE_REF.findall(captured):
                _add(f"{prefix or repo.split('/')[-1]}#{number}", provenance)

    section = _DEPENDENCIES_SECTION.search(_FENCE.sub("", body))
    if section:
        for bullet in _SECTION_BULLET_REFS.finditer(section.group("body")):
            for prefix, number in ISSUE_REF.findall(bullet.group(1)):
                _add(f"{prefix or repo.split('/')[-1]}#{number}", "linked")

    return edges


def file_scope(issue: dict) -> list[str]:
    return sorted(set(PATH_TOKEN.findall(issue.get("body") or "")))


def tooling_marker(issue: dict, scope: list[str]) -> str | None:
    if any(label["name"] == TOOLING_LABEL for label in issue.get("labels") or []):
        return "label"
    if (not scope or any(PRODUCT_SURFACE.search(path) for path in scope)
            or not all(TOOLING_HINT.search(path) for path in scope)):
        return None
    names = {label["name"] for label in issue.get("labels") or []}
    return "inferred" if names & {"tech-debt", "infrastructure"} else None


class Cache:
    def __init__(self, enabled: bool = True, rewrite: bool = False) -> None:
        self.path = cache_dir() / "file_scope.json"
        self.enabled, self.rewrite = enabled, rewrite
        self.data: dict[str, dict] = {}
        if enabled:
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self.data = {}
        self.fresh = self.cached = 0

    def scope(self, repo: str, issue: dict) -> list[str]:
        key = f"{repo}#{issue['number']}"
        entry = self.data.get(key)
        if self.enabled and entry and entry.get("updated_at") == issue.get("updatedAt"):
            self.cached += 1
            return entry["scope"]
        scope = file_scope(issue)
        self.data[key] = {"updated_at": issue.get("updatedAt"), "scope": scope}
        self.fresh += 1
        return scope

    def save(self) -> None:
        if not (self.enabled or self.rewrite):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
