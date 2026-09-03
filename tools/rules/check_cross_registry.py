#!/usr/bin/env python3
"""Cross-registry rule-ID check (harmonic-forge#454).

WHY THIS EXISTS SEPARATELY FROM check_rule_drift.py
------------------------------------------------------
`check_rule_drift.py` reads exactly one registry, and that is the defect this
tool covers. Two repos hold registries sharing one ID space; each allocated
`max(id) + 1` over its own file; so forge's `--next-id` returned `R-0253`
while hrse already owned `R-0253..R-0329`. Both drift checks reported clean,
because neither could see the other's file. Banding (`band_min`/`band_max`)
prevents new collisions at allocation time; this catches the ones banding
cannot — a hand-edited ID, a wrong band declaration, or two branches in two
repos racing.

WHY A MISSING SIBLING IS NOT "CLEAN"
---------------------------------------
A single-repo checkout has no sibling to compare against. Reporting that as a
pass would be the harmonic-forge#440 shape: a check that silently no-ops looks
exactly like a check that passed. This prints an explicit `skipped` line naming
the path it looked for, and the caller can tell the two apart.

THE ACCEPTED OUT-OF-BAND RANGE
---------------------------------
hrse's `R-0253..R-0329` sit inside forge's band. They merged before banding
existed and they stay put — an ID that moves is not a stable ID, which is the
whole product of harmonic-forge#447. The exception is declared in the owning
registry's own `accepted_out_of_band` key, not hardcoded here, so it is visible
to anyone reading that file and a second exception needs no code change.

Exit 0 clean or skipped, 1 on a real finding.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_REGISTRY = _HERE / "registry.toml"

#: Where the sibling registry lives, relative to $HOME. Checked in order; the
#: first that exists wins. A lane worktree is a legitimate checkout, so the
#: lane variants are included rather than assuming the canonical path.
_SIBLING_CANDIDATES = (
    "Harmonic_Projects/HRSE2/.claude/rules/registry.toml",
    "Harmonic_Projects/HRSE2-lane2/.claude/rules/registry.toml",
    "Harmonic_Projects/HRSE2-lane3/.claude/rules/registry.toml",
)

_RANGE = re.compile(r"^R-(\d{4})\.\.R-(\d{4})$")


def load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def ids_of(data: dict) -> dict[int, str]:
    """`{numeric id: rule id}` for one registry."""
    out: dict[int, str] = {}
    for rule in data.get("rule", []):
        rid = rule.get("id")
        if rid:
            out[int(rid.split("-")[1])] = rid
    return out


def accepted(data: dict) -> set[int]:
    """Numeric ids this registry declares as legitimately outside its band."""
    allowed: set[int] = set()
    for spec in data.get("accepted_out_of_band", []) or []:
        match = _RANGE.match(spec)
        if not match:
            raise SystemExit(f"unparseable accepted_out_of_band entry: {spec!r}")
        low, high = int(match.group(1)), int(match.group(2))
        allowed |= set(range(low, high + 1))
    return allowed


def check(own_path: Path, sibling_path: Path) -> list[str]:
    own, sibling = load(own_path), load(sibling_path)
    own_ids, sibling_ids = ids_of(own), ids_of(sibling)
    failures: list[str] = []

    shared = sorted(set(own_ids) & set(sibling_ids))
    if shared:
        failures.append(
            f"{len(shared)} id(s) present in BOTH registries: "
            f"{', '.join(own_ids[i] for i in shared[:10])}"
            f"{' …' if len(shared) > 10 else ''}. One of them must be reallocated."
        )

    for label, data, ids in (("own", own, own_ids), ("sibling", sibling, sibling_ids)):
        low, high = data.get("band_min"), data.get("band_max")
        if low is None or high is None:
            failures.append(f"{label} registry declares no band (band_min/band_max)")
            continue
        allowed = accepted(data)
        stray = sorted(i for i in ids if not (low <= i <= high) and i not in allowed)
        if stray:
            failures.append(
                f"{label} registry has {len(stray)} id(s) outside its band "
                f"{low}..{high} and not in accepted_out_of_band: "
                f"{', '.join(ids[i] for i in stray[:10])}"
                f"{' …' if len(stray) > 10 else ''}"
            )
    return failures


def find_sibling(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    home = Path.home()
    for rel in _SIBLING_CANDIDATES:
        candidate = home / rel
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--sibling", type=Path, default=None,
                        help="sibling registry; auto-discovered when omitted")
    args = parser.parse_args()

    sibling = find_sibling(args.sibling)
    if sibling is None:
        looked = str(args.sibling) if args.sibling else ", ".join(
            str(Path.home() / r) for r in _SIBLING_CANDIDATES)
        print(f"cross-registry check SKIPPED — sibling registry not found at {looked}")
        print("  (single-repo checkout; this is not a pass, nothing was compared)")
        return 0

    failures = check(args.registry, sibling)
    if failures:
        print(f"cross-registry: {len(failures)} finding(s)", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    own_n = len(ids_of(load(args.registry)))
    sib_n = len(ids_of(load(sibling)))
    print(f"cross-registry: clean. {own_n} + {sib_n} ids, no overlap, none stray "
          f"(sibling: {sibling})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
