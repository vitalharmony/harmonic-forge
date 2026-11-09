#!/usr/bin/env python3
"""The never-refuse sample: ordinary Bash commands that ran, derived locally
from Claude Code transcripts (harmonic-forge#1289).

  derive  read every main and subagent transcript under `--root`, keep the
          Bash calls that ran (a positive check: every other result shape is
          excluded and counted), de-duplicate exact strings, drop any string
          with a secret-shaped hit after blanking the record's own paths,
          keep only ordinary reads, probes and scratch writes, and write a
          seeded `--size` sample plus its manifest, manifest first.

The sample holds command text, so it lives in operator-memory and never in
harmonic-forge: an `--out` inside a forge checkout is refused. Nothing here
prints a command or a result text; only counts and coarse bucket labels.

Exit codes: 0 written; 2 refused, nothing written (an `--out` inside forge, a
wrong name, an existing sample, an orphan sample or manifest, a leftover temp
file); 3 nothing written for any other reason (a transcript shape this code
does not recognize, a pool smaller than `--size`, a secret hit on the pre-write
re-scan, an I/O error). Never 1.

Stdlib only, no network and no subprocess.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import fnmatch
import glob
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "memory"))
sys.path.insert(0, str(TOOLS / "hooks"))

import capture_failure  # noqa: E402
import secret_scan  # noqa: E402
from shell_parse import (  # noqa: E402
    _ASSIGNMENT,
    GroupMarker,
    NotOrdinary,
    simple_commands,
    strip_invocation_prefix,
    substitution_bodies,
)

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_OUT = capture_failure.MEMORY_STORE / "never-refuse" / "never-refuse-sample-v1.jsonl"
DEFAULT_SIZE = 10000
DEFAULT_SEED = 1279
NAME_PATTERN = "never-refuse-sample*.jsonl"
TEMP_PREFIX = "never-refuse-sample"
TEMP_PATTERN = "never-refuse-sample*.tmp"
MANIFEST_SUFFIX = ".manifest.json"

EXIT_REFUSED = 2
EXIT_ERROR = 3


class Refused(Exception):
    """Exit 2, nothing written."""


class Failed(Exception):
    """Exit 3, nothing written."""


# --------------------------------------------------------------------------
# The `gh api` rule: one positive allowlist, loaded by path from the
# cross-family brief builder so the two can never drift.

_BRIEF = TOOLS / "lane" / "build_cross_family_brief.py"


def _load_gh_api_rule() -> Callable[[list[str]], bool]:
    spec = importlib.util.spec_from_file_location("_precheck_cross_family_brief", _BRIEF)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._gh_api_allowlisted


gh_api_allowlisted = _load_gh_api_rule()


# --------------------------------------------------------------------------
# The ordinary classifier.

class Opts(NamedTuple):
    """The options a program may carry and still be ordinary. Fail-closed
    (harmonic-forge#1289 sticky-wicket REFORGE): any option not listed here,
    any `--name=value` whose name is not in `long_values`, and any short
    letter not in `flags`/`values` makes the command not ordinary. Nothing
    is enumerated as a write form; everything unlisted is excluded."""
    flags: str = ""                       # short letters taking no value
    values: str = ""                      # short letters taking a value
    long: frozenset = frozenset()         # long flags taking no value
    long_values: frozenset = frozenset()  # long flags taking a value
    numeric: bool = False                 # `-20` style counts (head, tail, git log)


def _o(flags: str = "", values: str = "", long: tuple = (), long_values: tuple = (),
       numeric: bool = False) -> Opts:
    return Opts(flags, values, frozenset(long), frozenset(long_values), numeric)


#: Programs whose every form allowed here only reads. `awk` is absent: its
#: script is a program, and no narrow shape of it was worth the risk.
READ: dict[str, Opts] = {
    "ls": _o("lahtrR1dAFSiGgonsU", "", ("--all", "--human-readable", "--color")),
    "cat": _o("nAvEeTsb"),
    "head": _o("qv", "nc", numeric=True),
    "tail": _o("qvfF", "nc", numeric=True),
    "grep": _o("inrRvlLcEFwoHhqsxzPZaI", "ABCemf", ("--color", "--line-number", "--recursive",
               "--ignore-case", "--count", "--files-with-matches", "--only-matching"),
               ("--include", "--exclude", "--exclude-dir")),
    "rg": _o("inlcwFvuSsLHNz.", "gtTABCmeM", ("--files", "--hidden", "--no-ignore", "--json",
             "--count", "--fixed-strings", "--line-number", "--no-heading",
             "--files-with-matches", "--color", "--ignore-case", "--multiline"),
             ("--glob", "--type", "--max-count", "--context", "--type-not")),
    "wc": _o("lwcmL"),
    "jq": _o("rcsenSjM", "", ("--raw-output", "--compact-output", "--slurp", "--sort-keys",
             "--exit-status", "--null-input")),
    "sort": _o("rnuftVhsbdgiMR", "kt"),
    "uniq": _o("cdui", "fs"),
    "cut": _o("s", "dfcb", ("--complement",)),
    "tr": _o("dsc"),
    "diff": _o("uqrNwiBbaty", "U", ("--brief", "--recursive", "--unified", "--color")),
    "cmp": _o("sl", "n"),
    "comm": _o("123i"),
    "stat": _o("Lt", "c"),
    "file": _o("biLkz"),
    "echo": _o("neE"),
    "printf": _o(),
    "pwd": _o("LP"),
    "which": _o("a"),
    "type": _o("aptP"),
    "true": _o(),
    "false": _o(),
    "basename": _o("az", "s"),
    "dirname": _o("z"),
    "realpath": _o("emsqzL"),
    "readlink": _o("fenmqsvz"),
    "du": _o("hsckmabxL", "d"),
    "df": _o("hTiklP", "t"),
    "printenv": _o("0"),
    "column": _o("tnxe", "sco"),
    "sha256sum": _o("bctwz"),
    "md5sum": _o("bctwz"),
    "tree": _o("adfFiCnpshugDlrtvxJ", "LIP"),
    "sleep": _o(),
    "ps": _o("aAuxefwlHj", "opUC"),
    "pgrep": _o("falnoxcvi", "uUPgGst"),
}
#: Read programs checked by their own rule below rather than an Opts row.
SPECIAL_READ = frozenset({"cd", "find", "sed", "date", "test", "["})
#: Scratch writers: only these options, and every operand must be scratch.
#: Options that name a destination (`-t`, `--target-directory`) are absent,
#: so the destination is always an operand the scratch check sees.
SCRATCH_WRITE: dict[str, Opts] = {
    "rm": _o("rRfvid", "", ("--recursive", "--force")),
    "mv": _o("fvinu"),
    "cp": _o("rRpfvaiunLHPdsl"),
    "tee": _o("ai"),
    "mkdir": _o("pv"),
    "touch": _o("acm"),
    "ln": _o("sfnvrT"),
    "truncate": _o("c", "s"),
    "rmdir": _o("pv"),
}
GIT_READ = frozenset({"log", "status", "diff", "show", "rev-parse", "ls-files", "ls-tree",
                      "ls-remote", "cat-file", "blame", "grep", "describe", "merge-base", "shortlog",
                      "for-each-ref"})
#: One option allowlist for every GIT_READ subcommand. No `--output`, no
#: `--ext-diff`/`--textconv`, no `-O` (git grep's open-in-pager).
GIT_OPTS = _o("nqspvlaibrcwzte", "n", (
    "--oneline", "--stat", "--name-only", "--name-status", "--short", "--porcelain", "--branch",
    "--all", "--graph", "--decorate", "--abbrev-ref", "--show-toplevel", "--cached", "--staged",
    "--is-ancestor", "--verify", "--quiet", "--reverse", "--first-parent", "--merges",
    "--no-merges", "--heads", "--tags", "--exit-code", "--word-diff", "--color", "--patch",
    "--shortstat", "--numstat", "--count", "--git-dir", "--show-current", "--no-color",
    "--summary", "--follow", "--ignore-case", "--line-number", "--fork-point"),
    ("--format", "--pretty", "--since", "--until", "--author", "--grep", "--max-count",
     "--untracked-files", "--abbrev", "--sort", "--date"), numeric=True)
GH_READ = frozenset({("issue", "view"), ("issue", "list"), ("pr", "view"), ("pr", "list"),
                     ("pr", "diff"), ("pr", "checks"), ("run", "view"), ("run", "list"),
                     ("repo", "view")})
#: No `--web` (opens a browser) and no `--watch`.
GH_OPTS = _o("c", "qRLslAS", ("--comments", "--log", "--log-failed", "--name-only", "--patch",
             "--required"), ("--json", "--jq", "--repo", "--limit", "--state", "--label",
             "--search", "--template", "--author", "--assignee", "--base", "--head", "--branch",
             "--workflow", "--job"))
FIND_VALUES = frozenset({"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth",
                         "-newer", "-mtime", "-mmin", "-size", "-user", "-group", "-perm", "-regex",
                         "-iregex", "-wholename"})
FIND_FLAGS = frozenset({"-print", "-print0", "-o", "-a", "-or", "-and", "-not", "!", "(", ")",
                        "-prune", "-empty", "-L", "-H", "-P", "-follow", "-xdev", "-depth",
                        "-readable", "-writable", "-executable"})
#: sed is ordinary only as `-n`/`-E`/`-r`/`-e` with scripts of this shape:
#: print, quit, delete or `=` at an optional address, or `s///` whose flags
#: are among `gIip` and digits (never `w`, `e` or `m`). `-f`, `-i`, `-s` and
#: anything else are excluded.
_SED_ADDR = r"(?:\d+|\$|/[^/\n]*/)"
SED_SAFE = re.compile(
    rf"^(?:{_SED_ADDR}(?:,{_SED_ADDR})?)?\s*!?\s*[pqd=]$"
    rf"|^(?:{_SED_ADDR}(?:,{_SED_ADDR})?)?\s*s(?P<d>[/|#,:@])(?:\\.|(?!(?P=d))[^\\\n])*(?P=d)"
    r"(?:\\.|(?!(?P=d))[^\\\n])*(?P=d)[gIip0-9]*$")
SED_OPTS = _o("nEr", "e")
DATE_OPTS = _o("uRI", "d", ("--utc", "--iso-8601", "--rfc-3339"))
_MKTEMP_EXACT = re.compile(r"^(?:\$\(mktemp(?: -d)?\)|`mktemp(?: -d)?`)$")
PROBE = frozenset({"--help", "--dry-run", "--check", "--gate", "--plan", "--validate"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
READ_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})
BACKGROUND_SEPS = frozenset({"|", "|&", "&"})
_VAR = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))(.*)$", re.S)
RANK = {"read": 1, "probe": 2, "scratch-write": 3}
MAX_DEPTH = 4


def resolve_path(word: str, cur: str | None, home: str, binds: dict[str, bool]) -> str | None:
    """The normalized absolute path `word` names, or None when it cannot be
    known. A word led by a name bound to a scratch path resolves under a
    placeholder one level below `/tmp/`, the shallowest a scratch path can be,
    so a `..` that could leave `/tmp/` does."""
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    elif word.startswith("~"):
        return None  # another user's home
    elif word == "$HOME" or word.startswith("$HOME/"):
        word = home + word[5:]
    elif word.startswith("${HOME}"):
        word = home + word[7:]
    match = _VAR.match(word)
    if match:
        name = match.group(1) or match.group(2)
        rest = match.group(3)
        if not binds.get(name) or (rest and not rest.startswith("/")):
            return None
        word = f"/tmp/.bound-{name}{rest}"
    if "$" in word or "`" in word:
        return None
    if not word.startswith("/"):
        if cur is None:
            return None
        word = os.path.join(cur, word)
    return os.path.normpath(word)


def is_scratch(word: str, cur: str | None, home: str, binds: dict[str, bool]) -> bool:
    resolved = resolve_path(word, cur, home, binds)
    return resolved is not None and resolved.startswith("/tmp/")


def parse_opts(words: list[str], spec: Opts, reason: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split `words` (argv without the program) into operands and
    `(option, value)` pairs under `spec`. Raises `NotOrdinary(reason)` on
    any option the spec does not list. Fail-closed by construction."""
    found: list[str] = []
    valued: list[tuple[str, str]] = []
    index, ended = 0, False
    while index < len(words):
        word = words[index]
        index += 1
        if ended or word == "-" or not word.startswith("-"):
            found.append(word)
        elif word == "--":
            ended = True
        elif word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in spec.long_values:
                if not eq:
                    if index >= len(words):
                        raise NotOrdinary(reason)
                    value, index = words[index], index + 1
                valued.append((name, value))
            elif eq or name not in spec.long:
                raise NotOrdinary(reason)
        elif spec.numeric and word[1:].isdigit():
            continue
        else:
            letters = word[1:]
            for pos, letter in enumerate(letters):
                if letter in spec.values:
                    value = letters[pos + 1:]
                    if not value:
                        if index >= len(words):
                            raise NotOrdinary(reason)
                        value, index = words[index], index + 1
                    valued.append(("-" + letter, value))
                    break
                if letter not in spec.flags:
                    raise NotOrdinary(reason)
    return found, valued


def operands(argv: list[str]) -> list[str]:
    """Words after the program that are not options, for `cd` (whose only
    options, `-L`/`-P`, take no value)."""
    return parse_opts(argv[1:], _o("LP"), "option")[0]


def _find_ok(words: list[str]) -> None:
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word in FIND_VALUES:
            if index >= len(words):
                raise NotOrdinary("option")
            index += 1
        elif word.startswith("-") or word in ("!", "(", ")"):
            if word not in FIND_FLAGS:
                raise NotOrdinary("option")


def _sed_ok(words: list[str]) -> None:
    found, valued = parse_opts(words, SED_OPTS, "option")
    scripts = [value for _name, value in valued]
    if not scripts:
        if not found:
            raise NotOrdinary("option")
        scripts = [found[0]]
    for script in scripts:
        pieces = [p.strip() for p in script.split(";") if p.strip()]
        if not pieces or not all(SED_SAFE.match(p) for p in pieces):
            raise NotOrdinary("option")


def _date_ok(words: list[str]) -> None:
    found, _valued = parse_opts(words, DATE_OPTS, "option")
    # An operand that is not a `+FORMAT` sets the clock.
    if not all(word.startswith("+") for word in found):
        raise NotOrdinary("option")


def _mktemp_scratch(argv: list[str], cur: str | None, home: str, binds: dict[str, bool]) -> bool:
    """True when mktemp has no template operand and no `-p`/`--tmpdir`, or
    every template operand and directory value is scratch."""
    values: list[str] = []
    index = 1
    ended = False
    while index < len(argv):
        word = argv[index]
        if ended or not word.startswith("-") or word == "-":
            values.append(word)
        elif word == "--":
            ended = True
        elif word in ("-p", "--tmpdir"):
            if index + 1 >= len(argv):
                return False
            values.append(argv[index + 1])
            index += 1
        elif word.startswith("--tmpdir="):
            values.append(word[len("--tmpdir="):])
        index += 1
    return all(is_scratch(v, cur, home, binds) for v in values)


def _better(best: str | None, cls: str | None) -> str | None:
    if cls is None or cls == "neutral":
        return best
    return cls if best is None or RANK[cls] > RANK[best] else best


def classify(command: str, cwd: str | None, home: str, depth: int = 0) -> str | None:
    """The class of an ordinary command: "read", "probe" or "scratch-write";
    None when every simple command is neutral. Raises `NotOrdinary(reason)`
    for anything else and `ValueError` when the text does not parse."""
    if depth > MAX_DEPTH:
        raise NotOrdinary("depth")
    cur = cwd
    stack: list[str | None] = []
    binds: dict[str, bool] = {}
    best: str | None = None
    pending_bind: str | None = None
    for item in simple_commands(command):
        if isinstance(item, GroupMarker):
            if item.kind == "open":
                stack.append(cur)
            elif stack:
                cur = stack.pop()
            continue
        argv = list(item.argv)
        bind_now, pending_bind = pending_bind, None

        for word in argv + [target for _op, target in item.redirects if target]:
            for body in substitution_bodies(word):
                best = _better(best, classify(body, cur, home, depth + 1))

        index = 0
        while index < len(argv) and _ASSIGNMENT.match(argv[index]):
            index += 1
        assignments, rest = argv[:index], strip_invocation_prefix(argv, unwrap_shells=False)
        for word in assignments:
            name, value = word.split("=", 1)
            if rest:
                # A prefix assignment does not apply to its own command's
                # expansions: `D=/tmp/x rm -rf "$D/"` runs `rm -rf /`. Bind
                # nothing, and drop any earlier binding of the name.
                binds.pop(name, None)
            elif value == "$" and word is assignments[-1]:
                pending_bind = name
            else:
                # Only an absolute scratch path or exactly `$(mktemp)` /
                # `$(mktemp -d)` binds: a relative value is re-read against
                # whatever directory a later command runs in.
                binds[name] = bool(_MKTEMP_EXACT.match(value)) or (
                    value.startswith("/") and is_scratch(value, None, home, binds))

        cls: str | None
        prog = os.path.basename(rest[0]) if rest else None
        if prog is None:
            cls = "neutral"
        elif prog in ("for", "case", "select"):
            if prog == "for" and len(rest) >= 3 and rest[2] == "in":
                words = rest[3:]
                binds[rest[1]] = bool(words) and all(
                    w.startswith("/") and is_scratch(w, None, home, binds) for w in words)
            cls = "neutral"
        elif prog in SHELLS:
            if len(rest) >= 3 and rest[1] in ("-c", "-lc", "--command"):
                cls = classify(rest[2], cur, home, depth + 1) or "neutral"
            else:
                raise NotOrdinary("shell")
        elif any(word in PROBE for word in rest):
            cls = "probe"
        elif prog == "mktemp":
            if not _mktemp_scratch(rest, cur, home, binds):
                raise NotOrdinary("mktemp")
            cls = "scratch-write"
            if bind_now:
                binds[bind_now] = True
        elif prog == "cd":
            targets = operands(rest)
            if item.sep_before in BACKGROUND_SEPS or item.sep_after in BACKGROUND_SEPS:
                cur = None
            elif not targets:
                cur = home
            elif targets[0] == "-":
                cur = None
            else:
                cur = resolve_path(targets[0], cur, home, binds)
            cls = "read"
        elif prog == "git":
            # The subcommand comes first: a global option (`-C`, `-c`) is not ordinary.
            if len(rest) < 2 or rest[1] not in GIT_READ:
                raise NotOrdinary("unclassified")
            parse_opts(rest[2:], GIT_OPTS, "option")
            cls = "read"
        elif prog == "gh":
            if len(rest) >= 2 and rest[1] == "api" and gh_api_allowlisted(rest[2:]):
                cls = "read"
            elif len(rest) >= 3 and (rest[1], rest[2]) in GH_READ:
                parse_opts(rest[3:], GH_OPTS, "option")
                cls = "read"
            else:
                raise NotOrdinary("unclassified")
        elif prog in ("test", "["):
            cls = "read"
        elif prog == "find":
            _find_ok(rest[1:])
            cls = "read"
        elif prog == "sed":
            _sed_ok(rest[1:])
            cls = "read"
        elif prog == "date":
            _date_ok(rest[1:])
            cls = "read"
        elif prog in READ:
            found, _valued = parse_opts(rest[1:], READ[prog], "option")
            if prog == "uniq" and len(found) >= 2:
                raise NotOrdinary("option")  # uniq IN OUT writes OUT
            cls = "read"
        elif prog in SCRATCH_WRITE:
            targets, valued = parse_opts(rest[1:], SCRATCH_WRITE[prog], "option")
            if prog == "truncate":
                valued = []  # -s SIZE is a size, not a path
            if not targets or not all(is_scratch(t, cur, home, binds)
                                      for t in [*targets, *(v for _n, v in valued)]):
                raise NotOrdinary("unclassified")
            cls = "scratch-write"
        else:
            raise NotOrdinary("unclassified")

        for op, target in item.redirects:
            if op in READ_REDIRECTS or target is None:
                continue
            if target == "/dev/null":
                continue
            if not is_scratch(target, cur, home, binds):
                raise NotOrdinary("redirect")
            if cls in ("neutral", "read"):
                cls = "scratch-write"
        best = _better(best, cls)
    return best


# --------------------------------------------------------------------------
# Reading transcripts.

class Corpus:
    def __init__(self) -> None:
        self.files = 0
        #: Bash id -> (key, command, meta); key = (timestamp or "", path, line)
        self.calls: dict[str, tuple[tuple, str, dict]] = {}
        #: tool_use id -> [(is_error, text, decision, stdout_dict, shape_ok)]
        self.results: dict[str, list[tuple]] = collections.defaultdict(list)


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def transcript_files(root: Path) -> list[str]:
    root = Path(root)
    return sorted(glob.glob(str(root / "*" / "*.jsonl"))
                  + glob.glob(str(root / "*" / "*" / "subagents" / "*.jsonl")))


def _result_text(content) -> str:
    if isinstance(content, list):
        content = " ".join(x.get("text", "") for x in content
                           if isinstance(x, dict) and isinstance(x.get("text"), str))
    return content.strip()[:200] if isinstance(content, str) else ""


def _shape_ok(item: dict) -> bool:
    content = item.get("content")
    content_ok = isinstance(content, str) or (
        isinstance(content, list)
        and all(isinstance(x, dict) and x.get("type") == "text" for x in content))
    return content_ok and ("is_error" not in item or isinstance(item.get("is_error"), bool))


def read_corpus(root: Path, until: dt.datetime, progress=None) -> Corpus:
    corpus = Corpus()
    files = transcript_files(root)
    corpus.files = len(files)
    for number, path in enumerate(files, 1):
        if progress and number % 500 == 0:
            progress(f"never-refuse-sample: read {number}/{len(files)} files")
        slug = os.path.relpath(path, root).split(os.sep)[0]
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                if '"Bash"' not in line and '"tool_result"' not in line and '"permissionDecision"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts = _parse_ts(record.get("timestamp"))
                if ts is not None and ts > until:
                    continue
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                content = message.get("content") if isinstance(message.get("content"), list) else []
                if record.get("type") == "assistant":
                    key = (record.get("timestamp") or "", path, line_no)
                    for item in content:
                        if (isinstance(item, dict) and item.get("type") == "tool_use"
                                and item.get("name") == "Bash" and item.get("id")):
                            previous = corpus.calls.get(item["id"])
                            if previous is None or key < previous[0]:
                                command = (item.get("input") or {}).get("command")
                                corpus.calls[item["id"]] = (key, command if isinstance(command, str) else "", {
                                    "ts": record.get("timestamp"), "cwd": record.get("cwd"),
                                    "session": record.get("sessionId"),
                                    "sidechain": record.get("isSidechain"),
                                    "version": record.get("version") or "unknown", "slug": slug})
                elif record.get("type") == "user":
                    decision = record.get("permissionDecision")
                    decision = decision.get("decision") if isinstance(decision, dict) else None
                    result = record.get("toolUseResult")
                    stdout_dict = isinstance(result, dict) and "stdout" in result
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("tool_use_id"):
                            corpus.results[item["tool_use_id"]].append(
                                (item.get("is_error") is True, _result_text(item.get("content")),
                                 decision, stdout_dict, _shape_ok(item)))
    return corpus


def shape_guard(corpus: Corpus) -> dict:
    """Per-version counts; raises `Failed` on a result shape this code does not
    recognize or a version with no joined result or no non-error result
    carrying a `stdout` dict."""
    versions: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unrecognized = 0
    for call_id, (_key, _command, meta) in corpus.calls.items():
        results = corpus.results.get(call_id, [])
        counts = versions[meta["version"]]
        counts["calls"] += 1
        if results:
            counts["joined"] += 1
        if any(not err and stdout for err, _t, _d, stdout, _s in results):
            counts["ok_with_stdout"] += 1
        unrecognized += sum(1 for *_x, shape in results if not shape)
    if unrecognized:
        raise Failed(f"shape guard: {unrecognized} Bash result(s) have an unrecognized shape "
                     "(content neither a string nor a list of text items, or is_error not a bool)")
    for version, counts in sorted(versions.items()):
        if counts["joined"] == 0 or counts["ok_with_stdout"] == 0:
            raise Failed(f"shape guard: version {version} has {counts['calls']} Bash call(s), "
                         f"{counts['joined']} with a joined result and {counts['ok_with_stdout']} "
                         "with a non-error stdout result; its transcript shape is not recognized")
    return {v: {k: c[k] for k in ("calls", "joined", "ok_with_stdout")} for v, c in sorted(versions.items())}


def bucket(text: str) -> str:
    """A coarse, data-free label for an excluded result: its first two words,
    digits as `N`, each cut to 24 characters, any word carrying a path, quote,
    variable or secret-shaped text replaced by `<arg>`."""
    words = []
    for word in re.sub(r"\d+", "N", text).split()[:2]:
        word = word[:24]
        if any(ch in word for ch in "/'\"`$") or secret_scan.findings(word):
            word = "<arg>"
        words.append(word)
    return " ".join(words) or "<empty>"


def ran_check(results: list[tuple]) -> str | None:
    """None when the call ran; else its exclusion reason. Every joined result
    must pass: no `reject`/`cancelled` decision, and non-error or `Exit code`."""
    if not results:
        return "no_result"
    for is_error, text, decision, _stdout, _shape in results:
        if decision in ("reject", "cancelled"):
            return f"decision:{decision}"
        if is_error and not text.startswith("Exit code "):
            return "error:" + bucket(text)
    return None


def masks_for(meta: dict, home: str, uid: int) -> list[str]:
    candidates = {meta.get("cwd") or "", home, f"/tmp/claude-{uid}/{meta.get('slug') or ''}"}
    return sorted((m for m in candidates if len(m) > 1 and not m.endswith("/")), key=len, reverse=True)


def masked_findings(command: str, masks: list[str]) -> list[str]:
    """`secret_scan.findings` after replacing each exact mask string with a
    space. A mask equals a known path, so no secret hides in it."""
    for mask in masks:
        command = command.replace(mask, " ")
    return secret_scan.findings(command)


def drop_secrets(distinct: dict, home: str, uid: int) -> tuple[dict, collections.Counter]:
    clean, kinds = {}, collections.Counter()
    for command, row in distinct.items():
        found = masked_findings(command, masks_for(row[1], home, uid))
        if found:
            kinds.update(found)
        else:
            clean[command] = row
    return clean, kinds


def build_pool(corpus: Corpus, home: str, uid: int) -> tuple[dict, dict]:
    """`(pool, stats)`: pool maps each ordinary command to `(call_id, meta, class)`."""
    stats: dict = {"files": corpus.files, "bash_calls": len(corpus.calls)}
    stats["versions"] = shape_guard(corpus)
    stats["multi_result_ids"] = sum(1 for cid in corpus.calls if len(corpus.results.get(cid, [])) > 1)
    excluded: collections.Counter = collections.Counter()
    ran: dict[str, tuple] = {}
    for call_id, (key, command, meta) in corpus.calls.items():
        results = corpus.results.get(call_id, [])
        reason = ran_check(results)
        if reason:
            excluded[reason] += 1
        else:
            ran[call_id] = (key, command, meta, any(stdout for *_x, stdout, _s in results))
    stats["ran"] = len(ran)
    stats["ran_without_tool_use_result"] = sum(1 for r in ran.values() if not r[3])
    stats["excluded_by_bucket"] = dict(sorted(excluded.items()))

    earliest: dict[str, tuple] = {}
    empty = 0
    for call_id, (key, command, meta, _stdout) in ran.items():
        if not command.strip():
            empty += 1
            continue
        if command not in earliest or key < earliest[command][0]:
            earliest[command] = (key, meta, call_id)
    stats["empty"] = empty
    stats["distinct"] = len(earliest)

    clean, kinds = drop_secrets({c: (r[0], r[1], r[2]) for c, r in earliest.items()}, home, uid)
    stats["secret_dropped"] = dict(sorted(kinds.items()))

    pool: dict[str, tuple] = {}
    unparseable = 0
    not_ordinary: collections.Counter = collections.Counter()
    for command, (_key, meta, call_id) in clean.items():
        try:
            cls = classify(command, meta.get("cwd") or None, home)
        except NotOrdinary as exc:
            not_ordinary[str(exc)] += 1
            continue
        except ValueError:
            unparseable += 1
            continue
        if cls is None:
            not_ordinary["all-neutral"] += 1
            continue
        pool[command] = (call_id, meta, cls)
    stats["unparseable"] = unparseable
    stats["not_ordinary"] = dict(sorted(not_ordinary.items()))
    stats["pool_by_class"] = dict(sorted(collections.Counter(p[2] for p in pool.values()).items()))
    return pool, stats


# --------------------------------------------------------------------------
# Writing.

def manifest_path(out: Path) -> Path:
    return out.with_name(out.name + MANIFEST_SUFFIX)


def orphan_refusal(out: Path) -> str | None:
    if out.exists() and not manifest_path(out).exists():
        return f"orphan sample from an interrupted derive: delete {out} and re-run"
    return None


def out_refusal(out: Path) -> str | None:
    refusal = capture_failure.forge_refusal(out, "--out")
    if refusal:
        return refusal
    if not fnmatch.fnmatch(out.name, NAME_PATTERN):
        return f"--out {out} must be named {NAME_PATTERN}"
    manifest = manifest_path(out)
    if out.exists() and manifest.exists():
        return f"already derived: {out} and its manifest exist; the sample is frozen"
    if out.exists():
        return orphan_refusal(out)
    if manifest.exists():
        return f"orphan manifest: delete {manifest} and re-run"
    if out.parent.is_dir():
        leftovers = sorted(p.name for p in out.parent.iterdir() if fnmatch.fnmatch(p.name, TEMP_PATTERN))
        if leftovers:
            return (f"leftover temp from an interrupted derive: delete {', '.join(leftovers)} "
                    f"in {out.parent} and re-run")
    return None


def _write_temp(folder: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd != -1:
            os.close(fd)
    return Path(name)


def rescan(rows: list[dict], manifest: dict, home: str, uid: int) -> int:
    hits = 0
    for row in rows:
        meta = {"cwd": row["cwd"], "slug": row["_slug"]}
        hits += bool(masked_findings(row["command"], masks_for(meta, home, uid)))
    hits += sum(bool(secret_scan.findings(label)) for label in manifest["excluded_by_bucket"])
    return hits


def derive(root: Path, out: Path, size: int, seed: int, until: dt.datetime, home: str,
           progress=None) -> dict:
    out = Path(out).expanduser()
    refusal = out_refusal(out)
    if refusal:
        raise Refused(refusal)
    corpus = read_corpus(Path(root).expanduser(), until, progress)
    pool, stats = build_pool(corpus, home, os.getuid())
    if len(pool) < size:
        raise Failed(f"the ordinary pool holds {len(pool)} distinct commands, fewer than --size {size}")
    chosen = random.Random(seed).sample(sorted(pool), size)
    rows = []
    for n, command in enumerate(chosen, 1):
        call_id, meta, cls = pool[command]
        rows.append({"n": n, "id": call_id, "ts": meta["ts"], "session": meta["session"],
                     "sidechain": meta["sidechain"], "version": meta["version"], "cwd": meta["cwd"],
                     "class": cls, "command": command, "_slug": meta["slug"]})
    manifest = {"version": 1, "seed": seed, "size": size, "until": until.isoformat(),
                "derived_at": dt.datetime.now(dt.timezone.utc).isoformat(), **stats}
    manifest["sample_by_class"] = dict(sorted(collections.Counter(r["class"] for r in rows).items()))
    if rescan(rows, manifest, home, os.getuid()):
        raise Failed("pre-write re-scan found a secret-shaped hit; nothing written")
    manifest["secret_hits_stored"] = 0
    data = "".join(json.dumps({k: v for k, v in r.items() if k != "_slug"}, ensure_ascii=False,
                              separators=(",", ":")) + "\n" for r in rows).encode("utf-8")
    manifest["sha256"] = hashlib.sha256(data).hexdigest()

    # The read above takes minutes: check again, then publish with os.link,
    # which refuses an existing name, so a second derive racing this one can
    # never overwrite a frozen sample (harmonic-forge#1289 pass 1).
    refusal = out_refusal(out)
    if refusal:
        raise Refused(refusal)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest_tmp = sample_tmp = None
    try:
        manifest_tmp = _write_temp(out.parent, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        sample_tmp = _write_temp(out.parent, data)
        try:
            os.link(sample_tmp, out)
        except FileExistsError:
            raise Refused(f"already derived: {out} appeared while this derive ran; the sample is frozen") from None
        try:
            os.link(manifest_tmp, manifest_path(out))
        except FileExistsError:
            out.unlink()  # ours: the link above just created it
            raise Refused(f"orphan manifest appeared while this derive ran: delete {manifest_path(out)} "
                          "and re-run") from None
    finally:
        for tmp in (sample_tmp, manifest_tmp):
            if tmp is not None:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
    return manifest


def _summary(out: Path, manifest: dict) -> str:
    return (f"never-refuse-sample: wrote {manifest['size']} rows to {out} "
            f"(files {manifest['files']}, bash calls {manifest['bash_calls']}, ran {manifest['ran']}, "
            f"excluded {sum(manifest['excluded_by_bucket'].values())}, distinct {manifest['distinct']}, "
            f"secret-dropped {sum(manifest['secret_dropped'].values())}, "
            f"unparseable {manifest['unparseable']}, not-ordinary {sum(manifest['not_ordinary'].values())}, "
            f"pool {sum(manifest['pool_by_class'].values())}, sample {manifest['sample_by_class']})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("derive", help="Derive and freeze the sample.")
    d.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    d.add_argument("--out", type=Path, default=DEFAULT_OUT)
    d.add_argument("--size", type=int, default=DEFAULT_SIZE)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    d.add_argument("--until", default=None, help="ISO timestamp; records after it are skipped (default now).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        until = _parse_ts(args.until) if args.until else dt.datetime.now(dt.timezone.utc)
        if until is None:
            raise Refused(f"--until {args.until!r} is not an ISO timestamp")
        manifest = derive(args.root, args.out, args.size, args.seed, until,
                          os.path.expanduser("~"),
                          progress=lambda msg: print(msg, file=sys.stderr))
        print(_summary(Path(args.out).expanduser(), manifest))
        sys.stdout.flush()
        return 0
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Failed as exc:
        print(f"never-refuse-sample: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - every other error is exit 3, never 1
        print(f"never-refuse-sample: error: {type(exc).__name__}", file=sys.stderr)
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
