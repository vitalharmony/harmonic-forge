#!/usr/bin/env python3
"""Query the rule registry, and produce the two measurements it exists for
(harmonic-forge#447).

    --id R-0001                 one rule
    --file rules/universal-agent.md   rules annotated in one file
    --enforcement hook|prose|unenforced
    --hooks-report              AC3: rules<->live wiring, both directions
    --duplicates                AC4: prose rules restating a hook-enforced rule
    --folded                    obligations with no ID of their own

WHY THE HOOK SCAN IS LIVE AND SCOPE-QUALIFIED
-----------------------------------------------
"Is rule X mechanized?" has no single answer. Hook wiring is **not uniform
across checkouts** — several hooks are HRSE2-only; `model_tier_gate` and
`batch_gate` are absent from `harmonic-forge-lane2`; `HRSE2-lane2` wires
`drift_check`/`milestone_summary` that main HRSE2 does not;
`block_missing_preclose_inspection` is absent from both lane-2 worktrees.
A scalar `enforcement` field would therefore be false somewhere no matter
what it said. So the registry stores `hooks = [{script, events, wired_in}]`
and every count here names the scope it was taken in.

Two hooks are easy to miss and are why this scans frontmatter and
`tools/gh/` too, not just `tools/hooks/` in one settings file:

* `tools/gh/block_closing_keywords.py` — wired only in the user-global
  `~/.claude/settings.json`, and lives outside `tools/hooks/`.
* `tools/hooks/deny_advisory_subagent_gh_writes.py` — wired only in agent
  frontmatter (`pitch-inspection`, `preclose-inspection`,
  `product-strategy`, `sticky-wicket`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLATFORM_ROOT = _HERE.parent.parent
_DEFAULT_REGISTRY = _HERE / "registry.toml"
_HOME = Path.home()

#: Every location that wires a hook, checked live. Missing paths are
#: reported as absent rather than skipped silently — an absent checkout
#: changes what "mechanized" means and the reader must see that.
_WIRING_LOCATIONS = {
    "user-global": _HOME / ".claude" / "settings.json",
    "HRSE2": _HOME / "Harmonic_Projects" / "HRSE2" / ".claude" / "settings.json",
    "HRSE2-lane2": _HOME / "Harmonic_Projects" / "HRSE2-lane2" / ".claude" / "settings.json",
    "HRSE2-lane3": _HOME / "Harmonic_Projects" / "HRSE2-lane3" / ".claude" / "settings.json",
    "forge": _HOME / "harmonic-forge" / ".claude" / "settings.json",
    "forge-lane2": _HOME / "Harmonic_Projects" / "harmonic-forge-lane2" / ".claude" / "settings.json",
    "forge-lane3": _HOME / "Harmonic_Projects" / "harmonic-forge-lane3" / ".claude" / "settings.json",
    # harmonic-forge#1307: the Forge lane hook set every Claude lane launch
    # passes as `--settings`, in any project. Read from THIS checkout.
    "forge-lane-set": _PLATFORM_ROOT / "tools" / "lane" / "lane_hooks.json",
    # harmonic-forge#1525: the operator overlay merged into that set when
    # instance.toml declares `lane_hook_overlay = "operator"`. This scope
    # reports where the wiring is DECLARED, read statically; with the overlay
    # declared `none` it is not in effect (a stated residual, operator
    # decision 2026-11-25: no instance.toml read here).
    "forge-lane-overlay (if declared)": _PLATFORM_ROOT / "tools" / "lane" / "lane_hooks.operator.json",
}

_SCRIPT_RE = re.compile(r"([\w./-]+\.py)")
#: A frontmatter `command:` field, the only frontmatter text that wires a hook (harmonic-forge#1594).
_COMMAND_FIELD_RE = re.compile(r"^\s*(?:-\s*)?command:(.*)$", re.MULTILINE)


def _scripts_in_settings(path: Path) -> dict[str, set[str]]:
    """`{script_basename: {event, ...}}` from one settings.json."""
    found: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return found
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return found
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries or []:
            for hook in entry.get("hooks", []) or []:
                for match in _SCRIPT_RE.findall(hook.get("command", "")):
                    found[Path(match).name].add(event)
    return found


def _scripts_in_agent_frontmatter(root: Path) -> dict[str, set[str]]:
    """Hooks wired in `agents/*.md` frontmatter — invisible to any
    settings.json scan, and the only wiring for
    `deny_advisory_subagent_gh_writes.py`."""
    found: dict[str, set[str]] = defaultdict(set)
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return found
    for path in sorted(agents_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        frontmatter = text[:end if end > 0 else len(text)]
        if "hooks:" not in frontmatter:
            continue
        for command in _COMMAND_FIELD_RE.findall(frontmatter):
            for match in _SCRIPT_RE.findall(command):
                found[Path(match).name].add(f"agent:{path.stem}")
    return found


def live_wiring(root: Path) -> dict[str, dict[str, set[str]]]:
    """`{scope: {script: {events}}}` across every wiring location."""
    wiring = {scope: _scripts_in_settings(path) for scope, path in _WIRING_LOCATIONS.items()}
    wiring["agent-frontmatter"] = _scripts_in_agent_frontmatter(root)
    return wiring


def load_rules(registry: Path) -> list[dict]:
    with registry.open("rb") as handle:
        return tomllib.load(handle).get("rule", [])


def enforcement_of(rule: dict) -> str:
    """Derived, never stored — a stored scalar would be false in some scope."""
    if rule.get("hooks"):
        return "hook"
    return rule.get("enforcement", "prose")


def _hook_claims(rule: dict) -> tuple[list[tuple[str, object]], list[str]]:
    """`([(script_path, entry), ...], [MALFORMED finding, ...])` for one rule's
    `hooks` field: a list of tables with a string `script`, or of path strings
    (harmonic-forge#1594). Anything else is a finding naming the rule."""
    hooks = rule.get("hooks")
    if hooks is None:
        return [], []
    if not isinstance(hooks, list):
        return [], [f"rule {rule['id']}: `hooks` field of type {type(hooks).__name__} is not a list"]
    claims: list[tuple[str, object]] = []
    malformed: list[str] = []
    for entry in hooks:
        script = entry.get("script") if isinstance(entry, dict) else entry
        if isinstance(script, str) and script:
            claims.append((script, entry))
        elif isinstance(entry, dict):
            malformed.append(f"rule {rule['id']}: hook table has no non-empty string `script` ({type(script).__name__})")
        else:
            malformed.append(f"rule {rule['id']}: hook entry of type {type(entry).__name__} is neither a table nor a path")
    return claims, malformed


def _print_rule(rule: dict) -> list[str]:
    """Print one rule; return its MALFORMED `hooks` findings."""
    print(f"{rule['id']}  [{enforcement_of(rule)}]  {rule['file']}")
    print(f"    {rule['statement']}")
    claims, malformed = _hook_claims(rule)
    for script, hook in claims:
        extra = f"  events={hook.get('events', [])}  wired_in={hook.get('wired_in', [])}" if isinstance(hook, dict) else ""
        print(f"    hook: {script}{extra}")
    for finding in malformed:
        print(f"    MALFORMED: {finding}")
    if rule.get("restates"):
        print(f"    restates: {rule['restates']}")
    return malformed


def claims_by_script(rules: list[dict]) -> tuple[dict[str, set[str]], list[str]]:
    """`({basename: {rule_id}}, MALFORMED findings)`: a full path and its
    basename are one claim (AC2)."""
    claimed: dict[str, set[str]] = defaultdict(set)
    malformed: list[str] = []
    for rule in rules:
        claims, bad = _hook_claims(rule)
        malformed += bad
        for path, _ in claims:
            claimed[Path(path).name].add(rule["id"])
    return claimed, malformed


def hooks_report(rules: list[dict], root: Path) -> int:
    """AC3, both directions: an unbacked `hook` claim and an unmapped live
    hook are each a finding, not a silent pass."""
    wiring = live_wiring(root)
    live_scripts: dict[str, set[str]] = defaultdict(set)
    for scope, scripts in wiring.items():
        for script in scripts:
            live_scripts[script].add(scope)

    print("=== live hook wiring, by scope ===")
    for scope in list(_WIRING_LOCATIONS) + ["agent-frontmatter"]:
        scripts = wiring.get(scope, {})
        path = _WIRING_LOCATIONS.get(scope)
        if path is not None and not path.exists():
            print(f"  {scope:<18} (settings file absent)")
        else:
            print(f"  {scope:<18} {len(scripts)} script(s)")

    claimed, malformed = claims_by_script(rules)
    unbacked = sorted(s for s in claimed if s not in live_scripts)
    unmapped = sorted(s for s in live_scripts if s not in claimed)

    print(f"\n=== rules claiming a hook: {len(claimed)} distinct script(s) ===")
    print(f"=== live hook scripts: {len(live_scripts)} ===")
    if unbacked:
        print(f"\nUNBACKED `hook` claims — rule names a script found in no wiring location ({len(unbacked)}):")
        for script in unbacked:
            print(f"  {script}  claimed by {sorted(claimed[script])}")
    if unmapped:
        print(f"\nUNMAPPED live hooks — wired but named by no rule ({len(unmapped)}):")
        for script in unmapped:
            print(f"  {script}  wired in {sorted(live_scripts[script])}")
    if malformed:
        print(f"\nMALFORMED `hooks` entries ({len(malformed)}):", *malformed, sep="\n  ")
    if not unbacked and not unmapped and not malformed:
        print("\nEvery claimed hook is live and every live hook maps to a rule.")
    return 1 if (unbacked or unmapped or malformed) else 0


def folded_report(rules: list[dict]) -> int:
    """Obligations known to be buried inside another rule's span.

    Markers are line-anchored (`check_rule_drift._OPEN`/`_CLOSE` are `^...$`),
    so a rule boundary falling mid-line cannot be expressed. The protocol's
    prose wraps at ~72 characters, so a second obligation inside a paragraph
    usually starts mid-line. Rather than relax the anchor scheme or reflow
    prose (both rejected, operator decision 2026-09-03), these are recorded
    here so the undercount is VISIBLE rather than silent — the registry's
    honesty about its own coverage is the point.

    These cannot be individually counted or retired. That is the accepted
    cost, and this report is what keeps it from being forgotten.
    """
    folded = [r for r in rules if r.get("folded_obligations")]
    total = sum(len(r["folded_obligations"]) for r in folded)
    for rule in folded:
        print(f"{rule['id']} ({rule['file']}) — {len(rule['folded_obligations'])} folded:")
        for obligation in rule["folded_obligations"]:
            print(f"    {obligation}")
    print()
    print(f"rules carrying a folded obligation: {len(folded)}")
    print(f"obligations with no ID of their own: {total}")
    print(f"  registry undercounts by {total} against {len(rules)} annotated "
          f"({total / (len(rules) + total) * 100:.1f}% of true total)")
    return 0


def duplicates_report(rules: list[dict]) -> int:
    """AC4 — the first measurement the registry exists to make, and the
    number the corpus trim is sized from. Stored via `restates`, not
    recomputed by guesswork each run."""
    by_id = {rule["id"]: rule for rule in rules}
    duplicates = [r for r in rules if r.get("restates")]
    print("=== AC4: prose rules restating a hook-enforced rule ===")
    hook_backed = 0
    for rule in duplicates:
        target = by_id.get(rule["restates"])
        target_enf = enforcement_of(target) if target else "<missing>"
        if target_enf == "hook":
            hook_backed += 1
        print(f"  {rule['id']} [{enforcement_of(rule)}] restates {rule['restates']} [{target_enf}]")
        print(f"      {rule['statement'][:96]}")
    print(f"\nrules restating another rule: {len(duplicates)}")
    print(f"  of those, restating a HOOK-ENFORCED rule: {hook_backed}")
    print("  (scope: hook-enforcement derived from the live wiring union across all "
          "locations — a rule mechanized in only some checkouts still counts as hook-backed here)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--root", type=Path, default=_PLATFORM_ROOT)
    parser.add_argument("--id")
    parser.add_argument("--file")
    parser.add_argument("--enforcement", choices=["hook", "prose", "unenforced"])
    parser.add_argument("--hooks-report", action="store_true")
    parser.add_argument("--duplicates", action="store_true")
    parser.add_argument("--folded", action="store_true")
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()

    rules = load_rules(args.registry)

    if args.hooks_report:
        return hooks_report(rules, args.root)
    if args.folded:
        return folded_report(rules)
    if args.duplicates:
        return duplicates_report(rules)
    if args.count:
        by_file: dict[str, int] = defaultdict(int)
        by_enf: dict[str, int] = defaultdict(int)
        for rule in rules:
            by_file[rule["file"]] += 1
            by_enf[enforcement_of(rule)] += 1
        print(f"total rules: {len(rules)}")
        for name, count in sorted(by_file.items()):
            print(f"  {name:<32} {count}")
        print("by enforcement:")
        for name, count in sorted(by_enf.items()):
            print(f"  {name:<12} {count}")
        return 0

    selected = rules
    if args.id:
        selected = [r for r in rules if r["id"] == args.id]
        if not selected:
            print(f"no such rule: {args.id}", file=sys.stderr)
            return 1
    if args.file:
        selected = [r for r in selected if r["file"] == args.file]
    if args.enforcement:
        selected = [r for r in selected if enforcement_of(r) == args.enforcement]

    malformed = [finding for rule in selected for finding in _print_rule(rule)]
    print(f"\n{len(selected)} rule(s)")
    return 1 if malformed else 0


if __name__ == "__main__":
    raise SystemExit(main())
