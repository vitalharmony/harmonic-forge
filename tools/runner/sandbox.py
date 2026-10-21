#!/usr/bin/env python3
"""The one boundary every run of the agent's code goes through (harmonic-forge#1098).

The te-runner's agent, and everything that executes the agent's code (`mise
run check`, the kill-check tests, the preclose refuters, the cross-family
reviewer), runs under `bwrap`. The boundary is an allowlist (sticky-wicket
ruling, issue comment 6284219941): nothing is visible unless named here.

- the system directories, read-only;
- a private, empty tmpfs over `/tmp`, `/var/tmp` and the whole of `$HOME`;
- the toolchain (mise's installs, state and config, `~/.local/bin`) read-only;
- the step's one writable directory, plus any read-only inputs the caller names;
- `MISE_STATE_DIR` on the private `/tmp` and `MISE_TRUSTED_CONFIG_PATHS` set to
  the writable directory, so mise can trust a fresh clone (harmonic-forge#1112);
- then the profile's own additions:
  - `oracle`: nothing more. It never sees the agent's clone, the runner's
    receipts, keys or config, or the operator's `~/.claude`.
  - `agent`: `~/.claude` and `~/.claude.json` read-only, with a tmpfs over
    `~/.claude/state` and `~/.claude/projects` bound to a private transcripts
    directory; the claude install; the runner's gh config; the trusted checkout
    read-only (the operator's hooks run from it).
  - `reviewer`: the codex install and credentials. `~/.codex` is
    writable: codex records its sessions there.

Probes on this host (issue comments 6283139358, 6283189924) showed writes outside
the writable directory never reach the real paths, and G3's hook events still
fire. `bwrap` is required: without it nothing is launched (fail closed).

    sandbox.py here [--ro PATH]... -- CMD...   # oracle profile, writable = the cwd

`here` exists for `kill_check.py`, which materializes its own scratch tree and
runs each test with that tree as its cwd. It is invoked by absolute path from
the trusted checkout, never from the tree under test.
"""
from __future__ import annotations

import os
import pwd
import shutil
import sys
from pathlib import Path

#: The account's real home, never `$HOME`: a caller that points `$HOME`
#: elsewhere (kill_check.py's tests do) must not move what the boundary hides.
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
#: `/var/lib/ca-certificates` is where this host's `/etc/ssl/certs` links point:
#: without it every TLS call inside the boundary fails (harmonic-forge#1107).
SYSTEM = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/var/lib/ca-certificates")
TOOLCHAIN = (HOME / ".local" / "share" / "mise", HOME / ".local" / "state" / "mise",
             HOME / ".config" / "mise", HOME / ".local" / "bin")
CLAUDE_HOME = HOME / ".claude"
CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_INSTALL = HOME / ".local" / "share" / "claude"
CLAUDE_STATE = CLAUDE_HOME / "state"
TRANSCRIPTS = CLAUDE_HOME / "projects"
RUNNER_GH = HOME / ".config" / "te-runner" / "gh"
#: The `~/harmonic-forge` link the operator's hooks name the trusted checkout by.
TRUSTED_LINK = HOME / "harmonic-forge"
REVIEWER_RO = (HOME / ".npm-global",)
REVIEWER_RW = (HOME / ".codex",)
#: Under the boundary's private `/tmp` tmpfs (harmonic-forge#1112 AC1).
MISE_STATE_DIR = "/tmp/mise-state"
PROFILES = ("oracle", "agent", "reviewer")
BWRAP = "bwrap"


class SandboxError(Exception):
    """The boundary cannot be put in place, so nothing is launched."""


def require_bwrap() -> str:
    # Tests only: a stand-in that records the argv and runs the command
    # unconfined. Honored only in a test process, never in a real run.
    stub = os.environ.get("TE_RUNNER_BWRAP")
    if stub and os.environ.get("HARMONIC_FORGE_TESTING") == "1":
        return stub
    found = shutil.which(BWRAP)
    if not found:
        raise SandboxError("bwrap is not installed; the agent's code never runs unconfined")
    return found


def _ro(path: Path) -> list[str]:
    return ["--ro-bind", str(path), str(path)] if path.exists() else []


def boundary_argv(writable: Path, transcripts: Path | None = None, *,
                  profile: str = "oracle", inputs: tuple[Path, ...] = ()) -> list[str]:
    """The `bwrap ... --` prefix. Mounts apply in order: the tmpfs over `$HOME`
    first, then each allowed path bound back on top of it."""
    if profile not in PROFILES:
        raise SandboxError(f"unknown boundary profile {profile!r}")
    bwrap = require_bwrap()
    writable = Path(writable).resolve()
    argv = [bwrap, "--die-with-parent", "--dev", "/dev", "--proc", "/proc"]
    for system in SYSTEM:
        path = Path(system)
        if path.is_symlink():  # a merged-/usr link (/bin -> usr/bin) stays a link
            argv += ["--symlink", os.readlink(path), system]
        elif path.is_dir():
            argv += ["--ro-bind", system, system]
    argv += ["--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", str(HOME)]
    for tool in TOOLCHAIN:
        argv += _ro(tool)
    if profile == "agent":
        argv += _ro(CLAUDE_HOME) + _ro(CLAUDE_JSON) + _ro(CLAUDE_INSTALL) + _ro(RUNNER_GH)
        if TRUSTED_LINK.is_symlink():
            argv += _ro(TRUSTED_LINK.resolve())
            argv += ["--symlink", os.readlink(TRUSTED_LINK), str(TRUSTED_LINK)]
        if CLAUDE_HOME.exists():
            argv += ["--tmpfs", str(CLAUDE_STATE)]
            if transcripts is not None:
                Path(transcripts).mkdir(parents=True, exist_ok=True)
                argv += ["--bind", str(Path(transcripts).resolve()), str(TRANSCRIPTS)]
            else:
                argv += ["--tmpfs", str(TRANSCRIPTS)]
    elif profile == "reviewer":
        for path in REVIEWER_RO:
            argv += _ro(path)
        for path in REVIEWER_RW:
            if path.exists():
                argv += ["--bind", str(path), str(path)]
    for item in inputs:
        item = Path(item).resolve()
        if not item.exists():
            raise SandboxError(f"read-only input {item} does not exist")
        argv += ["--ro-bind", str(item), str(item)]
    argv += ["--bind", str(writable), str(writable)]
    # mise writes a trust link for each new config under its state dir, which
    # the toolchain binds read-only: every profile trusts its writable dir and
    # keeps mise's state on the private /tmp (harmonic-forge#1112 AC1).
    argv += ["--setenv", "MISE_STATE_DIR", MISE_STATE_DIR,
             "--setenv", "MISE_TRUSTED_CONFIG_PATHS", str(writable)]
    return argv + ["--chdir", str(writable), "--"]


def wrap(argv: list[str], writable: Path, transcripts: Path | None = None, *,
         profile: str = "oracle", inputs: tuple[Path, ...] = ()) -> list[str]:
    return boundary_argv(writable, transcripts, profile=profile, inputs=inputs) + list(argv)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: sandbox.py here [--ro PATH]... -- CMD..."
    if not args or args[0] != "here" or "--" not in args:
        print(usage, file=sys.stderr)
        return 2
    split = args.index("--")
    options, command = args[1:split], args[split + 1:]
    if not command or len(options) % 2 or any(o != "--ro" for o in options[::2]):
        print(usage, file=sys.stderr)
        return 2
    try:
        full = wrap(command, Path.cwd(), profile="oracle",
                    inputs=tuple(Path(p) for p in options[1::2]))
    except (SandboxError, OSError) as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        return 2
    os.execv(full[0], full)
    return 2  # not reached


if __name__ == "__main__":
    sys.exit(main())
