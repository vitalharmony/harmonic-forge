#!/usr/bin/env python3
"""
harmonic-forge bootstrapper.

Wires a project's .claude/rules/, .claude/agents/, and .claude/skills/ into
this platform repo's universal sources via symlinks, so every project always
reads the current platform content rather than a stale copy.

Usage:
    python3 ~/harmonic-forge/sync_rules.py --project /path/to/project
    python3 ~/harmonic-forge/sync_rules.py --project /path/to/project --skill _stub
    python3 ~/harmonic-forge/sync_rules.py --verify --project /path/to/project
    python3 ~/harmonic-forge/sync_rules.py --pull

The symlinks this writes are gitignored in every consuming repo, so they travel
with no branch, clone, or worktree. That is deliberate -- machine-specific
absolute paths must not be committed -- but it means the linked state is not
recoverable from the repo, and for a long time nothing recorded what SHOULD be
linked either. `--verify`, and the per-project manifest it reads, are that
record (harmonic-forge#540/#541).
"""

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent
RULES_DIR = PLATFORM_ROOT / "rules"
AGENTS_DIR = PLATFORM_ROOT / "agents"
SKILLS_DIR = PLATFORM_ROOT / "skills"

# Three-valued, matching this repo's own established idiom (`forge-onboard`,
# `batch-preflight`): 0 all green, 1 a check failed, 2 the run could not
# happen. A caller can then tell a finding from a misconfiguration, which a
# bare 0/1 cannot express.
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CANNOT_RUN = 2

# Where a consuming repo declares the platform skills it consumes. Tracked in
# the CONSUMING repo -- deliberately not in this repo's projects.toml -- so the
# declaration survives a clone, a branch, and a new worktree. That is precisely
# the property the gitignored symlink lacks, and its absence is the whole of
# harmonic-forge#540.
SKILLS_MANIFEST_RELPATH = Path(".claude") / "platform-skills.toml"
_MANIFEST_KNOWN_KEYS = {"skills"}

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


class ManifestError(RuntimeError):
    """The consuming repo's skills manifest exists but cannot be trusted."""


def manifest_path(project_root: Path) -> Path:
    return project_root / SKILLS_MANIFEST_RELPATH


def load_skill_manifest(project_root: Path) -> list[str] | None:
    """Platform skills the consuming repo declares, or None when it declares none.

    **None is not an empty list, and the distinction is the entire point.**
    `UNIVERSAL_SKILL_DIRS = []` made "nothing declared" indistinguishable from
    "nothing expected," so every unlinked checkout verified clean -- the defect
    harmonic-forge#540 measured across nine checkouts. A caller that collapses
    these two states reinstates it exactly, so this returns a sentinel the type
    checker forces the caller to handle rather than a falsy list it can ignore.

    Raises rather than falling back on a malformed manifest, following
    `tools/onboard/manifest.py` and `tools/rules/check_rule_drift.py`'s
    `load_band()`: a loader that swallowed a parse error would report the same
    green as a correctly-linked repo, which is the failure being fixed.
    """
    target = manifest_path(project_root)
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {target}: {exc}") from exc

    # A typo in a manifest is the silent drift this file exists to end:
    # `skill = [...]` would parse, load, and declare nothing, with no complaint.
    unknown = set(raw) - _MANIFEST_KNOWN_KEYS
    if unknown:
        raise ManifestError(
            f"{target}: unknown key(s): {', '.join(sorted(unknown))}")

    skills = raw.get("skills")
    if skills is None:
        raise ManifestError(f"{target}: declares no `skills` key")
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        raise ManifestError(f"{target}: `skills` must be a list of strings")

    dupes = sorted({s for s in skills if skills.count(s) > 1})
    if dupes:
        raise ManifestError(f"{target}: `skills` repeats {', '.join(dupes)}")

    return skills


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


def verify_project(project_root: Path, extra_skills: list[str] | None = None) -> int:
    """Read the declaration, compare the links against it, report. Mutates nothing.

    Note the asymmetry with `link_project`: this never calls `mkdir`, so
    verifying a checkout that has no `.claude/` at all reports it rather than
    quietly creating one. Four of the nine checkouts harmonic-forge#540
    measured were in exactly that state.
    """
    try:
        declared = load_skill_manifest(project_root)
    except ManifestError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if declared is None:
        print(
            f"[UNDECLARED] {manifest_path(project_root)} does not exist — "
            "nothing states which platform skills this repo consumes.",
            file=sys.stderr,
        )
        print(
            "  This is not a pass, and nothing was compared: an undeclared "
            "repo and a correctly-linked one are indistinguishable without it "
            "(harmonic-forge#540).",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    # An explicit `skills = []` IS a declaration and verifies green. That is the
    # whole difference between a repo that has decided it consumes no platform
    # skills and one that has never been asked.
    skill_names = sorted(set(declared) | set(extra_skills or []))
    if not verify_links(project_root, skill_names):
        print(
            f"[DRIFT] {project_root} does not match its platform declaration.",
            file=sys.stderr,
        )
        return EXIT_DRIFT

    summary = ", ".join(skill_names) if skill_names else "no skills declared"
    print(f"[OK] {project_root} matches its platform declaration ({summary}).")
    return EXIT_OK


def print_remaining_steps(project_root: Path) -> None:
    print("\n[REMAINING STEPS]")
    print(f"  1. Confirm {project_root}/CLAUDE.md points to harmonic-forge/3-lane-protocol.md")
    print(f"  2. Confirm {project_root}/.claude/rules/ only carries project-specific overrides")
    print("  3. Read harmonic-forge/3-lane-protocol.md before pulling a first ticket")
    print("  4. Re-run with --pull whenever platform rules or agents change")


def main(argv: list[str] | None = None) -> int:
    # argv is injectable so the CLI surface itself is testable -- `--verify`
    # existing in --help is one of harmonic-forge#541's acceptance criteria,
    # and the defect it fixes (verify_links reachable only as a side effect of
    # --project) is invisible to any test that calls the functions directly.
    parser = argparse.ArgumentParser(description="harmonic-forge sync bootstrapper")
    parser.add_argument("--project", type=str, help="Path to the project root to link")
    parser.add_argument(
        "--pull", action="store_true", help="Pull latest platform rules via git"
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Report platform-link drift against the consuming repo's "
             f"{SKILLS_MANIFEST_RELPATH} and exit non-zero on a finding. Links "
             "and mutates nothing. Exit 0 verified, 1 drift, 2 the run could "
             "not happen (no manifest, malformed manifest, bad path) — an "
             "absent manifest is never green.",
    )
    parser.add_argument(
        "--skill", action="append", default=[],
        help="Additionally link this platform skill dir (repeatable). "
             "UNIVERSAL_SKILL_DIRS is empty by default — a skill's description "
             "is surfaced and directly invocable the moment it's linked, unlike "
             "an inert-until-activated vertical, so nothing is distributed for "
             "free just by existing in harmonic-forge/skills/. Prefer declaring "
             f"the skill in the project's {SKILLS_MANIFEST_RELPATH}, which "
             "travels with a clone; this flag does not.",
    )
    args = parser.parse_args(argv)

    if args.verify and not args.project:
        print("[ERROR] --verify needs --project <path>.", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if not args.project and not args.pull:
        parser.print_help()
        return EXIT_CANNOT_RUN

    if args.pull:
        if not pull_platform():
            return EXIT_CANNOT_RUN
        if not args.project:
            return EXIT_OK

    if args.project:
        project_root = Path(args.project).resolve()
        if not project_root.is_dir():
            print(f"[ERROR] Not a directory: {project_root}", file=sys.stderr)
            return EXIT_CANNOT_RUN

        if args.verify:
            return verify_project(project_root, args.skill)

        try:
            declared = load_skill_manifest(project_root)
        except ManifestError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return EXIT_CANNOT_RUN

        if declared is None:
            print(
                f"[UNDECLARED] no {SKILLS_MANIFEST_RELPATH} in {project_root} — "
                "linking only what --skill names on this invocation. A flag "
                "someone has to remember is what left belt-and-suspenders "
                "inert in nine checkouts; declare the manifest so the opt-in "
                "survives a clone (harmonic-forge#540).",
                file=sys.stderr,
            )

        skill_names = sorted(
            set(UNIVERSAL_SKILL_DIRS) | set(declared or []) | set(args.skill)
        )

        if not link_project(project_root, skill_names):
            return EXIT_DRIFT

        if not verify_links(project_root, skill_names):
            print("[ERROR] Symlink verification failed.", file=sys.stderr)
            return EXIT_DRIFT

        summary = "rules and agents" if not skill_names else f"rules, agents, and skills ({', '.join(skill_names)})"
        print(f"\n[OK] {project_root} is linked to harmonic-forge {summary}.")
        print_remaining_steps(project_root)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
