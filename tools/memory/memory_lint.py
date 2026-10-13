#!/usr/bin/env python3
"""Memory-store lint (harmonic-forge#494), ported from `HRSE2/scripts/memory_lint.py`.

WHY THIS MOVED OUT OF HRSE2
---------------------------
The original derived the store path from its own repo root:

    repo_root = Path(__file__).parent.parent.resolve()
    encoded = str(repo_root).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"

Since `autoMemoryDirectory` landed (harmonic-forge#493 Phase 0) every repo
resolves to one shared store, so that derivation names a directory nothing
reads or writes. The lint ran, reported, and exited 0 against an inert copy.

EXIT-CODE CONTRACT
------------------
Default invocation is REPORT-ONLY and exits 0. `--gate` is what turns the cap
check and Checks 1/4/5 into exit-1 conditions.

That split is the whole design, and the reason is worth stating: the store is
grown by the harness, not by any branch. A live-pinned exit-1 check is one that
no commit can turn green — `MEMORY.md` rises on its own — so `--gate` runs
against a frozen fixture and the live store is reported on but never gated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

#: `MEMORY.md` is the index, not a memory; `README.md` documents the store.
#: Neither carries memory frontmatter and neither is linked from the index, so
#: without this every check that iterates memories reports both. harmonic-forge#493's
#: own `README.md` is why the gate would otherwise be red the day it lands.
NON_MEMORY_FILES = frozenset({"MEMORY.md", "README.md"})

#: `MEMORY.md` is loaded into every session's context, so its size is a real
#: budget rather than a style preference.
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_000
WARN_FRACTION = 0.80


# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def resolve_store(repo_root: Path | None = None) -> Path:
    """`autoMemoryDirectory` if set and present, else the legacy repo-derived path.

    Only the one key is read. The rest of `settings.json` is the operator's and
    must never be printed — see the module's callers, which print the resolved
    path and nothing else from this file.

    Project-level settings files are deliberately NOT consulted. The harness
    itself may merge them; if it ever does so for this key, this resolver will
    disagree with the harness and that disagreement will be silent. It is
    recorded here rather than guarded because no project-level file sets the key
    today, and a guard against a hypothetical is a guard nobody can test.
    """
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        configured = data.get("autoMemoryDirectory")
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError, ValueError):
        configured = None
    if configured:
        candidate = Path(str(configured)).expanduser()
        if candidate.is_dir():
            return candidate
    root = (repo_root or Path(__file__).resolve().parent.parent.parent)
    encoded = str(root).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


def memory_files(store: Path) -> list[Path]:
    return sorted(f for f in store.glob("*.md") if f.name not in NON_MEMORY_FILES)


# ---------------------------------------------------------------------------
# Frontmatter parsing (no PyYAML)
# ---------------------------------------------------------------------------

#: Top-level frontmatter keys this lint reads. `first_seen`/`instances`/
#: `promoted` were added by harmonic-forge#500 for the aging check.
_TOP_LEVEL_KEYS = ("name", "description", "first_seen", "instances", "promoted", "rule_check")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level keys, plus `metadata.type` from the nested block.

    **The metadata block is exited on dedent, not held to the end of the
    frontmatter (harmonic-forge#500).** The original set `in_metadata_block`
    and never cleared it, so every line after `metadata:` was treated as one
    of its children. That was harmless while the only keys read were `name`
    and `description` — both conventionally written above `metadata:` — and
    became a real defect the moment #500 appended `first_seen:`/`instances:`
    after it: the backfill wrote them at column 0, the parser read them as
    nested, and `check_aging` reported 144 files as missing fields that were
    sitting in them. Indentation is what distinguishes the two, so
    indentation is what the parser now uses.
    """
    result: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return result
    in_metadata_block = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if not stripped:
            continue
        # A non-indented line ends the nested block, whatever it is.
        if in_metadata_block and line[:1] not in (" ", "\t"):
            in_metadata_block = False
        if stripped == "metadata:":
            in_metadata_block = True
            continue
        if in_metadata_block:
            m = re.match(r"\s+type:\s*(.+)", line)
            if m:
                result["metadata.type"] = m.group(1).strip()
            continue
        m = re.match(rf"^({'|'.join(_TOP_LEVEL_KEYS)}):\s*(.+)", stripped)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


def _body_text(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def _memory_links(text: str) -> list[str]:
    return re.findall(r"\(([^)]+\.md)\)", text)


def _index_text(store: Path) -> str:
    try:
        return (store / "MEMORY.md").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


_OVERFLOW_INDEX = re.compile(r"^INDEX-[^/]*\.md$")


def _index_texts(store: Path) -> list[tuple[str, str]]:
    """`MEMORY.md`, then each overflow `INDEX-*.md` it links (harmonic-forge#983).

    One level by decision: an `INDEX-*.md` linked only from another overflow
    index is not followed. A dead `INDEX-*.md` link is skipped here; check 2
    reports it from `MEMORY.md`'s row. Check 6 keeps `_index_text`, because
    only `MEMORY.md` is loaded every session.
    """
    root = _index_text(store)
    texts = [("MEMORY.md", root)]
    for target in dict.fromkeys(_memory_links(root)):
        path = store / target
        if _OVERFLOW_INDEX.match(target) and path.is_file():
            try:
                # errors="replace": a stray non-UTF-8 byte must not crash every
                # check; the links around it still parse (preclose pass 1).
                texts.append((target, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return texts


# ---------------------------------------------------------------------------
# Check 1 — orphan files
# ---------------------------------------------------------------------------

def check_orphans(store: Path) -> list[str]:
    linked = {t for _name, text in _index_texts(store) for t in _memory_links(text)}
    return [f"{f.name}: not linked in MEMORY.md or any linked INDEX-*.md (orphan)"
            for f in memory_files(store) if f.name not in linked]


# ---------------------------------------------------------------------------
# Check 2 — dead index links (MEMORY.md and its linked INDEX-*.md files)
# ---------------------------------------------------------------------------

def check_dead_links(store: Path) -> list[str]:
    return [f"{name} → ({t}): file does not exist"
            for name, text in _index_texts(store)
            for t in _memory_links(text) if not (store / t).exists()]


# ---------------------------------------------------------------------------
# Check 3 — stale project memories
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_STALE_KEYWORDS = re.compile(
    r"merge freeze|scheduled for|due|by \d{4}-\d{2}-\d{2}|deadline|expires", re.IGNORECASE)


def check_stale_projects(store: Path) -> list[str]:
    issues: list[str] = []
    today = date.today()
    for f in memory_files(store):
        text = f.read_text(encoding="utf-8")
        if _parse_frontmatter(text).get("metadata.type") != "project":
            continue
        body = _body_text(text)
        for m in _DATE_RE.finditer(body):
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if d < today:
                context = body[max(0, m.start() - 120):min(len(body), m.end() + 120)]
                if _STALE_KEYWORDS.search(context):
                    issues.append(f"{f.name}: date {m.group(1)} is in the past (possible stale deadline)")
                    break
    return issues


# ---------------------------------------------------------------------------
# Check 4 — broken [[slug]] references
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")

#: `[[hrse#1303]]` is a GitHub issue reference, not a memory link. Live example:
#: `user_ethical_employer_exclusions.md`. Reported in its own category and never
#: an exit-1 finding — a gate carrying a permanently unfixable finding is a gate
#: people learn to ignore.
_ISSUE_REF_RE = re.compile(r"^[\w.-]+#\d+$")


def _slug(s: str) -> str:
    """Normalize the two conventions the store actually uses.

    Filenames are `feedback_foo_bar.md`; the harness instructs sessions to write
    `name: <short-kebab-case-slug>`, so `name:` values are `feedback-foo-bar` —
    and sometimes a shortened variant of the stem. Collapsing `-` to `_` makes
    the two comparable.
    """
    return s.strip().lower().replace("-", "_")


def build_slug_index(store: Path) -> tuple[set[str], list[str]]:
    """`(known keys, collision findings)`.

    **This is an existence test against a set union, not a resolution to a
    unique file.** It never selects a file, so it cannot select the wrong one —
    which is what makes normalizing safe here and would not make it safe in a
    resolver. The collision guard exists anyway because the shortened-`name:`
    convention makes a genuine collision reachable, and a silent one would
    quietly weaken the check.
    """
    known: set[str] = set()
    owners: dict[str, list[str]] = {}
    for f in memory_files(store):
        fm = _parse_frontmatter(f.read_text(encoding="utf-8"))
        keys = {f.stem, _slug(f.stem)}
        if "name" in fm:
            keys.add(_slug(fm["name"]))
        known |= keys
        owners.setdefault(_slug(f.stem), []).append(f.name)
    collisions = [
        f"{' and '.join(sorted(names))}: normalize to the same key {key!r}"
        for key, names in sorted(owners.items()) if len(names) > 1
    ]
    return known, collisions


def check_broken_slugs(store: Path) -> tuple[list[str], list[str]]:
    """`(broken links, issue references)` — the second is report-only."""
    known, collisions = build_slug_index(store)
    broken: list[str] = list(collisions)
    issue_refs: list[str] = []
    for f in memory_files(store):
        for slug in _SLUG_RE.findall(_body_text(f.read_text(encoding="utf-8"))):
            if _ISSUE_REF_RE.match(slug.strip()):
                issue_refs.append(f"{f.name}: [[{slug}]] is an issue reference, not a memory link")
                continue
            if slug.strip() not in known and _slug(slug) not in known:
                broken.append(f"{f.name}: [[{slug}]] resolves to no memory file")
    return broken, issue_refs


# ---------------------------------------------------------------------------
# Check 5 — missing frontmatter
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("name", "description", "metadata.type")


def check_missing_frontmatter(store: Path) -> list[str]:
    issues: list[str] = []
    for f in memory_files(store):
        fm = _parse_frontmatter(f.read_text(encoding="utf-8"))
        missing = [x for x in _REQUIRED_FIELDS if x not in fm]
        if missing:
            issues.append(f"{f.name}: missing frontmatter field(s): {', '.join(missing)}")
    return issues


# ---------------------------------------------------------------------------
# Check 6 — index load cap
# ---------------------------------------------------------------------------

def index_size(store: Path) -> tuple[int, int]:
    text = _index_text(store)
    return len(text.splitlines()), len(text.encode("utf-8"))


def check_index_cap(store: Path) -> tuple[list[str], list[str]]:
    """`(over-cap findings, warnings)`. Over cap is exit-1 only under `--gate`."""
    lines, size = index_size(store)
    findings: list[str] = []
    warnings: list[str] = []
    if lines > MAX_INDEX_LINES:
        findings.append(f"MEMORY.md: {lines} lines exceeds the {MAX_INDEX_LINES}-line load cap")
    elif lines >= MAX_INDEX_LINES * WARN_FRACTION:
        warnings.append(f"MEMORY.md: {lines}/{MAX_INDEX_LINES} lines ({lines / MAX_INDEX_LINES:.0%})")
    if size > MAX_INDEX_BYTES:
        findings.append(f"MEMORY.md: {size} bytes exceeds the {MAX_INDEX_BYTES}-byte load cap")
    elif size >= MAX_INDEX_BYTES * WARN_FRACTION:
        warnings.append(f"MEMORY.md: {size}/{MAX_INDEX_BYTES} bytes ({size / MAX_INDEX_BYTES:.0%})")
    return findings, warnings


# ---------------------------------------------------------------------------
# Check 7 — staleness (report-only, never exit 1)
# ---------------------------------------------------------------------------

_MISE_TASK_RE = re.compile(r"mise run ([a-z0-9][\w-]*)")
_SCRIPT_PATH_RE = re.compile(r"\b((?:scripts|tools)/[\w/-]+\.py)\b")
_RULE_ID_RE = re.compile(r"\bR-\d{4}\b")
#: Both deleted from the boards in hrse#966.
_DEAD_BOARD_FIELDS = ("Estimate", "Priority")


def _repo_roots() -> list[Path]:
    forge = Path(__file__).resolve().parent.parent.parent
    return [forge, Path.home() / "Harmonic_Projects" / "HRSE2"]


def _corpus() -> tuple[str, set[str], list[Path]]:
    roots = _repo_roots()
    mise = "\n".join(
        (r / "mise.toml").read_text(encoding="utf-8", errors="replace")
        for r in roots if (r / "mise.toml").is_file()
    )
    # harmonic-forge#500: this repo's OWN registry is resolved relative to
    # the module, not through `Path.home()`. The absolute paths below exist
    # on the operator's machine and on no CI runner (`.github/workflows`
    # checks out under `/home/runner/work/`), so `rule_ids` came back empty
    # there — and an empty set silently disabled the `promoted:` validation
    # that Check 8 treats as an authorization input, letting any string act
    # as a promotion marker. The sibling paths are kept as a best-effort
    # extra source; the in-repo one is what makes the check work anywhere.
    registries = [Path(__file__).resolve().parents[1] / "rules" / "registry.toml",
                  Path.home() / "harmonic-forge/tools/rules/registry.toml",
                  Path.home() / "Harmonic_Projects/HRSE2/.claude/rules/registry.toml"]
    rule_ids: set[str] = set()
    for reg in registries:
        if reg.is_file():
            rule_ids |= set(_RULE_ID_RE.findall(reg.read_text(encoding="utf-8", errors="replace")))
    return mise, rule_ids, roots


def check_staleness(store: Path) -> list[str]:
    """Four literal shapes only. Best-effort and deliberately not generalized —
    a broader matcher would produce the 119/120 false-positive rate this issue
    exists to fix, in a new place."""
    mise_text, rule_ids, roots = _corpus()
    findings: list[str] = []
    for f in memory_files(store):
        body = _body_text(f.read_text(encoding="utf-8"))
        for task in set(_MISE_TASK_RE.findall(body)):
            if f"[tasks.{task}]" not in mise_text:
                findings.append(f"{f.name}: `mise run {task}` is in no mise.toml")
        for rel in set(_SCRIPT_PATH_RE.findall(body)):
            if not any((r / rel).is_file() for r in roots):
                findings.append(f"{f.name}: `{rel}` exists in neither repo")
        for rid in set(_RULE_ID_RE.findall(body)):
            if rule_ids and rid not in rule_ids:
                findings.append(f"{f.name}: `{rid}` is in neither registry")
        for field in _DEAD_BOARD_FIELDS:
            if re.search(rf"\b{field}\b field", body):
                findings.append(f"{f.name}: names the retired board field `{field}` (hrse#966)")
    return findings


# ---------------------------------------------------------------------------
# Check 8 — lesson aging and promotion (harmonic-forge#500)
# ---------------------------------------------------------------------------

#: A `feedback_*` memory reaching either threshold must be promoted to a rule
#: or hook and shrunk to a pointer (harmonic-forge#493's policy).
PROMOTION_INSTANCES = 2
PROMOTION_AGE_DAYS = 14

#: A memory carrying `promoted:` is a pointer to the rule that now holds the
#: lesson, not the incident log it replaced. The size cap is what makes the
#: promotion real rather than nominal -- a 2 KB "pointer" is the original file
#: with a header bolted on, and it still costs its full weight in every
#: session that loads the store.
MAX_PROMOTED_BYTES = 600

_ALLOW_UNPROMOTED = Path(__file__).resolve().parent / "allow_unpromoted.toml"


def _allow_unpromoted() -> dict[str, str]:
    """`{filename: reason}` for files exempted from the promotion rule.

    Hand-maintained and deliberately not auto-populated: an exemption with no
    stated reason is indistinguishable from an oversight, which is the state
    this whole check exists to end. Parsed with a two-line reader rather than
    tomllib so the file's shape stays obvious to a human editing it under
    time pressure.
    """
    if not _ALLOW_UNPROMOTED.is_file():
        return {}
    allowed: dict[str, str] = {}
    for line in _ALLOW_UNPROMOTED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        allowed[key.strip().strip('"')] = value.strip().strip('"')
    return allowed


_GATE_KEYS = ("type", "rule_check")
RULE_CHECK_FORM = re.compile(r"^(?:R-\d{4}|none\s*\(\s*searched:\s*[^)\s][^)]*\))$", re.IGNORECASE)


class GateFrontmatter:
    """The two fields the #981 gate decides on, read by a strict grammar.

    Reforged twice (sticky-wicket, harmonic-forge#981): regex readers let
    through every form they did not recognize, and a scope-blind reader read a
    nested key as the gate's. Frontmatter is now accepted only in the two-level
    `key: value` shape every memory in the store already uses (verified against
    all 367 on 2026-10-13): a top-level `key:` or `key: value` line, or a
    two-space-indented `key: value` under a top-level `key:` with no value.
    Anything else (a quoted key, a list item, deeper indentation, a block
    scalar, unclosed frontmatter) is an error, and so is a key seen twice in
    the same scope.

    `type` is read at the top level or directly under `metadata:`, with an
    inline ` #` comment and surrounding quotes stripped. `rule_check` is read at
    the top level only, verbatim but for surrounding quotes, and must be
    `R-NNNN` or `none (searched: <terms>)`.
    """

    _TOP = re.compile(r"^([A-Za-z_]+):(?: (.*))?$")
    _CHILD = re.compile(r"^  ([A-Za-z_]+):(?: (.*))?$")

    def __init__(self, text: str) -> None:
        self.values: dict[str, str] = {}
        self.errors: list[str] = []
        self.has_frontmatter = False
        self.frontmatter_lines = 0
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return
        body: list[str] = []
        for line in lines[1:]:
            if line.strip() == "---":
                self.has_frontmatter = True
                break
            body.append(line)
        if not self.has_frontmatter:
            self.errors.append("frontmatter opened with `---` is never closed")
            return
        self.frontmatter_lines = len(body) + 2
        seen: set[tuple[str, str]] = set()
        parent: str | None = None
        for line in body:
            if not line.strip():
                continue
            top, child = self._TOP.match(line), self._CHILD.match(line)
            if top:
                key, raw = top.group(1), (top.group(2) or "").strip()
                parent = key if not raw else None
                scope = ""
            elif child and parent is not None:
                key, raw = child.group(1), (child.group(2) or "").strip()
                scope = parent
            else:
                self.errors.append(f"frontmatter line {line.strip()[:40]!r} is outside the `key: value` grammar")
                continue
            if (scope, key) in seen:
                self.errors.append(f"`{key}:` appears more than once")
                continue
            seen.add((scope, key))
            if key == "type" and scope in ("", "metadata"):
                value = re.sub(r"\s+#.*$", "", raw).strip().strip("\"'").strip()
                if not value or value.startswith("["):
                    self.errors.append(f"`type:` has no single value ({raw!r})")
                elif "type" in self.values:
                    self.errors.append("`type:` appears more than once")
                else:
                    self.values["type"] = value
            elif key == "rule_check" and scope == "":
                value = raw.strip().strip("\"'").strip()
                if not RULE_CHECK_FORM.match(value):
                    self.errors.append(f"`rule_check: {value}` is neither `R-NNNN` nor `none (searched: <terms>)`")
                else:
                    self.values["rule_check"] = value

    @property
    def rule_check(self) -> str | None:
        return self.values.get("rule_check")

    @property
    def is_feedback_type(self) -> bool:
        return self.values.get("type", "").lower() == "feedback"


def is_feedback_memory(name: str, text: str) -> bool:
    """harmonic-forge#981: a `feedback_*.md` filename, or a frontmatter `type`
    of feedback (read by `GateFrontmatter`)."""
    if name.startswith("feedback_") and name.endswith(".md"):
        return True
    return GateFrontmatter(text).is_feedback_type


#: harmonic-forge#981: the feedback memories that predate the `rule_check:` gate.
FEEDBACK_ALLOWLIST = Path(__file__).resolve().parent / "feedback_allowlist.txt"


def _allowlisted() -> set[str]:
    try:
        lines = FEEDBACK_ALLOWLIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def check_rule_check(store: Path) -> list[str]:
    """Check 9 (report-only, harmonic-forge#981): a feedback memory with no
    `rule_check:` that is not in the committed allowlist.

    `gate_feedback_memory_write.py` refuses such a file at a Write/Edit; this
    catches what that hook cannot see: a Bash or Codex write, a type flipped
    after creation, or a write from a repo that does not wire the hook.
    """
    allowed = _allowlisted()
    findings: list[str] = []
    for f in memory_files(store):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f.name in allowed:
            continue
        gate = GateFrontmatter(text)
        if gate.errors:
            findings.append(f"{f.name}: malformed gate frontmatter: {'; '.join(gate.errors)}")
        elif is_feedback_memory(f.name, text) and not gate.rule_check:
            findings.append(f"{f.name}: feedback memory with no `rule_check:` and not in "
                            "feedback_allowlist.txt (written around the #981 gate?)")
    return findings


def check_aging(store: Path, today: date | None = None) -> list[str]:
    """Feedback memories that are due for promotion, or cannot be assessed.

    **The grandfathering is INVERTED relative to how harmonic-forge#494 first
    specified it, and that inversion is the entire point of this check
    existing.** #494 would have reported-only on a missing `first_seen:` and
    gated only files that had it. Measured live on 2026-09-06: **zero** of the
    144 `feedback_*` files carried the field, so every one was grandfathered
    and the check gated on nothing -- the `promoted:` requirement, the
    `allow_unpromoted.toml` hatch and the pointer-size rule were all
    unreachable on delivery. Nothing in the #493 tree ever populated the
    fields either, so it would have stayed inert rather than temporarily so.

    Here, a MISSING field is itself the finding. `backfill_frontmatter.py`
    populates them; after that, a file without them is a new memory written
    without them, which is exactly what should be caught.
    """
    today = today or date.today()
    allowed = _allow_unpromoted()
    _mise, rule_ids, _roots = _corpus()
    findings: list[str] = []

    for f in memory_files(store):
        if not f.name.startswith("feedback"):
            continue
        text = f.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)

        missing = [k for k in ("first_seen", "instances") if k not in fm]
        if missing:
            findings.append(
                f"{f.name}: missing {', '.join(missing)} — run "
                "`tools/memory/backfill_frontmatter.py --apply`")
            continue

        try:
            instances = int(str(fm["instances"]).strip())
        except ValueError:
            findings.append(f"{f.name}: instances: {fm['instances']!r} is not a number")
            continue
        try:
            first_seen = date.fromisoformat(str(fm["first_seen"]).strip())
        except ValueError:
            findings.append(f"{f.name}: first_seen: {fm['first_seen']!r} is not a date")
            continue

        promoted = fm.get("promoted", "").strip()
        if promoted:
            # Checked BEFORE the threshold test: a promoted file is over the
            # threshold by definition (that is why it was promoted), so
            # testing the threshold first would report every promoted file.
            if promoted not in rule_ids and rule_ids:
                findings.append(
                    f"{f.name}: promoted: {promoted} is in neither rule registry")
            size = len(text.encode("utf-8"))
            if size > MAX_PROMOTED_BYTES:
                findings.append(
                    f"{f.name}: carries promoted: {promoted} but is {size} bytes "
                    f"(> {MAX_PROMOTED_BYTES}) — a promoted memory is a pointer, "
                    "shrink it in the same commit that sets the marker")
            continue

        age = (today - first_seen).days
        over = []
        if instances >= PROMOTION_INSTANCES:
            over.append(f"instances={instances}")
        if age >= PROMOTION_AGE_DAYS:
            over.append(f"{age}d old")
        # `allowed.get(...)` not `in allowed`: an entry with an EMPTY reason
        # must not exempt. F500 requires a non-empty reason, and the file's
        # own header says why -- an exemption with no stated reason is
        # indistinguishable from an oversight, which is the state this check
        # exists to end. Caught by its own test; the first implementation
        # checked membership alone and silently honoured `file = ""`.
        if over and not allowed.get(f.name, "").strip():
            findings.append(
                f"{f.name}: {' and '.join(over)} — needs `promoted: R-NNNN` or "
                "an entry in tools/memory/allow_unpromoted.toml with a reason")
    return findings


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(store: Path, gate: bool, today: date | None = None) -> int:
    print(f"store: {store}")
    if not store.is_dir():
        print(f"ERROR: memory store not found: {store}", file=sys.stderr)
        return 2

    broken, issue_refs = check_broken_slugs(store)
    cap_findings, cap_warnings = check_index_cap(store)
    gating: list[tuple[str, list[str]]] = [
        ("Check 1 — orphan files", check_orphans(store)),
        ("Check 2 — dead index links", check_dead_links(store)),
        ("Check 4 — broken [[slug]] refs", broken),
        ("Check 5 — missing frontmatter", check_missing_frontmatter(store)),
        ("Check 6 — index load cap", cap_findings),
        ("Check 8 — lesson aging and promotion", check_aging(store, today)),
    ]
    advisory: list[tuple[str, list[str]]] = [
        ("Check 3 — stale project memories", check_stale_projects(store)),
        ("Check 6 — index load cap (warning)", cap_warnings),
        ("Check 4 — issue references (report-only)", issue_refs),
        ("Check 7 — staleness (report-only)", check_staleness(store)),
        ("Check 9 — feedback without rule_check (report-only)", check_rule_check(store)),
    ]

    for tag, group in (("FAIL", gating), ("note", advisory)):
        for label, items in group:
            if not items:
                print(f"  ok   {label}")
                continue
            print(f"  {tag} {label}")
            for item in items:
                print(f"         {item}")
    total = sum(len(items) for _, items in gating)

    lines, size = index_size(store)
    print(f"\nindex: {lines}/{MAX_INDEX_LINES} lines, {size}/{MAX_INDEX_BYTES} bytes"
          f" | memories: {len(memory_files(store))}")
    if total == 0:
        print("no gating findings.")
    else:
        print(f"{total} gating finding(s).")
    return 1 if (gate and total) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, default=None,
                   help="Lint this store instead of the resolved one (tests, fixtures).")
    p.add_argument("--today", type=date.fromisoformat, default=None,
                   help="Treat this ISO date as today (harmonic-forge#500). "
                        "Fixture runs MUST pin it: a fixture carrying an "
                        "absolute `first_seen:` silently crosses the 14-day "
                        "threshold on a future date and turns the repo's own "
                        "commit gate red with no code change.")
    p.add_argument("--gate", action="store_true",
                   help="Exit 1 on gating findings. Without it the run is report-only "
                        "and exits 0 — see the module docstring for why the live store "
                        "is never gated.")
    args = p.parse_args(argv)
    return run(args.store or resolve_store(), args.gate, args.today)


if __name__ == "__main__":
    sys.exit(main())
