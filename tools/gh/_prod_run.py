"""The production-run declaration an AE carries (harmonic-forge#878, R-0377).

A Tier P AE used to authorize "some production write at this SHA on this
issue": `check_lane3_ready --require-tier P` compared the sweep's tier
*ceiling* (a fail-safe for restricting) against `P`, and HRSE2's
`scripts/gate_production_run.py` then ran whatever `scripts/1-*.py`, with or
without `--apply`, its caller named. A coarse class was being used as a grant
for a specific action (F878 preclose pass 1, survivors #1-#3).

So the AE now declares the one action it authorizes, and only `l1_post.py`
can write that declaration: it is a field of the reserved attestation footer
(`<!-- l1-post v1; ... -->`), which `l1_post.reject_reserved_marker` refuses
in any body. The declaration is read only from the comment's own trailing
footer, after fenced blocks are stripped, so a quoted footer, a fenced
example, or the same words in prose never satisfy it.

Footer field (one token, no spaces, no `;`):

    prod-run=issue:<N>,sha:<40 hex>,script:scripts/1-<name>.py,apply:true|false
    prod-run=issue:<N>,sha:<40 hex>,count-label:<Label>

Command-line spec (`l1_post.py --prod-run`, `check_lane3_ready.py
--require-prod-run`), the same action without issue and SHA:

    script=scripts/1-<name>.py          (a dry run)
    script=scripts/1-<name>.py,apply    (with --apply)
    count-label=<Label>
"""
from __future__ import annotations

import re

#: A migration script directly under scripts/ -- the same shape
#: gate_production_run.py's `migration_target` accepts.
SCRIPT = re.compile(r"^scripts/1-[A-Za-z0-9._-]+\.py$")
#: A node label: an identifier, nothing a Cypher backtick could escape from.
LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

_FENCE = re.compile(r"```.*?```", re.S)
#: The comment's own trailing attestation footer -- anchored at the end of the
#: (fence-stripped) body, so an earlier quoted footer is never the one read.
_TRAILING_FOOTER = re.compile(r"<!--\s*l1-post\s+v1;([^>]*?)-->\s*\Z", re.I)
_FIELD = re.compile(r"(?:^|;)\s*prod-run=([^;\s]+)\s*(?:;|$)")


def parse_spec(spec: str) -> dict:
    """`script=<rel>[,apply]` or `count-label=<Label>` as an action dict:
    `{"script": rel, "apply": bool}` or `{"count_label": label}`. Raises
    ValueError on anything else."""
    spec = (spec or "").strip()
    if spec.startswith("script="):
        parts = spec[len("script="):].split(",")
        rel = parts[0]
        flags = parts[1:]
        if not SCRIPT.match(rel):
            raise ValueError(f"{rel!r} is not a scripts/1-*.py migration script")
        if flags not in ([], ["apply"]):
            raise ValueError(f"unknown script flag(s) {flags!r}; the only one is 'apply'")
        return {"script": rel, "apply": flags == ["apply"]}
    if spec.startswith("count-label="):
        label = spec[len("count-label="):]
        if not LABEL.match(label):
            raise ValueError(f"{label!r} is not a node label")
        return {"count_label": label}
    raise ValueError(f"{spec!r} is neither script=scripts/1-*.py[,apply] nor count-label=<Label>")


def footer_field(issue: int, sha: str, action: dict) -> str:
    """The footer token `l1_post.py` stamps on an AE for `action`."""
    if not FULL_SHA.match(sha or ""):
        raise ValueError(f"a production run names a full 40-hex SHA, not {sha!r}")
    head = f"prod-run=issue:{int(issue)},sha:{sha}"
    if "script" in action:
        return f"{head},script:{action['script']},apply:{'true' if action['apply'] else 'false'}"
    return f"{head},count-label:{action['count_label']}"


def declared(body: str) -> dict | None:
    """The `prod-run` declaration in `body`'s own trailing l1-post footer, as
    `{"issue", "sha", "script", "apply"}` or `{"issue", "sha", "count_label"}`;
    None when the footer carries none or it is malformed (fails closed)."""
    footer = _TRAILING_FOOTER.search(_FENCE.sub("", body or ""))
    if footer is None:
        return None
    field = _FIELD.search(footer.group(1))
    if field is None:
        return None
    pairs: dict[str, str] = {}
    for item in field.group(1).split(","):
        key, sep, value = item.partition(":")
        if not sep or key in pairs:
            return None
        pairs[key] = value
    try:
        issue = int(pairs.pop("issue"))
        sha = pairs.pop("sha")
    except (KeyError, ValueError):
        return None
    if not FULL_SHA.match(sha):
        return None
    if set(pairs) == {"script", "apply"} and SCRIPT.match(pairs["script"]) \
            and pairs["apply"] in ("true", "false"):
        return {"issue": issue, "sha": sha, "script": pairs["script"],
                "apply": pairs["apply"] == "true"}
    if set(pairs) == {"count-label"} and LABEL.match(pairs["count-label"]):
        return {"issue": issue, "sha": sha, "count_label": pairs["count-label"]}
    return None


def action_of(declaration: dict) -> dict:
    """The action half of a declaration, comparable to `parse_spec`'s result."""
    if "script" in declaration:
        return {"script": declaration["script"], "apply": declaration["apply"]}
    return {"count_label": declaration["count_label"]}


def describe(action: dict) -> str:
    if "script" in action:
        return f"script={action['script']}{',apply' if action['apply'] else ''}"
    return f"count-label={action['count_label']}"
