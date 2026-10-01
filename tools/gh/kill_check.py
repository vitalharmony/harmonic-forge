#!/usr/bin/env python3
"""Prove an AC-bearing test fails when its named mechanism is stubbed (F846)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_check as preclose  # noqa: E402

SCRATCH_AGE_SECONDS = 24 * 60 * 60


def run_command(argv: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None, timeout: int | None = None,
                input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def git_value(*args: str) -> str:
    result = run_command(["git", *args])
    if result.returncode:
        raise SystemExit(f"kill-check: git {' '.join(args)} failed: "
                         f"{result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode().strip()


def identity(repo_arg: str, issue: int, base: str, head: str) -> tuple[str, str, str | None]:
    repo = preclose.registered_repo(repo_arg)
    actual = preclose.origin_repo()
    if actual != preclose.normalize_repo(repo):
        raise SystemExit(f"kill-check: --repo {repo} does not match this checkout's origin {actual}")
    sha = git_value("rev-parse", "--verify", f"{head}^{{commit}}")
    patch_id = preclose.local_patch_id(base, head)
    return repo, sha, patch_id


def receipt_path(repo: str, issue: int) -> Path:
    return preclose.receipt_dir() / f"{preclose._repo_key(repo)}_{issue}.kill.json"


def read_receipt(repo: str, issue: int) -> dict | None:
    try:
        data = json.loads(receipt_path(repo, issue).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def covering_receipt(repo: str, issue: int, sha: str, patch_id: str | None) -> bool:
    receipt = read_receipt(repo, issue)
    return bool(receipt and receipt.get("repo") == repo and receipt.get("issue") == issue
                and receipt.get("status") in ("pass", "waived")
                and (receipt.get("head_sha") == sha
                     or (patch_id is not None and receipt.get("patch_id") == patch_id)))


def write_receipt(repo: str, issue: int, payload: dict) -> Path:
    directory = preclose.receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = receipt_path(repo, issue)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=directory, suffix=".tmp", delete=False,
                                         encoding="utf-8") as handle:
            name = handle.name
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)
    return path


def checked_inputs(path_arg: str) -> list[dict]:
    path = Path(path_arg).resolve()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"kill-check: cannot read checks {path}: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise SystemExit("kill-check: --checks must be a non-empty JSON list")
    checks: list[dict] = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise SystemExit(f"kill-check: check {number} must be an object")
        ac, mechanism, patch_arg, test = (entry.get(key) for key in
                                          ("ac", "mechanism", "patch", "test"))
        if not all(isinstance(value, str) and value.strip()
                   for value in (ac, mechanism, patch_arg)):
            raise SystemExit(f"kill-check: check {number} needs non-empty ac, mechanism, patch")
        if not isinstance(test, list) or not test or not all(
                isinstance(token, str) and token for token in test):
            raise SystemExit(f"kill-check: check {number} needs a non-empty argv string list")
        patch = Path(patch_arg).resolve()
        try:
            patch_text = patch.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"kill-check: cannot read patch {patch}: {exc}") from exc
        checks.append({"ac": ac, "mechanism": mechanism, "patch_path": str(patch),
                       "patch_text": patch_text,
                       "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
                       "test": test})
    return checks


def scratch_parent() -> Path:
    parent = Path.home() / ".cache" / "kill-check"
    parent.mkdir(parents=True, exist_ok=True)
    return parent.resolve()


def reap_old(parent: Path) -> None:
    cutoff = time.time() - SCRATCH_AGE_SECONDS
    for path in parent.iterdir():
        if (path.name.startswith("kill-check-") and path.is_dir()
                and not path.is_symlink() and path.stat().st_mtime < cutoff):
            shutil.rmtree(path)


def materialize(directory: Path, sha: str, origin: str) -> None:
    archive = run_command(["git", "archive", sha])
    if archive.returncode:
        raise RuntimeError(f"git archive failed: {archive.stderr.decode(errors='replace')}")
    extract = run_command(["tar", "-x", "-C", str(directory)], input_bytes=archive.stdout)
    if extract.returncode:
        raise RuntimeError(f"tar extraction failed: {extract.stderr.decode(errors='replace')}")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                 ["git", "-c", "user.name=kill-check", "-c", "user.email=kill-check@localhost",
                  "commit", "-q", "-m", "base"],
                 ["git", "branch", "-M", "main"],
                 ["git", "remote", "add", "origin", origin],
                 ["git", "update-ref", "refs/remotes/origin/main", "HEAD"]):
        result = run_command(argv, cwd=directory)
        if result.returncode:
            raise RuntimeError(f"{' '.join(argv)} failed: "
                               f"{result.stderr.decode(errors='replace')}")


def one_check(check: dict, *, sha: str, origin: str, repo: str,
              parent: Path, timeout: int) -> dict:
    result = {**check, "baseline_rcs": [], "mutated_rc": None, "verdict": "error"}
    directory = Path(tempfile.mkdtemp(prefix="kill-check-", dir=parent))
    try:
        materialize(directory, sha, origin)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if repo == "vitalharmony/harmonic-forge":
            env["HARMONIC_FORGE_ROOT"] = str(directory)
        for _ in range(2):
            baseline = run_command(check["test"], cwd=directory, env=env, timeout=timeout)
            result["baseline_rcs"].append(baseline.returncode)
            if baseline.returncode:
                result["verdict"] = "broken-test" if len(result["baseline_rcs"]) == 1 else "flaky"
                return result
        patch_path = check["patch_path"]
        for argv in (["git", "apply", "--check", patch_path],
                     ["git", "apply", patch_path]):
            applied = run_command(argv, cwd=directory)
            if applied.returncode:
                result["verdict"] = "patch-failed"
                result["detail"] = applied.stderr.decode(errors="replace").strip()
                return result
        mutated = run_command(check["test"], cwd=directory, env=env, timeout=timeout)
        result["mutated_rc"] = mutated.returncode
        result["verdict"] = "vacuous" if mutated.returncode == 0 else "killed"
    except subprocess.TimeoutExpired:
        result["verdict"] = "timeout"
    except (OSError, RuntimeError) as exc:
        result["detail"] = str(exc)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return result


def run(args: argparse.Namespace) -> int:
    checks = checked_inputs(args.checks)  # Resolve all paths before scratch commands.
    if args.timeout <= 0:
        raise SystemExit("kill-check: --timeout must be positive")
    repo, sha, patch_id = identity(args.repo, args.issue, args.base, args.head)
    origin = git_value("remote", "get-url", "origin")
    parent = scratch_parent()
    reap_old(parent)
    results = [one_check(check, sha=sha, origin=origin, repo=repo,
                         parent=parent, timeout=args.timeout) for check in checks]
    status = "pass" if all(item["verdict"] == "killed" for item in results) else "fail"
    path = write_receipt(repo, args.issue,
                         {"repo": repo, "issue": args.issue, "head_sha": sha,
                          "scratch_parent": str(parent), "patch_id": patch_id,
                          "checks": results, "status": status,
                          "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    print("AC | Mechanism | Verdict")
    for item in results:
        print(f"{item['ac']} | {item['mechanism']} | {item['verdict']}")
    print(f"kill-check: {status}; receipt: {path}")
    return 0 if status == "pass" else 1


def waive(args: argparse.Namespace) -> int:
    if not args.reason or not args.reason.strip():
        raise SystemExit("kill-check: waive needs --reason quoting the operator's instruction")
    repo, sha, patch_id = identity(args.repo, args.issue, args.base, args.head)
    path = write_receipt(repo, args.issue,
                         {"repo": repo, "issue": args.issue, "head_sha": sha,
                          "patch_id": patch_id, "status": "waived", "reason": args.reason,
                          "checks": [], "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    print(f"kill-check: waiver recorded; receipt: {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "waive"):
        command = commands.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--issue", type=int, required=True)
        command.add_argument("--base", default="origin/main")
        command.add_argument("--head", default="HEAD")
        if name == "run":
            command.add_argument("--checks", required=True)
            command.add_argument("--timeout", type=int, default=300)
        else:
            command.add_argument("--reason", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args) if args.command == "run" else waive(args))


if __name__ == "__main__":
    main()
