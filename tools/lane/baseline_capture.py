#!/usr/bin/env python3
"""Capture the effective launch tuple of every lane x agent x argument-shape
cell, without starting a single agent session (harmonic-forge#322 AC8).

Why this exists
---------------
AC8 requires proving that "existing Claude and Codex invocations behave
identically" across the registry refactor. That is only falsifiable against an
artifact recorded BEFORE the refactor -- it cannot be reconstructed afterwards
from the refactored code, because the refactored code is the thing under
suspicion. So the pre-refactor tuples are captured here and committed as
`baseline_launch_tuples.json`, and the post-refactor run must reproduce every
Claude and Codex cell byte-for-byte.

What a "launch tuple" is (harmonic-forge#322 NC6)
-------------------------------------------------
Not just the flag list. A `GH_CONFIG_DIR` or cwd regression is exactly the
silent-and-total failure this issue exists to prevent, and would be invisible
in a flags-only diff. Each cell records:

  * `argv`     -- the full `systemd-inhibit ...` command line, which carries
                  the `--why` string (AC7) and the `env -u ...` delta prefix
                  the Gemini branch injects, as well as the agent's own flags
  * `cwd`      -- the directory the launcher `cd`-ed into before exec
  * `env`      -- LANE, LANE_AGENT, GH_CONFIG_DIR as the child actually sees them

How it captures without launching
---------------------------------
The launchers end in `exec systemd-inhibit ... "${cli_args[@]}"`. A stub
`systemd-inhibit` placed first on PATH records its own argv/cwd/env as JSON and
exits 0, so the real agent CLI is never reached. Stub `claude`/`codex`/`gemini`
binaries answer `--version` for the registry's version floor (AC9) and nothing
else.

The tree it runs against is a disposable fixture, never a real lane worktree --
the launchers `cd` into `<project>-lane2`/`-lane3` by design, so a naive test
would start a session in the operator's actual Lane 2 tree. See the handoff's
Pre-Flight Preconditions.

The fixture's `origin` is a local bare repo reached through a `url.<local>.
insteadOf` rewrite of a `github.com/vitalharmony/...` URL, so that
`_gh_config_dir.sh` sees a vitalharmony remote (exercising GH_CONFIG_DIR) while
`git ls-remote origin` still resolves offline and deterministically.

Usage
-----
    python3 tools/lane/baseline_capture.py --lane-dir tools/lane
    python3 tools/lane/baseline_capture.py --lane-dir tools/lane --out fixture.json
    python3 tools/lane/baseline_capture.py --lane-dir <ref-checkout>/tools/lane \\
        --compare tools/lane/baseline_launch_tuples.json

`--compare` is the AC8 assertion: it diffs the captured tuples against a
committed fixture and exits non-zero on any Claude or Codex difference. Gemini
cells are captured and reported but are not part of the AC8 identity claim --
the Gemini path is deliberately moved by this issue.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LANES = ("1", "2", "3")
AGENTS = ("claude", "codex", "gemini")

# The four argument shapes harmonic-forge#363 exercised. Named so a diff points
# at a shape rather than at an index.
ARG_SHAPES: dict[str, list[str]] = {
    "none": [],
    "quoted-with-spaces": ["-p", "two words here"],
    "glob": ["*.md"],
    "explicit-permission-mode": ["--permission-mode", "plan"],
}

# Only these two agents are covered by AC8's byte-identity claim.
AC8_AGENTS = ("claude", "codex")

CAPTURED_ENV_KEYS = ("LANE", "LANE_AGENT", "GH_CONFIG_DIR")

_STUB_INHIBIT = """#!/usr/bin/env python3
import json, os, sys
payload = {
    "argv": ["systemd-inhibit"] + sys.argv[1:],
    "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in %(keys)r},
}
with open(os.environ["LANE_CAPTURE_OUT"], "w") as fh:
    json.dump(payload, fh)
"""

_STUB_AGENT = """#!/usr/bin/env bash
# Stub %(name)s -- answers --version for the registry's floor check and
# nothing else. Never reached during capture: systemd-inhibit is stubbed too.
if [ "${1:-}" = "--version" ]; then
  echo "%(version)s"
  exit 0
fi
exit 0
"""

# Versions the stubs report. Above every floor the registry declares, so a
# capture run is never rejected by AC9's check -- the floor itself is tested
# separately, with deliberately-low stubs.
STUB_VERSIONS = {
    "claude": "2.1.250 (Claude Code)",
    "codex": "codex-cli 0.150.1",
    "gemini": "0.56.0",
}


def _run(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def build_fixture_tree(
    root: Path,
    project: str = "proj",
    *,
    versions: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Create a disposable <project>/<project>-lane2/<project>-lane3 tree.

    Returns (main_checkout, stub_bin). The tree's `origin` resolves offline.
    `versions` overrides what the stub agent CLIs report for `--version`, which
    is how AC9's floor is exercised from below without downgrading a real CLI.
    """
    versions = {**STUB_VERSIONS, **(versions or {})}
    # The bare repo's own path is shaped to match _gh_config_dir.sh's
    # `*github.com*vitalharmony*` glob, so GH_CONFIG_DIR resolution is
    # exercised by a remote URL that is nonetheless a local path and never
    # touches the network. Simpler and more honest than a url.insteadOf
    # rewrite -- `git remote get-url` applies insteadOf, so a rewrite would
    # have hidden the github-shaped URL from the very check under test.
    origin = root / "github.com" / "vitalharmony" / "lane-fixture.git"
    origin.parent.mkdir(parents=True)
    _run(["git", "init", "--bare", "-b", "main", str(origin)])

    main = root / project
    _run(["git", "init", "-b", "main", str(main)])
    _run(["git", "config", "user.email", "lane@example.invalid"], cwd=main)
    _run(["git", "config", "user.name", "Lane Fixture"], cwd=main)
    (main / "README.md").write_text("fixture\n")
    _run(["git", "add", "README.md"], cwd=main)
    _run(["git", "commit", "-q", "-m", "fixture"], cwd=main)

    _run(["git", "remote", "add", "origin", str(origin)], cwd=main)
    _run(["git", "push", "-q", "origin", "main"], cwd=main)
    _run(["git", "fetch", "-q", "origin", "main"], cwd=main)

    for lane in ("2", "3"):
        _run(
            ["git", "worktree", "add", "-q", "--detach",
             str(root / f"{project}-lane{lane}"), "origin/main"],
            cwd=main,
        )

    stub_bin = root / "stubbin"
    stub_bin.mkdir()
    inhibit = stub_bin / "systemd-inhibit"
    inhibit.write_text(_STUB_INHIBIT % {"keys": list(CAPTURED_ENV_KEYS)})
    inhibit.chmod(0o755)
    for name, version in versions.items():
        for binary in (name, f"{name}-api", f"{name}-pro"):
            stub = stub_bin / binary
            stub.write_text(_STUB_AGENT % {"name": binary, "version": version})
            stub.chmod(0o755)

    return main, stub_bin


def capture_cell(
    lane_dir: Path,
    main: Path,
    stub_bin: Path,
    lane: str,
    launch_args: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
) -> dict:
    """Run one launcher and return its recorded launch tuple (or an error)."""
    with tempfile.TemporaryDirectory() as out_dir:
        out = Path(out_dir) / "capture.json"
        env = dict(os.environ)
        # Strip anything that would leak the operator's real session into the
        # capture, then rebuild a minimal deterministic environment.
        for key in ("LANE", "LANE_AGENT", "LANE_CLI", "LANE_PERMISSION_MODE",
                    "GH_CONFIG_DIR", "GOOGLE_CLOUD_PROJECT"):
            env.pop(key, None)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
        env["LANE_CAPTURE_OUT"] = str(out)
        env["HOME"] = env.get("HOME", str(main))
        env.update(env_overrides or {})

        proc = subprocess.run(
            ["bash", str(lane_dir / f"lane{lane}"), *launch_args],
            cwd=main,
            env=env,
            capture_output=True,
            text=True,
        )
        if not out.exists():
            return {
                "launched": False,
                "returncode": proc.returncode,
                "stderr": proc.stderr.strip(),
            }
        payload = json.loads(out.read_text())
        payload["launched"] = True
        payload["returncode"] = proc.returncode
        payload["stderr"] = proc.stderr.strip()
        return payload


def _normalize(cell: dict, root: Path, lane_dir: Path) -> dict:
    """Replace fixture-specific absolute paths with stable placeholders.

    Without this the fixture would record a `/tmp/xxxx` that differs on every
    run, and the AC8 diff would be pure noise.
    """
    home = os.environ.get("HOME", "")

    def sub(value):
        if isinstance(value, str):
            value = (value
                     .replace(str(lane_dir), "<LANEDIR>")
                     .replace(str(root), "<ROOT>"))
            # GH_CONFIG_DIR is $HOME-derived; leaving the operator's real home
            # in a committed fixture would make it unusable on any other machine.
            return value.replace(home, "<HOME>") if home else value
        if isinstance(value, list):
            return [sub(v) for v in value]
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        return value

    return sub(cell)


def capture_all(lane_dir: Path) -> dict:
    """Capture every lane x agent x argument-shape cell."""
    cells: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        main, stub_bin = build_fixture_tree(root)
        for lane in LANES:
            for agent in AGENTS:
                for shape, args in ARG_SHAPES.items():
                    key = f"lane{lane}/{agent}/{shape}"
                    cell = capture_cell(
                        lane_dir, main, stub_bin, lane, args,
                        env_overrides={"LANE_CLI": agent},
                    )
                    cells[key] = _normalize(cell, root, lane_dir)
    return {"schema": 1, "cells": cells}


# ---------------------------------------------------------------------------
# The two declared deltas -- everything AC8's "identically" is allowed to miss.
#
# AC8 says "existing Claude and Codex invocations behave identically." Two
# acceptance criteria of the SAME issue mandate a change to the launch tuple, so
# read literally AC8 contradicts AC2 and AC7. Lane 1's Plan-First verdict
# resolved that as "identical except for deliberately-specified, ENUMERATED
# additions" -- and required the enumeration to be committed rather than left as
# a soft reading. This is that enumeration for the tuple itself;
# `lane3_safety_additions.txt` is its companion for Lane 3 launch flags.
#
# Nothing outside these two is tolerated. The comparison below applies them to
# the BASELINE and then demands byte-identity, so a third change -- however
# well-intentioned -- fails the check until someone amends this list in its own
# issue.
#
#   1. AC2 -- LANE_AGENT is exported. It did not exist anywhere in the repo
#      before this issue (ADR-007 contract item 1 requires it alongside LANE),
#      so every baseline cell records it as null.
#   2. AC7 -- the systemd-inhibit --why string names the actual agent. Every
#      baseline cell says "Claude Code" regardless of what is being launched.
# ---------------------------------------------------------------------------
DECLARED_DELTAS = ("AC2:LANE_AGENT-exported", "AC7:agent-aware-why")

# Operator-facing display name per agent, mirroring AGENT_DISPLAY in
# _agent_registry.sh. Duplicated here deliberately and minimally: the point of
# the check is to assert the shell's output independently, so reading the value
# out of the file under test would make the assertion circular.
AGENT_DISPLAY = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini"}


def _load_lane3_additions(path: Path) -> list[str]:
    """Read the Lane 3 safety-addition closed list (required change 9)."""
    if not path.exists():
        return []
    tokens = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tokens.append(line)
    return tokens


def _apply_declared_deltas(cell: dict, agent: str) -> dict:
    """Rewrite a baseline cell to carry the two deltas AC2 and AC7 mandate."""
    updated = json.loads(json.dumps(cell))
    if not updated.get("launched"):
        return updated
    updated["env"]["LANE_AGENT"] = agent
    updated["argv"] = [
        arg.replace("Claude Code", AGENT_DISPLAY[agent])
        if arg.startswith("--why=") else arg
        for arg in updated["argv"]
    ]
    return updated


def compare(captured: dict, fixture: dict, lane3_additions: list[str]) -> list[str]:
    """Return AC8 differences.

    A Claude or Codex cell must equal its baseline exactly, once the two
    declared deltas above are applied. At Lane 3, a token from the committed
    safety-addition list may additionally appear -- today that list is empty, so
    Lane 3 must be exactly identical, which is the point.
    """
    diffs: list[str] = []
    got, want = captured["cells"], fixture["cells"]
    for key in sorted(set(got) | set(want)):
        agent = key.split("/")[1]
        if agent not in AC8_AGENTS:
            continue
        if key not in got:
            diffs.append(f"{key}: missing from capture")
            continue
        if key not in want:
            diffs.append(f"{key}: not present in baseline fixture")
            continue
        expected = _apply_declared_deltas(want[key], agent)
        actual = got[key]
        if actual == expected:
            continue
        # A Lane 3 cell may differ by tokens on the committed closed list, and
        # by nothing else.
        if key.startswith("lane3/") and lane3_additions:
            stripped = dict(actual)
            stripped["argv"] = [a for a in actual["argv"]
                                if a not in lane3_additions]
            if stripped == expected:
                continue
        diffs.append(
            f"{key}:\n    expected: {json.dumps(expected, sort_keys=True)}"
            f"\n    captured: {json.dumps(actual, sort_keys=True)}"
        )
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-dir", type=Path,
                        default=Path(__file__).resolve().parent,
                        help="directory holding lane1/lane2/lane3 to capture")
    parser.add_argument("--out", type=Path,
                        help="write the captured tuples here as JSON")
    parser.add_argument("--compare", type=Path,
                        help="diff the capture against this committed fixture "
                             "and exit non-zero on any Claude/Codex change")
    args = parser.parse_args()

    if shutil.which("git") is None:
        print("baseline_capture: git not found", file=sys.stderr)
        return 2

    captured = capture_all(args.lane_dir.resolve())

    if args.out:
        args.out.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
        print(f"[baseline] wrote {len(captured['cells'])} cells to {args.out}")

    if args.compare:
        fixture = json.loads(args.compare.read_text())
        additions = _load_lane3_additions(
            args.compare.parent / "lane3_safety_additions.txt")
        diffs = compare(captured, fixture, additions)
        if diffs:
            print("[baseline] AC8 REGRESSION -- Claude/Codex launch tuples "
                  "changed beyond the declared deltas "
                  f"({', '.join(DECLARED_DELTAS)}):", file=sys.stderr)
            for diff in diffs:
                print(f"  {diff}", file=sys.stderr)
            return 1
        print("[baseline] AC8 holds: every Claude and Codex cell matches the "
              "committed baseline, differing only by the declared deltas "
              f"({', '.join(DECLARED_DELTAS)})")
        print(f"[baseline] Lane 3 safety additions permitted: "
              f"{additions or '(none — the list is empty)'}")

    if not args.out and not args.compare:
        print(json.dumps(captured, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
