#!/usr/bin/env python3
"""
harmonic-forge bootstrapper.

Wires a project's .claude/rules/ and .claude/agents/ into this platform
repo's universal rule/agent files via symlinks, so every project always
reads the current platform rules/agents rather than a stale copy.

Usage:
    python3 ~/harmonic-forge/sync_rules.py --project /path/to/project
    python3 ~/harmonic-forge/sync_rules.py --pull
"""

import argparse
import subprocess
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent
RULES_DIR = PLATFORM_ROOT / "rules"
AGENTS_DIR = PLATFORM_ROOT / "agents"

# Rule files that are universal across every project's stack.
UNIVERSAL_RULE_FILES = [
    "backend-python.md",
    "frontend-typescript.md",
]

# Devin Local advisory agent profiles, each a directory containing AGENT.md.
UNIVERSAL_AGENT_PROFILES = [
    "product-strategy",
    "sticky-wicket",
    "pitch-inspection",
]


def _universal_agent_files() -> list[str]:
    """Auto-discover every agent in harmonic-forge/agents/.

    No separate list to keep in sync: per harmonic-forge.md's own convention,
    anything placed in agents/ is definitionally meant to be universal and
    project-agnostic (see the note in harmonic-forge.md before adding one) —
    unlike rules/, which mixes universal files with ones a project opts
    into individually.
    """
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(p.name for p in AGENTS_DIR.glob("*.md"))


def pull_platform() -> bool:
    """Pulls the latest harmonic-forge rules via git."""
    print(f"[SYNC] Pulling latest platform rules in {PLATFORM_ROOT}...")
    result = subprocess.run(
        ["git", "-C", str(PLATFORM_ROOT), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] git pull failed:\n{result.stderr}", file=sys.stderr)
        return False
    print(result.stdout.strip() or "[SYNC] Already up to date.")
    return True


def _link_dir(source_dir: Path, target_dir: Path, filenames: list[str], label: str) -> bool:
    """Symlinks target_dir/<filename> -> source_dir/<filename> for each filename."""
    target_dir.mkdir(parents=True, exist_ok=True)

    ok = True
    for filename in filenames:
        source = source_dir / filename
        target = target_dir / filename

        if not source.exists():
            print(f"[ERROR] Platform {label} file missing: {source}", file=sys.stderr)
            ok = False
            continue

        if target.is_symlink():
            if target.resolve() == source.resolve():
                print(f"[OK] {target} already linked correctly.")
                continue
            print(f"[FIX] {target} points elsewhere — relinking.")
            target.unlink()
        elif target.exists():
            print(
                f"[SKIP] {target} exists as a real file, not a symlink. "
                f"Remove or back it up manually, then re-run.",
                file=sys.stderr,
            )
            ok = False
            continue

        target.symlink_to(source)
        print(f"[LINK] {target} -> {source}")

    return ok


def _link_profile_dirs(source_dir: Path, target_dir: Path, dirnames: list[str], label: str) -> bool:
    """Symlinks target_dir/<dirname> -> source_dir/<dirname> for agent profile directories."""
    target_dir.mkdir(parents=True, exist_ok=True)

    ok = True
    for dirname in dirnames:
        source = source_dir / dirname
        target = target_dir / dirname

        if not source.exists():
            print(f"[ERROR] Platform {label} directory missing: {source}", file=sys.stderr)
            ok = False
            continue

        if target.is_symlink():
            if target.resolve() == source.resolve():
                print(f"[OK] {target} already linked correctly.")
                continue
            print(f"[FIX] {target} points elsewhere — relinking.")
            target.unlink()
        elif target.exists():
            print(
                f"[SKIP] {target} exists as a real file/directory, not a symlink. "
                f"Remove or back it up manually, then re-run.",
                file=sys.stderr,
            )
            ok = False
            continue

        target.symlink_to(source, target_is_directory=True)
        print(f"[LINK] {target} -> {source}")

    return ok


def _verify_profile_dirs(target_dir: Path, dirnames: list[str]) -> bool:
    """Confirms every expected agent profile symlink in target_dir resolves to a real directory."""
    all_good = True
    for dirname in dirnames:
        target = target_dir / dirname
        if not target.is_symlink():
            print(f"[BROKEN] {target} is not a symlink.", file=sys.stderr)
            all_good = False
            continue
        if not target.resolve().exists():
            print(f"[BROKEN] {target} points to a missing directory.", file=sys.stderr)
            all_good = False
    return all_good


def _verify_dir(target_dir: Path, filenames: list[str]) -> bool:
    """Confirms every expected symlink in target_dir resolves to a real file."""
    all_good = True
    for filename in filenames:
        target = target_dir / filename
        if not target.is_symlink():
            print(f"[BROKEN] {target} is not a symlink.", file=sys.stderr)
            all_good = False
            continue
        if not target.resolve().exists():
            print(f"[BROKEN] {target} points to a missing file.", file=sys.stderr)
            all_good = False
    return all_good


def link_project(project_root: Path) -> bool:
    """Symlinks project .claude/rules/, .claude/agents/, and .devin/agents/ to platform sources."""
    rules_ok = _link_dir(RULES_DIR, project_root / ".claude" / "rules", UNIVERSAL_RULE_FILES, "rule")
    agent_files = _universal_agent_files()
    agents_ok = _link_dir(AGENTS_DIR, project_root / ".claude" / "agents", agent_files, "agent")
    profiles_ok = _link_profile_dirs(
        AGENTS_DIR,
        project_root / ".devin" / "agents",
        UNIVERSAL_AGENT_PROFILES,
        "agent profile",
    )
    return rules_ok and agents_ok and profiles_ok


def verify_links(project_root: Path) -> bool:
    """Confirms every expected rule and agent symlink resolves to the platform source."""
    rules_ok = _verify_dir(project_root / ".claude" / "rules", UNIVERSAL_RULE_FILES)
    agents_ok = _verify_dir(project_root / ".claude" / "agents", _universal_agent_files())
    profiles_ok = _verify_profile_dirs(
        project_root / ".devin" / "agents",
        UNIVERSAL_AGENT_PROFILES,
    )
    return rules_ok and agents_ok and profiles_ok


def print_remaining_steps(project_root: Path) -> None:
    print("\n[REMAINING STEPS]")
    print(f"  1. Confirm {project_root}/CLAUDE.md points to harmonic-forge/3-lane-protocol.md")
    print(f"  2. Confirm {project_root}/.windsurfrules only carries project-specific overrides")
    print("  3. Read harmonic-forge/3-lane-protocol.md before pulling a first ticket")
    print("  4. Re-run with --pull whenever platform rules or agents change")


def main() -> int:
    parser = argparse.ArgumentParser(description="harmonic-forge sync bootstrapper")
    parser.add_argument("--project", type=str, help="Path to the project root to link")
    parser.add_argument(
        "--pull", action="store_true", help="Pull latest platform rules via git"
    )
    args = parser.parse_args()

    if not args.project and not args.pull:
        parser.print_help()
        return 1

    if args.pull:
        if not pull_platform():
            return 1
        if not args.project:
            return 0

    if args.project:
        project_root = Path(args.project).resolve()
        if not project_root.is_dir():
            print(f"[ERROR] Not a directory: {project_root}", file=sys.stderr)
            return 1

        if not link_project(project_root):
            return 1

        if not verify_links(project_root):
            print("[ERROR] Symlink verification failed.", file=sys.stderr)
            return 1

        print(f"\n[OK] {project_root} is linked to harmonic-forge rules and agents.")
        print_remaining_steps(project_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
