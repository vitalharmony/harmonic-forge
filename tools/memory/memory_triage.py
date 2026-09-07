#!/usr/bin/env python3
"""Classify every feedback memory against the rule registries (harmonic-forge#495).

**What this is for.** Promotion out of memory into rules and hooks has only ever
happened as a manual campaign (hrse#458, F385-F389). The classification is the
expensive part and it was redone from scratch each time, which is why the store
reached 238 files before anyone looked. This makes the mechanical half
repeatable so the human half is the only part that costs anything.

**The split, and why it is drawn here.** This script emits a *mechanical
pre-classification*: everything decidable from the file's own frontmatter, its
text, and the two rule registries. It deliberately does NOT decide the cases
that need judgment -- whether a lesson is genuinely the same lesson as an
existing rule, what two lines of rule text should say, which section they
belong in. Those live in `skills/memory-triage/SKILL.md`, which a session reads
and applies to this script's output.

**Measured against the human audit, this agrees on 38% of 208 files** -- and
that number is the point, not a defect to be tuned away. `--audit` reproduces
it on demand. The disagreement is almost entirely judgment, in two places:

| human -> mechanical | n | why |
|---|---|---|
| DUP -> FOLD / FOLD+HOOK | 47 | the audit's DUP means "a reader judged this already a rule"; here it means "a `promoted:` marker resolves", which is 0 until harmonic-forge#496 writes them |
| LOCAL -> FOLD / STATE | 32 | the audit reads content; this reads `metadata.type` |
| FOLD -> FOLD+HOOK | 18 | mentioning a command is not the same as the lesson's TRIGGER being a command |

An earlier draft of this docstring asserted the audit did not exist on disk and
substituted a 6-row fixture whose counts the code passed by construction. It
does exist, it is vendored at `testdata/triage/audit_2026_09_06.md`, and a
regression check that cannot fail is worse than none.

**Read-only, everywhere, by construction.** No function here opens a file for
writing and none shells out to `gh`. The report goes to stdout; redirecting it
is the caller's business (AC4).

## The classes

| class | meaning | disposition |
|---|---|---|
| `DUP` | the lesson is already stated as a rule | shrink to a pointer at that `R-` ID |
| `FOLD` | a real recurring lesson with no rule | write rule text into a named file |
| `FOLD+HOOK` | a FOLD whose text NAMES an operational trigger | candidate for a hook -- verify, do not assume |
| `STALE` | names something that no longer exists | delete, with the evidence |
| `LOCAL` | operator-specific context, not a general lesson | keep in memory |
| `STATE` | ongoing work state rather than a lesson | keep in memory; not promotable |

**`FOLD+HOOK` is a signal, not a verdict**, for the same reason `DUP` is
restricted to a resolving marker. It fires when the memory's text names a tool
call, which is necessary for hook-enforceability and nowhere near sufficient: a
memory saying "run `mise run hygiene` to check" mentions a command without its
own trigger being one. The human audit judged 6 of the corpus hook-enforceable
where this flags 46, so treat the class as a shortlist to read, and expect to
demote most of it.

`LOCAL` and `STATE` are separated on purpose even though both mean "keep". A
`STATE` row is expected to go out of date and be deleted later; a `LOCAL` row is
expected to stay forever. Collapsing them would make the store's growth look
permanent when most of it is not.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_lint import (  # noqa: E402
    _body_text,
    _parse_frontmatter,
    _repo_roots,
    memory_files,
    resolve_store,
)

#: Recurrences at or above this file under the filing bar's test 2. Below it, a
#: FOLD is still reported but marked "batch", not "file" -- a true observation
#: nobody will action is not a reason to file (universal-agent.md).
FILE_THRESHOLD = 2

#: How many ranked rule candidates each FOLD row carries.
#:
#: **There is deliberately no similarity THRESHOLD here, and adding one would
#: be a regression.** Measured over the live store (145 feedback memories x 261
#: rules): the IDF-weighted scores run 0.13 at the median to 0.24 at the top,
#: with no gap anywhere in between. The ranking is genuinely useful — the top
#: hits are real matches — but there is no cutoff that separates "already a
#: rule" from "merely about the same subject", because the corpus is 261 rules
#: about one protocol and everything is about the same subject.
#:
#: A cutoff picked anyway would produce a confident DUP verdict with an
#: accuracy nobody measured. The first cut did exactly that and called 103 of
#: 209 memories duplicates, including a scheduling-link memory against an
#: AE-sweep rule. So the score ranks and the human decides, and the only
#: verdict this script asserts as DUP is the one that is mechanically certain:
#: an explicit `promoted:` marker resolving to a real rule.
CANDIDATES = 3

#: Words carried by almost every rule and every memory alike. Left in, they
#: dominate the overlap score and make everything look like a duplicate of
#: everything.
_STOP = frozenset("""
a an and are as at be been before but by can do does for from had has have if in
into is it its la must never no not of on once only or so than that the their
then there these they this to until up use used using was what when where which
while who why will with without you your always also any all each every more
most other same such take takes taken over under after again both because been
""".split())

_WORD_RE = re.compile(r"[a-z0-9_]+")
_RULE_BLOCK_RE = re.compile(
    r"<!--\s*(R-\d{4})\s*-->(.*?)<!--\s*/\1\s*-->", re.S)

#: A memory whose trigger is a tool call is hook-enforceable. These are the
#: shapes that have actually produced hooks in this repo, not a guess at what
#: might: a command, a git operation, a file write, a GitHub mutation.
#
# **Every alternative must carry an operational qualifier.** A bare `\bmerge\b`
# was here and fired on prose with no merge operation in it -- a branch name
# containing "merge", a parenthetical "(merge, close, push)" in a sentence
# saying those are NOT the subject, "my own recap of the merge". It alone
# produced 9 of 56 FOLD+HOOK rows, each of which the report then told a session
# to invent a hook trigger for. The human audit classed 6 memories as
# hook-enforceable; anything near 56 is the regex talking, not the corpus.
_HOOKABLE = (
    r"\bgh (?:issue|pr|api|project)\b",
    r"\bgit (?:push|commit|merge|checkout|rebase)\b",
    r"\bmise run\b",
    r"\bclos(?:e|es|ed|ing) (?:the )?(?:issue|pr)\b",
    r"\bmerg(?:e|es|ed|ing) (?:the )?(?:pr|branch|it)\b",
    r"\bwrite to\b",
    r"\bedit(?:ing|s)? \S+\.(?:py|md|toml|ts|tsx)\b",
    r"\bnever (?:write|commit|push|edit)\b",
    r"\bbefore (?:posting|committing|filing|merging)\b",
)
_HOOKABLE_RE = re.compile("|".join(_HOOKABLE), re.I)

#: Backticked paths and flags a memory asserts exist. Used for STALE evidence.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_PATHISH_RE = re.compile(r"^[\w./-]+\.(?:py|md|toml|ts|tsx|json|yml|yaml|sh)$")


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower())
            if w not in _STOP and len(w) > 2}


def build_idf(rules: list["Rule"]) -> dict[str, float]:
    """Inverse document frequency of each word across the rule corpus.

    **This is the correction that makes the DUP class mean anything.** The
    first cut scored plain containment and called 103 of 209 memories
    duplicates, including `feedback_always_include_scheduling_link` against a
    rule about AE sweeps. The cause is that every rule and every memory here
    share one large protocol vocabulary — lane, issue, gate, post, handoff,
    branch — so two texts about entirely different things still overlap
    heavily on words that distinguish nothing.

    Weighting by IDF makes a word count for less the more rules contain it. A
    word in 200 of 261 rules contributes almost nothing; `scheduling` in one
    rule contributes almost all of the score. Removing them by hand instead
    would mean maintaining a stopword list against a corpus that grows every
    week, and getting it wrong in the silent direction.
    """
    import math

    total = len(rules) or 1
    freq: dict[str, int] = {}
    for rule in rules:
        for word in rule.words:
            freq[word] = freq.get(word, 0) + 1
    return {w: math.log(total / (1 + n)) for w, n in freq.items()}


#: A word never seen in any rule is maximally distinctive, but it is also
#: unverifiable — it may just be a typo. Capped rather than unbounded.
_UNSEEN_IDF = 2.0

#: Below this many shared words, a high score is an artifact of two short
#: texts, not evidence of duplication.
MIN_SHARED_WORDS = 4


def _overlap(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """IDF-weighted containment of the memory in the rule.

    Containment (divide by the memory's own weight, not the union) is still
    the right shape — a two-line memory genuinely restating one clause of a
    long rule IS a duplicate, and true Jaccard would score that near zero. The
    fix for the false positives is the weighting, not the denominator.
    """
    if not a or not b:
        return 0.0
    shared = a & b
    if len(shared) < MIN_SHARED_WORDS:
        return 0.0
    weight = sum(idf.get(w, _UNSEEN_IDF) for w in a)
    if weight <= 0:
        return 0.0
    return sum(idf.get(w, _UNSEEN_IDF) for w in shared) / weight


@dataclass
class Rule:
    rule_id: str
    file: str
    text: str
    words: set[str] = field(default_factory=set)


def load_rules(roots: list[Path] | None = None) -> list[Rule]:
    """Every `<!-- R-NNNN -->`-delimited rule body across both repos.

    Read from the rule FILES, not from `registry.toml`. The registry records
    each rule's id, file and a hash; the statement itself only exists in the
    markdown, and the statement is what a memory would be a duplicate of.
    """
    rules: list[Rule] = []
    seen: set[str] = set()
    explicit = roots is not None
    # `testdata` is skipped only when scanning DISCOVERED roots, where fixture
    # rules would contaminate the real corpus. When a caller names a root
    # explicitly it is asking for exactly that directory, and skipping it there
    # loaded zero rules while still returning successfully -- every verdict
    # then silently degraded to "no rule shares enough vocabulary", and a
    # `promoted:` marker pointing at a real fixture rule was reported STALE.
    # Caught by the fixture on its first run.
    skip = {".git", "node_modules"} if explicit else {".git", "node_modules", "testdata"}
    for root in (roots if explicit else _repo_roots()):
        for path in sorted(root.rglob("*.md")):
            if any(part in skip for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "<!-- R-" not in text:
                continue
            for match in _RULE_BLOCK_RE.finditer(text):
                rule_id = match.group(1)
                if rule_id in seen:
                    continue
                seen.add(rule_id)
                body = match.group(2).strip()
                rules.append(Rule(rule_id, str(path), body, _words(body)))
    return rules


@dataclass
class Row:
    name: str
    path: Path
    kind: str
    instances: int
    first_seen: str
    promoted: str
    verdict: str
    evidence: str
    target: str = ""
    candidates: list[tuple[float, str, str]] = field(default_factory=list)
    #: A cited path that resolves nowhere, on a row too recurrent to delete.
    stale_note: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict, "type": self.kind,
                "instances": self.instances, "first_seen": self.first_seen,
                "promoted": self.promoted, "target": self.target,
                "evidence": self.evidence, "stale_note": self.stale_note,
                "candidates": [{"score": s, "rule": r, "file": f}
                               for s, r, f in self.candidates]}


def _stale_evidence(body: str, roots: list[Path]) -> str:
    """A backticked path the memory asserts, which exists in no repo.

    Only path-shaped tokens are checked. A memory citing `--gate` or
    `resolve_store()` is asserting an interface, and this cannot tell a removed
    interface from a renamed one without parsing every repo -- reporting those
    as stale would bury the real cases in false ones.
    """
    for token in _BACKTICK_RE.findall(body):
        token = token.strip()
        if not _PATHISH_RE.match(token):
            continue
        if any((root / token).exists() or list(root.rglob(Path(token).name))
               for root in roots):
            continue
        return f"cites `{token}`, which exists in no repo"
    return ""


def classify(path: Path, rules: list[Rule], roots: list[Path],
             idf: dict[str, float]) -> Row:
    """One memory file's mechanical verdict.

    Order matters and is deliberate. `STATE`/`LOCAL` are decided from the
    declared type BEFORE any text analysis, because a `project` memory that
    happens to share vocabulary with a rule is still not a lesson, and letting
    the overlap score reach it would file dispositions against work notes.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = _parse_frontmatter(text)
    body = _body_text(text)
    kind = fm.get("metadata.type", "")
    try:
        instances = int(fm.get("instances", "") or 0)
    except ValueError:
        instances = 0
    row = Row(name=path.stem, path=path, kind=kind, instances=instances,
              first_seen=fm.get("first_seen", ""), promoted=fm.get("promoted", ""),
              verdict="", evidence="")

    if kind == "project":
        row.verdict, row.evidence = "STATE", "declared type: project — work state, not a lesson"
        return row
    if kind in {"reference", "user"}:
        row.verdict = "LOCAL"
        row.evidence = f"declared type: {kind} — operator context, not promotable"
        return row

    if row.promoted:
        known = {r.rule_id for r in rules}
        if row.promoted in known:
            row.verdict, row.target = "DUP", row.promoted
            row.evidence = f"already promoted to {row.promoted}"
        else:
            row.verdict, row.target = "STALE", row.promoted
            row.evidence = f"claims promotion to {row.promoted}, which is in no registry"
        return row

    # The store itself is a search root: a memory citing a sibling memory
    # file by name is citing something that exists, and reporting those as
    # stale put 1 of the first 6 STALE rows in the wrong class.
    stale = _stale_evidence(body, roots + [path.parent])
    if stale and row.instances < FILE_THRESHOLD:
        row.verdict, row.evidence = "STALE", stale
        return row

    mem_words = _words(body + " " + fm.get("description", ""))
    scored = sorted(((_overlap(mem_words, r.words, idf), r) for r in rules),
                    key=lambda pair: -pair[0])[:CANDIDATES]
    row.candidates = [(round(score, 3), r.rule_id, Path(r.file).name)
                      for score, r in scored if score > 0]

    row.verdict = "FOLD+HOOK" if _HOOKABLE_RE.search(body) else "FOLD"
    # A recurring lesson is NOT deleted because one artifact it cites was
    # renamed. `feedback_verify_live_not_source` (3 recurrences, the strongest
    # in the set) was classed STALE and routed to "verify, then delete" over a
    # renamed component, while the lesson itself -- verify live behavior, not
    # source -- is entirely current. The citation is still worth flagging, so
    # it rides along in the evidence instead of deciding the verdict.
    row.stale_note = stale
    if row.candidates:
        # The file, not the rule, is the actionable half: the ranking is
        # reliable about neighbourhood and unreliable about identity, so it
        # names where the rule text would go and leaves whether it is already
        # there to the reader.
        row.target = row.candidates[0][2]
        row.evidence = "nearest: " + ", ".join(
            f"{rid} {score}" for score, rid, _f in row.candidates)
    else:
        row.evidence = "no rule shares enough distinctive vocabulary to rank"
    return row


def triage(store: Path, rules: list[Rule], roots: list[Path]) -> list[Row]:
    idf = build_idf(rules)
    return [classify(p, rules, roots, idf) for p in memory_files(store)]


#: Rendered order. `FOLD+HOOK` and `FOLD` lead because they are the only
#: classes that produce work; `LOCAL`/`STATE` are the "nothing to do" tail and
#: putting them first would bury the actionable rows under the majority.
_ORDER = ("FOLD+HOOK", "FOLD", "DUP", "STALE", "LOCAL", "STATE")


def render(rows: list[Row]) -> str:
    counts = {v: 0 for v in _ORDER}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    out = ["# memory triage", "",
           f"{len(rows)} memories. "
           + ", ".join(f"{v} {counts.get(v, 0)}" for v in _ORDER), ""]
    for verdict in _ORDER:
        group = [r for r in rows if r.verdict == verdict]
        if not group:
            continue
        out += [f"## {verdict} ({len(group)})", ""]
        if verdict in {"FOLD", "FOLD+HOOK"}:
            out.append("| memory | inst | action | target | evidence |")
            out.append("|---|---|---|---|---|")
            for row in sorted(group, key=lambda r: -r.instances):
                action = "file" if row.instances >= FILE_THRESHOLD else "batch"
                note = f" ⚠ {row.stale_note}" if row.stale_note else ""
                out.append(f"| `{row.name}` | {row.instances} | {action} "
                           f"| {row.target or '—'} | {row.evidence}{note} |")
        else:
            out.append("| memory | evidence |")
            out.append("|---|---|")
            for row in sorted(group, key=lambda r: r.name):
                out.append(f"| `{row.name}` | {row.evidence} |")
        out.append("")
    return "\n".join(out)


#: The human audit this classifier is measured against, vendored so the
#: comparison is reproducible and hermetic. It is a REFERENCE, never an
#: expected value: reproducing it would require reproducing judgment, which
#: this script deliberately does not attempt.
AUDIT_FIXTURE = Path(__file__).resolve().parent / "testdata" / "triage" / "audit_2026_09_06.md"

_AUDIT_VALID = frozenset(_ORDER)


def parse_audit(path: Path) -> dict[str, str]:
    """`{memory name: class}` from the audit's per-file table.

    Tolerant by design -- rows carrying a qualifier ("LOCAL-legitimate") keep
    their base class, and any row whose class column is not one of the six is
    skipped rather than guessed at. A strict parser here would silently drop
    the comparison to a handful of rows and report high agreement over them.
    """
    table: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name = re.sub(r"[`\s]", "", cells[0]).removesuffix(".md")
        klass = cells[2].strip().upper().replace("\u2014", "-").split("-")[0].strip()
        if name and klass in _AUDIT_VALID:
            table[name] = klass
    return table


def audit_report(rows: list[Row], audit: dict[str, str]) -> str:
    """Per-class agreement against the human audit, and the confusion pairs."""
    mine = {r.name: r.verdict for r in rows}
    common = sorted(set(mine) & set(audit))
    if not common:
        return "audit comparison: no overlapping files — nothing to compare."
    agree = sum(1 for n in common if mine[n] == audit[n])
    out = [f"audit comparison — {len(common)} files in both, "
           f"{agree} agree ({agree * 100 // len(common)}%)", "",
           "| class | audit | mechanical | delta |", "|---|---|---|---|"]
    for verdict in _ORDER:
        h = sum(1 for n in common if audit[n] == verdict)
        m = sum(1 for n in common if mine[n] == verdict)
        out.append(f"| {verdict} | {h} | {m} | {m - h:+d} |")
    pairs: dict[tuple[str, str], int] = {}
    for name in common:
        if audit[name] != mine[name]:
            key = (audit[name], mine[name])
            pairs[key] = pairs.get(key, 0) + 1
    out += ["", "Largest disagreements (audit → mechanical):"]
    for (h, m), count in sorted(pairs.items(), key=lambda kv: -kv[1])[:5]:
        out.append(f"  {count:3}  {h} → {m}")
    out += ["", "This is a REFERENCE, not a target. The audit classifies by "
                "reading each memory; this classifies by frontmatter and text "
                "shape. Closing the gap means reproducing judgment, which is "
                "the reader's job (see SKILL.md step 2)."]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Classify feedback memories (harmonic-forge#495).")
    p.add_argument("--store", type=Path, default=None,
                   help="Memory store (default: the configured shared store).")
    p.add_argument("--repo-root", type=Path, action="append", default=None,
                   help="Repo to read rules from. Repeatable. Default: both.")
    p.add_argument("--json", action="store_true", help="Machine-readable rows.")
    p.add_argument("--counts", action="store_true",
                   help="Only the class counts.")
    p.add_argument("--audit", nargs="?", const=str(AUDIT_FIXTURE), default=None,
                   metavar="PATH",
                   help="Compare against the human audit (default: the vendored "
                        "2026-09-06 one) and print per-class agreement.")
    args = p.parse_args(argv)

    store = args.store or resolve_store()
    if not store.is_dir():
        print(f"memory_triage: no store at {store}", file=sys.stderr)
        return 2
    roots = args.repo_root or _repo_roots()
    rows = triage(store, load_rules(roots), roots)

    if args.audit:
        print(audit_report(rows, parse_audit(Path(args.audit))))
    elif args.counts:
        counts = {v: sum(1 for r in rows if r.verdict == v) for v in _ORDER}
        print(json.dumps({"total": len(rows), "counts": counts}, indent=2))
    elif args.json:
        print(json.dumps([r.as_dict() for r in rows], indent=2))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
