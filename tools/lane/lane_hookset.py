#!/usr/bin/env python3
"""The Forge lane hook set: build, check and freeze it (harmonic-forge#1307).

`tools/lane/lane_hooks.json` is the core set of lane hooks. The lane set is
that core plus the overlay the per-user `instance.toml` declares
(`lane_hook_overlay`, harmonic-forge#1525): `"operator"` appends
`tools/lane/lane_hooks.operator.json` (the belt and memory hooks), `"none"`
adds nothing. There is no default: an undeclared or unknown overlay refuses,
so no path builds a core-only set silently. Every Claude `lane1`/`lane2`/`lane3`
launch passes the set to the session as ONE inline `--settings` JSON value,
built here by `build` after the launcher's rules pull, so a lane session runs
the same guards in any project and nothing in that project can remove, narrow
or disable them:

  * a caller's own `--settings` (and `--bare`, `--safe-mode`,
    `--setting-sources`, `--restricted`) is refused by the launcher;
  * the value declares `disableAllHooks: false`, which outranks a project or
    local `true` (measured on 2.1.322, the plan's probe runs 2-4);
  * the value is inline, so a session that can edit the forge root cannot
    change the settings its own launch was given.

`build` fails closed. Claude Code drops a malformed hooks entry with a warning
and starts the session anyway (probe run 5), so every shape the CLI would drop
is refused here instead, and the launcher refuses to start a session with no
guards.

Hook identity, measured on 2.1.322: a hook runs once per event when its
command STRING is identical across settings sources, whatever the matcher or
timeout, and twice when the strings differ. So a project entry naming a set
script must be byte-identical to the set's entry; `doubled` reports any not.

Subcommands:
  build   --set F --forge-root R --forge-root-source S --forge-root-as-written W [--policy P --manifest M]
  doubled [--set F --forge-root R] [--settings S ...]   doubled/removable entries
  root    --launcher-checkout DIR       print set, root, source, as written: each NUL-ended (#1526)
  freeze  --repo R --commit C [--project-specific SNAPSHOT]   AC4 snapshot
  running [--project DIR]                       live lane processes without the set

Run as `python3 -I -E`, which drops the cwd and the `PYTHON*` variables. `HOME`
and an absolute `XDG_CONFIG_HOME` still choose which `instance.toml` declares
the overlay (and the root `root` prints, with `HARMONIC_FORGE_ROOT`): the
launcher refuses an `XDG_CONFIG_HOME` other than `$HOME/.config` before calling
`build` (harmonic-forge#1525), and any other caller owns that environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# -I drops the script's directory from sys.path; the shared helper is named
# explicitly, by this file's own resolved location.
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "settings"))
from hookset_root import (PLACEHOLDER, TEMPLATES, SetError,  # noqa: E402,F401
                          render, render_checked, spelling, stored_form, stored_paths)
import instance  # noqa: E402

SET_KEYS = frozenset({"schema", "disableAllHooks", "lane_only", "hooks"})
BLOCK_KEYS = frozenset({"matcher", "hooks"})
# `onFailure` only ever as "block" (harmonic-forge#1608): a failed or timed-out
# refusal hook then refuses the call; "continue" would let a set relax a guard.
HANDLER_KEYS = frozenset({"type", "command", "timeout", "onFailure"})
# The Claude Code hook events (2.1.322). An unknown name is refused rather than
# passed: the CLI ignores an event it does not know, silently.
KNOWN_EVENTS = frozenset({
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest",
    "UserPromptSubmit", "Stop", "SubagentStart", "SubagentStop", "SessionStart",
    "SessionEnd", "Notification", "PreCompact",
})
SCRIPT_RE = re.compile(r"tools/hooks/([A-Za-z0-9_]+\.py)")
# The declared overlay maps through this fixed table to a file beside the set,
# in the same checkout: the declaration never carries a path (harmonic-forge#1525).
OVERLAY_FILES = {"operator": "lane_hooks.operator.json"}
OVERLAY_CHOICES = instance.OVERLAY_CHOICES
OVERLAY_KEYS = frozenset({"schema", "lane_only", "hooks"})
# The frozen pre-split set (f61ac37). The operator overlay must carry every
# handler of it whose script the core set does not wire, exactly as recorded:
# an overlay that drops a deny handler refuses rather than builds
# (harmonic-forge#1525). Read from this file's own checkout, never beside a
# caller-chosen set.
OPERATOR_FIXTURE = _HERE / "testdata" / "lane_hooks_f61ac37_tuples.json"


def load_json_object(path: Path, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetError(f"{what} is missing: {path}") from None
    except (OSError, ValueError) as exc:
        raise SetError(f"{what} is not valid JSON: {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SetError(f"{what} is not a JSON object: {path}")
    return data


def handlers(hooks: dict):
    """Yield (event, matcher, handler) for every handler, in file order."""
    for event, blocks in hooks.items():
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            for handler in block.get("hooks") or ():
                if isinstance(handler, dict):
                    yield event, block.get("matcher"), handler


def validate(data: dict, *, forge_root: Path) -> None:
    """Raise SetError for any set the launcher must not pass. Each command is
    in the stored form, and each script it names exists under `forge_root`
    (harmonic-forge#1526)."""
    extra = sorted(set(data) - SET_KEYS)
    if extra:
        raise SetError(f"unknown top-level key(s): {', '.join(extra)}")
    if data.get("schema") != 1:
        raise SetError("`schema` must be 1")
    if "disableAllHooks" not in data or data["disableAllHooks"] is not False:
        raise SetError("`disableAllHooks` must be declared, and literally false: "
                       "without it a project or local `true` silences the set")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        raise SetError("`hooks` is empty or not an object")
    count = 0
    for event, blocks in hooks.items():
        if event not in KNOWN_EVENTS:
            raise SetError(f"unknown hook event: {event!r}")
        if not isinstance(blocks, list) or not blocks:
            raise SetError(f"hooks.{event} must be a non-empty list of blocks")
        for block in blocks:
            if not isinstance(block, dict) or set(block) - BLOCK_KEYS or "hooks" not in block:
                raise SetError(f"hooks.{event}: a block must be {{matcher?, hooks}}: {block!r}")
            matcher = block.get("matcher")
            if "matcher" in block:
                if not isinstance(matcher, str):
                    raise SetError(f"hooks.{event}: matcher is not a string: {matcher!r}")
                try:
                    re.compile(matcher)
                except re.error as exc:
                    raise SetError(f"hooks.{event}: matcher {matcher!r} does not compile: {exc}") from None
            entries = block["hooks"]
            if not isinstance(entries, list) or not entries:
                raise SetError(f"hooks.{event}: a block holds no handler")
            for handler in entries:
                if not isinstance(handler, dict) or set(handler) - HANDLER_KEYS:
                    raise SetError(f"hooks.{event}: a handler must be {{type, command, timeout?, onFailure?}}: {handler!r}")
                if handler.get("type") != "command":
                    raise SetError(f"hooks.{event}: handler type is not \"command\": {handler!r}")
                command = handler.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise SetError(f"hooks.{event}: handler has no command: {handler!r}")
                timeout = handler.get("timeout")
                if "timeout" in handler and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                             or timeout <= 0):
                    raise SetError(f"hooks.{event}: timeout is not a positive integer: {handler!r}")
                if "onFailure" in handler and handler["onFailure"] != "block":
                    raise SetError(f"hooks.{event}: onFailure must be \"block\": {handler!r}")
                # Change 4: an unchecked command fails open at runtime (`stored_paths`).
                try:
                    stored_paths(command)
                except SetError as exc:
                    raise SetError(f"hooks.{event}: {exc}") from None
                count += 1
    if not count:
        raise SetError("`hooks` holds no handler")
    lane_only = data.get("lane_only", [])
    if not isinstance(lane_only, list) or not all(isinstance(n, str) for n in lane_only):
        raise SetError("`lane_only` must be a list of script names")
    named = {s for _e, _m, h in handlers(hooks) for s in SCRIPT_RE.findall(h["command"])}
    stray = sorted(set(lane_only) - named)
    if stray:
        raise SetError(f"`lane_only` names no set entry: {', '.join(stray)}")
    missing = sorted({str(Path(forge_root) / rel) for _e, _m, h in handlers(hooks)
                      for rel in stored_paths(h["command"])
                      if not (Path(forge_root) / rel).is_file()})
    if missing:
        raise SetError(f"named script(s) do not exist under forge root {forge_root}: {', '.join(missing)}")


def load_set(path: Path, *, forge_root: Path, stored: bool = False) -> dict:
    """The core set, validated in its stored form; rendered for `forge_root`
    unless `stored` (harmonic-forge#1526)."""
    data = load_json_object(path, "lane hook set")
    validate(data, forge_root=forge_root)
    return data if stored else render_checked(data, forge_root)


def _scripts(data: dict) -> set[str]:
    return {s for _e, _m, h in handlers(data.get("hooks") or {})
            for s in SCRIPT_RE.findall(h.get("command") or "")}


def load_overlay(path: Path, core: dict, *, forge_root: Path) -> dict:
    """The operator overlay at `path`, validated as a set of its own and
    disjoint from `core`: it may only add hooks, never carry
    `disableAllHooks` or name a script the core set wires. It must also be
    complete: every operator handler `_operator_handlers(core)` derives from
    the frozen record, byte for byte, or it refuses naming each one missing."""
    data = load_json_object(path, "operator hook overlay")
    if "disableAllHooks" in data:
        raise SetError(f"operator hook overlay carries `disableAllHooks`, which only "
                       f"the core set may declare: {path}")
    extra = sorted(set(data) - OVERLAY_KEYS)
    if extra:
        raise SetError(f"operator hook overlay has unknown top-level key(s) "
                       f"{', '.join(extra)}: {path}")
    try:
        validate({**data, "disableAllHooks": False}, forge_root=forge_root)
    except SetError as exc:
        raise SetError(f"operator hook overlay {path}: {exc}") from None
    shared = sorted(_scripts(data) & _scripts(core))
    if shared:
        raise SetError(f"overlay names core script(s) {', '.join(shared)}: "
                       f"an overlay only adds hooks: {path}")
    missing = sorted((_operator_handlers(core) - Counter(_handler_keys(data))).elements(),
                     key=lambda k: (k[0], k[1] or "", k[2], k[3] or 0))
    if missing:
        listed = "; ".join(f"{e} {m or '(no matcher)'} {'/'.join(SCRIPT_RE.findall(c)) or c} "
                           f"timeout={t}" for e, m, c, t in missing)
        raise SetError(f"operator hook overlay is missing {len(missing)} operator handler(s) "
                       f"recorded in {OPERATOR_FIXTURE.name} (every recorded handler whose "
                       f"script the core set does not wire; a core script retired since "
                       f"then needs that record refreshed): {listed}: {path}")
    return data


def _handler_keys(data: dict) -> list[tuple]:
    """Every handler's (event, matcher, command, timeout), the identity
    `doubled` and `snapshot_gaps` use."""
    return [(e, m, h.get("command"), h.get("timeout"))
            for e, m, h in handlers(data.get("hooks") or {})]


def _operator_handlers(core: dict) -> Counter:
    """The handlers the operator overlay must carry: every handler of the
    frozen f61ac37 set (`OPERATOR_FIXTURE`) whose script `core` does not wire,
    in the stored form (#1526). Derived, not listed: no hand list can drift."""
    frozen = load_json_object(OPERATOR_FIXTURE, "frozen operator hook record")
    rows = frozen.get("handlers")
    if (not isinstance(rows, list) or not rows
            or not all(isinstance(r, list) and len(r) == 4 for r in rows)):
        raise SetError(f"frozen operator hook record has no handler list: {OPERATOR_FIXTURE}")
    wired = _scripts(core)
    required = Counter((r[0], r[1], stored_form(r[2]), r[3]) for r in rows
                       if isinstance(r[2], str) and SCRIPT_RE.findall(r[2])
                       and not set(SCRIPT_RE.findall(r[2])) & wired)
    if not required:
        raise SetError(f"frozen operator hook record names no operator handler: {OPERATOR_FIXTURE}")
    return required


def merge(core: dict, overlay: dict, *, forge_root: Path) -> dict:
    """`core` with `overlay` appended: per event, every core block and then
    every overlay block (never into an existing block); `lane_only` is the
    union. The result is validated again."""
    merged = json.loads(json.dumps(core))
    hooks = merged.setdefault("hooks", {})
    for event, blocks in (overlay.get("hooks") or {}).items():
        hooks.setdefault(event, []).extend(json.loads(json.dumps(blocks)))
    merged["lane_only"] = list(core.get("lane_only") or []) + list(overlay.get("lane_only") or [])
    validate(merged, forge_root=forge_root)
    return merged


def load_effective(set_path: Path, overlay: str, *, forge_root: Path) -> dict:
    """The lane set for a declared overlay, rendered for `forge_root`: the core
    set alone for `"none"`, plus `OVERLAY_FILES[overlay]` beside it otherwise."""
    if overlay not in OVERLAY_CHOICES:
        raise SetError(f"unknown lane hook overlay {overlay!r}: must be one of "
                       f"{', '.join(OVERLAY_CHOICES)}")
    data = load_set(set_path, forge_root=forge_root, stored=True)
    if overlay != "none":
        path = Path(set_path).parent / OVERLAY_FILES[overlay]
        data = merge(data, load_overlay(path, data, forge_root=forge_root), forge_root=forge_root)
    return render_checked(data, forge_root)


def _overlay_from_config() -> tuple[str, Path]:
    """The overlay `instance.toml` declares, and its path. Every declaration
    error is a SetError, so the build refuses (harmonic-forge#1525)."""
    try:
        return instance.lane_hook_overlay()
    except ValueError as exc:
        raise SetError(str(exc)) from None


def _named(forge_root: Path, source: str, written: str | None) -> str:
    """The prefix every build SetError carries: `instance.named_root`, the
    shared formatter (sticky-wicket C1; post-verdict survivor 3)."""
    return instance.named_root(str(forge_root) if written is None else written, source, forge_root)


def build(set_path: Path, forge_root: Path, source: str, policy: Path | None = None,
          manifest: Path | None = None, *, overlay: str, written: str | None = None) -> dict:
    """The one `--settings` object: the set, plus the Lane 3 policy when given.

    `forge_root` (from `source`, as `written`) holds every script; one with no
    `tools/hooks/` refuses. Every SetError is named by `_named` (harmonic-forge#1526). `overlay` is the declared overlay
    (`load_effective`). The policy is read ONCE: its digest is checked against
    the manifest's `policy_sha256` and those same bytes are merged, so nothing
    can swap it between the launcher's ownership check and here. Neither
    source may carry the other's keys, so neither can override the other.
    """
    named = _named(forge_root, source, written)  # C1
    if not (Path(forge_root) / "tools" / "hooks").is_dir():
        raise SetError(f"{named} has no tools/hooks/")
    try:
        return _build(set_path, forge_root, policy, manifest, overlay=overlay)
    except SetError as exc:
        raise SetError(f"{named}: {exc}") from None


def _build(set_path: Path, forge_root: Path, policy: Path | None, manifest: Path | None,
           *, overlay: str) -> dict:
    """`build`'s body, unnamed: the set, then the policy."""
    data = load_effective(set_path, overlay, forge_root=forge_root)
    value = {"disableAllHooks": False, "hooks": data["hooks"]}
    if policy is None:
        return value
    if manifest is None:
        raise SetError("a policy needs its manifest")
    try:
        raw = Path(policy).read_bytes()
    except OSError as exc:
        raise SetError(f"policy cannot be read: {policy}: {exc}") from None
    expected = load_json_object(manifest, "policy manifest").get("policy_sha256")
    if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
        raise SetError(f"policy does not match its manifest's policy_sha256: {policy}")
    try:
        merged = json.loads(raw)
    except ValueError as exc:
        raise SetError(f"policy is not valid JSON: {policy}: {exc}") from None
    if not isinstance(merged, dict):
        raise SetError(f"policy is not a JSON object: {policy}")
    clash = sorted(set(merged) & {"hooks", "disableAllHooks"})
    if clash:
        raise SetError(f"policy carries {', '.join(clash)}, which only the set may declare")
    value.update(merged)
    return value


def doubled(set_data: dict, settings: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(doubled, removable) entries across `{label: settings object}`.

    Doubled: an entry naming a set script on an event the set wires it on, whose
    command string is not the set's (it runs twice), or is the set's but with a
    different timeout. Removable: byte-identical to a `lane_only` set entry.
    """
    set_hooks = set_data.get("hooks") or {}
    lane_only = set(set_data.get("lane_only") or ())
    commands: dict[str, dict[str, object]] = {}
    scripts: dict[str, set[str]] = {}
    for event, _m, handler in handlers(set_hooks):
        commands.setdefault(event, {})[handler["command"]] = handler.get("timeout")
        scripts.setdefault(event, set()).update(SCRIPT_RE.findall(handler["command"]))
    doubles: list[str] = []
    removable: list[str] = []
    for label, data in settings.items():
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        for event, matcher, handler in handlers(hooks):
            command = handler.get("command")
            if not isinstance(command, str):
                continue
            named = set(SCRIPT_RE.findall(command))
            where = f"{label}: {event} {matcher or '(no matcher)'}"
            if command in commands.get(event, {}):
                if handler.get("timeout") != commands[event][command]:
                    doubles.append(f"{where}: {command} -- timeout {handler.get('timeout')} "
                                   f"differs from the set's {commands[event][command]}")
                elif named & lane_only:
                    removable.append(f"{where}: {command}")
                continue
            hit = sorted(named & scripts.get(event, set()))
            if hit:
                doubles.append(f"{where}: {', '.join(hit)} runs twice -- this command "
                               f"string differs from the set's: {command}")
    return doubles, removable


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _repo_name(repo: Path) -> str:
    try:
        url = _git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return repo.name
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else repo.name


def freeze(repo: Path, commit: str, project_specific: list | None = None) -> dict:
    """The AC4 snapshot of `<commit>:.claude/settings.json`.

    `project_specific` (each `{event, matcher, command, reason}`) is carried
    from an existing snapshot: the reasons are the AC6 classification, not
    something a commit can be read for.
    """
    full = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    path = ".claude/settings.json"
    blob = _git(repo, "rev-parse", f"{full}:{path}").strip()
    data = json.loads(_git(repo, "show", f"{full}:{path}"))
    entries = [{"event": e, "matcher": m, "command": h.get("command"),
                "timeout": h.get("timeout")}
               for e, m, h in handlers(data.get("hooks") or {})]
    return {"schema": 1,
            "source": {"repo": _repo_name(repo), "commit": full, "path": path, "blob": blob},
            "entries": entries,
            "project_specific": list(project_specific or [])}


def snapshot_gaps(set_data: dict, snapshot: dict) -> list[str]:
    """AC4: every snapshot entry the set does not carry and no
    `project_specific` item names, plus every stale `project_specific` item.

    An entry is carried only by a set entry with the same event, the same
    command string, the same matcher STRING (exact equality is the narrowing
    check: any matcher edit fails until the snapshot is re-frozen on purpose)
    and a timeout not lower than the snapshot's (a shortened timeout kills a
    deny hook into an allow). An absent timeout is the CLI default and is
    matched only by another absent one.
    """
    carried = [(e, m, h.get("command"), h.get("timeout"))
               for e, m, h in handlers(set_data.get("hooks") or {})]
    exempt = [(p.get("event"), p.get("matcher"), p.get("command"))
              for p in snapshot.get("project_specific") or ()]
    entries = [(s.get("event"), s.get("matcher"), s.get("command"), s.get("timeout"))
               for s in snapshot.get("entries") or ()]
    gaps: list[str] = []
    for event, matcher, command, timeout in entries:
        if (event, matcher, command) in exempt:
            continue
        ok = any(e == event and m == matcher and c == command
                 and (t == timeout if timeout is None or t is None else t >= timeout)
                 for e, m, c, t in carried)
        if not ok:
            gaps.append(f"{event} {matcher!r} {command} (timeout {timeout}): "
                        "dropped or narrowed, and not named project-specific")
    for event, matcher, command in exempt:
        if not any((e, m, c) == (event, matcher, command) for e, m, c, _t in entries):
            gaps.append(f"project_specific names no snapshot entry: {event} {matcher!r} {command}")
    return gaps


def carries_set(argv: list[str]) -> bool:
    """Change 2: the VALUE is tested, not the flag. Lane 3's pre-#1307 launch
    passes `--settings <policy path>`, which is not the set."""
    for i, arg in enumerate(argv):
        if arg == "--settings" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--settings="):
            value = arg[len("--settings="):]
        else:
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("hooks"), dict) and parsed["hooks"]:
            return True
    return False


def running(proc_root: Path = Path("/proc"), project: Path | None = None) -> list[str]:
    """Live `claude` processes with LANE set whose argv carries no set."""
    found: list[str] = []
    for entry in sorted(proc_root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv if a]
        env = dict(item.decode(errors="replace").split("=", 1)
                   for item in environ if b"=" in item)
        lane = env.get("LANE", "")
        if lane not in ("1", "2", "3") or not argv:
            continue
        if not any(Path(a).name.startswith("claude") for a in argv[:2]):
            continue
        if carries_set(argv):
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = "?"
        if project is not None:
            root = str(project.resolve())
            if cwd != root and not cwd.startswith(root + os.sep):
                continue
        found.append(f"{entry.name} LANE={lane} cwd={cwd}")
    return found


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("--set", type=Path, required=True)
    p.add_argument("--forge-root", type=Path, required=True)
    p.add_argument("--forge-root-source", required=True)
    p.add_argument("--forge-root-as-written")
    p.add_argument("--policy", type=Path)
    p.add_argument("--manifest", type=Path)
    p = sub.add_parser("doubled")
    p.add_argument("--set", type=Path)  # default: the set and root a launch resolves (#1526)
    p.add_argument("--forge-root", type=Path)
    p.add_argument("--settings", type=Path, action="append", default=[])
    p.add_argument("--user-settings", type=Path,
                   default=Path.home() / ".claude" / "settings.json",
                   help="also read (change 6); pass an empty string to skip")
    p = sub.add_parser("freeze")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--project-specific", type=Path,
                   help="carry `project_specific` from this snapshot")
    p = sub.add_parser("running")
    p.add_argument("--project", type=Path)
    sub.add_parser("root").add_argument("--launcher-checkout", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "root":
            # NUL-ended, so a tab or newline in any field (the instance.toml
            # path in the source) survives the launcher's read (post-verdict 2).
            sys.stdout.write("".join(f"{field}\0" for field in instance.launch_view(args.launcher_checkout)))
            return 0
        if args.cmd == "build":
            try:  # named like every build refusal (post-verdict survivor 3)
                if (args.policy is None) != (args.manifest is None):
                    raise SetError("--policy and --manifest go together")
                overlay, config = _overlay_from_config()
            except SetError as exc:
                raise SetError(f"{_named(args.forge_root, args.forge_root_source, args.forge_root_as_written)}: "
                               f"{exc}") from None
            value = build(args.set, args.forge_root, args.forge_root_source, args.policy,
                          args.manifest, overlay=overlay, written=args.forge_root_as_written)
            if overlay == "none":
                print(f'lane_hookset: hook overlay "none" (from {config}): core set only '
                      f'-- the operator hooks (belt, memory) are NOT wired', file=sys.stderr)
            else:
                core_n = sum(1 for _ in handlers(load_set(args.set, forge_root=args.forge_root)["hooks"]))
                total = sum(1 for _ in handlers(value["hooks"]))
                print(f'lane_hookset: hook overlay "{overlay}" (from {config}): '
                      f'{core_n} core + {total - core_n} operator handler(s)', file=sys.stderr)
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if args.cmd == "doubled":
            view = instance.launch_view(_HERE.parent.parent)  # the launch's set and root (B1)
            set_data = load_effective(args.set or view[0], _overlay_from_config()[0], forge_root=args.forge_root or view[1])
            files = list(args.settings)
            if str(args.user_settings) not in ("", "."):
                files.append(args.user_settings)
            settings = {str(f): load_json_object(f, "settings file") for f in files if f.is_file()}
            doubles, removable = doubled(set_data, settings)
            for line in doubles:
                print(f"DOUBLED {line}")
            for line in removable:
                print(f"REMOVABLE {line}")
            return 1 if doubles else 0
        if args.cmd == "freeze":
            carried = None
            if args.project_specific is not None:
                carried = load_json_object(args.project_specific, "snapshot").get("project_specific")
            print(json.dumps(freeze(args.repo, args.commit, carried), indent=2))
            return 0
        for line in running(project=args.project):
            print(line)
        return 0
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"lane_hookset: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
