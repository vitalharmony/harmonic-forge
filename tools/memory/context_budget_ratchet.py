#!/usr/bin/env python3
"""Asymmetric ratchet over the always-loaded context surface (harmonic-forge#521).

`context_budget.py` measures the surface. This locks the measurement in, so a
surface that shrinks stays shrunk and a surface that grows has to say why in
the same review as the change that grew it.

## Why a ratchet rather than only a ceiling

A ceiling alone permits unlimited growth right up to it, and the growth arrives
one plausible paragraph at a time — each defensible, none reviewed against the
total. The budget check had exactly that failure in a stronger form: **it never
gated at all.** `context_budget.py` exits non-zero only with `--gate`, and
HRSE2's `hygiene` invocation never passed it, so the check printed `OVER` and
exited 0 through the entire drift. There was no unheeded gate; there was no
gate.

## The two numbers, and why there are two

Derived from harmonic-forge#497's compaction measurement: sessions compact
repeatedly (**10-56 times each, 160 since 2026-09-01**) down to roughly **12k
surviving tokens**. At the 4 bytes/token estimate `context_budget.py` uses,
that is about **48,000 bytes** of room in a compacted window.

- **Ceiling 48,000 bytes** — *measured*. Above this, a compacted session cannot
  hold its own directive corpus at all, and which half it loses is unsignalled.
- **Target 24,000 bytes** — *a judgment call, and labelled as one.* Half the
  ceiling, so a compacted session has room to actually work rather than merely
  to hold its instructions. The ½ is not derived from anything; it is a choice
  about how much of the window the preamble may claim, and stating that plainly
  is more useful than dressing it as arithmetic.

A single number cannot be both measured and reachable, which is why there are
two.

Compliance when this shipped (2026-09-09, after hrse#1730's partial trim):

| repo | surface | vs target 24,000 | vs ceiling 48,000 |
|---|---|---|---|
| hrse | 77,123 B | 3.2x | 1.6x |
| harmonic-forge | ~23,600 B | under | under |

## What the ratchet actually enforces

- **A decrease never requires acknowledgement.** It passes silently, and
  `--update` locks the new lower figure into the baseline. Making someone
  justify an improvement is how a ratchet becomes theatre.
- **An increase requires an `[[increase]]` entry** in the baseline naming the
  date, the files, the byte delta and why.
- **The entry is re-measured, not read.** Its `files` must equal the set that
  actually grew and its `bytes` must equal the actual total delta, both
  computed here from the tool's own output. A copy-pasted or unread entry
  fails, which is the whole anti-rubber-stamp property — an acknowledgement
  nobody checks is a checkbox, and a checkbox measures nothing.

## Coverage

An onboarded repo with no baseline reports as **unmeasured**, never omitted.
Same discipline as harmonic-forge#519: absence is a finding, and a report that
silently skips what it could not measure reads as a green zero.

No network calls anywhere in this path.
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))

from context_budget import BYTES_PER_TOKEN, surface  # noqa: E402

#: Measured: harmonic-forge#497 put a compacted session at ~12k surviving
#: tokens; at 4 bytes/token that is ~48,000 bytes of room.
CEILING_BYTES = 48_000

#: A judgment call, not a derivation — half the ceiling, so a compacted session
#: has room to work rather than only to hold its instructions.
TARGET_BYTES = 24_000

BASELINE_NAME = "context-budget.baseline.toml"


class RatchetError(RuntimeError):
    """A baseline that cannot be trusted to mean what it says."""


def baseline_path(repo: Path) -> Path:
    return repo / BASELINE_NAME


def measure(repo: Path) -> dict[str, int]:
    """Per-file byte counts, keyed by a path stable across machines.

    Per-file and not just the total (spec item 3): a bump then shows exactly
    which file grew and by how much, in the same review as the change that grew
    it. A total-only baseline says a number moved and leaves the reader to
    find out where.
    """
    rows: dict[str, int] = {}
    for _category, path, size in surface(repo):
        rows[_relative_key(repo, path)] = size
    return rows


def _relative_key(repo: Path, path: Path) -> str:
    """Repo-relative where possible, `~`-relative otherwise.

    Two of the measured files live OUTSIDE the repo — the operator's own
    `~/.claude/CLAUDE.md` and the shared memory index — so a repo-relative-only
    key would either crash on them or silently drop them, and dropping them
    would understate the surface by the two files nobody can trim from inside
    the repo.
    """
    try:
        return str(path.relative_to(repo))
    except ValueError:
        try:
            return "~/" + str(path.relative_to(Path.home()))
        except ValueError:
            return str(path)


def load_baseline(repo: Path) -> dict | None:
    path = baseline_path(repo)
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise RatchetError(f"{path} is not valid TOML: {exc}") from exc


def render_baseline(rows: dict[str, int], increases: list[dict]) -> str:
    """Emit the baseline by hand — there is no TOML writer in this toolchain,
    and adding a dependency for four literal types is not worth it."""
    total = sum(rows.values())
    lines = [
        "# Always-loaded context surface, locked in (harmonic-forge#521).",
        "#",
        "# Regenerate with:",
        "#   python3 ~/harmonic-forge/tools/memory/context_budget_ratchet.py \\",
        "#     --repo . --update",
        "#",
        "# A DECREASE needs no acknowledgement — re-run --update and commit.",
        "# An INCREASE needs an [[increase]] entry below whose `files` and",
        "# `bytes` are re-measured here, not copied. An entry that does not",
        "# match the real delta fails the check.",
        "",
        f"total_bytes = {total}",
        f"ceiling_bytes = {CEILING_BYTES}",
        f"target_bytes = {TARGET_BYTES}",
        "",
        "[files]",
    ]
    for key in sorted(rows):
        lines.append(f'"{key}" = {rows[key]}')
    for entry in increases:
        lines.extend([
            "",
            "[[increase]]",
            f'date = "{entry.get("date", "")}"',
            "files = [" + ", ".join(f'"{f}"' for f in entry.get("files", [])) + "]",
            f'bytes = {entry.get("bytes", 0)}',
            f'why = "{entry.get("why", "")}"',
        ])
    return "\n".join(lines) + "\n"


def compare(baseline: dict, current: dict[str, int]) -> tuple[dict[str, int], dict[str, int], int]:
    """`(grew, shrank, net_delta)` — per-file deltas plus the total change."""
    previous: dict[str, int] = dict(baseline.get("files", {}))
    grew: dict[str, int] = {}
    shrank: dict[str, int] = {}
    for key, size in current.items():
        delta = size - previous.get(key, 0)
        if delta > 0:
            grew[key] = delta
        elif delta < 0:
            shrank[key] = delta
    for key, size in previous.items():
        if key not in current:
            shrank[key] = -size
    net = sum(current.values()) - sum(previous.values())
    return grew, shrank, net


def validate_increase(baseline: dict, grew: dict[str, int], net: int) -> list[str]:
    """The anti-rubber-stamp check. Returns failures, empty when satisfied.

    Both halves are re-measured against this run's own output. An entry whose
    `files` merely overlaps, or whose `bytes` is round and plausible, fails —
    that is the point. An acknowledgement nobody checks is a checkbox.
    """
    entries = baseline.get("increase") or []
    if not entries:
        return ["no [[increase]] entry, and the surface grew"]
    latest = entries[-1]
    failures: list[str] = []
    claimed_files = set(latest.get("files") or [])
    actual_files = set(grew)
    if claimed_files != actual_files:
        missing = sorted(actual_files - claimed_files)
        extra = sorted(claimed_files - actual_files)
        detail = []
        if missing:
            detail.append(f"grew but not named: {', '.join(missing)}")
        if extra:
            detail.append(f"named but did not grow: {', '.join(extra)}")
        failures.append("the [[increase]] entry's `files` does not match what "
                        "actually grew — " + "; ".join(detail))
    claimed_bytes = latest.get("bytes")
    if claimed_bytes != net:
        failures.append(f"the [[increase]] entry says `bytes = {claimed_bytes}` "
                        f"but the measured net change is {net}")
    if not str(latest.get("why", "")).strip():
        failures.append("the [[increase]] entry has no `why`")
    if not str(latest.get("date", "")).strip():
        failures.append("the [[increase]] entry has no `date`")
    return failures


def failure_message(repo: Path, total: int, grew: dict[str, int], net: int,
                    failures: list[str]) -> str:
    """Name what to move, not just that a number moved (spec item 7)."""
    lines = [
        f"context budget ratchet FAILED for {repo}",
        "",
        f"  surface grew by {net} bytes to {total} "
        f"(~{total // BYTES_PER_TOKEN} tokens)",
        f"  target {TARGET_BYTES}, ceiling {CEILING_BYTES}",
        "",
        "  what grew:",
    ]
    for key in sorted(grew, key=lambda k: -grew[k]):
        lines.append(f"    +{grew[key]:>6}  {key}")
    lines += ["", "  why this failed:"]
    lines += [f"    - {failure}" for failure in failures]
    lines += [
        "",
        "  To fix, in order of preference:",
        "",
        "  1. MOVE something out instead of growing. The criterion is",
        "     hrse#1730's: does a session need this BEFORE its first prompt,",
        "     or can it read it when the situation arises? A behavioral rule",
        "     that must fire without being consulted stays always-loaded; a",
        "     reference table consulted at a known moment goes to `docs/` with",
        "     a one-line pointer. See docs/context-budget-classification.md in",
        "     hrse for the worked classification.",
        "  2. PATH-SCOPE it. A rules file whose frontmatter declares `paths:`",
        "     fires only when a matching file is open and leaves the",
        "     always-loaded surface entirely. Note `globs:` alone does NOT do",
        "     this — measured in harmonic-forge#497.",
        "  3. If the growth is genuinely necessary, record it:",
        "",
        f"     [[increase]]",
        f'     date = "{date.today().isoformat()}"',
        "     files = ["
        + ", ".join(f'"{k}"' for k in sorted(grew)) + "]",
        f"     bytes = {net}",
        '     why = "..."',
        "",
        f"     in {baseline_path(repo)}. `files` and `bytes` are re-measured",
        "     against this tool's own output — a copied entry fails.",
    ]
    return "\n".join(lines)


def check(repo: Path) -> tuple[int, str]:
    """`(exit_code, report)` for one repo."""
    current = measure(repo)
    total = sum(current.values())
    baseline = load_baseline(repo)
    if baseline is None:
        return 1, (f"context budget: {repo} is UNMEASURED — no {BASELINE_NAME}. "
                   f"Create it with `--update`. Reported rather than skipped: a "
                   f"repo nobody measured is not a repo that is under budget.")
    grew, shrank, net = compare(baseline, current)
    headline = (f"context budget: {total} bytes (~{total // BYTES_PER_TOKEN} "
                f"tokens), target {TARGET_BYTES}, ceiling {CEILING_BYTES}")
    if net <= 0:
        note = ""
        if shrank:
            note = (f"\n  surface shrank by {-net} bytes — no acknowledgement "
                    f"needed. Run `--update` to lock it in.")
        return 0, headline + note
    failures = validate_increase(baseline, grew, net)
    if failures:
        return 1, failure_message(repo, total, grew, net, failures)
    latest = (baseline.get("increase") or [])[-1]
    return 0, (headline + f"\n  grew by {net} bytes, acknowledged "
               f"{latest.get('date')}: {latest.get('why')}")


def update(repo: Path) -> str:
    """Rewrite the baseline to the current measurement, preserving history."""
    current = measure(repo)
    existing = load_baseline(repo)
    increases = list(existing.get("increase") or []) if existing else []
    path = baseline_path(repo)
    path.write_text(render_baseline(current, increases), encoding="utf-8")
    return f"wrote {path} ({sum(current.values())} bytes across {len(current)} files)"


def coverage() -> tuple[int, str]:
    """Every onboarded repo, with unmeasured ones named (spec item 6)."""
    from manifest import load as load_manifest  # noqa: E402

    lines = ["context budget coverage"]
    unmeasured: list[str] = []
    for project in load_manifest():
        if not project.onboarded or project.checkout is None:
            continue
        repo = project.checkout
        if not repo.exists():
            lines.append(f"  {project.name:<20} checkout missing at {repo}")
            unmeasured.append(project.name)
            continue
        if not baseline_path(repo).exists():
            lines.append(f"  {project.name:<20} UNMEASURED — no {BASELINE_NAME}")
            unmeasured.append(project.name)
            continue
        code, report = check(repo)
        state = "ok" if code == 0 else "FAIL"
        total = sum(measure(repo).values())
        lines.append(f"  {project.name:<20} {total:>7} B  {state}")
    if unmeasured:
        lines.append("")
        lines.append(f"  {len(unmeasured)} repo(s) unmeasured: "
                     f"{', '.join(unmeasured)}")
        lines.append("  An unmeasured repo is a finding, not a pass.")
    return (1 if unmeasured else 0), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--update", action="store_true",
                        help="Rewrite the baseline to the current measurement, "
                             "preserving any [[increase]] history.")
    parser.add_argument("--coverage", action="store_true",
                        help="Report every onboarded repo, naming unmeasured ones.")
    parser.add_argument("--gate", action="store_true",
                        help="Exit 1 on failure. Without it the run is "
                             "report-only and exits 0.")
    args = parser.parse_args(argv)

    if args.coverage:
        code, report = coverage()
    elif args.update:
        print(update(args.repo.resolve()))
        return 0
    else:
        code, report = check(args.repo.resolve())
    print(report)
    return code if args.gate else 0


if __name__ == "__main__":
    sys.exit(main())
