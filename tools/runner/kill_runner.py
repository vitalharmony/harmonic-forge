#!/usr/bin/env python3
"""Harness-run kill checks from outside the worktree (harmonic-forge#1052, G4).

The unattended runner (epic harmonic-forge#1050) needs an oracle the
implementing agent cannot influence: Lane-1-authored tests, kept outside the
agent's worktree, that fail on the base commit and pass on the PR head.
`tools/gh/kill_check.py` answers a different question (does a test fail when
a stub patch is applied to the head) and is a gate every Tooling Exception
depends on, so this is a separate tool.

A bundle is a directory holding `checks.json` (a non-empty list of
`{"ac": str, "test": [argv...]}`) and `files/`, a tree of test files copied
into each scratch checkout at the same relative paths.

    kill_runner.py run --repo OWNER/REPO --issue N --base REF --head REF \\
        --bundle DIR --source-repo PATH [--timeout 300]

prints one JSON object, `{"checks": [{"ac", "base_rc", "head_rc"}],
"bundle_sha256": hex}`, and exits 0 whether the checks pass or fail: the
verdict is `tools/runner/verdict.py`'s. It exits 2 only when it could not
run (unreadable bundle, bad ref, refused overlay, scratch refusal, or a cwd
inside the worktree under review). A check that times out records rc 124.

Every git call runs as `git -C <source-repo>`: the trusted forge checkout
shares the object store, so the agent's commits resolve there, and the
worktree under review is never the cwd and never read. The agent's own tests
in the diff are not run here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))
import _scratch  # noqa: E402
from kill_check import run_command  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402

TIMEOUT_RC = 124


class RunnerError(Exception):
    """The checks could not be run; the message says why."""


def git(source: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = run_command(["git", "-C", str(source), *args], input_bytes=input_bytes)
    if result.returncode:
        raise RunnerError(f"git {' '.join(args)} failed: "
                          f"{result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def resolve(source: Path, ref: str) -> str:
    return git(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()


def load_checks(bundle: Path) -> list[dict]:
    try:
        entries = json.loads((bundle / "checks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError(f"cannot read {bundle / 'checks.json'}: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise RunnerError("checks.json must be a non-empty JSON list")
    checks = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise RunnerError(f"check {number} must be an object")
        ac, test = entry.get("ac"), entry.get("test")
        if not isinstance(ac, str) or not ac.strip():
            raise RunnerError(f"check {number} needs a non-empty ac")
        if not isinstance(test, list) or not test or not all(
                isinstance(token, str) and token for token in test):
            raise RunnerError(f"check {number} needs a non-empty argv string list")
        checks.append({"ac": ac, "test": test})
    return checks


def bundle_files(bundle: Path) -> list[str]:
    """Relative paths of every file under files/. A symlink or special file
    anywhere in the tree is refused: it could point the overlay outside the
    checkout."""
    root = bundle / "files"
    if root.is_symlink():
        raise RunnerError("files/ is a symlink")
    if not root.is_dir():
        raise RunnerError(f"{root} is not a directory")
    paths = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise RunnerError(f"symlink in bundle: {path.relative_to(root)}")
            if name in filenames and not path.is_file():
                raise RunnerError(f"not a regular file in bundle: {path.relative_to(root)}")
        paths.extend((Path(dirpath) / name).relative_to(root).as_posix() for name in filenames)
    return sorted(paths)


def bundle_digest(bundle: Path) -> str:
    """sha256 over checks.json and every file under files/, in sorted path
    order. Each entry contributes its path and its content hash, so an edit,
    an added file or a rename all change the digest. Lane 1 pins this value in
    the handoff (`kill-bundle-sha256: <hex>`)."""
    digest = hashlib.sha256()
    entries = ["checks.json"] + [f"files/{rel}" for rel in bundle_files(bundle)]
    for rel in sorted(entries):
        try:
            content = (bundle / rel).read_bytes()
        except OSError as exc:
            raise RunnerError(f"cannot read {rel}: {exc}") from exc
        digest.update(rel.encode() + b"\0" + hashlib.sha256(content).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def materialize(source: Path, sha: str, directory: Path) -> None:
    directory.mkdir()
    archive = git(source, "archive", sha)
    extract = run_command(["tar", "-x", "-C", str(directory)], input_bytes=archive)
    if extract.returncode:
        raise RunnerError(f"tar extraction failed: {extract.stderr.decode(errors='replace')}")


def check_overlay(tree: Path, rel: str, changed: set[str]) -> None:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts:
        raise RunnerError(f"overlay path escapes the checkout: {rel}")
    if rel in changed:
        raise RunnerError(f"overlay would overwrite a path the head changed: {rel}")
    current = tree
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise RunnerError(f"overlay path crosses a symlink in the checkout: {rel}")
        if index < len(parts) - 1 and current.exists() and not current.is_dir():
            raise RunnerError(f"overlay path crosses a non-directory in the checkout: {rel}")
    if current.exists() and not current.is_file():
        raise RunnerError(f"overlay path is not a file in the checkout: {rel}")


def overlay(bundle: Path, tree: Path, files: list[str]) -> None:
    for rel in files:
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / "files" / rel, target)


def test_env(repo: str, tree: Path) -> dict[str, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HARMONIC_FORGE_TESTING": "1",
           "HARMONIC_FORGE_TELEMETRY_STORE": str(tree / ".telemetry-store")}
    if repo == "vitalharmony/harmonic-forge":
        env["HARMONIC_FORGE_ROOT"] = str(tree)
    return env


def run_one(argv: list[str], pristine: Path, run_dir: Path, bundle: Path,
            files: list[str], repo: str, timeout: int) -> int:
    shutil.copytree(pristine, run_dir, symlinks=True)
    try:
        overlay(bundle, run_dir, files)
        # Inside the boundary a missing binary is just a non-zero exit, so it is
        # refused here, before wrapping: a check that cannot launch is an error
        # (exit 2), never a test result.
        if shutil.which(argv[0], path=None if os.sep not in argv[0] else "") is None \
                and not (run_dir / argv[0]).is_file():
            raise RunnerError(f"cannot launch {argv[0]!r}: not found")
        try:
            # The test executes the head's (the agent's) code, so it runs inside
            # the bwrap boundary with only its run directory writable (#1098).
            confined = sandbox.wrap(argv, run_dir)
        except sandbox.SandboxError as exc:
            raise RunnerError(str(exc)) from exc
        try:
            return run_command(confined, cwd=run_dir, env=test_env(repo, run_dir),
                               timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return TIMEOUT_RC
        except OSError as exc:
            raise RunnerError(f"cannot launch {argv[0]!r}: {exc}") from exc
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def linked_worktrees(source: Path) -> list[Path]:
    """Every worktree of the source repo except the trusted checkout itself.
    Whichever of them is under review, none is a place to run from."""
    listing = git(source, "worktree", "list", "--porcelain", "-z").decode()
    trees = [Path(entry[len("worktree "):]).resolve() for entry in listing.split("\0")
             if entry.startswith("worktree ")]
    return [tree for tree in trees if tree != source]


def run(args: argparse.Namespace) -> dict:
    source = Path(args.source_repo).resolve()
    if args.timeout <= 0:
        raise RunnerError("--timeout must be positive")
    base, head = resolve(source, args.base), resolve(source, args.head)
    cwd = Path.cwd().resolve()
    for tree in linked_worktrees(source):
        if cwd == tree or tree in cwd.parents:
            raise RunnerError(f"refusing to run from inside a worktree under review: {tree}")
    changed = set(filter(None, git(source, "diff", "--name-only", "-z", "--no-renames",
                                   base, head).decode().split("\0")))
    try:
        with _scratch.scratch_dir(f"kill-runner {args.repo}#{args.issue}", prefix="scratch-") as parent:
            # One copy, read once: the digest covers exactly the bytes that run,
            # however long the scratch lock wait above took.
            bundle = parent / "bundle"
            try:
                shutil.copytree(Path(args.bundle).resolve(), bundle, symlinks=True)
            except (OSError, shutil.Error) as exc:
                raise RunnerError(f"cannot read bundle {args.bundle}: {exc}") from exc
            checks = load_checks(bundle)
            files = bundle_files(bundle)
            digest = bundle_digest(bundle)
            results = [{"ac": check["ac"]} for check in checks]
            pristine = {}
            for side, sha in (("base", base), ("head", head)):
                pristine[side] = parent / f"pristine-{side}"
                materialize(source, sha, pristine[side])
                for rel in files:
                    check_overlay(pristine[side], rel, changed)
            for number, (check, result) in enumerate(zip(checks, results)):
                for side in ("base", "head"):
                    # Its own directory per run: a run whose cleanup failed
                    # cannot break the next one.
                    result[f"{side}_rc"] = run_one(check["test"], pristine[side],
                                                   parent / f"run-{number}-{side}",
                                                   bundle, files, args.repo, args.timeout)
    except _scratch.ScratchError as exc:
        raise RunnerError(str(exc)) from exc
    return {"checks": results, "bundle_sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run")
    for flag in ("--repo", "--base", "--head", "--bundle", "--source-repo"):
        command.add_argument(flag, required=True)
    command.add_argument("--issue", type=int, required=True)
    command.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args)))
    except RunnerError as exc:
        print(f"kill-runner: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
