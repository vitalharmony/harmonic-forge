#!/usr/bin/env python3
"""
harmonic-forge bootstrapper.

Wires a project's .claude/rules/, .claude/agents/, and .claude/skills/ into
this platform repo's universal sources via symlinks, so every project always
reads the current platform content rather than a stale copy.

Usage:
    python3 ~/harmonic-forge/sync_rules.py --project /path/to/project
    python3 ~/harmonic-forge/sync_rules.py --project /path/to/project --skill _stub
    python3 ~/harmonic-forge/sync_rules.py --pull
"""

import argparse
import subprocess
import sys
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent
RULES_DIR = PLATFORM_ROOT / "rules"
AGENTS_DIR = PLATFORM_ROOT / "agents"
SKILLS_DIR = PLATFORM_ROOT / "skills"

# Rule files that are universal across every project's stack.
UNIVERSAL_RULE_FILES = [
    "backend-python.md",
    "frontend-typescript.md",
    # harmonic-forge#289: unlike the two above, this one is deliberately NOT
    # path-scoped -- it carries no `paths:` frontmatter, so it loads in every
    # session rather than when a matching file is opened. Shorthand arrives in
    # the operator's first message, before any file is open, and a table a
    # session has to choose to go read is a table it will guess instead.
    "lane-shorthand.md",
]

# hrse#678's precedent (a real vertical package, on disk, inert until
# explicitly switched on) is "on disk, inert by default" — NOT "distributed
# by default." A skill has no activation gate the way a vertical does: its
# description is surfaced in every session's skill listing and is directly
# invocable the moment it's linked. Empty by default, same as
# UNIVERSAL_RULE_FILES was before any rule existed — a project opts a skill
# in explicitly via --skill, never gets one for free just by existing.
# harmonic-forge#207's impl-worktree becomes this list's first real entry.
UNIVERSAL_SKILL_DIRS: list[str] = []

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


def _link_skill_dir(source_dir: Path, target_dir: Path, skill_names: list[str], label: str) -> bool:
    """Symlinks target_dir/<skill_name> -> source_dir/<skill_name> as whole-directory
    symlinks. Skills are directories (SKILL.md plus optional sibling files like
    agents/openai.yaml), not flat files — _link_dir's per-filename model doesn't apply.

    A stale link pointing elsewhere is only auto-relinked if it currently points
    somewhere *inside this platform's own skills dir* (a previous, now-renamed
    platform skill) — never a foreign symlink a project owns for its own reasons
    (live precedent: .claude/skills/ai-review-queue-synthesis is a project-owned
    symlink to Google Drive, not platform-owned; auto-relinking it would silently
    destroy that project's own configuration). A real (non-symlink) directory at
    the target is refused-and-messaged, same as _link_dir's own file-level
    precedent — a directory is a bigger unit than a file, making "never silently
    overwrite" more important here, not less.
    """
    if not skill_names:
        return True  # nothing opted in — don't create an empty .claude/skills/

    target_dir.mkdir(parents=True, exist_ok=True)
    platform_skills_root = SKILLS_DIR.resolve()

    ok = True
    for name in skill_names:
        source = source_dir / name
        target = target_dir / name

        if not source.is_dir():
            print(f"[ERROR] Platform {label} directory missing: {source}", file=sys.stderr)
            ok = False
            continue

        if target.is_symlink():
            resolved = target.resolve()
            if resolved == source.resolve():
                print(f"[OK] {target} already linked correctly.")
                continue
            if platform_skills_root in resolved.parents:
                print(f"[FIX] {target} points elsewhere in the platform skills dir — relinking.")
                target.unlink()
            else:
                print(
                    f"[SKIP] {target} is a symlink to {resolved}, outside the platform "
                    f"skills dir. Remove it manually if you want the platform version.",
                    file=sys.stderr,
                )
                ok = False
                continue
        elif target.exists():
            print(
                f"[SKIP] {target} exists as a real directory, not a symlink. "
                f"Remove or back it up manually, then re-run.",
                file=sys.stderr,
            )
            ok = False
            continue

        target.symlink_to(source, target_is_directory=True)
        print(f"[LINK] {target} -> {source}")

    return ok


def _verify_skill_dir(target_dir: Path, skill_names: list[str]) -> bool:
    """Confirms every expected skill directory symlink resolves to a real,
    loadable skill (the directory exists, contains SKILL.md, AND actually
    resolves to this platform's own skills dir — not just any directory that
    happens to contain a SKILL.md, which would pass a plausibility check but
    not an identity check)."""
    all_good = True
    for name in skill_names:
        target = target_dir / name
        if not target.is_symlink():
            print(f"[BROKEN] {target} is not a symlink.", file=sys.stderr)
            all_good = False
            continue
        resolved = target.resolve()
        expected = (SKILLS_DIR / name).resolve()
        if resolved != expected:
            print(f"[BROKEN] {target} resolves to {resolved}, expected {expected}.", file=sys.stderr)
            all_good = False
            continue
        if not resolved.is_dir():
            print(f"[BROKEN] {target} points to a missing directory.", file=sys.stderr)
            all_good = False
            continue
        if not (resolved / "SKILL.md").exists():
            print(f"[BROKEN] {target} does not contain SKILL.md.", file=sys.stderr)
            all_good = False
    return all_good


def link_project(project_root: Path, skill_names: list[str] | None = None) -> bool:
    """Symlinks project .claude/rules/, .claude/agents/, and .claude/skills/
    (opted-in only) to platform sources."""
    rules_ok = _link_dir(RULES_DIR, project_root / ".claude" / "rules", UNIVERSAL_RULE_FILES, "rule")
    agent_files = _universal_agent_files()
    agents_ok = _link_dir(AGENTS_DIR, project_root / ".claude" / "agents", agent_files, "agent")
    skills_ok = _link_skill_dir(
        SKILLS_DIR, project_root / ".claude" / "skills", skill_names or [], "skill"
    )
    return rules_ok and agents_ok and skills_ok


def verify_links(project_root: Path, skill_names: list[str] | None = None) -> bool:
    """Confirms every expected rule, agent, and (opted-in) skill symlink
    resolves to the platform source."""
    rules_ok = _verify_dir(project_root / ".claude" / "rules", UNIVERSAL_RULE_FILES)
    agents_ok = _verify_dir(project_root / ".claude" / "agents", _universal_agent_files())
    skills_ok = _verify_skill_dir(project_root / ".claude" / "skills", skill_names or [])
    return rules_ok and agents_ok and skills_ok


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
    parser.add_argument(
        "--skill", action="append", default=[],
        help="Additionally link this platform skill dir (repeatable). "
             "UNIVERSAL_SKILL_DIRS is empty by default — a skill's description "
             "is surfaced and directly invocable the moment it's linked, unlike "
             "an inert-until-activated vertical, so nothing is distributed for "
             "free just by existing in harmonic-forge/skills/.",
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

        skill_names = UNIVERSAL_SKILL_DIRS + args.skill

        if not link_project(project_root, skill_names):
            return 1

        if not verify_links(project_root, skill_names):
            print("[ERROR] Symlink verification failed.", file=sys.stderr)
            return 1

        summary = "rules and agents" if not skill_names else f"rules, agents, and skills ({', '.join(skill_names)})"
        print(f"\n[OK] {project_root} is linked to harmonic-forge {summary}.")
        print_remaining_steps(project_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
