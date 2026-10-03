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
Only then does it exec the verified script with the manifest's interpreter.
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
GATE_SCRIPT = "scripts/gate_production_run.py"
SCRIPT = re.compile(r"^scripts/1-[A-Za-z0-9._-]+\.py$")
LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CONSUME_RULE = re.compile(
    r"^\(hrse-gate\)\s+NOPASSWD:\s+/usr/local/libexec/hrse-gate consume \*$")

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
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def load_manifest() -> dict:
    _immutable(MANIFEST)
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("worktree", "python", "script_sha256", "broker_sha256"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise Refused(f"manifest lacks {key}")
    return data


def check_broker(manifest: dict) -> None:
    result = _run([str(BROKER), "--version"])
    if result.returncode or result.stdout.strip() != manifest["broker_sha256"]:
        raise Refused("installed broker does not match the manifest digest; reinstall from a reviewed SHA")


def check_script(manifest: dict) -> Path:
    script = Path(manifest["worktree"]) / GATE_SCRIPT
    if _sha256(script) != manifest["script_sha256"]:
        raise Refused(f"{script} does not match the reviewed digest in the manifest")
    return script


def check_sudo() -> None:
    result = _run(["sudo", "-n", "-l"])
    lines = [line.strip() for line in result.stdout.splitlines() if "hrse-gate" in line]
    if len(lines) != 1 or not CONSUME_RULE.match(lines[0]):
        raise Refused("sudo rules naming the broker are not exactly the one consume-only rule: "
                      + " | ".join(lines or ["none"]))


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
        check_sudo()
        head = _run(["git", "-C", manifest["worktree"], "rev-parse", "HEAD"])
        if head.returncode:
            raise Refused("cannot read the gate worktree's HEAD")
        consume = _run(["sudo", "-n", "-u", "hrse-gate", str(BROKER), "consume",
                        args.issue, head.stdout.strip(), broker_action])
        if consume.returncode:
            raise Refused(f"no grant was spent: {consume.stderr.strip() or consume.stdout.strip()}")
    except Refused as exc:
        print(f"hrse-gate-run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"hrse-gate-run: {consume.stdout.strip()}", file=sys.stderr)
    os.chdir(manifest["worktree"])
    os.execv(manifest["python"], [manifest["python"], str(script), "--issue", args.issue, *script_args])
    return EXIT_REFUSED  # unreachable


if __name__ == "__main__":
    sys.exit(main())
