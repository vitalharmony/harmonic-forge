#!/usr/bin/env python3
"""Prove an AC-bearing test fails when its named mechanism is stubbed (F846)."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_check as preclose  # noqa: E402
import _scratch  # noqa: E402

SCRATCH_AGE_SECONDS = 24 * 60 * 60


def run_command(argv: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None, timeout: int | None = None,
                input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                               stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
    except BaseException:
        # The test runs in its own session: terminal Ctrl-C reaches this
        # process, not its children. Kill the group on every interruption,
        # not only on timeout, before scratch cleanup can begin.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # A descendant that escaped the process group must not hold this
            # runner hostage through inherited capture pipes.
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


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


def lease_path(directory: Path) -> Path:
    return directory.with_name(directory.name + ".lease")


@contextlib.contextmanager
def scratch_lease(directory: Path):
    path = lease_path(directory)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if not directory.exists():
        path.unlink(missing_ok=True)


def reap_old(parent: Path) -> None:
    cutoff = time.time() - SCRATCH_AGE_SECONDS
    for path in parent.iterdir():
        if (path.name.startswith("kill-check-") and path.is_dir()
                and not path.is_symlink() and path.stat().st_mtime < cutoff):
            lease = lease_path(path)
            fd = os.open(lease, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # An active run owns this scratch, regardless of age.
                error = remove_tree(path)
                if error:
                    raise RuntimeError(f"cannot reap {path}: {error}")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            lease.unlink(missing_ok=True)


def materialize(directory: Path, sha: str, origin: str) -> None:
    archive = run_command(["git", "archive", sha])
    if archive.returncode:
        raise RuntimeError(f"git archive failed: {archive.stderr.decode(errors='replace')}")
    extract = run_command(["tar", "-x", "-C", str(directory)], input_bytes=archive.stdout)
    if extract.returncode:
        raise RuntimeError(f"tar extraction failed: {extract.stderr.decode(errors='replace')}")
    # harmonic-forge#871: without these, the commit below starts a detached
    # `git maintenance run --auto` -> `git repack --cruft` that deletes the
    # loose objects while the checks copy this tree. Repo-level, so it also
    # covers git that a check's own test command runs here.
    for argv in (["git", "init", "-q"],
                 ["git", "config", "gc.auto", "0"],
                 ["git", "config", "maintenance.auto", "false"],
                 ["git", "add", "-A"],
                 ["git", "-c", "user.name=kill-check", "-c", "user.email=kill-check@localhost",
                  "commit", "-q", "-m", "base"],
                 ["git", "branch", "-M", "main"],
                 ["git", "remote", "add", "origin", origin],
                 ["git", "update-ref", "refs/remotes/origin/main", "HEAD"]):
        result = run_command(argv, cwd=directory)
        if result.returncode:
            raise RuntimeError(f"{' '.join(argv)} failed: "
                               f"{result.stderr.decode(errors='replace')}")


def remove_tree(directory: Path) -> str | None:
    """Remove scratch, repairing test-created permissions, or report failure."""
    if not directory.exists():
        return None
    try:
        # Walk top-down so restoring each directory's search bit permits the
        # next level. Never follow or chmod symlinks out of scratch.
        directory.chmod(stat.S_IRWXU)
        for root, dirs, _files in os.walk(directory, topdown=True, followlinks=False):
            for name in dirs:
                child = Path(root) / name
                if not child.is_symlink():
                    child.chmod(stat.S_IRWXU)
        shutil.rmtree(directory)
    except OSError as exc:
        return str(exc)
    return None if not directory.exists() else f"{directory} still exists"


def one_check(check: dict, *, sha: str, origin: str, repo: str,
              parent: Path, timeout: int) -> dict:
    result = {**check, "baseline_rcs": [], "control_rc": None,
              "mutated_rc": None, "verdict": "error"}
    directory = Path(tempfile.mkdtemp(prefix="kill-check-", dir=parent))
    snapshot: Path | None = None
    with contextlib.ExitStack() as leases:
        leases.enter_context(scratch_lease(directory))
        try:
            materialize(directory, sha, origin)
            # harmonic-forge#865: mark the child as a test so belt writers it
            # reaches (in-process or spawned) never touch the real store.
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HARMONIC_FORGE_TESTING": "1",
                   # harmonic-forge#907: the tests it runs emit telemetry too;
                   # send it to the scratch tree, never the operator's store.
                   "HARMONIC_FORGE_TELEMETRY_STORE": str(directory / ".telemetry-store")}
            if repo == "vitalharmony/harmonic-forge":
                env["HARMONIC_FORGE_ROOT"] = str(directory)
            for _ in range(2):
                baseline = run_command(check["test"], cwd=directory, env=env, timeout=timeout)
                result["baseline_rcs"].append(baseline.returncode)
                if baseline.returncode:
                    result["verdict"] = "broken-test" if len(result["baseline_rcs"]) == 1 else "flaky"
                    return result
            # Match control and mutant from one post-baseline state and path.
            snapshot = Path(tempfile.mkdtemp(prefix="kill-check-snapshot-", dir=parent))
            leases.enter_context(scratch_lease(snapshot))
            shutil.copytree(directory, snapshot, dirs_exist_ok=True, symlinks=True)
            control = run_command(check["test"], cwd=directory, env=env, timeout=timeout)
            result["control_rc"] = control.returncode
            if control.returncode:
                result["verdict"] = "flaky"
                return result
            removal_error = remove_tree(directory)
            if removal_error:
                raise RuntimeError(f"cannot restore control state: {removal_error}")
            shutil.copytree(snapshot, directory, symlinks=True)
            patch_bytes = check["patch_text"].encode()
            for argv in (["git", "apply", "--check", "-"], ["git", "apply", "-"]):
                applied = run_command(argv, cwd=directory, input_bytes=patch_bytes)
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
            errors = [error for error in
                      (remove_tree(directory), remove_tree(snapshot) if snapshot else None) if error]
            if errors:
                result["verdict"] = "cleanup-failed"
                result["detail"] = "; ".join(errors)
    return result


def run(args: argparse.Namespace) -> int:
    repo, sha, patch_id = identity(args.repo, args.issue, args.base, args.head)
    payload = {"repo": repo, "issue": args.issue, "head_sha": sha,
               "patch_id": patch_id, "checks": [],
               "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    with preclose.receipt_lock(repo, args.issue):
        write_receipt(repo, args.issue, {**payload, "status": "running"})
        try:
            checks = checked_inputs(args.checks)  # Resolve before scratch commands.
            if args.timeout <= 0:
                raise ValueError("--timeout must be positive")
            origin = git_value("remote", "get-url", "origin")
            # harmonic-forge#949: under the shared, locked, reaped scratch root.
            with _scratch.scratch_dir(f"kill-check {repo}#{args.issue}", prefix="kill-check-") as parent:
                results = [one_check(check, sha=sha, origin=origin, repo=repo,
                                     parent=parent, timeout=args.timeout) for check in checks]
        except (Exception, SystemExit) as exc:
            # checked_inputs/git_value use SystemExit for invalid input;
            # record failure rather than leaving an ambiguous running receipt.
            # Deliberately leave KeyboardInterrupt uncaught: an interrupted
            # recheck must retain its nonpassing running receipt.
            write_receipt(repo, args.issue, {**payload, "status": "fail", "error": str(exc)})
            raise
        status = "pass" if all(item["verdict"] == "killed" for item in results) else "fail"
        path = write_receipt(repo, args.issue,
                             {**payload, "scratch_parent": str(parent),
                              "checks": results, "status": status})
    print("AC | Mechanism | Verdict")
    for item in results:
        print(f"{item['ac']} | {item['mechanism']} | {item['verdict']}")
    print(f"kill-check: {status}; receipt: {path}")
    return 0 if status == "pass" else 1


def waive(args: argparse.Namespace) -> int:
    if not args.reason or not args.reason.strip():
        raise SystemExit("kill-check: waive needs --reason quoting the operator's instruction")
    repo, sha, patch_id = identity(args.repo, args.issue, args.base, args.head)
    with preclose.receipt_lock(repo, args.issue):
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
