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
import json
import os
import sys
import tomllib
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))

from context_budget import BYTES_PER_TOKEN, _platform_rule_sources, surface  # noqa: E402

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


def render_baseline(rows: dict[str, int], increases: list[dict],
                    outside: list[str] | None = None) -> str:
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
        # harmonic-forge#1414 reforge: which keys were outside this repo when the
        # baseline was written. A shrink is credited only for a key not listed, so
        # bytes an outside file contributed never offset this repo's growth.
        "outside_files = [" + ", ".join(json.dumps(k) for k in sorted(outside or [])) + "]",
        "",
        "[files]",
    ]
    for key in sorted(rows):
        lines.append(f'{json.dumps(key)} = {rows[key]}')
    for entry in increases:
        lines.extend([
            "",
            "[[increase]]",
            f'date = "{entry.get("date", "")}"',
            "files = [" + ", ".join(f'"{f}"' for f in entry.get("files", [])) + "]",
            f'bytes = {entry.get("bytes", 0)}',
            "why = " + _toml_string(str(entry.get("why", ""))),
        ])
    return "\n".join(lines) + "\n"


def _toml_string(text: str) -> str:
    """A TOML basic string, multi-line when `text` is. A raw newline inside a
    single-line string is invalid TOML, so every rationale longer than a line
    made `--update` write a baseline it could not read back (harmonic-forge#909).

    One pass decides each character once: a backslash doubles, a control
    character other than newline and tab becomes `\\uXXXX`, and in the
    multi-line form a quote is escaped only when it would otherwise start a
    `\"\"\"` run or touch the closing delimiter."""
    multi = "\n" in text
    out = []
    for i, ch in enumerate(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch in "\n\t" and (multi or ch == "\t"):
            out.append(ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        elif ch == '"' and (not multi or text.startswith('""', i + 1) or i == len(text) - 1):
            out.append('\\"')
        else:
            out.append(ch)
    body = "".join(out)
    return f'"""\n{body}"""' if multi else f'"{body}"'


def in_repo(key: str, repo: Path | None = None) -> bool:
    """True when a baseline key names a file inside the repo being measured.

    Keys `_relative_key` wrote as `~`-relative or absolute are outside. A
    repo-relative key is inside unless it is a symlink whose real path leaves
    the repo (harmonic-forge#1414): hrse's `.claude/rules/lane-shorthand.md`
    links to the platform checkout, so its growth is forge's, and gating it
    in hrse failed a commit for bytes no hrse author wrote. Resolution is
    non-strict, so a deleted tracked file stays in-repo.

    `repo=None` keeps the key-shape answer only; `check()` must always pass
    `repo` (a test fails if any of its call sites does not).

    This is what the gate narrows to (harmonic-forge#538). Out-of-repo files
    stay measured, stay in the total, and stay in the printed breakdown; they
    simply cannot fail a commit in a repo that does not contain them.
    """
    if key.startswith("~") or key.startswith("/"):
        return False
    if repo is None:
        return True
    root = repo.resolve()
    try:
        (repo / key).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _credited_shrink(baseline: dict, key: str, current: dict[str, int]) -> bool:
    """Whether a shrunk key's bytes may offset in-repo growth (harmonic-forge#1414
    reforge). Whether those bytes were this repo's is a fact about when the
    baseline was written, so it is read from the baseline's `outside_files`, never
    inferred now: a key listed there is never credited, a key not listed is (a
    deleted tracked file still counts, AC2). A baseline written before the field
    existed keeps the pre-#1414 rule and credits every shrunk key, so a deleted
    in-repo file still counts (AC2) and no existing `[[increase]]` changes
    meaning; the next `--update` writes `outside_files` and the stricter rule
    applies from then on (reforge pass 1)."""
    outside = baseline.get("outside_files")
    if isinstance(outside, list):
        return key not in outside
    return True


def _was_in_repo(baseline: dict, key: str) -> bool:
    """Whether the baseline counted `key`'s bytes as this repo's: not listed in
    its `outside_files`, or, for a pre-#1414 baseline, an in-repo key shape."""
    outside = baseline.get("outside_files")
    if isinstance(outside, list):
        return key not in outside
    return not (key.startswith("~") or key.startswith("/"))


def _symlink_note(repo: Path, key: str) -> str:
    """` (symlink outside the repo: <target>)` for a repo-relative key that
    resolves outside, else empty. Never an absolute path: this line reaches
    CI logs, so an absolute link target is rendered relative to the repo's
    parent, falling back to the bare key."""
    if key.startswith("~") or key.startswith("/"):
        return ""
    link = repo / key
    if not link.is_symlink():
        return ""
    target = os.readlink(link)
    if os.path.isabs(target):
        # Never `os.path.relpath`: from a checkout whose parent does not hold
        # the target (a `.worktrees/` checkout, a scratch copy) it climbs
        # `../../..` to the root and spells the home path out anyway.
        absolute = Path(target)
        for base, prefix in ((repo.resolve().parent, "../"), (Path.home(), "~/")):
            try:
                target = prefix + str(absolute.relative_to(base))
                break
            except ValueError:
                continue
        else:
            target = key
    return f" (symlink outside the repo: {target})"


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

    # harmonic-forge#538: the GATE narrows to files the repo controls; the
    # REPORT does not. 29% of HRSE2's baselined surface is `~/.claude/CLAUDE.md`
    # and the shared memory index, and a memory write is a standing directive
    # every session follows — so gating on those turned a routine, instructed
    # action into a red build plus a bookkeeping commit in an unrelated repo.
    # A gate that a session breaks by doing as it is told gets commented out,
    # and then the ratchet protects nothing.
    # harmonic-forge#1414: the platform's own rule links are measured from
    # this checkout's file (context_budget.surface), so they are this repo's
    # growth even when the link itself points at another checkout.
    platform_keys = {f".claude/rules/{n}" for n in _platform_rule_sources(repo)}

    gated_grew = {key: delta for key, delta in grew.items()
                  if key in platform_keys or in_repo(key, repo=repo)}
    # harmonic-forge#1414 sticky-wicket PATCH: growth is gated by where a key is
    # now; shrinkage by where the baseline recorded it, so a rule replaced by a
    # link to a smaller outside file still credits the bytes it took away.
    gated_shrank = {key: delta for key, delta in shrank.items()
                    if key in platform_keys or _was_in_repo(baseline, key)}
    uncredited = sorted(key for key in gated_shrank
                        if not _credited_shrink(baseline, key, current))
    gated_net = (sum(gated_grew.values())
                 + sum(delta for key, delta in gated_shrank.items()
                       if key not in uncredited))
    outside_grew = {key: delta for key, delta in grew.items()
                    if not (key in platform_keys or in_repo(key, repo=repo))}
    outside_note = ""
    if outside_grew:
        named = ", ".join(f"{key} +{outside_grew[key]}{_symlink_note(repo, key)}"
                          for key in sorted(outside_grew))
        outside_note = (
            f"\n  outside this repo, reported and NOT gated: {named}."
            f"\n  Nothing a commit here can be accountable for — re-baseline "
            f"with `--update` when convenient (harmonic-forge#538).")
    if uncredited:
        outside_note += (
            f"\n  outside this repo when the baseline was written (or missing "
            f"under a pre-#1414 baseline), NOT credited as shrinkage: "
            f"{', '.join(uncredited)}.")

    if gated_net <= 0:
        note = ""
        if any((key in platform_keys or _was_in_repo(baseline, key))
               and key not in uncredited for key in shrank):
            note = (f"\n  in-repo surface shrank by {-gated_net} bytes — no "
                    f"acknowledgement needed. Run `--update` to lock it in.")
        return 0, headline + note + outside_note
    failures = validate_increase(baseline, gated_grew, gated_net)
    if failures:
        return 1, (failure_message(repo, total, gated_grew, gated_net, failures)
                   + outside_note)
    latest = (baseline.get("increase") or [])[-1]
    return 0, (headline + f"\n  grew by {gated_net} bytes, acknowledged "
               f"{latest.get('date')}: {latest.get('why')}" + outside_note)


def update(repo: Path) -> str:
    """Rewrite the baseline to the current measurement, preserving history."""
    current = measure(repo)
    existing = load_baseline(repo)
    increases = list(existing.get("increase") or []) if existing else []
    platform_keys = {f".claude/rules/{n}" for n in _platform_rule_sources(repo)}
    outside = [key for key in current if not (key in platform_keys or in_repo(key, repo=repo))]
    path = baseline_path(repo)
    path.write_text(render_baseline(current, increases, outside), encoding="utf-8")
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
