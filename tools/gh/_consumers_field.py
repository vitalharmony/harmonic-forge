"""The `Consumers and Equivalents` handoff field's form check (harmonic-forge#1502).

The field names what it points at in one of two forms, each a labeled line of its own:

- one or more `Contract: <id>` lines, each id resolving in the contract index
  (`tools/registry/contracts.toml`, harmonic-forge#1501); or
- a `No index entry applies` line and a non-empty `Search: <command>` line.

It checks the FORM only. Whether the search ran and found every reader is still
pitch-inspection's check 6: a regex over free text failed both ways in two preclose
passes (harmonic-forge#838 sticky-wicket PATCH), which is why the forms are labeled
lines and never a scan for any id-shaped token (`contracts.toml` matches the id regex).

An id in another project resolves from that project's checkout and reads `unmeasured`
when the checkout or its index is absent, which passes. An id whose namespace is not
`harmonic-forge` or a project in `projects.toml` is refused: a typo such as `forge.x`
would otherwise read as `unmeasured` and pass.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_FORGE / "tools" / "registry"))
import contracts  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scenario_trace  # noqa: E402

#: A list bullet or number, surrounding whitespace and Markdown emphasis, stripped
#: before a label is read (`**Contract:** x` reads as `Contract: x`).
_LEAD = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*")
_EMPHASIS = re.compile(r"(\*\*|__|`)")
_CONTRACT = re.compile(r"^contract:\s*(.*)$", re.IGNORECASE)
_NO_ENTRY = re.compile(r"^no index entry applies\b", re.IGNORECASE)
_SEARCH = re.compile(r"^search:\s*(.*)$", re.IGNORECASE)

FORMS = ("one or more `Contract: <id>` lines naming ids in the contract index "
         "(docs/CONTRACTS.md), or a `No index entry applies` line plus a "
         "`Search: <command>` line")


def _lines(section: str) -> list[str]:
    """The section's label-bearing lines. Examples are removed with `_scenario_trace`'s own
    refusal-leaning helpers (harmonic-forge#920's ruling: shrink the parsing, never grow
    it): comments first, an unclosed `<!--` hiding the rest; then fences, matched at any
    indentation so a fence inside a list item counts, an unclosed fence hiding the rest.
    Then bullets, emphasis and inline-code backticks are stripped."""
    text = _scenario_trace.strip_comments(section)
    text = _scenario_trace.strip_fences("\n".join(line.lstrip() for line in text.split("\n")))
    return [_LEAD.sub("", _EMPHASIS.sub("", line), count=1).strip() for line in text.split("\n")]


def _known_projects(manifest: Path) -> set[str]:
    try:
        projects = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    return {item.get("name") for item in projects if isinstance(item, dict) and item.get("name")}


def _present_without_index(project: str, checkout) -> bool:
    if project == contracts.OWN_PROJECT:
        return False
    base = (checkout or contracts.project_checkout)(project)
    return base is not None and not any(
        (base / name).is_file() for name in ("contracts.toml", "tools/registry/contracts.toml"))


def consumers_refusal(section: str, *, index: Path | None = None,
                      manifest: Path | None = None, checkout=None) -> str | None:
    """A refusal message for the field's form, or None when it passes."""
    lines = _lines(section)
    ids = [m.group(1).strip().strip("`").strip() for line in lines
           if (m := _CONTRACT.match(line))]
    if ids:
        try:
            own = contracts.load(index or contracts.TOML_PATH)
        except SystemExit as exc:
            return f"Consumers and Equivalents: the contract index is unreadable ({exc})"
        known = _known_projects(manifest or _FORGE / "projects.toml") | {contracts.OWN_PROJECT}
        bad = []
        for cid in ids:
            if not contracts._ID.match(cid):
                bad.append(f"`{cid}` is not a contract id (<project>.<name>)")
            elif cid.split(".", 1)[0] not in known:
                bad.append(f"`{cid}` names no known project")
            elif contracts.resolve(cid, own, checkout=checkout) == contracts.UNRESOLVED:
                bad.append(f"`{cid}` is not in the contract index")
            elif _present_without_index(cid.split(".", 1)[0], checkout):
                # AC5 lets only an ABSENT checkout pass unmeasured; a present one with
                # no index cannot vouch for any id (sticky-wicket PATCH).
                bad.append(f"`{cid}`: that project's checkout has no contract index yet")
        if bad:
            return "Consumers and Equivalents: " + "; ".join(bad)
        return None
    no_entry = any(_NO_ENTRY.match(line) for line in lines)
    searches = [m.group(1).strip() for line in lines if (m := _SEARCH.match(line))]
    if no_entry and any(searches):
        return None
    if no_entry:
        return ("Consumers and Equivalents: `No index entry applies` needs a non-empty "
                "`Search: <command>` line naming the search that found no entry")
    return f"Consumers and Equivalents names neither accepted form: {FORMS}"
