#!/usr/bin/python3 -I
"""The one auto-approved object for a Lane 3 production run (harmonic-forge#878,
sticky-wicket PATCH on the new epoch's pass-1 cluster).

Installed root-owned at /usr/local/libexec/hrse-gate-run (0755) and run as the
lane's own user; root ownership alone makes it unwritable by the agent. The
Lane 3 Claude policy allows exactly `Bash(/usr/local/libexec/hrse-gate-run:*)`,
an absolute path naming an object the agent cannot replace -- the relative
`backend/.venv/bin/python scripts/gate_production_run.py` rule it replaces
named whatever file the agent's cwd supplied (cluster C1a).

    hrse-gate-run --issue <N> --script scripts/1-<name>.py [--mode apply]
    hrse-gate-run --issue <N> --count-label <Label>

`--mode apply` is the sanctioned apply form, deliberately not `--apply`: HRSE2's
tracked `permissions.ask` rules `Bash(* --apply)` / `Bash(* --apply *)` outrank
any allow rule (survivor S2) and must keep prompting for every ad hoc --apply.

In order, it refuses unless:
  1. LANE=3.
  2. The manifest (/usr/local/share/hrse-gate/manifest.json) and its directory
     are owned by root and not group/other-writable.
  3. The installed broker's --version digest equals the manifest's.
  4. The gate worktree's scripts/gate_production_run.py hashes to the
     manifest's digest -- content, never `git status`, which is a property of
     whichever repo the agent points at.
  5. The host's own `sudo -n -l` shows exactly one rule naming the broker, the
     consume-only one (survivor S5: the host, not the install doc).
  6. The broker consumes one operator-minted grant for this exact issue, the
     worktree's HEAD and the action.
Only then does it exec the verified script with the manifest's interpreter,
setting HRSE_GATE_GRANT to the spent grant's nonce. The gate script refuses
without it, so a direct, retyped invocation -- which spends no grant -- refuses
too. It is a MISTAKE GUARD, not a boundary: the lane can set the variable
itself (a determined agent is hrse#2188's scope).
Steps 4 and the interpreter/payload checks are DRIFT DETECTORS, not a
boundary: the lane venv's site-packages and __pycache__ stay writable by the
lane's uid, which already holds the production credential. What this runner
enforces is the grant: one operator approval, one spent grant, one run.
A grant spent on step 6 stays spent if the script then refuses: an
interrupted run never reopens its grant.

Every input that carries authorization load is listed, with who can write it,
in tools/gate/install-hrse-gate.md ("Authorization inputs").
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

MANIFEST = Path("/usr/local/share/hrse-gate/manifest.json")
BROKER = Path("/usr/local/libexec/hrse-gate")
# Absolute, root-owned tools, run with a scrubbed environment (sticky-wicket
# PATCH 2, item 1): a lane's PATH puts ~/.local/bin first, where a planted
# `sudo` would fake both the rule check and the consume.
SUDO = Path("/usr/bin/sudo")
GIT = Path("/usr/bin/git")
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
GATE_SCRIPT = "scripts/gate_production_run.py"
SCRIPT = re.compile(r"^scripts/1-[A-Za-z0-9._-]+\.py$")
LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CONSUME_RULE = re.compile(
    r"^\(hrse-gate\)\s+NOPASSWD:\s+/usr/local/libexec/hrse-gate consume \*$")

#: The broker's success line; its nonce is handed to the gate script.
CONSUMED = re.compile(r"^consumed ([0-9a-f]{32})$")

EXIT_REFUSED = 7


class Refused(Exception):
    pass


def _immutable(path: Path) -> None:
    for p in (path, path.parent):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise Refused(f"{p} must be root-owned, not a symlink, and not group/other-writable")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=SAFE_ENV)


def _root_tool(path: Path) -> str:
    """A tool the runner executes must be root-owned and not writable by the
    agent; returns the absolute path to exec.

    A root-owned symlink is followed (openSUSE ships /usr/bin/git as a symlink
    to /usr/libexec/git/git; a found-live L3B on hrse#1867): the link itself
    and its directory must be root-owned and not group/other-writable, and so
    must the resolved target and the target's directory. The resolved target
    is what gets exec'd, so a link swapped after the check changes nothing."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        parent = os.lstat(path.parent)
        if st.st_uid != 0 or parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise Refused(f"{path} is a symlink that is not root-owned in a root-owned directory")
        real = Path(os.path.realpath(path))
        if not stat.S_ISREG(os.lstat(real).st_mode):
            raise Refused(f"{path} resolves to {real}, which is not a regular file")
        _immutable(real)
        return str(real)
    _immutable(path)
    return str(path)


def load_manifest() -> dict:
    _immutable(MANIFEST)
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("worktree", "python", "script_sha256", "broker_sha256", "python_sha256"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise Refused(f"manifest lacks {key}")
    if not isinstance(data.get("sudo_rules"), list) or not data["sudo_rules"]:
        raise Refused("manifest lacks sudo_rules (the host's reviewed sudo rule set)")
    return data


def check_broker(manifest: dict) -> None:
    result = _run([_root_tool(BROKER), "--version"])
    if result.returncode or result.stdout.strip() != manifest["broker_sha256"]:
        raise Refused("installed broker digest does not match the manifest's broker_sha256; "
                      "reinstall from a reviewed SHA")


def check_script(manifest: dict) -> Path:
    script = Path(manifest["worktree"]) / GATE_SCRIPT
    if _sha256(script) != manifest["script_sha256"]:
        raise Refused(f"{script} does not match the reviewed digest in the manifest")
    return script


def check_interpreter(manifest: dict) -> str:
    """DRIFT DETECTOR, not a boundary (sticky-wicket PATCH 2, item 9): the
    interpreter's resolved binary must hash to the manifest's python_sha256. Its
    venv's site-packages and every __pycache__ stay writable by the lane's uid,
    so the executed closure is not integrity-bounded; see install-hrse-gate.md."""
    real = Path(os.path.realpath(manifest["python"]))
    if _sha256(real) != manifest["python_sha256"]:
        raise Refused(f"interpreter {real} does not match the manifest's python_sha256")
    return manifest["python"]


def check_payload(manifest: dict, rel: str) -> None:
    """DRIFT DETECTOR (item 9): the migration script must be unmodified against
    the worktree's HEAD, so an edit after the grant was minted refuses."""
    worktree = manifest["worktree"]
    blob = _run([_root_tool(GIT), "-C", worktree, "rev-parse", f"HEAD:{rel}"])
    here = _run([_root_tool(GIT), "-C", worktree, "hash-object", rel])
    if blob.returncode or here.returncode or blob.stdout.strip() != here.stdout.strip():
        raise Refused(f"{rel} differs from HEAD (or is not tracked); a grant buys the committed script only")


def _sudo_rules(output: str) -> list[str]:
    """The rule lines of `sudo -n -l`: everything after the 'may run' header."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "may run the following commands" in line:
            return [r.strip() for r in lines[i + 1:] if r.strip()]
    return []


def check_sudo(manifest: dict) -> None:
    """An allowlist, not a grep (sticky-wicket PATCH 2, item 2): the host's
    whole rule set must equal the reviewed set recorded in the root-owned
    manifest, contain the one consume rule, and contain no other password-free
    rule that could reach the broker (ALL, or any wildcard)."""
    result = _run([_root_tool(SUDO), "-n", "-l"])
    if result.returncode:
        raise Refused(f"cannot list sudo rules: {result.stderr.strip() or result.returncode}")
    rules = _sudo_rules(result.stdout)
    if sorted(rules) != sorted(manifest["sudo_rules"]):
        raise Refused("the host's sudo rules differ from the reviewed set in the manifest: "
                      + " | ".join(rules or ["none"]))
    if sum(1 for r in rules if CONSUME_RULE.match(r)) != 1:
        raise Refused("the one consume-only rule is not present exactly once")
    for rule in rules:
        if "NOPASSWD" in rule and not CONSUME_RULE.match(rule) \
                and (rule.rstrip().endswith(" ALL") or "*" in rule):
            raise Refused(f"a password-free rule could reach the broker: {rule}")


def action(args) -> tuple[str, list[str]]:
    """(broker action, gate-script arguments)."""
    if args.script:
        if not SCRIPT.match(args.script):
            raise Refused(f"{args.script!r} is not a scripts/1-*.py migration script")
        apply = args.mode == "apply"
        return (f"script={args.script}" + (",apply" if apply else ""),
                ["--script", args.script] + (["--apply"] if apply else []))
    if not LABEL.match(args.count_label):
        raise Refused(f"{args.count_label!r} is not a node label")
    if args.mode != "dry-run":
        raise Refused("--mode applies to --script only")
    return f"count-label={args.count_label}", ["--count-label", args.count_label]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hrse-gate-run", allow_abbrev=False)
    p.add_argument("--issue", required=True)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--script")
    which.add_argument("--count-label")
    p.add_argument("--mode", choices=("dry-run", "apply"), default="dry-run")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.environ.get("LANE") != "3":
            raise Refused("LANE must be 3: only a Lane 3 session runs a production action")
        if not args.issue.isdecimal():
            raise Refused("--issue must be a positive integer")
        broker_action, script_args = action(args)
        manifest = load_manifest()
        check_broker(manifest)
        script = check_script(manifest)
        python = check_interpreter(manifest)
        if args.script:
            check_payload(manifest, args.script)
        check_sudo(manifest)
        head = _run([_root_tool(GIT), "-C", manifest["worktree"], "rev-parse", "HEAD"])
        if head.returncode:
            raise Refused("cannot read the gate worktree's HEAD")
        consume = _run([_root_tool(SUDO), "-n", "-u", "hrse-gate", _root_tool(BROKER), "consume",
                        args.issue, head.stdout.strip(), broker_action])
        if consume.returncode:
            raise Refused(f"no grant was spent: {consume.stderr.strip() or consume.stdout.strip()}")
        spent = CONSUMED.match(consume.stdout.strip())
        if not spent:
            raise Refused(f"the broker printed no grant id ({consume.stdout.strip()!r}); "
                          "the grant may be spent -- check `hrse-gate status`")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Item 7: an absent manifest, broker or script is a refusal with its
        # cause, never a traceback.
        print(f"hrse-gate-run: cannot decide, refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Refused as exc:
        print(f"hrse-gate-run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"hrse-gate-run: {consume.stdout.strip()}", file=sys.stderr)
    os.chdir(manifest["worktree"])
    os.execve(python, [python, str(script), "--issue", args.issue, *script_args],
              {**os.environ, "HRSE_GATE_GRANT": spent.group(1)})
    return EXIT_REFUSED  # unreachable


if __name__ == "__main__":
    sys.exit(main())
