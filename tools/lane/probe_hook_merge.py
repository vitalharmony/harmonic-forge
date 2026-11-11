#!/usr/bin/env python3
"""Re-measure how Claude Code merges hooks from `--settings` with project
settings (harmonic-forge#1307). Outside every gate; run it after a CLI update.

The Forge lane hook set relies on four measured properties (2.1.322, the
#1307 plan's probe):

  1. hooks from `--settings` ADD to the project's hooks;
  2. a hook runs once per event when its command STRING is identical across
     sources (whatever the matcher or timeout), and twice when it differs --
     which is why a project entry naming a set script must be byte-identical;
  3. `${CLAUDE_PROJECT_DIR}` and shell variables expand in a `--settings` hook;
  4. `disableAllHooks: false` in `--settings` outranks a project or local
     `true`, and without the declaration a project `true` silences it.

Without `--real-run` this writes the fixture and prints the command each
variant would run; nothing is launched. With it, each variant runs one real
`claude -p` (haiku, a scratch project, one Bash call) and prints how many times
each tagged stub hook fired. Read the counts against the four properties.

`--env NAME=VALUE` (repeatable) adds a variable to every launch, to measure
whether it drops a hook (the refused-environment list in `_agent_registry.sh`
is classified this way, not from `--help` text). The stub log only sees the
fixture's `--settings` and project hooks, so a real run also prints, per
event, the hook runs the CLI reported (`--include-hook-events`) beyond the
stub's: the user-level hooks of the real `~/.claude/settings.json`, which is
read and never written to.

    python3 tools/lane/probe_hook_merge.py [--dir D] [--variant V] [--env N=V] [--real-run]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VARIANTS = ("merge", "disable_project", "disable_local", "disable_nodecl")
PROMPT = "Run the shell command `true` once with the Bash tool, then reply ok."


def _h(cmd: str, **kw) -> dict:
    return {"type": "command", "command": cmd, **kw}


def write_fixture(root: Path, variant: str) -> tuple[Path, dict]:
    """(project dir, the --settings object) for one variant, under `root`."""
    proj = root / "proj"
    shutil.rmtree(proj, ignore_errors=True)
    (proj / ".claude").mkdir(parents=True)
    log = root / "log.txt"
    log.write_text("")
    stub = root / "stub.sh"
    stub.write_text("#!/bin/sh\nev=$(jq -r '.hook_event_name' 2>/dev/null)\n"
                    f'echo "$1 event=$ev" >> "{log}"\n')
    stub.chmod(0o755)
    shutil.copy(stub, proj / "pstub.sh")
    plain = f'sh "{stub}"'
    guarded = f'f="{stub}"; [ -f "$f" ] && sh "$f" BOTH_DIFF_FORM || true'
    project = {"hooks": {
        "UserPromptSubmit": [{"hooks": [
            _h(f"{plain} PROJ_ONLY"), _h(f"{plain} BOTH_IDENT", timeout=10),
            _h(f"{plain} BOTH_DIFF_TIMEOUT", timeout=10), _h(guarded, timeout=10)]}],
        "SessionStart": [{"matcher": "startup|resume|clear|fork", "hooks": [
            _h(f"{plain} SS_BOTH_SAME_MATCHER", timeout=10),
            _h(f"{plain} SS_BOTH_DIFF_MATCHER", timeout=10)]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            _h(f"{plain} PTU_BOTH_IDENT"), _h(f"{plain} PTU_PROJ_ONLY")]}],
        "Stop": [{"hooks": [_h(f"{plain} STOP_BOTH_IDENT", timeout=10)]}],
    }}
    flag = {"hooks": {
        "UserPromptSubmit": [{"hooks": [
            _h(f"{plain} FLAG_ONLY"), _h(f"{plain} BOTH_IDENT", timeout=10),
            _h(f"{plain} BOTH_DIFF_TIMEOUT", timeout=25),
            _h(f"{plain} BOTH_DIFF_FORM", timeout=10),
            _h('sh "${CLAUDE_PROJECT_DIR}/pstub.sh" FLAG_CPD_EXPANDS'),
            _h(f'X="{stub}"; sh "$X" FLAG_SHELL_VAR_EXPANDS')]}],
        "SessionStart": [
            {"matcher": "startup|resume|clear|fork", "hooks": [
                _h(f"{plain} SS_BOTH_SAME_MATCHER", timeout=10)]},
            {"matcher": "startup", "hooks": [_h(f"{plain} SS_BOTH_DIFF_MATCHER", timeout=10)]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            _h(f"{plain} PTU_BOTH_IDENT"), _h(f"{plain} PTU_FLAG_ONLY")]}],
        "Stop": [{"hooks": [_h(f"{plain} STOP_BOTH_IDENT", timeout=10)]}],
    }}
    if variant == "disable_project":
        project["disableAllHooks"] = True
        flag["disableAllHooks"] = False
    elif variant == "disable_local":
        (proj / ".claude" / "settings.local.json").write_text(json.dumps({"disableAllHooks": True}))
        flag["disableAllHooks"] = False
    elif variant == "disable_nodecl":
        project["disableAllHooks"] = True
    elif variant != "merge":
        raise ValueError(variant)
    (proj / ".claude" / "settings.json").write_text(json.dumps(project, indent=1))
    return proj, flag


def command(flag: dict) -> list[str]:
    return ["claude", "-p", PROMPT, "--model", "haiku", "--permission-mode", "acceptEdits",
            "--allowedTools", "Bash(true)", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--settings", json.dumps(flag, separators=(",", ":"))]


def other_hooks(stream: str, log: Path) -> list[str]:
    """Per event, the hook runs the CLI reported (`hook_started`) minus the
    fixture stub's runs: what is left is the user-level (or managed) hooks.
    The events carry no command, so the split is by count."""
    started = collections.Counter()
    for line in stream.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("subtype") == "hook_started":
            started[str(msg.get("hook_event"))] += 1
    stub = collections.Counter(line.rsplit("event=", 1)[-1].strip()
                               for line in log.read_text().splitlines() if "event=" in line)
    return [f"{started[ev] - stub[ev]:7d} OTHER {ev} (cli reported {started[ev]}, stub {stub[ev]})"
            for ev in sorted(set(started) | set(stub))]


def counts(log: Path) -> list[str]:
    tally = collections.Counter(line.strip() for line in log.read_text().splitlines() if line.strip())
    return [f"{n:7d} {line}" for line, n in sorted(tally.items())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, help="fixture directory (default: a new temp dir)")
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        help="run only this variant (repeatable; default: all four)")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="set this variable on every launch (repeatable)")
    parser.add_argument("--real-run", action="store_true",
                        help="launch one real `claude -p` per variant (haiku)")
    args = parser.parse_args()
    root = (args.dir or Path(tempfile.mkdtemp(prefix="probe_hook_merge-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LANE", "CLAUDE_CODE_")) and k != "CLAUDECODE"}
    for pair in args.env:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            parser.error(f"--env takes NAME=VALUE, not {pair!r}")
        env[name] = value
    if args.env:
        print("env: " + " ".join(args.env))
    for variant in args.variant or VARIANTS:
        proj, flag = write_fixture(root, variant)
        argv = command(flag)
        if not args.real_run:
            print(f"== {variant}: (cd {proj} && {' '.join(argv[:-1])} '<inline JSON>')")
            continue
        proc = subprocess.run(argv, cwd=proj, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=300)
        (root / f"{variant}.stream.jsonl").write_text(proc.stdout)
        print(f"== {variant} (inline JSON --settings) exit={proc.returncode}")
        print("\n".join(counts(root / "log.txt")) or "      (no lines: no stub hook ran)")
        print("\n".join(other_hooks(proc.stdout, root / "log.txt")) or "      (the CLI reported no hook run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
