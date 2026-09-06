#!/usr/bin/env python3
"""`SessionStart` handler for context compaction (harmonic-forge#446).

WHAT THIS IS FOR
-------------------
A lane session that gets auto-compacted loses ~96% of its context (measured:
936,523 -> 33,974 tokens) and keeps working as though nothing happened. This
fires once at the compaction rebuild, writes a marker, and injects a short
situational note into the reconstructed context.

WHY THE INJECTION CARRIES NO RULES
-------------------------------------
The harness re-reads the **auto-loaded** surface after a compaction --
`CLAUDE.md` and `.claude/rules/*.md` -- proven live by putting a token in each,
changing it after the compaction, and seeing the NEW value come back. So
shipping rule text here would duplicate content that is already restored,
spending the context budget this issue exists to protect.

But the auto-loaded surface is NOT the lane's directive corpus, and conflating
the two was this issue's most expensive error. `harmonic-forge/CLAUDE.md` is 19
lines -- a pointer -- and the repo has no `.claude/rules/` at all. The real
corpus is Read-tool-loaded and never comes back on its own.

That corpus is enumerated ONCE, in `CORPUS` below, and the injection is derived
from it. It used to be listed here as well, with line counts, and the two lists
drifted by two entries -- named here as lost and then routed to nobody
(harmonic-forge#464). A second hand-maintained copy of one fact is what produced
that, so there is now one. No filename belongs in this docstring: naming one
here is how the duplicate list starts again.

Telling a compacted session "your directives are back" would suppress the exact
recovery action it needs. So the payload states the split -- auto-loaded surface
restored, protocol corpus NOT -- and names the paths to re-read. A path list,
not rule content.

WHAT DOES NOT FIRE HERE
--------------------------
The subagent-compaction shape (`agentType == "subagent"` with
`delegatedObservation == true`) does not reach this hook. That is the harness's
behavior, not an omission -- named so its absence is not later read as a defect.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

#: Same convention as `item_list_cache.py:30` / `model_tier_gate.py:94`.
MARKER_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-compaction-gate"

#: Markers older than this are pruned opportunistically. Generous on purpose:
#: nothing reads them yet (harmonic-forge#451 is blocked), and deleting one
#: early is strictly worse than keeping it -- see `prune_markers`.
TTL_SECONDS = 7 * 24 * 60 * 60

class Routing(str, Enum):
    """How one corpus file reaches a compacted session.

    Two axes, not one, which is the finding that shaped harmonic-forge#464:
    some of the corpus routes by **lane** and some by **repo**, and a structure
    that can only express lanes has nowhere to put the second kind.
    """

    #: Every session, whatever its lane or cwd.
    ALWAYS = "always"
    #: Only the lane named in `CorpusFile.lane`.
    BY_LANE = "by_lane"
    #: Only a session whose cwd is inside the harmonic-forge checkout.
    FORGE_REPO = "forge_repo"
    #: Corpus, deliberately not re-injected. Requires a stated `reason` --
    #: an exclusion should be readable in source, not inferred from a file's
    #: absence from a list, which is precisely how #464's two went missing.
    #: No entry uses this today.
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class CorpusFile:
    """One corpus path and the rule for who is told to re-read it."""

    path: str
    routing: Routing
    #: Required for `BY_LANE`, meaningless otherwise.
    lane: str | None = None
    #: Required for `EXCLUDED`, meaningless otherwise.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.routing is Routing.BY_LANE and not self.lane:
            raise ValueError(f"{self.path}: BY_LANE routing needs a lane")
        if self.routing is Routing.EXCLUDED and not self.reason:
            raise ValueError(f"{self.path}: EXCLUDED routing needs a stated reason")
        if self.routing is not Routing.BY_LANE and self.lane:
            raise ValueError(f"{self.path}: lane is meaningless for {self.routing.value}")


#: **The single enumeration of the protocol corpus.** Paths, never content.
#:
#: Nothing else in this module may list corpus paths. `corpus_for()` derives the
#: injection from this and the module docstring points here rather than
#: repeating it -- that duplication is what harmonic-forge#464 removed.
#:
#: Line counts are deliberately not recorded: they were a third hand-maintained
#: fact in the old docstring block and would go stale the same way.
CORPUS: tuple[CorpusFile, ...] = (
    CorpusFile("3-lane-protocol.md", Routing.ALWAYS),
    CorpusFile("rules/universal-agent.md", Routing.ALWAYS),
    # Every lane, unconditionally. Lane 2 and Lane 3 accept either Claude Code
    # or Codex (`rules/universal-lane1.md` § Current lane assignment), so this
    # is a deliberate choice rather than an oversight: `universal-lane1.md`
    # defers to this file for tool-use safeguards, the memory system and the
    # concrete advisory-invocation mechanism, and the cost of naming one
    # ignorable path to a Codex session is smaller than the cost of dropping it.
    # A `LANE_AGENT` gate was considered and rejected -- an unset variable there
    # fails by silently dropping the file, which is this bug in a harder-to-see
    # form.
    CorpusFile("rules/universal-claude.md", Routing.ALWAYS),
    CorpusFile("rules/universal-lane1.md", Routing.BY_LANE, lane="1"),
    CorpusFile("rules/testing-gate.md", Routing.BY_LANE, lane="3"),
    # Repo-scoped, not lane-scoped: vendor-neutral direction for agents working
    # *inside* the forge repo ("Editing the platform from inside the platform is
    # the one case with no other coverage"). Loaded by forge's `CLAUDE.md`, a
    # 19-line pointer, so it is Read-loaded and genuinely lost -- but naming it
    # to an HRSE2 lane session is noise.
    CorpusFile("docs/agent-foundation.md", Routing.FORGE_REPO),
)


def forge_root() -> str:
    """Where the protocol corpus lives, for the paths named in the injection."""
    return os.environ.get("HARMONIC_FORGE_ROOT") or str(Path.home() / "harmonic-forge")


#: harmonic-forge#480. Verbs that READ a file's contents. Beside `CORPUS`
#: rather than in the consuming hook, deliberately: this module's own
#: docstring records (harmonic-forge#464) that a second hand-maintained copy
#: of one fact is what makes two lists drift, and a corpus path list and a
#: "what counts as reading one" list are one fact in two halves.
#:
#: `wc` is deliberately ABSENT. It reads the file and returns a COUNT, never
#: content -- and it is the near-miss this list exists to decide about: the
#: session that motivated this issue ran `wc -l` on four corpus files at tool
#: action 2, then actually read two of them at actions 3 and 4. Marking
#: "reloaded" for having measured the corpus is the opposite of what this
#: detects.
READ_VERBS = frozenset({"cat", "sed", "head", "tail", "less", "more", "bat", "view"})

#: A command is split on these before any segment is examined, so
#: `cd x && cat corpus.md` is judged on the `cat`, not the `cd`.
_SEGMENT = re.compile(r"&&|\|\||;|\|")

#: Output redirection makes a segment a WRITE regardless of its verb.
#: `cat >> file <<EOF` leads with `cat`, which is on the read list above.
#: Real example from the motivating transcript, action 29.
_REDIRECT = re.compile(r"(?<![0-9])>>?")


def is_corpus_reload(tool_name: str, tool_input: dict) -> bool:
    """Did this tool call actually re-read a corpus file?

    Three conditions, each earned from a false positive in real transcript
    data rather than imagined:

    1. **A segment containing a heredoc or a redirect is skipped.** Both
       make it a write, or make a corpus path an argument that was never
       read. `cat` is a writer in `cat >> tests.py <<EOF`, and a reader of
       the wrong thing in `cat <<EOF ... rules/universal-agent.md ... EOF`.
    2. **The corpus path must be an ARGUMENT to a read verb**, not merely
       present in the segment's text. This is what separates
       `sed -n '1,240p' rules/universal-lane1.md` from a Python heredoc that
       mentions the same filename -- actions 20 and 38 of the motivating
       transcript, both of which this rule alone already rejects, since
       `python3` is not a read verb.

    Per SEGMENT, not per command, and that distinction was earned: a
    whole-command heredoc rejection also threw away
    `cat rules/universal-agent.md && python3 - <<PY ... PY`, which is a real
    reload with an unrelated heredoc after it.

    The `Read` tool needs none of that -- it cannot write.
    """
    if tool_name == "Read":
        path = str((tool_input or {}).get("file_path") or "")
        # Basename only: `path.endswith(entry.path)` was also here and never
        # added a match, since any path ending in `rules/testing-gate.md`
        # ends in `testing-gate.md` too. Mutation testing found it inert;
        # a redundant clause that reads like a second check is worse than
        # one clause that is honestly the whole rule.
        return any(path.endswith(Path(entry.path).name) for entry in CORPUS)
    if tool_name != "Bash":
        return False
    command = str((tool_input or {}).get("command") or "")
    names = [Path(entry.path).name for entry in CORPUS]
    for segment in _SEGMENT.split(command):
        segment = segment.strip()
        # Both disqualifiers are PER SEGMENT, not per command. Rejecting the
        # whole command on a heredoc anywhere was the first shape written and
        # mutation testing showed it wrong in both directions:
        #
        #   `cd x && python3 - <<PY ... "rules/testing-gate.md" ... PY`
        #       the motivating false positive -- already False without any
        #       heredoc rule at all, because `python3` is not a read verb.
        #   `echo x && cat <<EOF ... rules/universal-agent.md ... EOF`
        #       False ONLY because of this rule. `cat` reading FROM a heredoc
        #       makes the corpus path an argument that was never read.
        #   `cat rules/universal-agent.md && python3 - <<PY ... PY`
        #       a genuine reload that a whole-command rule REJECTED.
        if not segment or "<<" in segment or _REDIRECT.search(segment):
            continue
        try:
            parts = shlex.split(segment)
        except ValueError:
            continue
        if not parts or parts[0] not in READ_VERBS:
            continue
        if any(any(name in argument for name in names) for argument in parts[1:]):
            return True
    return False


def note_reload(session_id: str, when: str) -> bool:
    """Record that this session re-read its corpus. `True` if it wrote.

    **Update-only: never creates a marker.** A `PreToolUse` that could create
    one would manufacture a compaction that never happened, and
    harmonic-forge#451 would then deny on it. A session with no marker has
    not compacted, and that is the common case this must leave untouched.

    **Set-once.** The question is "has a reload happened since the
    boundary", so the first one answers it; later reads are noise and
    rewriting would lose the original timestamp.
    """
    target = MARKER_DIR / f"{session_id}.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("reloaded_at"):
        return False
    payload["reloaded_at"] = when
    try:
        write_marker(session_id, payload)
    except (OSError, ValueError):
        return False
    return True


def resolve_trigger(session_id: str, transcript_path: str = "") -> str | None:
    """Fill in a marker's `trigger` from the transcript. Returns what it holds.

    **Update-only and set-once**, exactly like `note_reload` above and for the
    same reasons: a `PreToolUse` that could create a marker would manufacture a
    compaction that never happened, and re-resolving would let a later
    compaction's record overwrite this one's answer.

    Called from `compaction_reload_probe.py` rather than from the `SessionStart`
    hook -- see `trigger_of`'s docstring for the 42-68 ms measurement that makes
    the earlier call site structurally unable to succeed (harmonic-forge#489).

    `transcript_path` falls back to the one the marker carries, so the probe
    works even on a payload that omits it.
    """
    target = MARKER_DIR / f"{session_id}.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if "trigger" in payload:
        return payload["trigger"] if isinstance(payload["trigger"], str) else None

    path = transcript_path or payload.get("transcript_path") or ""
    found = trigger_of(path, not_before=payload.get("compacted_at"))
    if found is None:
        # Left ABSENT, not written as null: the record may simply not have been
        # flushed yet on a very fast first tool call, and a null would freeze
        # that transient state permanently under the set-once rule above.
        return None
    payload["trigger"] = found
    try:
        write_marker(session_id, payload)
    except (OSError, ValueError):
        return None
    return found


#: The distance from EOF at which a transcript's newest `compactMetadata`
#: record actually sits, measured across all 37 transcripts on this machine
#: (harmonic-forge#489):
#:
#:     min      16,191      p50  3,581,245
#:     p90  12,009,408      max 13,945,800   (13.3 MiB)
#:
#: The former 1 MiB budget was outside that distance on **32 of 37**. It was
#: not resized, it was deleted: a chunked reverse scan stops at the record, so
#: its cost is a function of the DISTANCE, not the file size -- a 112 MB
#: transcript resolved in 6.4 ms, and the worst case across all 37 was 14.7 MB
#: read in 6.6 ms. A budget picked from this sample is a bet against the next
#: busier session; the scan needs no constant at all.
CHUNK_BYTES = 1 << 20


def trigger_of(transcript_path: str, not_before: str | None = None) -> str | None:
    """`auto` / `manual` / `None` -- how the compaction was triggered.

    **NOT from the hook payload.** harmonic-forge#480's gate directed reading
    `payload.get("trigger")` in `handle()` "since the SessionStart compaction
    event carries one". It does not. Measured across every transcript on this
    machine, the field exists in exactly one place -- on a `system` record
    inside the TRANSCRIPT, alongside `preTokens` / `postTokens` /
    `cumulativeDroppedTokens`. The captured real payload in
    `testdata/sessionstart_compact.json` carries no `trigger` either.

    **This must NOT be called from the `SessionStart` hook** (harmonic-forge#489).
    F480 assumed "at hook time the compaction record is the newest thing in the
    file". Measured on five real markers, the record's own timestamp is 42-68 ms
    AFTER the marker's `compacted_at` -- the hook reads before the harness has
    written the record, so the record is not merely far back, it is ABSENT. All
    five markers recorded `trigger: null` for that reason, and widening the scan
    would have found the PREVIOUS compaction's record and reported its trigger
    as this one's: usually `auto`, usually right by luck, always describing the
    wrong event, and silently empty on a session's first compaction.

    The fix is to call this at read time (first `PreToolUse`), where F480's
    assumption is finally true, and to prove the pairing rather than assume it:

    `not_before` -- an ISO timestamp, normally the marker's `compacted_at`.
    A record older than it belongs to an earlier compaction and is rejected.
    That turns the 42-68 ms delta from the hazard above into the evidence that
    the record found is this compaction's.

    Returns `None` rather than raising on anything unreadable -- a missing
    trigger costs harmonic-forge#451 one discriminator, while an exception in a
    `PreToolUse` costs the session every tool call.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            tail = b""
            while position > 0:
                step = min(CHUNK_BYTES, position)
                position -= step
                stream.seek(position)
                # Carry a small overlap so a record straddling a chunk
                # boundary is not split in half and missed.
                chunk = stream.read(step) + tail[:512]
                if b'"compactMetadata"' in chunk:
                    found, stop = _trigger_in(chunk, not_before)
                    if found is not None:
                        return found
                    if stop:
                        return None
                tail = chunk
    except OSError:
        return None
    return None


def _trigger_in(chunk: bytes,
                not_before: str | None) -> tuple[str | None, bool]:
    """`(trigger, stop)` for one chunk, scanned newest-record-first.

    `stop` means a record older than `not_before` was reached. Records are
    append-ordered, so everything further back is older still: continuing would
    walk the whole file only to find a record that belongs, by definition, to an
    earlier compaction. Stopping is the difference between "no answer" and "the
    previous compaction's answer", which is the defect harmonic-forge#489 exists
    to close.
    """
    floor = _parse_stamp(not_before)
    for raw in reversed(chunk.split(b"\n")):
        if b'"compactMetadata"' not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            # A chunk boundary cut this record in half, or it is a fenced
            # quotation of a footer rather than a record. Either way the next
            # line back is the one to try; the overlap above means a genuine
            # record is never lost to the cut.
            continue
        if not isinstance(record, dict):
            continue
        stamp = _parse_stamp(record.get("timestamp"))
        if floor is not None and stamp is not None and stamp < floor:
            return None, True
        found = (record.get("compactMetadata") or {}).get("trigger")
        if isinstance(found, str) and found:
            return found, False
    return None, False


def _parse_stamp(value: object) -> datetime | None:
    """An ISO timestamp as an aware `datetime`, or `None`.

    **Parsed, never string-compared.** The two sides genuinely differ in
    spelling: the transcript writes `...T04:23:24.146Z` and the marker writes
    `...T04:23:24.104434+00:00`. Lexicographic order across those two forms is
    not chronological order -- `Z` (0x5A) sorts after `+` (0x2B) at the same
    offset -- so a string comparison would invert for any pair whose seconds
    match, which is exactly the pair this predicate is asked about.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lane_of(env: dict[str, str]) -> str:
    """`LANE`, or `"unknown"`.

    Never a bare `LANE=` and never a raise: this hook runs at the moment a
    session is least able to cope with a crash, and a session with no `LANE` is
    a legitimate state (an operator shell, a subagent), not an error.

    **Not the answer on its own** -- see `resolve_lane`. `LANE` is wrong for
    every daemon-spawned background job on a machine whose daemon was first
    started from a Lane 1 shell (harmonic-forge#479).
    """
    lane = (env.get("LANE") or "").strip()
    return lane if lane else "unknown"


#: A worktree named for its lane. Matched against every path COMPONENT, not
#: the basename: a session's cwd is routinely a subdirectory of its worktree
#: (`HRSE2-lane3/backend` is a real recorded marker), and a basename-only
#: match returns nothing there -- silently indistinguishable from a Lane 1
#: checkout that legitimately has no suffix. That was 1 of the 5 markers on
#: disk when this was written.
#:
#: Checked against every worktree basename across both repos for false
#: matches: only `HRSE2-lane2`, `HRSE2-lane3`, `harmonic-forge-lane2` and
#: `harmonic-forge-lane3` match. `hrse2-1443-impl`, `harmonic-forge-f326`,
#: `hrse2-733p` and the rest do not.
_LANE_DIR = re.compile(r"^.*-lane(\d+)$", re.I)


def lane_from_cwd(cwd: str) -> str | None:
    """The lane a path lives in, or `None` when nothing says.

    `None` is not "lane unknown" -- it is "this path carries no claim",
    which is the normal state of Lane 1's own unsuffixed checkout and must
    never be read as a contradiction.
    """
    for part in Path(cwd).parts:
        found = _LANE_DIR.match(part)
        if found:
            return found.group(1)
    return None


def resolve_lane(env: dict[str, str], cwd: str) -> tuple[str, str]:
    """`(lane, source)` -- cross-check `LANE` against the cwd it runs in.

    **Why `LANE` cannot be trusted alone (harmonic-forge#479).** `lane1`/
    `lane2`/`lane3` export `LANE` into the *interactive* process they exec. A
    background job is spawned by the long-lived `claude daemon run`, which is
    not in the launcher's process tree at all -- it inherits whatever shell
    first started the daemon. On this machine that was a Lane 1 shell, so
    every daemon-spawned background job reports `LANE=1` regardless of the
    launcher used or the worktree it actually runs in.

    Live incident: a marker recorded `"lane": "1"` with
    `"cwd": ".../HRSE2-lane2"`, and the injection built from it told a Lane 2
    session *"You are LANE=1"*. That session stopped and asked the operator
    whether it was Lane 1 -- twice.

    **The blast radius is asymmetric, and worse for Lane 3 than for Lane 2.**
    Only two corpus files are `BY_LANE`: `universal-lane1.md` (lane 1) and
    `testing-gate.md` (lane 3). Lane 2 has none of its own, so a Lane 2
    session mislabelled as Lane 1 is handed one file it does not need and
    loses nothing. A **Lane 3** job mislabelled as Lane 1 loses
    `testing-gate.md` -- the gate's own rules, dropped from the reload list of
    the lane whose entire job is the gate.

    Three sources, because two would collapse a routine case into a bug
    report:

      `env`          -- they agree, or the cwd makes no claim. Nothing to see.
      `cwd`          -- `LANE` was unset and the cwd filled it in. Routine:
                        an unset var has nothing to contradict, so this is
                        strictly more information at no risk.
      `cwd_override` -- `LANE` was set and WRONG. This is the daemon bug,
                        and it is the value worth surfacing to an operator.
    """
    env_lane = lane_of(env)
    cwd_lane = lane_from_cwd(cwd)
    if cwd_lane is None:
        return env_lane, "env"
    if env_lane == "unknown":
        return cwd_lane, "cwd"
    if env_lane != cwd_lane:
        return cwd_lane, "cwd_override"
    return env_lane, "env"


def _inside_forge_repo(cwd: str, root: str) -> bool:
    """Is this session working inside the harmonic-forge checkout?

    Both sides are resolved before comparing, so a symlinked or relative cwd
    does not silently miss — which would drop `agent-foundation.md` for exactly
    the sessions that need it, this issue's own failure shape.

    A cwd that cannot be resolved (deleted directory, permission error) is
    treated as "not in the forge repo": the cost is one unnamed path, against a
    raise in a hook that runs when the session is least able to cope with one.
    """
    try:
        resolved_cwd = Path(cwd).resolve()
        resolved_root = Path(root).resolve()
    except (OSError, ValueError):
        return False
    return resolved_cwd == resolved_root or resolved_root in resolved_cwd.parents


def _routes_to(entry: "CorpusFile", lane: str, cwd: str, root: str) -> bool:
    """Whether one corpus entry is named to this session.

    An unhandled `Routing` raises rather than defaulting either way. Silently
    dropping an unrouted entry is harmonic-forge#464 itself; silently including
    it would make the `EXCLUDED` tier meaningless. `handle()` catches this and
    still ships the injection, so a bad declaration degrades visibly instead of
    costing the session its recovery note.
    """
    if entry.routing is Routing.ALWAYS:
        return True
    if entry.routing is Routing.BY_LANE:
        return entry.lane == lane
    if entry.routing is Routing.FORGE_REPO:
        return _inside_forge_repo(cwd, root)
    if entry.routing is Routing.EXCLUDED:
        return False
    raise ValueError(f"{entry.path}: unhandled routing {entry.routing!r}")


def corpus_for(lane: str, cwd: str = "") -> list[str]:
    """The paths this session is told to re-read, derived from `CORPUS`.

    `cwd` defaults to empty so an existing caller that only knows the lane keeps
    working; an empty cwd simply routes no repo-scoped file, which is the same
    answer it would have given before repo scoping existed.
    """
    root = forge_root()
    return [
        f"{root}/{entry.path}"
        for entry in CORPUS
        if _routes_to(entry, lane, cwd, root)
    ]


def build_context(compacted_at: str, lane: str, cwd: str) -> str:
    """The injected payload. Situational state and paths — no rule text."""
    paths = "\n".join(f"  - {p}" for p in corpus_for(lane, cwd))
    return (
        f"This session was compacted at {compacted_at}. You are LANE={lane} in {cwd}.\n"
        f"Your CLAUDE.md and .claude/rules/ directives were re-loaded automatically. "
        f"Your protocol corpus was NOT — re-read:\n{paths}\n"
        f"Your task state is also gone — re-read the issue thread before acting."
    )


def prune_markers(now: float) -> None:
    """Drop markers past the TTL, keyed on their own `compacted_at`.

    Keyed on the recorded timestamp and **never on file mtime**: a long-running
    session's marker keeps its original `compacted_at` while its mtime may be
    refreshed or stale for unrelated reasons. Pruning a live session's marker
    would make harmonic-forge#451's gate silently conclude "no compaction
    happened" — a false negative in a guard, which is the failure shape
    harmonic-forge#440 exists as a warning about.

    Per-entry errors are swallowed. Concurrent lanes write and prune this
    directory simultaneously, so a `FileNotFoundError` mid-iteration is normal
    operation. A prune problem must never fail the hook — the injection is the
    product; the prune is housekeeping.
    """
    try:
        entries = list(MARKER_DIR.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(data["compacted_at"]).timestamp()
            if now - stamp > TTL_SECONDS:
                entry.unlink()
        except (FileNotFoundError, PermissionError, OSError,
                ValueError, KeyError, TypeError):
            continue


def write_marker(session_id: str, payload: dict) -> None:
    """Atomic write — temp file in the same directory, then `os.replace`.

    Concurrent lanes share this directory, and #451 will read these while they
    are being written. A partially-written marker parses as corrupt and reads as
    "no compaction", so the write must be all-or-nothing.
    """
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    target = MARKER_DIR / f"{session_id}.json"
    handle, tmp_name = tempfile.mkstemp(dir=str(MARKER_DIR), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def handle(payload: dict, env: dict[str, str], now: float | None = None) -> dict:
    """Returns the hook's stdout object. `{}` means "do nothing"."""
    if payload.get("source") != "compact":
        return {}

    now = time.time() if now is None else now
    compacted_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or "an unknown directory"
    # harmonic-forge#479: resolved BEFORE the marker and the injection are
    # built, so both carry the same corrected value. Fixing only the marker
    # would leave `build_context()` still printing "You are LANE=1" into a
    # Lane 2 session -- which is the half of the incident the operator
    # actually saw.
    lane, lane_source = resolve_lane(env, cwd)

    if session_id:
        try:
            write_marker(session_id, {
                "compacted_at": compacted_at, "source": "compact",
                "lane": lane, "lane_source": lane_source, "cwd": cwd,
                "transcript_path": payload.get("transcript_path") or "",
                # harmonic-forge#489: `trigger` is deliberately NOT written
                # here. F480 resolved it at this point and got `null` on every
                # real compaction -- measured, the harness appends the
                # `compactMetadata` record 42-68 ms AFTER this hook reads, so
                # there is nothing to find yet. `compaction_reload_probe.py`
                # resolves it on the first `PreToolUse`, where the record
                # exists and is genuinely the newest in the file.
                #
                # ABSENT rather than `null`: "not resolved yet" and "resolved,
                # no answer" are different states, and #451 must not treat the
                # first as the second. `transcript_path` is carried so the
                # probe does not have to re-derive it.
            })
            prune_markers(now)
        except (OSError, ValueError):
            # The injection still ships. A marker we could not persist costs
            # #451 a signal; a hook that raised here would cost the session its
            # recovery note, which is the thing that actually helps right now.
            pass

    try:
        context = build_context(compacted_at, lane, cwd)
    except ValueError as err:
        # A malformed `CORPUS` entry — an unhandled routing value. Visible, not
        # silent: a bad declaration must not cost the session its recovery note,
        # and it must not look like "nothing to re-read" either, which is the
        # exact shape harmonic-forge#464 was about. `systemMessage` surfaces it
        # to the operator alongside a payload naming what can still be derived.
        return {
            "systemMessage": f"compaction_marker: corpus declaration is invalid — {err}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"This session was compacted at {compacted_at}. You are LANE={lane} "
                    f"in {cwd}.\nThe protocol-corpus list could not be built ({err}) — "
                    f"re-read your directives from {forge_root()} manually.\n"
                    f"Your task state is also gone — re-read the issue thread before acting."
                ),
            },
        }

    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Visible, not silent: a malformed payload means the gate did not run,
        # and a quiet `{}` here is indistinguishable from "no compaction".
        print(json.dumps({"systemMessage":
                          "compaction_marker: malformed hook payload; no marker written"}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"systemMessage":
                          "compaction_marker: hook payload was not an object"}))
        return
    print(json.dumps(handle(payload, dict(os.environ))))


if __name__ == "__main__":
    main()
