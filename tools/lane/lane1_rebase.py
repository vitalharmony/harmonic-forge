#!/usr/bin/env python3
"""Lane 1 rebases a clean Lane 2 branch itself (harmonic-forge#1018).

When one branch merges, every other open branch falls behind `origin/main`
and `l1_post.py` refuses its `ready-for-l3`:

    if run("git", "merge-base", "--is-ancestor", main_sha, sha).returncode:

(`tools/gh/l1_post.py`, `static_checks`). That check is kept. What this adds
is the cheap answer to it: a mechanical rebase no longer costs a `--kind
rework` and a Lane 2 round trip. A conflict still goes to Lane 2.

Usage: lane1_rebase.py <repo-dir> <issue> <branch>

Exit codes:
  0  `refs/heads/<branch>` (local) and `origin/<branch>` both end at the new
     SHA, printed as `new=<sha>`. Post `ready-for-l3` on it (R-0338).
  1  any other failure (fetch, a lease mismatch, a busy stale prep worktree).
  2  the rebase conflicts beyond a `package.json`/`package-lock.json` version
     line and the project's declared generated files, or regenerating those
     failed; aborted, nothing pushed, conflicting paths (or the task output)
     listed.

Declared generated files (harmonic-forge#1057): a conflict in a path the
target project lists under `[project.protocol] rebase_regenerate` takes
origin/main's side at every stop; after the rebase the tool runs
`rebase_provision_task` and each declared task once in the prep worktree,
commits only those paths ("regenerate <paths> after rebase onto origin/main
(lane1-rebase)") and prints `regenerated=<paths>`. A failed task, a change
outside the declared paths, or a delete/modify conflict on a declared path is
exit 2. Only files generated in full may be declared: taking main's side drops
the branch's hand edits. A re-run after a crash that followed a regeneration
exits 3, as for a version resolution.

A `package-lock.json`'s root `"version"` and `packages[""].version` resolve
with its `package.json`, to the same `T` (harmonic-forge#1057 gap 1); any other
lock difference is exit 2.

A conflict confined to a `package.json`'s top-level `"version"` (both sides
equal as JSON, numbers compared by literal text, once that key is removed) is resolved, not
exit 2 (harmonic-forge#1035): the value becomes `origin/main`'s version plus
one patch, `T`, computed once before the rebase, and exit 0 then prints
`version-resolved=<T>` after `new=<sha>`. A branch version whose MAJOR or
MINOR differs from `origin/main`'s is exit 2, so a minor or major bump is
never discarded. The resolved commit keeps its old message (e.g. "Auto-bump
to v2.9.44") while carrying `T`. A resolved commit is not patch-equivalent
to the local one, so in the same run the local-ref step skips its cherry
check only when the local branch is exactly this run's pre-rebase origin SHA.
Nothing of that is persisted: a re-run after a crash that followed a
resolution exits 3 (fail-closed); recover by resetting the local branch to
`origin/<branch>`.
  3  the local branch cannot be moved (dirty impl worktree, missing ref, or
     local commits origin does not carry); both SHAs printed.
  4  a precondition failed (newest lane marker is neither `l2.done` nor a
     Lane 3 PASS on the branch head, a Lane 3 gate is open on the old SHA, or
     the issue is `tooling-exception`).
  5  the pre-push hook refused the push. Never retried with `--no-verify`.

Safe to re-run: a stale prep worktree is pruned at start and the prep
worktree is removed in a `finally`; if `origin/<branch>` already contains
`origin/main` (a crash after the push), it skips straight to the local-ref
step. That holds on the Lane 3 PASS path too (harmonic-forge#1034): before
the push a receipt `{issue, passed_sha, rebased_shas}` is written in the git
dir, and after a crash the PASS (which names the pre-rebase SHA) is accepted
only when that receipt names it and `origin/<branch>` is exactly one of the
receipt's recorded rebased SHAs; the local branch is left to the cherry check
in the local-ref step (operator ruling amending 6219927415). The receipt is
deleted once the local ref moves. The crash state is recorded, never
inferred from history (sticky-wicket reforge, F1034).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_GH = _HERE.parent / "gh"
_BUSY = _HERE.parent / "worktree" / "check_worktree_busy.py"

DEFAULT_WORKTREES = Path.home() / "Harmonic_Projects" / ".worktrees"


class Exit(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def git(cwd: str | Path, *args: str, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, env=env)
    if check and proc.returncode:
        raise Exit(1, f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev(cwd: str | Path, ref: str) -> str | None:
    proc = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return proc.stdout.strip() or None


# ---------------------------------------------------------------- preconditions

def repo_slug(repo_dir: str) -> str:
    url = git(repo_dir, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise Exit(1, f"cannot derive owner/repo from origin url {url!r}")
    return match.group(1)


def _watch():
    sys.path.insert(0, str(_GH))
    import watch_lane_posts  # noqa: PLC0415 -- lazy: the network reader
    return watch_lane_posts


def _gate_ci():
    sys.path.insert(0, str(_GH))
    import gate_ci  # noqa: PLC0415 -- lazy, beside watch_lane_posts
    return gate_ci


def regenerate_config(repo_dir: str) -> tuple[dict[str, str], str | None]:
    """harmonic-forge#1057: the target project's declared regenerable files
    (`path -> mise task [args]`) and its prep-worktree provision task, from its
    `[project.protocol]` in projects.toml. A repo absent from the manifest, or
    a project declaring nothing, yields `({}, None)`: today's behavior."""
    sys.path.insert(0, str(_HERE.parent / "onboard"))
    try:
        import manifest  # noqa: PLC0415 -- lazy: only a conflicting rebase needs it
        from manifest_protocol import normalize_repo  # noqa: PLC0415
        url = git(repo_dir, "remote", "get-url", "origin").stdout.strip()
        project = manifest.by_repo().get(normalize_repo(url))
    except Exception:  # noqa: BLE001 -- an unreadable manifest declares nothing
        return {}, None
    protocol = getattr(project, "protocol", None)
    if protocol is None or not protocol.rebase_regenerate:
        return {}, None
    return dict(protocol.rebase_regenerate), protocol.rebase_provision_task


def run_task(prep: Path, task: str) -> subprocess.CompletedProcess:
    """One declared mise task in the prep worktree (`task` may carry args)."""
    try:
        return subprocess.run(["mise", "run", *shlex.split(task)], cwd=str(prep),
                              capture_output=True, text=True)
    except OSError as exc:
        # F1057 preclose: a task that cannot even launch is a failed task (exit 2).
        return subprocess.CompletedProcess(["mise", "run", task], 127, "", str(exc))


def newest_lane_marker(repo_dir: str, issue: int) -> str | None:
    """`lane.detail` of the issue's newest classified comment, e.g.
    `l2.done`, read with the belt's own classifier so the two cannot drift.
    A Lane 3 gate report (`_classify` == `('l3', 'gate-result')`: footer
    `kind=gate-result` + `posted-by=LANE3`) whose verdict is PASS is
    `l3.pass:<gated sha>` (harmonic-forge#1034), read with gate_ci's own
    `verdict_of`/`gated_sha`."""
    watch = _watch()
    gate_ci = _gate_ci()
    comments = watch._fetch_all_comments(repo_slug(repo_dir), issue)
    if comments is None:
        raise Exit(4, f"cannot read the comments of #{issue}")
    newest = None
    for comment in comments:
        body = comment.get("body") or ""
        found = watch._classify(body)
        if found:
            lane, detail = found
            # harmonic-forge#1034 (reforge r2, operator ruling): a gate
            # report is recognized by ALLOWLIST only -- the belt's own
            # classifier reading a `kind=gate-result` footer stamped
            # `posted-by=LANE3`. No heading fallback, no kind denylist: a
            # quoted heading in any other comment is never a verdict.
            if found == ("l3", "gate-result"):
                if gate_ci.verdict_of(body) == "PASS":
                    newest = f"l3.pass:{gate_ci.gated_sha(body) or ''}"
                else:
                    newest = f"l3.{detail}"
                continue
            # sticky-wicket PATCH (F1018 pass 2): Lane 1's own posts after the
            # L2D (ready-for-l3, AE, sweep, discussion) do not change the
            # branch's state -- an AE on the old SHA carries forward (R-0209,
            # R-0390). Only a Lane 1 rework sends the branch back to Lane 2.
            if lane == "l1" and detail != "rework":
                continue
            # preclose F1018-1: l2_post stamps `kind=completion`, and a legacy
            # heading classifies as `## L2D ...`; both are the belt's own
            # completion test, so the two cannot drift.
            if watch._is_l2_completion(lane, detail) or (
                    lane == "l2" and detail.lstrip("# ").startswith("L2D")):
                detail = "done"
            newest = f"{lane}.{detail}"
    return newest


def issue_labels(repo_dir: str, issue: int) -> set[str]:
    state, labels, _ = _watch()._read_issue_meta(repo_slug(repo_dir), issue)
    if labels is None:
        raise Exit(4, f"cannot read the labels of #{issue}")
    return labels


def worktrees(repo_dir: str) -> list[dict[str, str]]:
    out = git(repo_dir, "worktree", "list", "--porcelain").stdout
    entries, entry = [], {}
    for line in out.splitlines():
        if not line.strip():
            if entry:
                entries.append(entry)
            entry = {}
            continue
        key, _, value = line.partition(" ")
        entry[key] = value
    if entry:
        entries.append(entry)
    return entries


def lane3_open_on(repo_dir: str, sha: str) -> str | None:
    """A worktree whose HEAD is `sha` and which carries `lane3-begin`'s
    `LANE3_ACTIVE` marker, or None."""
    for entry in worktrees(repo_dir):
        path = entry.get("worktree")
        if entry.get("HEAD") != sha or not path or not Path(path).is_dir():
            continue
        git_dir = git(path, "rev-parse", "--absolute-git-dir", check=False).stdout.strip()
        if git_dir and (Path(git_dir) / "LANE3_ACTIVE").exists():
            return path
    return None


def _same_sha(a: str | None, b: str | None) -> bool:
    sys.path.insert(0, str(_GH))
    import check_lane3_ready  # noqa: PLC0415
    return check_lane3_ready.same_sha(a, b)


def receipt_path(repo_dir: str, issue: int) -> Path:
    common = git(repo_dir, "rev-parse", "--git-common-dir").stdout.strip()
    base = Path(common) if Path(common).is_absolute() else Path(repo_dir) / common
    return base / f"lane1-rebase-{issue}.json"


def read_receipt(repo_dir: str, issue: int) -> dict | None:
    try:
        data = json.loads(receipt_path(repo_dir, issue).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(repo_dir: str, issue: int, passed: str, old: str, rebased: str) -> None:
    """Record `{issue, passed_sha, rebased_shas}` before a push, `passed`
    being the Lane 3 PASS Head-SHA (never guessed). The rebased SHAs carry
    forward only when the prior receipt names this same PASS and `old` is
    one it says was pushed (a re-rebase after a crash and a main move);
    otherwise the receipt is fresh (sticky-wicket PATCH, F1034)."""
    prior = read_receipt(repo_dir, issue)
    if (prior and prior.get("issue") == issue
            and _same_sha(passed, prior.get("passed_sha"))
            and old in prior.get("rebased_shas", [])):
        shas = prior["rebased_shas"] + [rebased]
    else:
        shas = [rebased]
    receipt_path(repo_dir, issue).write_text(json.dumps(
        {"issue": issue, "passed_sha": passed, "rebased_shas": shas}))


def drop_receipt(repo_dir: str, issue: int) -> None:
    receipt_path(repo_dir, issue).unlink(missing_ok=True)


def _pass_covers(repo_dir: str, issue: int, old: str, passed: str) -> bool:
    """A PASS covers the branch when it names the head, or (operator ruling
    on #1034) the crash-after-push state -- recorded, never inferred
    (sticky-wicket reforge): this tool's own receipt names this PASS and
    origin is exactly a SHA the receipt says was pushed. The local branch
    is not consulted (operator ruling amending 6219927415): unpushed local
    work is guarded by move_local_ref's cherry check. Fails closed (exit 4)
    otherwise."""
    if _same_sha(passed, old):
        return True
    receipt = read_receipt(repo_dir, issue)
    if not receipt or receipt.get("issue") != issue:
        return False
    return (_same_sha(passed, receipt.get("passed_sha"))
            and old in receipt.get("rebased_shas", []))


def check_preconditions(repo_dir: str, issue: int, branch: str, old: str) -> str | None:
    """Return the Lane 3 PASS Head-SHA, or None on the l2.done path."""
    # preclose F1018-2: the checks below read #issue, so the branch pushed
    # must be #issue's own (`feat|fix/<issue>-...`), never another issue's.
    if not re.match(rf"(?:feat|fix)/{issue}-", branch):
        raise Exit(4, f"{branch} is not #{issue}'s branch (expected feat/{issue}-... or fix/{issue}-...)")
    marker = newest_lane_marker(repo_dir, issue)
    passed = None
    if marker and marker.startswith("l3.pass:"):
        passed = marker.split(":", 1)[1]
        if not _pass_covers(repo_dir, issue, old, passed):
            raise Exit(4, f"the Lane 3 PASS on #{issue} names {passed[:12] or 'no SHA'}, "
                          f"not the branch head {old[:12]}")
    elif marker != "l2.done":
        raise Exit(4, f"newest lane marker on #{issue} is {marker!r}, "
                      "neither 'l2.done' nor a Lane 3 PASS on the branch head")
    if "tooling-exception" in issue_labels(repo_dir, issue):
        raise Exit(4, f"#{issue} is tooling-exception; Lane 2 branches only")
    gate = lane3_open_on(repo_dir, old)
    if gate:
        raise Exit(4, f"a Lane 3 gate is open on {old[:12]} in {gate}")
    return passed


# ---------------------------------------------------------------- git steps

def fetch(repo_dir: str, branch: str) -> None:
    git(repo_dir, "fetch", "origin",
        "+refs/heads/main:refs/remotes/origin/main",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


def prep_path(repo_dir: str, issue: int) -> Path:
    root = Path(os.environ.get("LANE1_REBASE_WORKTREES") or DEFAULT_WORKTREES)
    return root / f"{Path(repo_dir).resolve().name}-{issue}-prep"


def remove_prep(repo_dir: str, prep: Path, force: bool = False) -> None:
    # Not forced in the `finally`: an aborted rebase leaves a clean tree, so a
    # removal that fails there means the abort did not happen, and the left
    # prep worktree is the visible signal. The stale-prune at start forces.
    if prep.exists():
        git(repo_dir, "worktree", "remove", *(["--force"] if force else []),
            str(prep), check=False)
    git(repo_dir, "worktree", "prune", check=False)


def prune_stale_prep(repo_dir: str, prep: Path) -> None:
    if prep.exists() and _BUSY.exists():
        busy = subprocess.run([sys.executable, str(_BUSY), str(prep)],
                              capture_output=True, text=True)
        if busy.returncode == 1:
            raise Exit(1, f"stale prep worktree {prep} is busy:\n{busy.stdout}{busy.stderr}")
    remove_prep(repo_dir, prep, force=True)


def push(prep: Path, branch: str, old: str) -> None:
    proc = git(prep, "push", f"--force-with-lease={branch}:{old}", "origin",
               f"HEAD:refs/heads/{branch}", check=False)
    if proc.returncode == 0:
        return
    err = proc.stderr.strip()
    if "[rejected]" in err or "stale info" in err:
        raise Exit(1, f"push refused (lease: origin/{branch} is no longer {old[:12]}):\n{err}")
    hook = git(prep, "rev-parse", "--git-path", "hooks/pre-push", check=False).stdout.strip()
    hook_path = Path(hook) if Path(hook).is_absolute() else prep / hook
    if hook and os.access(hook_path, os.X_OK):
        raise Exit(5, f"pre-push hook refused the push (never --no-verify):\n{err}")
    raise Exit(1, f"push failed:\n{err}")


_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TOP_VERSION = re.compile(rb'^(\s*"version"\s*:\s*")([^"\\]*)(")', re.MULTILINE)
# Every `"version":` key anywhere in the file, nested or inline (F1035 post-verdict).
_ANY_VERSION = re.compile(rb'"version"\s*:')


def _json(text: str):
    try:
        # Numbers and constants kept as their literal text (sticky-wicket
        # PATCH, F1035): parsed floats conflate 1e400/1e401 and 1.0/1.00.
        value = json.loads(text, parse_int=str, parse_float=str, parse_constant=str)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _blank_version(raw: bytes) -> tuple[bytes, str] | None:
    """The raw bytes with the top-level `"version"` value blanked, and that
    value; None unless the file holds exactly one `"version":` key anywhere,
    on its own line, and `json.loads` agrees it is the top-level value.
    Equality is then a plain byte compare of the blanked bytes (operator ruling
    on #1035): no newline translation, so CRLF vs LF differs too."""
    if len(_ANY_VERSION.findall(raw)) != 1:
        return None
    found = _TOP_VERSION.findall(raw)
    if len(found) != 1:
        return None
    try:
        value = found[0][1].decode("utf-8")
        data = _json(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if not data or data.get("version") != value:
        return None
    blanked, count = _TOP_VERSION.subn(lambda m: m.group(1) + m.group(3), raw)
    return (blanked, value) if count == 1 else None


_LOCK = "package-lock.json"
_VERSIONED = ("package.json", _LOCK)


def _blank_lock(raw: bytes) -> tuple[bytes, str] | None:
    """harmonic-forge#1057 gap 1: a `package-lock.json`'s two package-version
    keys (the root `"version"` and `packages[""].version`) blanked, and their
    common value; None unless they are the file's first two `"version"` lines,
    they agree, and `json.loads` confirms both were blanked."""
    try:
        data = _json(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    root = (data or {}).get("packages", {}).get("", {}) if data else {}
    value = data.get("version") if data else None
    if not isinstance(value, str) or not isinstance(root, dict) or root.get("version") != value:
        return None
    blanked, count = _TOP_VERSION.subn(lambda m: m.group(1) + m.group(3), raw, count=2)
    check = _json(blanked.decode("utf-8")) if count == 2 else None
    if not check or check.get("version") != "" or check["packages"][""].get("version") != "":
        return None
    return blanked, value


def _blank(path: str, raw: bytes) -> tuple[bytes, str] | None:
    return _blank_lock(raw) if Path(path).name == _LOCK else _blank_version(raw)


def _set_version(path: str, raw: bytes, target: str) -> bytes | None:
    count = 2 if Path(path).name == _LOCK else 1
    text, done = _TOP_VERSION.subn(
        lambda m: m.group(1) + target.encode() + m.group(3), raw, count=count)
    return text if done == count else None


def _show_raw(prep: Path, spec: str) -> bytes | None:
    """`git show` as raw bytes: text mode would translate CRLF (F1035)."""
    proc = subprocess.run(["git", "-C", str(prep), "show", spec], capture_output=True)
    return None if proc.returncode else proc.stdout


def version_target(repo_dir: str, old: str) -> tuple[str | None, dict[str, str]]:
    """`T` (origin/main's `package.json` version plus one patch) and
    origin/main's version per `package.json` the branch touches. No `T` when
    those files disagree or none carries a MAJOR.MINOR.PATCH version."""
    base = git(repo_dir, "merge-base", "refs/remotes/origin/main", old).stdout.strip()
    touched = git(repo_dir, "diff", "--name-only", base, old).stdout.split()
    mains: dict[str, str] = {}
    for path in touched:
        if Path(path).name not in _VERSIONED:
            continue
        shown = git(repo_dir, "show", f"refs/remotes/origin/main:{path}", check=False)
        data = _json(shown.stdout) if shown.returncode == 0 else None
        version = data.get("version") if data else None
        if isinstance(version, str) and _SEMVER.match(version):
            mains[path] = version
    # `T` comes from package.json alone (F1057 preclose): hrse's auto-bump never
    # updates the lock, so the lock's version lags and must not veto `T`. The
    # lock's own main version stays in `mains` for its MAJOR/MINOR check.
    packages = {v for p, v in mains.items() if Path(p).name == "package.json"}
    if len(packages) != 1:
        return None, mains
    major, minor, patch = _SEMVER.match(next(iter(packages))).groups()
    return f"{major}.{minor}.{int(patch) + 1}", mains


def _resolve_versions(prep: Path, paths: list[str], target: str | None,
                      mains: dict[str, str]) -> str | None:
    """Resolve every unmerged path as a version-only `package.json` conflict
    and stage it; return None on success, else why not (nothing staged is
    pushed: the caller aborts)."""
    if not target or not paths:
        return "not confined to a package.json version line"
    if any(Path(p).name not in _VERSIONED for p in paths):
        return "not confined to a package.json version line"
    staged: list[tuple[str, bytes]] = []
    for path in paths:
        ours, theirs = _show_raw(prep, f":2:{path}"), _show_raw(prep, f":3:{path}")
        if ours is None or theirs is None:
            return f"{path}: not a two-sided content conflict"
        upstream, branch_side = _blank(path, ours), _blank(path, theirs)
        if upstream is None or branch_side is None:
            return f"{path}: not exactly one top-level version line per side"
        if upstream[0] != branch_side[0]:
            return f"{path}: differs beyond the top-level version"
        branch_version = branch_side[1]
        main_version = mains.get(path)
        found = _SEMVER.match(branch_version) if isinstance(branch_version, str) else None
        if not found or not main_version:
            return f"{path}: version {branch_version!r} is not MAJOR.MINOR.PATCH"
        # Operator ruling on #1035: a MAJOR or MINOR bump is never discarded.
        if found.groups()[:2] != _SEMVER.match(main_version).groups()[:2]:
            return (f"{path}: branch version {branch_version} differs from "
                    f"origin/main's {main_version} in MAJOR or MINOR")
        text = _set_version(path, ours, target)
        result = _blank(path, text) if text is not None else None
        if result is None or result != (upstream[0], target):
            return f"{path}: the top-level version could not be replaced alone"
        staged.append((path, text))
    for path, text in staged:
        (prep / path).write_bytes(text)
        git(prep, "add", "--", path)
    return None


def _take_upstream(prep: Path, paths: list[str]) -> None:
    """harmonic-forge#1057: resolve each declared generated path to stage :2
    (origin/main plus the commits already replayed); regenerated after."""
    for path in paths:
        # Either side missing is a delete/modify conflict (F1057 preclose: a
        # branch-side delete must not be silently undone by main's copy).
        if _show_raw(prep, f":2:{path}") is None or _show_raw(prep, f":3:{path}") is None:
            raise Exit(2, f"{path}: declared generated file was deleted on one side "
                          "(delete/modify conflict); not regenerated")
        git(prep, "checkout", "--ours", "--", path)
        git(prep, "add", "--", path)


def _regenerate(prep: Path, declared: dict[str, str], provision: str | None,
                paths: list[str]) -> None:
    """Run the provision task, then each declared task once, and commit only the
    declared paths. Any failure resets the prep worktree and exits 2, so nothing
    is pushed and the `finally` removal finds a clean tree."""
    def fail(message: str) -> None:
        git(prep, "reset", "--hard", check=False)
        # A task may create untracked files reset leaves behind, and they would
        # block the prep worktree's removal (found by test r4).
        git(prep, "clean", "-fdq", check=False)
        raise Exit(2, message)

    for task in ([provision] if provision else []) + [declared[p] for p in paths]:
        proc = run_task(prep, task)
        if proc.returncode:
            fail(f"regeneration task `mise run {task}` failed (exit {proc.returncode}); "
                 f"nothing pushed:\n{(proc.stdout + proc.stderr).strip()[-2000:]}")
    git(prep, "add", "--", *paths)
    stray = [line for line in git(prep, "status", "--porcelain").stdout.splitlines()
             if line[3:] not in paths]
    if stray:
        fail("regeneration left changes outside the declared paths; nothing pushed:\n"
             + "\n".join(f"  {line}" for line in stray))
    if git(prep, "diff", "--cached", "--quiet", check=False).returncode:
        git(prep, "commit", "-q", "--no-verify", "-m",
            f"regenerate {', '.join(paths)} after rebase onto origin/main (lane1-rebase)")


def rebase_and_push(repo_dir: str, issue: int, branch: str, old: str,
                    passed: str | None = None) -> tuple[str, str | None, list[str]]:
    prep = prep_path(repo_dir, issue)
    prune_stale_prep(repo_dir, prep)
    prep.parent.mkdir(parents=True, exist_ok=True)
    target, mains = version_target(repo_dir, old)
    declared, provision = regenerate_config(repo_dir)
    git(repo_dir, "worktree", "add", "--detach", str(prep), old)
    resolved = None
    regenerated: list[str] = []
    try:
        try:
            proc = git(prep, "rebase", "origin/main", check=False)
            # Never `git rebase --skip`: a resolution that empties a commit
            # is dropped by --continue itself, so a failed --continue is the
            # next commit's conflict, re-read here.
            while proc.returncode:
                paths = git(prep, "diff", "--name-only", "--diff-filter=U",
                            check=False).stdout.split()
                generated = [p for p in paths if p in declared]
                rest = [p for p in paths if p not in declared]
                why = _resolve_versions(prep, rest, target, mains) if rest or not paths else None
                if why:
                    listed = "\n".join(f"  {p}" for p in paths) or "  (none reported)"
                    raise Exit(2, f"rebase of {branch} onto origin/main conflicts:\n"
                                  f"{listed}\n  ({why})")
                _take_upstream(prep, generated)
                regenerated += [p for p in generated if p not in regenerated]
                if rest:
                    resolved = target
                proc = git(prep, "rebase", "--continue", check=False,
                           env={**os.environ, "GIT_EDITOR": "true"})
        except BaseException:
            git(prep, "rebase", "--abort", check=False)
            raise
        if regenerated:
            _regenerate(prep, declared, provision, sorted(regenerated))
        # Order (F1057 pitch): regenerate and commit, then read `new`, then the
        # receipt, then push, so a crash receipt never names an unpushed SHA.
        new = rev(prep, "HEAD")
        # sticky-wicket reforge F1034: record the crash state before it can
        # exist, so a re-run never has to infer it from history.
        # No receipt on the l2.done path: there is no PASS to carry.
        if passed:
            write_receipt(repo_dir, issue, passed, old, new)
        push(prep, branch, old)
        return new, resolved, sorted(regenerated)
    finally:
        remove_prep(repo_dir, prep)


def move_local_ref(repo_dir: str, branch: str, new: str,
                   rebased_from: str | None = None) -> None:
    local = rev(repo_dir, f"refs/heads/{branch}")
    if local == new:
        return
    if local is None:
        raise Exit(3, f"local {branch} does not exist; origin is at {new}")
    # Operator ruling F1034 (option A): `git cherry` skips merge commits, so
    # a local merge carrying unique edits would pass the check below and be
    # reset away. Refuse any local merge commit origin lacks.
    merges = git(repo_dir, "rev-list", "--merges",
                 f"refs/remotes/origin/{branch}..refs/heads/{branch}").stdout.split()
    if merges:
        raise Exit(3, f"local {branch} carries merge commit(s) origin lacks: "
                      f"{', '.join(m[:12] for m in merges)}; origin is at {new}. "
                      "The operator finishes the local move by hand.")
    # Every local commit origin lacks must be patch-equivalent to one in
    # `new`; otherwise moving the ref would drop unpushed Lane 2 work.
    # harmonic-forge#1035: a resolved version line makes the rebased commit
    # non-equivalent. `rebased_from` is this run's own pre-rebase origin SHA,
    # passed only when this run resolved a version and pushed `new`; a local
    # branch exactly at it carries nothing origin lacked. In-process only: a
    # crash re-run after a resolution exits 3 (sticky-wicket PATCH, R-0381).
    if rebased_from and local == rebased_from:
        cherry = []
    else:
        cherry = git(repo_dir, "cherry", new, local).stdout.splitlines()
    if any(line.startswith("+") for line in cherry):
        raise Exit(3, f"local {branch} is at {local}; origin is at {new} "
                      "(local carries commits origin does not)")
    holder = next((e["worktree"] for e in worktrees(repo_dir)
                   if e.get("branch") == f"refs/heads/{branch}"), None)
    if holder:
        if git(holder, "status", "--porcelain").stdout.strip():
            raise Exit(3, f"local {branch} is at {local}; origin is at {new} "
                          f"(impl worktree {holder} is dirty)")
        git(holder, "reset", "--keep", new)
    else:
        git(repo_dir, "update-ref", f"refs/heads/{branch}", new, local)
    if rev(repo_dir, f"refs/heads/{branch}") != new:
        raise Exit(3, f"local {branch} is at {rev(repo_dir, f'refs/heads/{branch}')}; "
                      f"origin is at {new}")


def run(repo_dir: str, issue: int, branch: str) -> tuple[str, str | None, list[str]]:
    # preclose F1018-3: one run per issue at a time. The stale-prep busy check
    # ignores same-lane processes, so a concurrent Lane 1 run would look stale.
    lock_path = prep_path(repo_dir, issue).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Exit(1, f"another lane1-rebase run holds {lock_path}; nothing done") from None
        return _run_locked(repo_dir, issue, branch)


def _run_locked(repo_dir: str, issue: int, branch: str) -> tuple[str, str | None, list[str]]:
    fetch(repo_dir, branch)
    old = rev(repo_dir, f"refs/remotes/origin/{branch}")
    main = rev(repo_dir, "refs/remotes/origin/main")
    if not old or not main:
        raise Exit(1, f"origin/{branch} or origin/main did not resolve after fetch")
    passed = check_preconditions(repo_dir, issue, branch, old)
    if git(repo_dir, "merge-base", "--is-ancestor", main, old, check=False).returncode == 0:
        new, resolved, regenerated = old, None, []  # already on current main (incl. a crash re-run)
    else:
        new, resolved, regenerated = rebase_and_push(repo_dir, issue, branch, old, passed)
    # harmonic-forge#1057: a regeneration commit, like a resolved version,
    # makes the rebase non-patch-equivalent to the local branch.
    move_local_ref(repo_dir, branch, new, old if (resolved or regenerated) else None)
    drop_receipt(repo_dir, issue)
    return new, resolved, regenerated


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[1].isdigit():
        print("usage: lane1-rebase <repo-dir> <issue> <branch>", file=sys.stderr)
        return 1
    try:
        new, resolved, regenerated = run(argv[0], int(argv[1]), argv[2])
    except Exit as exc:
        print(f"[lane1-rebase] exit {exc.code}: {exc.message}", file=sys.stderr)
        return exc.code
    print(f"new={new}")
    if resolved:
        print(f"version-resolved={resolved}")
    if regenerated:
        print(f"regenerated={','.join(regenerated)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
