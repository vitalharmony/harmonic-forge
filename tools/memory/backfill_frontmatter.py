#!/usr/bin/env python3
"""Backfill `first_seen:` and `instances:` into every `feedback_*` memory
(harmonic-forge#500).

**Why this exists, and why it is not optional.** F493's promotion policy — a
`feedback_*` memory at 2 instances or 14 days must be promoted to a hook or an
`R-` rule and shrunk to a pointer — is enforced by `memory_lint`'s aging
check. That check reads `first_seen:` and `instances:`. Measured live against
`operator-memory@d494c39` on 2026-09-06: **zero** of the 144 `feedback_*`
files carried either field. F494 originally shipped the check with
grandfathering (report-only on a missing field), which meant every file was
grandfathered and the check gated on nothing — the `promoted:` requirement,
the `allow_unpromoted.toml` escape hatch and the ≤600-byte pointer rule were
all unreachable on delivery. This script is what makes the data exist, and
F500 inverts the grandfathering afterward so a MISSING field is itself the
finding.

**Idempotent, and it never overwrites.** A field already present is left
exactly as it is — including one a human has since corrected by hand. Re-runs
are safe and are expected: new memories get written continuously, so this is a
maintenance tool, not a one-shot migration.

## `first_seen`

The earliest ISO date evidenced anywhere in the file, excluding the `modified:`
and `originSessionId:` metadata lines. Falling back to the file's git add date
is second-best and materially worse: the whole store was seeded in one commit
on 2026-09-06, so the fallback stamps every file that hits it with that one
date. The fallback is reported per-file, never applied silently.

**Measured, not estimated: 55 of 144 files (38%) hit that fallback.** The issue
predicted ~12, from a count of files carrying any ISO date; the gap is that
several of those carry a date only in a `modified:` line, which is a
maintenance timestamp rather than incident evidence and is deliberately
excluded. For those 55 the age arm of the promotion threshold is measured from
the seed date rather than the real one — it under-reports age, so it delays
promotion rather than forcing it early, and the field is never overwritten, so
a hand correction sticks. Reported here because a third of the store carrying
an approximate date is a property worth knowing when reading Check 8's output,
not a detail to bury.

## `instances`

Parsed from the recurrence marker: line-leading enumeration headings
("**Instance 6**", "**Tenth instance**"), cardinal counts ("corrected four
times"), and uncounted recurrence verbs ("Recurred twice" -> 2). Absent a
marker, 1 — the conservative direction, because `instances >= 2` is what
triggers the promotion requirement, so guessing high manufactures obligations
nobody recorded.

**20 of 144 files carry a parseable marker.** An earlier, looser version
scored 36 and four of those were wrong: it read advice ("retry once or
**twice**"), narration of a single incident ("reported ready **twice**, before
and after") and a citation of a *different* memory's enumeration
("`[[feedback_git_er_done_bias]]` instance 4") as counts. Two of the four were
under the age threshold, so the manufactured count was the only reason each
emitted a gating promotion finding — a wrong `instances` does not just
misreport, it invents work. Hence: enumerations must be line-leading, bare
multipliers need a recurrence verb within 40 characters, and wikilinks are
stripped before counting.

**Ordinals and cardinals mean different things and are read differently.**
"the fourth occurrence" is a count of 4; "corrected four times" is also 4;
but "twice" is 2 and "the second time" is 2. They converge here, but the
regexes are kept separate so a future phrasing change breaks loudly on one
shape rather than silently mis-scoring across all of them.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_lint import memory_files, resolve_store  # noqa: E402

#: Ordinal words → the count they denote. "second occurrence" means 2 have
#: happened, not 2 more.
_ORDINALS = {
    "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12,
}

#: Cardinal words → count, for the "corrected four times" shape.
_CARDINALS = {
    "twice": 2, "thrice": 3, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}

_NOUN = r"(?:time|occurrence|instance|recurrence)s?"

#: Markdown a memory's enumeration heading may lead with: `**`, `-`, `#`,
#: or nothing.
_LEAD = r"(?m)^[ \t]*(?:[-*#>]+[ \t]*)?(?:\*\*)?"

#: "**Fourth occurrence**", "**Tenth instance**", "8th occurrence" — as a
#: LINE-LEADING heading. Enumerations in this store are written as section
#: headings; the same words mid-sentence are prose. Live false positive the
#: unanchored version produced: "don't wait to be asked a **second time**"
#: (advice) scored `feedback_lane1_relays_ae` as 2. A cardinal count
#: ("confirmed six times") is genuinely mid-sentence and keeps its own,
#: separate, unanchored patterns below.
_ORDINAL_WORD_RE = re.compile(
    rf"{_LEAD}({'|'.join(_ORDINALS)})\s+{_NOUN}\b", re.I)
_ORDINAL_DIGIT_RE = re.compile(rf"{_LEAD}(\d+)(?:st|nd|rd|th)\s+{_NOUN}\b", re.I)

#: "corrected four times", "recurred 3 times", "bitten twice".
_CARDINAL_WORD_RE = re.compile(
    rf"\b({'|'.join(_CARDINALS)})\s+{_NOUN}\b", re.I)
_CARDINAL_DIGIT_RE = re.compile(rf"\b(\d+)\s+{_NOUN}\b", re.I)

#: NOUN-then-NUMBER: "Instance 6", "instance 4", "Occurrence 3". The section
#: heading style several files use to enumerate recurrences, and the reverse
#: word order of every pattern above -- `feedback_git_er_done_bias` scored 1
#: instead of 6 until this was added, which is exactly the file F500's AC2
#: names as a spot-check. Kept as its own regex rather than an alternation
#: inside the cardinal one so a miss here is attributable to this shape.
_NOUN_FIRST_RE = re.compile(rf"{_LEAD}{_NOUN}\s+(\d+)\b", re.I)

#: A recurrence VERB within a short window before a bare multiplier. The
#: unrestricted `\b(twice|thrice)\b` this replaces matched advice and
#: narrative as readily as counts -- live false positives it produced:
#: "retry the connection check at least once or **twice**" (advice, scored 2)
#: and "`check_lane3_ready.py` reported \"ready\" — **twice**, before and
#: after" (a narrated detail of ONE incident, scored 2). Both files were
#: under the age threshold, so the manufactured count was the sole reason
#: each emitted a gating promotion finding.
_RECURRENCE_VERB = (r"corrected|recurred|repeated|happened|bitten|caught|"
                    r"missed|flagged|violated|occurred|slipped")
_BARE_MULTIPLIER_RE = re.compile(
    rf"\b(?:{_RECURRENCE_VERB})\b[^.]{{0,40}}?\b(twice|thrice)\b", re.I)

#: Uncounted recurrence language: "recurred", "recurring", "again". F500's
#: scope names "recurred" as an explicit marker, and it is — but it states no
#: number. A memory that says only "this recurred" evidences at least a second
#: occurrence, so it scores 2: the value that trips the promotion threshold,
#: which is the whole point of recording it. Scoring it 1 would file it as a
#: one-off, and scoring it higher would invent a count the prose never gave.
_UNCOUNTED_RECURRENCE_RE = re.compile(
    r"\b(recurr(?:ed|ence|ing)|re-?occurr(?:ed|ing))\b", re.I)

#: Recurrence language too loose to score, but too suggestive to ignore.
#: "again" alone is genuinely ambiguous ("read it again", "do it again"), so
#: scoring it would manufacture promotion obligations the prose never
#: recorded. Scored 1 like any other unmarked file, and REPORTED — the
#: alternative is silently filing a file that recurred as a one-off, which is
#: the inertness this whole issue exists to end. A human decides these.
_LOOSE_RECURRENCE_RE = re.compile(
    r"\b(again|repeatedly|keeps? (?:happening|recurring|doing)|"
    r"still (?:happening|doing)|another instance)\b", re.I)

_ISO_DATE_RE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")

#: `instances:` above this is almost certainly a parse error rather than a
#: real recurrence count -- the highest genuine one measured in the store is
#: 10 ("Tenth instance"). Reported, never written.
_IMPLAUSIBLE_INSTANCES = 20


def split_frontmatter(text: str) -> tuple[list[str], str] | None:
    """`(frontmatter_lines, rest)`, or None when the file has no `---` block.

    Returns the frontmatter's INNER lines (between the fences), so a caller
    appending a field does not have to find the closing fence itself.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return text[4:end].split("\n"), text[end + 5:]


def body_of(text: str) -> str:
    split = split_frontmatter(text)
    return split[1] if split else text


def has_field(frontmatter: list[str], field: str) -> bool:
    """Top-level only. A `metadata:`-nested key of the same name is a
    different fact and must not suppress the backfill of the top-level one.
    """
    return any(line.startswith(f"{field}:") for line in frontmatter)


#: `[[other_memory]] instance 4` cites ANOTHER file's enumeration. Live false
#: positive: `feedback_wait_after_asking` scored 4 by importing
#: `[[feedback_git_er_done_bias]] instance 4`. Wikilinks are stripped before
#: counting so a citation cannot lend its neighbour's count.
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")


def parse_instances(body: str) -> tuple[int, str | None]:
    """`(count, evidence)`. `evidence` is the matched phrase, or None when
    nothing matched and the count fell back to 1.

    Counts are read only from phrasing that states a recurrence of THIS
    memory's own lesson. Advice ("retry once or twice"), narration of a
    single incident ("reported ready twice, before and after") and citations
    of another memory's enumeration are all excluded -- each produced a live
    false positive whose only effect was a spurious promotion obligation.
    """
    # All counted shapes are scanned TOGETHER and the highest wins, rather
    # than returning on the first regex that matches anything. Trying them in
    # sequence scored `feedback_verify_time_before_mentioning` as 8 ("8th
    # occurrence", an ordinal) while the same file's later "Tenth instance"
    # went unread — the file records ten, and the count that matters for
    # promotion is the latest one, not the first phrasing encountered.
    body = _WIKILINK_RE.sub(" ", body)
    best: tuple[int, str] | None = None
    for regex in (_ORDINAL_WORD_RE, _ORDINAL_DIGIT_RE,
                  _CARDINAL_WORD_RE, _CARDINAL_DIGIT_RE, _NOUN_FIRST_RE):
        for match in regex.finditer(body):
            token = match.group(1).lower()
            value = (_ORDINALS.get(token) or _CARDINALS.get(token)
                     or (int(token) if token.isdigit() else 0))
            if not value or value > _IMPLAUSIBLE_INSTANCES:
                continue
            if best is None or value > best[0]:
                best = (value, match.group(0))
    if best:
        return best
    bare = _BARE_MULTIPLIER_RE.search(body)
    if bare:
        return _CARDINALS[bare.group(1).lower()], bare.group(0)
    uncounted = _UNCOUNTED_RECURRENCE_RE.search(body)
    if uncounted:
        return 2, uncounted.group(0)
    return 1, None


#: Frontmatter keys whose dates are maintenance metadata, not incident
#: evidence. `modified:` is when the file was last touched -- for a memory
#: edited long after the incident it records, taking it as `first_seen` would
#: date the lesson to its last edit.
_NON_EVIDENCE_KEYS = ("modified:", "originSessionId:")


def parse_first_seen(path: Path, text: str) -> tuple[str, str]:
    """`(date, source)` where source is `prose` or `git`.

    **Scans the description as well as the body.** F500's scope says "the
    earliest date evidenced in the body", and body-only was the first
    implementation — but several memories carry the origin date in their
    `description:` and nowhere else. `feedback_git_er_done_bias` is the live
    case: its description names 2026-07-08 (when the operator named the
    pattern) while its earliest body date is 2026-07-15, so body-only dated
    the lesson a week late. `first_seen` drives the 14-day promotion
    threshold, so under-dating delays promotion — the wrong direction to err
    in for a check whose whole purpose is to stop lessons ageing unnoticed.

    `modified:` and `originSessionId:` are excluded: they are maintenance
    metadata, and `modified:` in particular would date a memory to its last
    edit rather than its incident.
    """
    evidence = "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith(_NON_EVIDENCE_KEYS))
    dates = _ISO_DATE_RE.findall(evidence)
    if dates:
        return min(dates), "prose"
    return git_added(path), "git"


def git_added(path: Path) -> str:
    """The file's earliest commit date in its own repo, `YYYY-MM-DD`.

    Falls back to the file's mtime when the store is not a git repo (a
    fixture, a copy) rather than failing the whole run over one file.
    """
    result = subprocess.run(
        ["git", "-C", str(path.parent), "log", "--diff-filter=A",
         "--format=%ad", "--date=short", "--", path.name],
        capture_output=True, text=True, check=False)
    line = result.stdout.strip().splitlines()
    if result.returncode == 0 and line:
        return line[-1]
    import datetime
    return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()


def plan(store: Path) -> list[dict]:
    """What the run would write, without writing it."""
    rows: list[dict] = []
    for path in memory_files(store):
        if not path.name.startswith("feedback"):
            continue
        text = path.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        if split is None:
            rows.append({"file": path.name, "skip": "no frontmatter block"})
            continue
        frontmatter, _rest = split
        body = body_of(text)
        row: dict = {"file": path.name, "path": path}
        if has_field(frontmatter, "first_seen"):
            row["first_seen"] = None  # already present, untouched
        else:
            row["first_seen"], row["first_seen_source"] = parse_first_seen(path, text)
        if has_field(frontmatter, "instances"):
            row["instances"] = None
        else:
            row["instances"], row["instances_evidence"] = parse_instances(body)
            if row["instances"] == 1 and _LOOSE_RECURRENCE_RE.search(body):
                row["review"] = _LOOSE_RECURRENCE_RE.search(body).group(0)
        rows.append(row)
    return rows


def apply(rows: list[dict]) -> int:
    """Write the planned fields. Returns the number of files changed.

    Fields are appended to the END of the frontmatter block rather than
    inserted in sorted position: the existing files put `name`/`description`
    first and a nested `metadata:` mapping last, and inserting a top-level key
    after that mapping's children would silently re-parent them under
    `metadata`. Appending after the whole block is the only position that
    cannot change what any existing key means.
    """
    changed = 0
    for row in rows:
        if row.get("skip") or (row.get("first_seen") is None
                               and row.get("instances") is None):
            continue
        path: Path = row["path"]
        text = path.read_text(encoding="utf-8")
        frontmatter, rest = split_frontmatter(text)
        additions = []
        if row.get("first_seen") is not None:
            additions.append(f"first_seen: {row['first_seen']}")
        if row.get("instances") is not None:
            additions.append(f"instances: {row['instances']}")
        # Trailing blank lines inside the block would put the new key after a
        # gap; strip them so the frontmatter stays one contiguous mapping.
        while frontmatter and not frontmatter[-1].strip():
            frontmatter.pop()
        new = "---\n" + "\n".join(frontmatter + additions) + "\n---\n" + rest
        path.write_text(new, encoding="utf-8")
        changed += 1
    return changed


def report(rows: list[dict], applied: bool) -> None:
    skipped = [r for r in rows if r.get("skip")]
    wrote_first = [r for r in rows if r.get("first_seen") is not None]
    wrote_inst = [r for r in rows if r.get("instances") is not None]
    from_git = [r for r in wrote_first if r.get("first_seen_source") == "git"]
    with_evidence = [r for r in wrote_inst if r.get("instances_evidence")]
    multi = sorted((r for r in wrote_inst if (r.get("instances") or 1) > 1),
                   key=lambda r: -(r["instances"]))

    verb = "wrote" if applied else "would write"
    print(f"feedback memories scanned: {len(rows)}")
    print(f"{verb} first_seen: {len(wrote_first)} "
          f"({len(from_git)} from the git add date, the rest from prose)")
    print(f"{verb} instances:  {len(wrote_inst)} "
          f"({len(with_evidence)} from an explicit recurrence marker)")
    if multi:
        print(f"\nrecurrence markers found ({len(multi)} files with instances > 1):")
        for row in multi:
            print(f"  {row['instances']:>3}  {row['file']}"
                  f"  <- {row['instances_evidence']!r}")
    review = [r for r in rows if r.get("review")]
    if review:
        print(f"\nNEEDS REVIEW — scored `instances: 1` but the prose suggests "
              f"recurrence without stating a count ({len(review)} files). "
              "Set the field by hand where the memory really did recur; the "
              "backfill will not overwrite it:")
        for row in review:
            print(f"       {row['file']}  <- {row['review']!r}")
    if from_git:
        print(f"\nfell back to the git add date ({len(from_git)} files carry no "
              "ISO date in their prose):")
        for row in from_git:
            print(f"       {row['file']} -> {row['first_seen']}")
    for row in skipped:
        print(f"  SKIP {row['file']}: {row['skip']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, default=None,
                   help="Backfill this store instead of the resolved one.")
    p.add_argument("--apply", action="store_true",
                   help="Write the fields. Without it the run is a dry-run "
                        "report and touches nothing.")
    args = p.parse_args(argv)
    store = args.store or resolve_store()
    if not store.is_dir():
        print(f"ERROR: memory store not found: {store}", file=sys.stderr)
        return 2
    print(f"store: {store}")
    rows = plan(store)
    if args.apply:
        changed = apply(rows)
        report(rows, applied=True)
        print(f"\n{changed} file(s) changed.")
    else:
        report(rows, applied=False)
        print("\ndry run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
