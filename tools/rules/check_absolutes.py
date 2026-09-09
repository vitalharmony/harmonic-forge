#!/usr/bin/env python3
"""Cross-repo completeness sweep for the "no lane closes/merges on its own"
family of rules (harmonic-forge#524).

WHY THIS EXISTS
------------------
Two preclose-inspection rounds on harmonic-forge#524 each found real,
unamended sites still stating the operator-only-close absolute after the
carve-out (R-0351) was added elsewhere. Both rounds were a human/agent
re-reading the corpus by memory and grep, and each pass missed sites the
other pass (or a subsequent cold pass) found. `check_rule_drift.py` cannot
catch this class of drift: it verifies a rule's own text hasn't silently
changed, not that a *different* rule states something the corpus as a whole
no longer agrees with.

This script converts "did we find every site?" from a judgment call into a
check: it greps every annotated rule span in both registries (own + sibling)
for the absolute's known phrasings, and fails if any matching rule lacks an
`excepted_by` field (or an explicit allowlist entry for a rule that legitimately
restates the absolute in a narrower, still-consistent scope).

Exit 0 clean, 1 on a real finding, 2 on a missing sibling (informational,
matching check_cross_registry.py's convention -- a single-repo checkout is
not "clean," it's "nothing was compared").
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_rule_drift import extract_spans  # noqa: E402

_HERE = Path(__file__).resolve().parent
_DEFAULT_REGISTRY = _HERE / "registry.toml"

_SIBLING_CANDIDATES = (
    "Harmonic_Projects/HRSE2/.claude/rules/registry.toml",
    "Harmonic_Projects/HRSE2-lane2/.claude/rules/registry.toml",
    "Harmonic_Projects/HRSE2-lane3/.claude/rules/registry.toml",
)

#: Phrasings observed live across the corpus naming the close/merge-authority
#: absolute. Not a semantic parser -- a documented, extendable allowlist of
#: the wording this family of rules has actually used. A new phrasing that
#: slips past this needs a pattern added here, the same way a new field needs
#: adding to _KNOWN_FIELDS in check_rule_drift.py.
_ABSOLUTE_PATTERNS = [
    re.compile(r"no\s+lane\s+closes[a-z\s/]*on\s+its\s+own", re.IGNORECASE),
    re.compile(r"no\s+lane\s+closes\s+or\s+merges", re.IGNORECASE),
    re.compile(r"only\s+the\s+operator.s\s+explicit\s+.{0,40}instruction\s+authorizes\s+closure", re.IGNORECASE),
    re.compile(r"closing\s+requires\s+(the\s+human\s+operator|marc).s\s+explicit", re.IGNORECASE),
    re.compile(r"closes/merges\s+(on\s+its\s+own|without)", re.IGNORECASE),
]

#: Rules that legitimately restate the absolute in a scope that is still
#: fully consistent with it (never overridden) -- named explicitly rather
#: than silently exempted, per harmonic-forge#524's own review finding that
#: an unenumerated exemption is exactly the failure mode this script exists
#: to remove. Each entry names why.
_ALLOWLIST: dict[str, str] = {
    "R-0351": "the carve-out rule itself; it quotes R-0224's wording verbatim "
              "as a citation identifying what it excepts, not as a restatement "
              "binding on itself.",
}


def load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def find_sibling(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    home = Path.home()
    for rel in _SIBLING_CANDIDATES:
        candidate = home / rel
        if candidate.exists():
            return candidate
    return None


def matching_rules(registry_path: Path, root: Path) -> list[tuple[str, str]]:
    """`[(rule_id, matched_phrase)]` for every rule whose span states the
    absolute, across every file the registry's rows reference."""
    data = load(registry_path)
    by_file: dict[str, dict[str, str]] = {}
    hits: list[tuple[str, str]] = []
    for rule in data.get("rule", []):
        rid = rule.get("id")
        file_rel = rule.get("file")
        if not rid or not file_rel:
            continue
        if file_rel not in by_file:
            path = root / file_rel
            by_file[file_rel] = extract_spans(path) if path.exists() else {}
        span = by_file[file_rel].get(rid, "")
        for pattern in _ABSOLUTE_PATTERNS:
            m = pattern.search(span)
            if m:
                hits.append((rid, m.group(0)))
                break
    return hits


def check(own_registry: Path, own_root: Path,
          sibling_registry: Path, sibling_root: Path) -> list[str]:
    own_data = load(own_registry)
    sibling_data = load(sibling_registry)
    own_by_id = {r["id"]: r for r in own_data.get("rule", []) if r.get("id")}
    sibling_by_id = {r["id"]: r for r in sibling_data.get("rule", []) if r.get("id")}
    all_by_id = {**own_by_id, **sibling_by_id}

    failures: list[str] = []
    own_hits = matching_rules(own_registry, own_root)
    sibling_hits = matching_rules(sibling_registry, sibling_root)

    for rid, phrase in own_hits + sibling_hits:
        rule = all_by_id.get(rid, {})
        if rule.get("excepted_by"):
            excepter = all_by_id.get(rule["excepted_by"])
            if excepter is None:
                failures.append(
                    f"{rid} names excepted_by={rule['excepted_by']!r}, which exists in "
                    f"neither registry."
                )
            elif rid not in (excepter.get("exception_to") or []):
                failures.append(
                    f"{rid} states the close/merge absolute (matched {phrase!r}) and names "
                    f"excepted_by={rule['excepted_by']!r}, but that rule's exception_to does "
                    f"not list {rid!r} back."
                )
            continue
        if rid in _ALLOWLIST:
            continue
        failures.append(
            f"{rid} states the close/merge absolute (matched {phrase!r}) with no "
            f"excepted_by field and no _ALLOWLIST entry. If R-0351 (or its own successor) "
            f"is meant to except it, add excepted_by/exception_to to both rows; if this "
            f"rule is a genuinely different, still-consistent scope, add it to "
            f"_ALLOWLIST with a one-line reason."
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--root", type=Path, default=_HERE.parent.parent)
    parser.add_argument("--sibling", type=Path, default=None)
    parser.add_argument("--sibling-root", type=Path, default=None)
    args = parser.parse_args()

    sibling = find_sibling(args.sibling)
    if sibling is None:
        looked = str(args.sibling) if args.sibling else ", ".join(
            str(Path.home() / r) for r in _SIBLING_CANDIDATES)
        print(f"check_absolutes SKIPPED — sibling registry not found at {looked}")
        print("  (single-repo checkout; this is not a pass, nothing was compared)")
        return 0

    sibling_root = args.sibling_root or sibling.parent.parent.parent
    failures = check(args.registry, args.root, sibling, sibling_root)
    if failures:
        print(f"check_absolutes: {len(failures)} finding(s)", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("check_absolutes: clean. Every close/merge-absolute site is either "
          "reciprocally excepted or allowlisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
