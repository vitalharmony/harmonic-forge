"""The post-time check behind R-0378, the operator's `/auto-ae` toggle
(harmonic-forge#874).

While the operator has typed `/auto-ae on` in a Lane 1 session (the
`UserPromptSubmit` half of `tools/hooks/auto_ae_toggle.py` writes the state),
Lane 1 may approve a Lane 3 spec and post its AE and sweep itself, for a spec
whose cases are Tier R or W only, on an issue under a live `BATCH` lease. This
module is where those preconditions are checked mechanically, modeled on
`_standing_grant.py` (R-0374):

- `cites_auto_ae(body)` says whether an AE claims auto-AE. The claim lives on
  the AE's own **Authorized:** line, outside any fenced block; naming either
  "auto-AE" or the rule ID there is a claim, so a half-citation is still
  checked rather than posted as if it were the operator's AE.
- `auto_ae_refusal(...)` is why such an AE must not be posted, or None. It
  fails closed: anything it cannot show refuses, and the operator's AE remains
  the path.
- `check_lane3_ready.carry_forward` refuses to carry an auto-AE to a new SHA:
  the toggle or the lease can lapse between a FAIL and the carry, and nothing
  is posted on a carry, so no post-time check would run.

**Tier P is never covered, by spec ceiling, not by case.** A spec whose write
tier ceiling is P (or states none) is refused whole: per-case tiers are not
reliably machine-readable, and the operator's ruling is "never Tier P". Such a
spec keeps the operator's AE (and R-0374 for the production step a Tier R gate
verified).

**Threat model, the same as R-0374's (operator decision, 2026-10-02): a
mistake-detector, not an authorization boundary.** Every lane posts as the same
GitHub account and runs as the same user, so nothing here proves who typed
`/auto-ae on` or who wrote a comment. It catches the honest mistake: the toggle
off, an issue outside any lease, a Tier P spec, an AE naming the wrong spec or
SHA, an edited spec.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import check_lane3_ready as clr

RULE = "R-0378"
STATE_RELPATH = Path(".claude") / "state" / "auto-ae.json"
_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
_FENCE = re.compile(r"```.*?```", re.S)
_AUTHORIZED_LINE = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*Authorized\**:?\**[^\n]*$")
_CLAIM = re.compile(r"\bauto[- ]?AE\b|\bR-0378\b", re.I)
#: The only tiers auto-AE covers (operator ruling 1, 2026-10-02).
COVERED_TIERS = frozenset({"R", "W"})
MERGE_ACTION = "gh pr merge"
#: A Lane 3 spec recognized by its heading, whatever its footer says.
_SPEC_HEADING = re.compile(r"(?im)^#{1,4}[ \t]*Lane 3 Test Spec\b")

# --- the tier rule: two gates (sticky-wicket PATCH, fix 2) -------------------
# Gate 1, the declaration: unfenced "Write tier: X" / "Write tier **X**", one
# letter on the same line. Every declaration must agree, and be R or W; none,
# or two that differ, refuses. A fenced example can never supply the tier.
# harmonic-forge#947 (sticky-wicket REFORGE after two preclose passes): the
# colon is required, inside or outside the bold (`Write tier: W`,
# `**Write tier:** W`, `**Write tier**: W`), and the letter must stand alone
# (whitespace, `|` or end of line after it). A bare label with no separator
# let prose parse as a declaration (`w/o`, `W's`, `R or W`).
_DECLARATION = re.compile(r"(?im)\bwrite[ \t]*tier[ \t]*\**[ \t]*:[ \t]*\**[ \t]*`?([RWP])`?\**(?=[ \t.,;)]|\||$)")
# Gate 2, the veto: ANY standalone capital P token anywhere in the raw body,
# fences and tables included, in whatever layout (a first column, no leading
# pipe, a slash or comma list), plus a `--tier p` flag in either case. Two
# rounds of naming P's spellings each missed a layout, so it names none: no
# real Lane 3 spec carries a bare P, and a false refusal costs one operator AE,
# so the broadest form is the safe one (sticky-wicket ruling, post-verdict).
_P_TOKEN = re.compile(r"(?<![\w-])[`*]*P[`*]*(?![\w-])|(?i:--tier)[^A-Za-z0-9]*[pP](?![A-Za-z0-9])")


_UNQUOTE = re.compile(r"[\"'`\\]")


def declared_tier(body: str) -> str | None:
    """The one tier the unfenced declarations agree on, or None."""
    found = {m.group(1).upper() for m in _DECLARATION.finditer(_unquoted(body))}
    return found.pop() if len(found) == 1 else None


def p_vetoed(body: str) -> bool:
    """Whether anything in the raw body, fences included, could mean Tier P."""
    text = body or ""
    # The flag is also matched with every quote, backtick and backslash
    # removed, so a shell-quoted spelling (`--'tier'=p`, `--ti"er"=p`) reads
    # as the flag it expands to: three post-verdict checks each found one more
    # separator or quoting form, so the class is closed rather than listed.
    return bool(_P_TOKEN.search(text) or _P_TOKEN.search(_UNQUOTE.sub("", text)))


def tier_refusal(label: str, body: str) -> str | None:
    """Why a spec or sweep body is outside auto-AE's tiers, or None."""
    if p_vetoed(body):
        return f"the {label} mentions Tier P (in any form, fenced text included)"
    tier = declared_tier(body)
    if tier is None:
        return (f"the {label} has no single unfenced `Write tier: R|W` declaration "
                "(none, or two that disagree)")
    if tier not in COVERED_TIERS:
        return f"the {label} declares Write tier {tier}"
    return None


def state_path() -> Path:
    return Path.home() / STATE_RELPATH


def _parse_ts(value: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else None


def state(path: Path | None = None, now: datetime | None = None) -> dict:
    """The toggle state. Anything unreadable, not exactly `on: true`, without a
    lease map, or past its `expires_at` is off: a broken or stale state file
    never turns auto-AE on (sticky-wicket PATCH, fix 5)."""
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"on": False}
    if not isinstance(data, dict) or data.get("on") is not True \
            or not isinstance(data.get("leases_at_set"), dict):
        return {"on": False}
    expires = _parse_ts(data.get("expires_at"))
    if expires is None or not (now or datetime.now(timezone.utc)) < expires:
        return {"on": False}
    return data


def _batch_auth():
    if str(_HOOKS) not in sys.path:
        sys.path.insert(0, str(_HOOKS))
    import batch_auth  # noqa: PLC0415
    return batch_auth


def live_leases(now: datetime | None = None, batch_state: Path | None = None) -> dict[str, str]:
    """`{issue key: expires_at}` for every live BATCH lease: unexpired, with an
    unconsumed `gh pr merge` target. The `expires_at` is the lease's identity:
    a renewed grant for the same key carries a new one. Empty on any failure
    (fails closed)."""
    try:
        batch_auth = _batch_auth()
        data = batch_auth._load(batch_state or batch_auth.STATE_PATH)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    now = now or datetime.now(timezone.utc)
    leases = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            if not now < datetime.fromisoformat(entry["expires_at"]):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        targets = entry.get("targets") or []
        if any(isinstance(t, dict) and t.get("action") == MERGE_ACTION and not t.get("consumed")
               for t in targets):
            leases[str(key).upper()] = str(entry["expires_at"])
    return leases


def live_lease_keys(now: datetime | None = None, batch_state: Path | None = None) -> set[str]:
    return set(live_leases(now, batch_state))


def issue_key(repo: str, issue: int) -> str | None:
    try:
        return _batch_auth().issue_key(repo, issue)
    except Exception:  # noqa: BLE001
        return None


def _unquoted(body: str) -> str:
    return _FENCE.sub("", body or "")


def _authorized_lines(body: str) -> list[str]:
    return [m.group(0) for m in _AUTHORIZED_LINE.finditer(_unquoted(body))]


def cites_auto_ae(body: str) -> bool:
    """Whether an AE claims auto-AE on its **Authorized:** line."""
    return any(_CLAIM.search(line) for line in _authorized_lines(body))


def _kind(comment: dict) -> str | None:
    match = clr.FOOTER_KIND.search(comment.get("body", ""))
    return match.group(1).lower() if match else None


def _is_spec(comment: dict) -> bool:
    """A spec by its `kind=spec` footer or by its heading, whatever other footer
    kind it carries: a revised spec posted through another route (as a
    `discussion`, say) must still supersede the footered one, and then fail the
    digest check (sticky-wicket PATCH, fix 4)."""
    return _kind(comment) == "spec" or bool(_SPEC_HEADING.search(comment.get("body", "")))


def approved_spec(comments: list[dict]) -> tuple[dict | None, dict | None]:
    """`(ready, spec)`: the newest Lane 1 `ready-for-l3`, and the newest Lane 3
    spec posted after it (which may lack a footer). Either may be None."""
    readies = [c for c in comments if _kind(c) == "ready-for-l3"]
    if not readies:
        return None, None
    ready = max(readies, key=lambda c: int(c["id"]))
    specs = [c for c in comments if _is_spec(c) and int(c["id"]) > int(ready["id"])]
    return ready, (max(specs, key=lambda c: int(c["id"])) if specs else None)


def auto_ae_refusal(ae_body: str, sweep_body: str | None, repo: str, issue: int, sha: str,
                    comments: list[dict], *, spec_comment: int | None, prod_run: bool,
                    ack_no_pr_required: str | None, now: datetime | None = None,
                    toggle_state: Path | None = None, batch_state: Path | None = None) -> str | None:
    """Why an AE claiming auto-AE (R-0378) must not be posted, or None."""
    prefix = f"this AE cites auto-AE ({RULE}), but"
    fallback = " The operator's AE is required."
    if sweep_body is None:
        return (f"{prefix} it is not posted with its sweep; an auto-AE goes out only as "
                "`--kind ae-and-sweep`." + fallback)
    if prod_run:
        return f"{prefix} it declares a production run (--prod-run), which is Tier P." + fallback
    if ack_no_pr_required is not None:
        return (f"{prefix} it carries --ack-no-pr-required, an operator acknowledgment no "
                "operator gave." + fallback)
    if len(_authorized_lines(ae_body)) != 1:
        return f"{prefix} it has more than one Authorized: line." + fallback
    toggle = state(toggle_state, now)
    if not toggle.get("on"):
        return f"{prefix} auto-AE is off (the operator turns it on with `/auto-ae on`)." + fallback
    key = issue_key(repo, issue)
    if key is None:
        return f"{prefix} {repo}#{issue} has no issue key, so no BATCH lease can cover it." + fallback
    # Auto-AE covers the exact leases live when the operator turned it on, by
    # identity (key and expiry): a later or renewed BATCH, even for the same
    # key, needs `/auto-ae on` again (sticky-wicket PATCH, fix 5).
    covered = {str(k).upper(): str(v) for k, v in (toggle.get("leases_at_set") or {}).items()}
    live = live_leases(now, batch_state)
    if key.upper() not in covered:
        return (f"{prefix} {key} was not under a lease when auto-AE was turned on; "
                "the operator types `/auto-ae on` again to cover a later BATCH." + fallback)
    if key.upper() not in live:
        return f"{prefix} {key} is not under a live BATCH lease." + fallback
    if live[key.upper()] != covered[key.upper()]:
        return (f"{prefix} {key}'s BATCH lease was renewed after auto-AE was turned on; "
                "the operator types `/auto-ae on` again to cover it." + fallback)
    ready, spec = approved_spec(comments)
    if ready is None:
        return f"{prefix} the thread has no Lane 1 ready-for-l3." + fallback
    if not clr.same_sha(clr.footer_sha(ready), sha):
        return (f"{prefix} the newest ready-for-l3 names {str(clr.footer_sha(ready))[:12]}, "
                f"not the AE's {sha[:12]}." + fallback)
    if spec is None:
        return f"{prefix} no Lane 3 spec was posted after the newest ready-for-l3." + fallback
    if spec_comment is not None and int(spec_comment) != int(spec["id"]):
        return (f"{prefix} --spec-comment {spec_comment} is not the newest Lane 3 spec "
                f"({spec['id']})." + fallback)
    # verify_body_sha256 passes a comment with no marker, so absence is checked first.
    if not clr.FOOTER_BODY_SHA.search(spec.get("body", "")):
        return (f"{prefix} the Lane 3 spec {spec['id']} carries no body-sha256 marker, so it "
                "cannot be shown unedited." + fallback)
    if not clr.verify_body_sha256(spec):
        return f"{prefix} the Lane 3 spec {spec['id']} was edited after it was posted." + fallback
    if not re.search(rf"\b{int(spec['id'])}\b", _authorized_lines(ae_body)[0]):
        return f"{prefix} its Authorized: line does not name the spec comment {spec['id']}." + fallback
    for label, body in (("spec", spec.get("body", "")), ("sweep", sweep_body)):
        why = tier_refusal(label, body)
        if why:
            return f"{prefix} {why}; auto-AE covers only Tier R or W throughout." + fallback
    return None
