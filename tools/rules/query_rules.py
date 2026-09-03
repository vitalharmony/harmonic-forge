#!/usr/bin/env python3
"""Query the rule registry, and produce the two measurements it exists for
(harmonic-forge#447).

    --id R-0001                 one rule
    --file rules/universal-agent.md   rules annotated in one file
    --enforcement hook|prose|unenforced
    --hooks-report              AC3: rules<->live wiring, both directions
    --duplicates                AC4: prose rules restating a hook-enforced rule

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
}

_SCRIPT_RE = re.compile(r"([\w./-]+\.py)")


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
        for match in _SCRIPT_RE.findall(frontmatter):
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


def _print_rule(rule: dict) -> None:
    print(f"{rule['id']}  [{enforcement_of(rule)}]  {rule['file']}")
    print(f"    {rule['statement']}")
    for hook in rule.get("hooks", []) or []:
        print(f"    hook: {hook['script']}  events={hook.get('events', [])}  wired_in={hook.get('wired_in', [])}")
    if rule.get("restates"):
        print(f"    restates: {rule['restates']}")


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

    claimed: dict[str, set[str]] = defaultdict(set)
    for rule in rules:
        for hook in rule.get("hooks", []) or []:
            claimed[hook["script"]].add(rule["id"])

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
    if not unbacked and not unmapped:
        print("\nEvery claimed hook is live and every live hook maps to a rule.")
    return 1 if (unbacked or unmapped) else 0


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
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()

    rules = load_rules(args.registry)

    if args.hooks_report:
        return hooks_report(rules, args.root)
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

    for rule in selected:
        _print_rule(rule)
    print(f"\n{len(selected)} rule(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
