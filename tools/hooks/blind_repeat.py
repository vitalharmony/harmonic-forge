"""harmonic-forge#1216: a lane cannot blindly repeat a command that just failed.

Shared by `record_failed_command.py` (PostToolUseFailure, Bash) and
`block_blind_repeat.py` (PreToolUse, Bash).

The key is a digest of the literal command string plus the state it ran
against: cwd, git HEAD, and the working tree's content (`git diff HEAD` plus
untracked file contents). Nothing in the command is parsed, so there is no
grammar to recognize (harmonic-forge#1076's ruling) and the only command it
can refuse is a byte-identical repeat against an unchanged tree inside the
TTL. It is a speed bump, not enforcement: changing one byte clears it.

`git status --porcelain` is deliberately not the tree key: re-editing an
already-modified file does not change it, so a real fix would be refused.

Fail-open everywhere (AC6): any store, git or parse error allows. A cwd
outside a git repo is neither recorded nor refused. No session id, no effect.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

TTL_SECONDS = 300
STALE_FILE_SECONDS = 24 * 60 * 60
#: AC3: one unchanged retry, matched by exact whole-string equality only.
FLAKY_COMMANDS = ("mise run check", "mise run check-full")
STORE_ENV = "BLIND_REPEAT_DIR"
#: pass 2 survivor 2: bounds on the out-of-repo files a key reads.
NAMED_FILES_MAX = 20
NAMED_FILE_BYTES_MAX = 1 << 20


def store_dir() -> Path:
    override = os.environ.get(STORE_ENV)
    return Path(override) if override else Path.home() / ".claude" / "state" / "blind-repeat"


def session_file(session: str) -> Path:
    return store_dir() / f"{hashlib.sha256(session.encode()).hexdigest()[:32]}.jsonl"


def _git(cwd: str, *args: str) -> bytes | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def state_key(command: str, cwd: str) -> str | None:
    """The digest of command plus repo state, or None outside a git repo."""
    head = _git(cwd, "rev-parse", "HEAD")
    if head is None:
        return None
    digest = hashlib.sha256()
    for part in (command.encode(), os.path.realpath(cwd).encode(), head.strip()):
        digest.update(part + b"\0")
    # pass 1 survivor 2: a git read that fails or times out means the tree is
    # undecidable, so there is no key (allow), never an empty-digest key.
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel is None or not toplevel.strip():
        return None
    root = toplevel.strip().decode()
    # pass 2 survivor 1: both reads run from the repo top, so untracked paths
    # are top-relative and cover the whole tree, whatever the cwd.
    diff = _git(root, "diff", "HEAD", "--binary")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if diff is None or untracked is None:
        return None
    digest.update(diff)
    for name in sorted(n for n in untracked.split(b"\0") if n):
        digest.update(name + b"\0")
        try:
            digest.update(hashlib.sha256((Path(root) / name.decode()).read_bytes()).digest())
        except OSError:
            return None  # pass 2 survivor 1: an unreadable file is an undecidable tree
    named = _named_outside_files(command, cwd, root)
    if named is None:
        return None
    digest.update(named)
    return digest.hexdigest()


def _named_outside_files(command: str, cwd: str, root: str) -> bytes | None:
    """pass 2 survivor 2: the contents of existing files outside the repo that
    the command names (a token, or a `--flag=value` value), so fixing an
    out-of-repo `--file` body changes the key. This only adds state to the
    key, so it can turn a refusal into an allow, never the reverse; it reads
    no command meaning (AC2). An unsplittable command or an unreadable file
    is undecidable (None, allow)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    top = os.path.realpath(root) + os.sep
    out = hashlib.sha256()
    seen = 0
    for token in tokens:
        candidate = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if not candidate or "\0" in candidate:
            continue
        path = os.path.realpath(os.path.join(cwd, os.path.expanduser(candidate)))
        if path.startswith(top) or not os.path.isfile(path):
            continue
        seen += 1
        if seen > NAMED_FILES_MAX:
            return None
        try:
            if os.path.getsize(path) > NAMED_FILE_BYTES_MAX:
                return None
            with open(path, "rb") as fh:
                out.update(path.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
        except OSError:
            return None
    return out.digest()


def _first_failure_line(payload: dict) -> str:
    response = payload.get("tool_response")
    candidates = [payload.get("error"),
                  response.get("stderr") if isinstance(response, dict) else response]
    # The documented PostToolUseFailure payload carries `error`, e.g.
    # "Exit code 1\nError: Cannot find module 'express'": the exit-code line
    # says nothing the refusal does not already, so the first other line wins.
    for text in candidates:
        if isinstance(text, str) and text.strip():
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            useful = [ln for ln in lines if not ln.startswith("Exit code ")]
            return (useful or lines)[0][:200]
    return ""


def _read(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def record(payload: dict, now: float | None = None) -> None:
    session = payload.get("session_id") or ""
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or ""
    if not session or not command or not cwd:
        return
    key = state_key(command, cwd)
    if key is None:
        return
    now = time.time() if now is None else now
    path = session_file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    # pass 1 survivor 3: one O_APPEND write per record, never a
    # read-modify-replace, so concurrent failures cannot overwrite each other.
    # Expired records are filtered at read time (refusal); a session's file is
    # removed by the stale-file prune below once the session has ended.
    line = json.dumps({"key": key, "t": now, "line": _first_failure_line(payload),
                       "flaky": command in FLAKY_COMMANDS}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    for other in store_dir().glob("*.jsonl"):  # AC7: other sessions' stale files
        try:
            if other != path and now - other.stat().st_mtime > STALE_FILE_SECONDS:
                other.unlink()
        except OSError:
            pass


def refusal(payload: dict, now: float | None = None) -> str | None:
    session = payload.get("session_id") or ""
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or ""
    if not session or not command or not cwd:
        return None
    records = _read(session_file(session))
    if not records:
        return None
    key = state_key(command, cwd)
    if key is None:
        return None
    now = time.time() if now is None else now
    hits = [r for r in records if r.get("key") == key and now - r.get("t", 0) < TTL_SECONDS]
    if not hits:
        return None
    if command in FLAKY_COMMANDS and len(hits) < 2:
        return None  # AC3: one unchanged retry of a declared flaky command
    last = hits[-1]
    ago = int(now - last.get("t", now))
    why = f": {last['line']}" if last.get("line") else ""
    return (f"This exact command failed {ago}s ago against the same HEAD and tree{why}. "
            "Read what it requires and change something (the command, the code, or the state) "
            f"before running it again; an unchanged repeat is refused for {TTL_SECONDS}s "
            "(harmonic-forge#1216, R-0419).")
