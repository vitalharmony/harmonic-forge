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

harmonic-forge#1026: HRSE2's runner starts a script with every inherited
`NEO4J_*` cleared and `HRSE_ENV_FILE` set, passing `--apply` or nothing. So a
`script=` AE is refused unless the named script reads its graph through
`_migration_env.ensure_env()`, never calls `load_dotenv()`, defines `--apply`,
and has no required or positional argument (`script_contract_violations`).
"""
from __future__ import annotations

import ast

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


def raw_field(body: str) -> str | None:
    """The literal `prod-run=...` token from `body`'s own trailing l1-post
    footer, or None. What the body digest covers alongside the body text."""
    footer = _TRAILING_FOOTER.search(_FENCE.sub("", body or ""))
    field = _FIELD.search(footer.group(1)) if footer else None
    return f"prod-run={field.group(1)}" if field else None


def covered_text(prefix: str, field: str | None) -> str:
    """The text an l1-post `body-sha256` is computed over (harmonic-forge#878
    sticky-wicket PATCH, C1c): the rstripped body, plus the `prod-run` footer
    token when there is one -- so editing the declaration after posting breaks
    the digest. Comments without a declaration hash exactly as before."""
    text = prefix.rstrip("\n")
    if not field:
        return text
    # Domain-separated (sticky-wicket PATCH 2, item 5): each part is
    # length-prefixed, so the same token moved from the body's last line into
    # the footer produces different covered bytes and fails verification.
    return f"l1-post-covered v2\n{len(text)}:{text}\n{len(field)}:{field}"


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


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def script_contract_violations(source: str) -> list[str]:
    """harmonic-forge#1026: how `source` breaks the production runner's
    contract, in a fixed order; empty when it holds. Static (`ast`), so a
    docstring or comment that only mentions `ensure_env` never counts."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"not parseable Python ({exc.msg}, line {exc.lineno})"]
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ("ensure_env", "load_dotenv") and alias.asname:
                    aliases[alias.asname] = alias.name
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = [aliases.get(n, n) for n in (_call_name(c.func) for c in calls) if n]
    violations: list[str] = []
    if "ensure_env" not in names:
        violations.append("no ensure_env() call")
    if "load_dotenv" in names:
        violations.append("calls load_dotenv()")
    add_args = [c for c in calls if _call_name(c.func) == "add_argument"]

    def flags(call: ast.Call) -> list[str]:
        return [a.value for a in call.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]

    # Sticky-wicket PATCH (F1026 pass 1): an allowlist. Anything this cannot
    # prove safe for a no-argument or `--apply`-only run is refused, because a
    # static reading of argparse can never enumerate every way to require one.
    def is_true(node) -> bool:
        return isinstance(node, ast.Constant) and node.value is True

    apply_calls = [c for c in add_args if "--apply" in flags(c)]
    if not apply_calls:
        violations.append("no --apply option")
    elif not any(isinstance(k.value, ast.Constant) and k.arg == "action"
                 and k.value.value == "store_true" for c in apply_calls for k in c.keywords):
        violations.append("--apply is not a literal action='store_true' flag")
    for name in ("add_mutually_exclusive_group", "add_subparsers", "error"):
        if any(_call_name(c.func) == name for c in calls):
            violations.append(f"uses {name}() (the runner passes only --apply or nothing)")
    for c in calls:
        if _call_name(c.func) == "ArgumentParser" and any(k.arg == "parents" for k in c.keywords):
            violations.append("ArgumentParser(parents=...) is not checkable")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_call_name(b) == "ArgumentParser" for b in node.bases):
            violations.append(f"subclasses ArgumentParser ({node.name})")
    for call in add_args:
        strs = flags(call)
        label = strs[0] if strs else "<non-literal name>"
        if not strs or any(isinstance(a, ast.Starred) for a in call.args) \
                or any(k.arg is None for k in call.keywords):
            violations.append(f"add_argument({label}) is not a literal declaration")
            continue
        kw = {k.arg: k.value for k in call.keywords}
        if "required" in kw and not (isinstance(kw["required"], ast.Constant)
                                     and kw["required"].value is False):
            violations.append(f"required argument {label}")
            continue
        if not label.startswith("-"):
            nargs = kw.get("nargs")
            if not (isinstance(nargs, ast.Constant) and nargs.value in ("?", "*")):
                violations.append(f"required argument {label}")
    return violations
