#!/usr/bin/env python3
"""State-bound precheck receipts (harmonic-forge#1291 decisions 2-4).

A gated tool's precheck form (`l1_post.py --dry-run`, `kill_check.py run
--validate`, `preclose_check.py` without `--force`, ...) calls `record()` at
its own clean point, and only there: exit 0, no refusal. The receipt binds the
tool, the precheck mode, the canonical argv (the call minus its strip/add
tokens) and the state the registry's `binds` name, resolved now (`head_sha`,
`origin_*_sha`, `checks_file`, `body_content`). Every receipt also expires
1800 s after it was written; for a state-bound mode that is a ceiling on top
of the binding, never the only binding.

Two readers decide on the same receipt, so they can never disagree:

- `tools/precheck/gate.py` (the PreToolUse gate) before the call runs;
- the tool itself through `_preflight.require` -> `consume()` (the backstop the
  operator kept on 2026-11-18), which sees argparse's real parse.

`consume()` does not spend a precheck receipt: the gate and the tool both read
it, and the real run may be retried within the binding. With no receipt, it
spends a usable `FORCE <KEY> gate <id>` grant bound to this call, at the point
the real run starts; the gate only checks that such a grant exists, because a
PreToolUse hook cannot know the call will run (harmonic-forge#1291 sticky-wicket
PATCH, cluster "grant spent before the effect is assured").

The store is `~/.claude/state/precheck-gate/` (`PRECHECK_GATE_DIR` overrides
it for tests). `tools/hooks/precheck_store_guard.py` denies agent writes into
it; like the FORCE and IMC stores it is an audit trail, not proof against a
session that can write the store. A receipt never holds the command text.

The session is `CLAUDE_CODE_SESSION_ID` (the PreToolUse payload's
`session_id`). A blank or missing session is the fixed `no-session` bucket,
which needs its own receipt like any other: the caller controls that variable,
so a blank one is never a skip (harmonic-forge#1291, operator ruling
2026-11-19, reversing design alternative 6's "empty session stays ungated").

CLI (lanes-claude's dry-run exit only; a Python tool calls `record()`):
    gate_receipt.py record --tool tools/lane/lanes-claude -- <original args>
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORGE_ROOT = HERE.parents[1]
sys.path.insert(0, str(FORGE_ROOT / "tools" / "registry"))
for _extra_path in ("gh", "hooks"):  # force_grant, batch_auth: the gate grant
    if str(FORGE_ROOT / "tools" / _extra_path) not in sys.path:
        sys.path.append(str(FORGE_ROOT / "tools" / _extra_path))

STORE_ENV = "PRECHECK_GATE_DIR"
CEILING_S = 1800
NO_SESSION = "no-session"
DEADLINE_S = 5.0
LANES_CLAUDE = "tools/lane/lanes-claude"
#: lanes-claude's options that consume the next token (`shift 2`, tools/lane/lanes-claude:50-75).
LANES_CLAUDE_VALUED = ("--session", "--project", "--lane1-dir", "--lane2-dir", "--lane3-dir",
                       "--advisor-dir")
LANES_CLAUDE_STOP = ("--stop-lanes", "--stop-advisor", "--record-lanes")
HELP = ("-h", "--help")


class Deadline(Exception):
    """Fingerprint resolution passed its internal deadline (design alternative 7)."""


class ReceiptRefused(Exception):
    """`record()` refuses: not a receipt writer, or not the precheck form."""


def session() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or NO_SESSION


def store_dir() -> Path:
    # PREFLIGHT_RECEIPT_DIR: the pre-#1291 override, still honored for tests.
    override = os.environ.get(STORE_ENV) or os.environ.get("PREFLIGHT_RECEIPT_DIR")
    return Path(override) if override else Path.home() / ".claude" / "state" / "precheck-gate"


def _session_dir(sid: str) -> Path:
    return store_dir() / hashlib.sha256(sid.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# The registry's view of a call.

@functools.lru_cache(maxsize=1)
def registry() -> dict:
    import effects  # noqa: PLC0415
    return effects.load()


@functools.lru_cache(maxsize=1)
def tool_options() -> dict:
    path = FORGE_ROOT / "tools" / "precheck" / "tool_options.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def precheck_tool(path: str) -> dict | None:
    """The registry tool at `path` (aliases resolved) when it has a precheck mode."""
    import effects  # noqa: PLC0415
    try:
        tool = effects.find_tool(registry(), path)
    except KeyError:
        return None
    return tool if any("precheck" in m for m in tool["modes"]) else None


def _options(tool: dict, mode: dict) -> list[str]:
    subs = [t for t in mode["match"] if not t.startswith("-")]
    return tool_options().get(" ".join([tool["path"], *subs]), [])


def resolve_option(token: str, options: list[str], prefix: bool) -> str | None:
    """The option `token` names: itself, or, when `prefix`, the one option it
    is an unambiguous prefix of (argparse's rule). An ambiguous or foreign
    prefix names nothing."""
    if not token.startswith("-"):
        return None
    name = token.split("=", 1)[0] if token.startswith("--") else token
    if name in options:
        return name
    if not prefix or not name.startswith("--") or len(name) < 3:
        return None
    hits = [o for o in options if o.startswith("--") and o.startswith(name)]
    return hits[0] if len(hits) == 1 else None


def _lanes_claude(argv: list[str]) -> tuple[str, list[int]]:
    """Replays lanes-claude's own `case` loop: (`help`|`stop`|`dry-run`|`launch`,
    the indexes of `--dry-run` tokens in flag position). A value-taking option
    consumes the next token, whatever it is; a missing value or an unknown
    token is a launch (the conservative reading: consume() then requires its receipt)."""
    index, dry, stop, broken = 0, [], False, False
    while index < len(argv):
        token = argv[index]
        if token in LANES_CLAUDE_VALUED:
            if index + 1 >= len(argv) or argv[index + 1] == "":
                broken = True
                break
            index += 2
            continue
        if token == "--dry-run":
            dry.append(index)
        elif token in LANES_CLAUDE_STOP:
            stop = True
        elif token in HELP:
            return "help", dry
        else:
            broken = True
            break
        index += 1
    if broken:
        return "launch", dry
    if stop:
        return "stop", dry
    return ("dry-run" if dry else "launch"), dry


def _has(tool: dict, mode: dict, argv: list[str], token: str) -> list[int]:
    """Indexes in `argv` naming `token` for this mode."""
    form = mode["precheck_form"]
    if tool["path"] == LANES_CLAUDE:
        return _lanes_claude(argv)[1] if token == "--dry-run" else []
    if token in form["add"] and "add_at" in form:
        at = form["add_at"]
        return [at] if len(argv) > at and argv[at] == token else []
    options = _options(tool, mode)
    return [i for i, t in enumerate(argv)
            if (t == token if not mode["prefix"] else resolve_option(t, options, True) == token)]


def _applies(tool: dict, mode: dict, argv: list[str]) -> bool:
    strip = set(mode["precheck_form"]["strip"])
    positional = [t for t in mode["match"] if not t.startswith("-")]
    if positional and argv[:len(positional)] != positional:
        return False
    options = _options(tool, mode)
    for token in mode["match"]:
        if token.startswith("-") and token not in strip:
            if not any((t == token) or (mode["prefix"] and resolve_option(t, options, True) == token)
                       for t in argv):
                return False
    return True


def is_help(tool: dict, argv: list[str]) -> bool:
    if tool["path"] == LANES_CLAUDE:
        return _lanes_claude(argv)[0] == "help"
    return any(t in HELP or (t.startswith("--he") and "--help".startswith(t)) for t in argv)


def classify(tool: dict, argv: list[str]) -> tuple[dict, bool] | None:
    """(precheck mode, is_real_run) for a call, or None when no precheck mode
    applies (another mode, a help run). A real run carries a strip token or
    lacks an add token; exactly the precheck form is not real."""
    if is_help(tool, argv):
        return None
    if tool["path"] == LANES_CLAUDE:
        # The script's own loop decides, never a token search: `--session
        # --dry-run` and `--dry-run --session` (no value) are both launches.
        state = _lanes_claude(argv)[0]
        if state == "stop":
            return None
        mode = next(m for m in tool["modes"] if "precheck" in m)
        return mode, state != "dry-run"
    for mode in tool["modes"]:
        if "precheck" not in mode or not _applies(tool, mode, argv):
            continue
        form = mode["precheck_form"]
        real = (any(_has(tool, mode, argv, t) for t in form["strip"])
                or any(not _has(tool, mode, argv, t) for t in form["add"]))
        return mode, real
    return None


def canonical(tool: dict, mode: dict, argv: list[str]) -> list[str]:
    """argv with every strip and add token removed. A token is removed only
    where it names the option (exactly, or as an unambiguous prefix when the
    mode allows that); an ambiguous or foreign prefix stays in place."""
    form = mode["precheck_form"]
    drop: set[int] = set()
    for token in form["strip"] + form["add"]:
        drop.update(_has(tool, mode, argv, token))
    return [t for i, t in enumerate(argv) if i not in drop]


def precheck_argv(tool: dict, mode: dict, argv: list[str]) -> list[str]:
    """The exact precheck call for a real run's argv: strip tokens removed, add
    tokens inserted at `add_at`, else appended."""
    form = mode["precheck_form"]
    out = canonical(tool, mode, argv)
    for token in form["add"]:
        if "add_at" in form:
            out.insert(form["add_at"], token)
        else:
            out.append(token)
    return out


# --------------------------------------------------------------------------
# The state fingerprint (decision 4).

def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def _arg(argv: list[str], spec: str) -> str | None:
    if spec.startswith("#"):
        index = int(spec[1:])
        return argv[index] if index < len(argv) else None
    if spec.startswith("-"):
        return _flag_value(argv, spec)
    return None


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise Deadline("fingerprint resolution passed its deadline")
    return left


def _git_sha(directory: Path, ref: str, deadline: float) -> str:
    try:
        done = subprocess.run(["git", "-C", str(directory), "rev-parse", "--verify", "--quiet", ref],
                              capture_output=True, text=True, timeout=_remaining(deadline), check=False)
    except subprocess.TimeoutExpired as exc:
        raise Deadline(f"git rev-parse {ref} timed out") from exc
    return done.stdout.strip() if done.returncode == 0 else f"unresolved:{ref}"


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _checks_digest(path: Path, cwd: Path) -> str:
    """The checks file and every patch it names, by content (the digest kill_check.py kept before harmonic-forge#1291,
    which resolves a relative patch path against the caller's cwd)."""
    parts = [_file_digest(path)]
    try:
        for entry in json.loads(path.read_bytes()):
            parts.append(_file_digest(_path(cwd, str(entry.get("patch", "")))))
    except (OSError, ValueError, AttributeError, TypeError):
        parts.append("unreadable-patches")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _path(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else cwd / path


def fingerprint(tool: dict, mode: dict, argv: list[str], cwd: str | Path,
                deadline: float | None = None) -> str:
    """sha256 over the tool, the mode, the canonical argv and each resolved
    bind. Raises Deadline past `deadline` (time.monotonic())."""
    deadline = deadline if deadline is not None else time.monotonic() + DEADLINE_S
    _remaining(deadline)
    cwd = Path(cwd)
    canon = canonical(tool, mode, argv)
    bind_args = mode["bind_args"]
    git_dir = _arg(canon, bind_args["git_dir"]) if "git_dir" in bind_args else None
    git_dir = _path(cwd, git_dir) if git_dir else cwd
    state: dict[str, object] = {}
    for bind in mode["binds"]:
        if bind == "args":
            continue
        spec = bind_args[bind]
        if bind == "head_sha":
            ref = (_arg(canon, spec) if spec != "cwd" else None) or "HEAD"
            state[bind] = _git_sha(git_dir, f"{ref}^{{commit}}", deadline)
        elif bind == "origin_main_sha":
            state[bind] = _git_sha(git_dir, "refs/remotes/origin/main", deadline)
        elif bind == "origin_branch_sha":
            branch = _arg(canon, spec) or ""
            state[bind] = _git_sha(git_dir, f"refs/remotes/origin/{branch}", deadline)
        elif bind == "checks_file":
            value = _arg(canon, spec)
            state[bind] = _checks_digest(_path(cwd, value), cwd) if value else "absent"
        elif bind == "body_content":
            specs = spec if isinstance(spec, list) else [spec]
            state[bind] = {s: (_file_digest(_path(cwd, v)) if (v := _arg(canon, s)) else None)
                           for s in specs}
        _remaining(deadline)
    blob = json.dumps({"tool": tool["path"], "mode": mode["name"], "argv": canon, "state": state},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------
# The store.

def _receipt_path(sid: str, fp: str) -> Path:
    return _session_dir(sid) / f"{fp[:32]}.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise


def _extra_digest(extra: str | None) -> str | None:
    return hashlib.sha256(extra.encode()).hexdigest() if extra is not None else None


def write_receipt(sid: str, tool: dict, mode: dict, fp: str, *, kind: str = "precheck",
                  ttl: int = CEILING_S, now: float | None = None, extra: str | None = None) -> Path:
    moment = time.time() if now is None else now
    path = _receipt_path(sid, fp)
    _write_json(path, {"tool": tool["path"], "mode": mode["name"], "fp": fp, "kind": kind,
                       "recorded_at": moment, "expires_at": moment + ttl,
                       "extra": _extra_digest(extra)})
    return path


def live_receipt(sid: str, fp: str, now: float | None = None) -> dict | None:
    path = _receipt_path(sid, fp)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    moment = time.time() if now is None else now
    if not isinstance(data, dict) or data.get("fp") != fp:
        return None
    expires = data.get("expires_at")
    if not isinstance(expires, (int, float)) or moment >= expires:
        return None
    return data


def _caller_tool(argv0: str) -> str | None:
    try:
        rel = Path(argv0).resolve().relative_to(FORGE_ROOT.resolve())
    except (ValueError, OSError):
        # A consuming repo's checkout runs the live forge copy: match by the
        # tools/<dir>/<name> suffix.
        parts = Path(argv0).parts
        rel = Path(*parts[-3:]) if len(parts) >= 3 and parts[-3] == "tools" else None
    return rel.as_posix() if rel is not None else None


def _caller_cwd(cwd: str | Path | None) -> Path:
    if cwd is not None:
        return Path(cwd)
    caller = sys.modules.get("_caller_path")
    consumed = getattr(caller, "CONSUMED", None) if caller is not None else None
    return Path(os.environ.get("HF_CALLER_CWD") or consumed or os.getcwd())


def record(tool_path: str, argv: list[str], cwd: str | Path | None = None, *,
           now: float | None = None, extra: str | None = None,
           _argv0: str | None = None) -> Path | None:
    """Write this session's receipt for the precheck call `argv` of
    `tool_path`. Called only from the tool's own clean point. Refuses
    (ReceiptRefused) unless the running program is that registry tool and its
    precheck mode is a receipt writer, and unless `argv` is the precheck form.
    No session: written to the `no-session` bucket.

    `extra` is state only the tool can read (lane1_rebase's `ls-remote` SHAs,
    a network read the gate never makes): the backstop `consume(extra=...)`
    also requires it unchanged; the gate, which cannot read it, ignores it."""
    caller = _caller_tool(_argv0 if _argv0 is not None else sys.argv[0])
    tool = precheck_tool(tool_path)
    if tool is None:
        raise ReceiptRefused(f"{tool_path} has no precheck mode in the registry")
    if caller is None or (precheck_tool(caller) or {}).get("path") != tool["path"]:
        raise ReceiptRefused(f"only {tool['path']} itself writes its receipt (caller {caller})")
    found = classify(tool, list(argv))
    if found is None or found[1]:
        raise ReceiptRefused(f"{tool['path']}: these arguments are not a precheck form")
    mode = found[0]
    if not mode["receipt_writer"]:
        raise ReceiptRefused(f"{tool['path']} {mode['name']}: not a receipt writer")
    sid = session()
    fp = fingerprint(tool, mode, list(argv), _caller_cwd(cwd))
    try:
        return write_receipt(sid, tool, mode, fp, now=now, extra=extra)
    except OSError as exc:
        print(f"[precheck] receipt not written ({exc}); the real run will refuse until "
              f"{store_dir()} is writable", file=sys.stderr)
        return None


def gate_binding(tool: dict, mode: dict, fp: str) -> dict:
    return {"gate_tool": tool["path"], "gate_mode": mode["name"], "arg_fp": fp}


def _gate_grant_keys(argv: list[str]) -> list[str]:
    import force_grant  # noqa: PLC0415
    repo, issue = _flag_value(argv, "--repo"), _flag_value(argv, "--issue")
    if repo and issue and issue.isdigit():
        key = None
        with contextlib.suppress(Exception):
            import batch_auth  # noqa: PLC0415
            key = batch_auth.issue_key(repo, int(issue))
        if isinstance(key, str) and key:
            return [key]
    # No resolvable issue key: every grant file is a candidate (each is still
    # matched on its bound tool, mode and argument fingerprint).
    return [p.stem for p in sorted(force_grant.state_dir().glob("*.json"))]


def gate_grant_usable(tool: dict, mode: dict, argv: list[str], fp: str) -> bool:
    """Whether a usable gate grant is bound to this call. Never spends it."""
    import force_grant  # noqa: PLC0415
    for key in _gate_grant_keys(argv):
        grant = force_grant.read_grant(key)
        if grant and grant.get("mode") == "gate" and grant.get("arg_fp") == fp \
                and grant.get("status") == "usable":
            return True
    return False


def spend_gate_grant(tool: dict, mode: dict, argv: list[str], fp: str) -> bool:
    """Spend a usable gate grant bound to this call, as the real run starts."""
    import force_grant  # noqa: PLC0415
    for key in _gate_grant_keys(argv):
        grant = force_grant.read_grant(key)
        if not grant or grant.get("mode") != "gate" or grant.get("arg_fp") != fp:
            continue
        try:
            force_grant.consume(key, grant.get("granted_at"), "", mode="gate",
                                gate=gate_binding(tool, mode, fp))
        except force_grant.GrantRefused:
            continue
        return True
    return False


def consume(tool_path: str, argv: list[str], preflight_cmd: str | None = None,
            cwd: str | Path | None = None, *, now: float | None = None,
            extra: str | None = None) -> str | None:
    """The tool-side check: None when this real run may proceed, else the
    refusal. Reads the same state-bound receipt the gate reads; with none, it
    spends a usable gate grant bound to this exact call."""
    tool = precheck_tool(tool_path)
    sid = session()
    if tool is None:
        return None
    found = classify(tool, list(argv))
    if found is None or not found[1]:
        return None
    mode = found[0]
    fp = fingerprint(tool, mode, list(argv), _caller_cwd(cwd))
    receipt = live_receipt(sid, fp, now)
    if receipt is not None and receipt.get("extra") is not None \
            and receipt.get("extra") != _extra_digest(extra):
        receipt = None  # the tool-only state moved since the precheck
    if receipt is not None or spend_gate_grant(tool, mode, list(argv), fp):
        return None
    command = preflight_cmd or " ".join(["python3", str(FORGE_ROOT / tool["path"]),
                                         *precheck_argv(tool, mode, list(argv))])
    return (f"no live precheck receipt for this exact call (harmonic-forge#1291): run its "
            f"precheck form first, with the same arguments, at the same state:\n  {command}\n"
            f"A receipt binds the arguments and {', '.join(b for b in mode['binds'] if b != 'args') or 'nothing else'}"
            f", and expires after {CEILING_S // 60} minutes. If you already ran it, the store "
            f"{store_dir()} is not writable (see its [precheck] warning).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a precheck receipt for a non-Python tool.")
    sub = parser.add_subparsers(dest="command", required=True)
    for verb in ("record", "consume"):
        cmd = sub.add_parser(verb)
        cmd.add_argument("--tool", required=True)
        cmd.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.args[1:] if args.args[:1] == ["--"] else args.args
    tool = precheck_tool(args.tool)
    # A Python tool records and consumes in process; only a script that cannot
    # import this module (lanes-claude, a bash script) uses the CLI.
    if tool is None or tool["path"].endswith(".py"):
        print(f"gate_receipt: {args.tool} does not use the CLI", file=sys.stderr)
        return 2
    if args.command == "consume":
        # harmonic-forge#1291 (REFORGE, issuecomment-7002756185): the same
        # tool-side backstop the Python tools call, for a bash tool's real run.
        try:
            refusal = consume(tool["path"], rest)
        except Deadline as exc:
            refusal = f"the precheck receipt could not be checked ({exc}); retry"
        if refusal:
            print(f"gate_receipt: {refusal}", file=sys.stderr)
            return 1
        return 0
    try:
        record(tool["path"], rest, _argv0=str(FORGE_ROOT / tool["path"]))
    except (ReceiptRefused, Deadline) as exc:
        print(f"gate_receipt: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
