#!/usr/bin/env python3
"""Deterministic verdict for one unattended-runner item (harmonic-forge#1051).

The runner (epic harmonic-forge#1050) decides, per issue, whether its work is
proposed to the operator for review (`green`) or set aside. That call is made
here, by a pure function over facts the harness already holds, never by a
model: a model that grades its own run is "self-grading with one level of
indirection" (`tools/gh/preclose_check.py`'s docstring), which is the failure
the spec exists to prevent.

`reduce(facts)` returns `{"state": "green"|"set-aside", "reason": <one of
SET_ASIDE_REASONS>|None, "detail": str}`. No I/O, no model call, no network,
no clock.

Clauses run in a fixed order and the first hit wins `reason` and `detail`;
`reasons` lists every clause that hit, in that order, each as
`{"reason", "detail"}` (harmonic-forge#1112 AC8). A fact gap still stops the
walk at its clause:

    ineligible, deny-path, tool-refusal, budget, self-flag, check-fail,
    kill-check-fail

`green` only when no clause hits. Every clause fails closed: a fact that is
missing, `None` or of the wrong type sets the item aside with the reason of
the clause that reads it, so an incomplete harness can never produce `green`.

Facts (a plain dict; unknown keys are ignored):

- `eligible: bool` -- false is `ineligible`.
- `deny_hits: list[str]` -- non-empty is `deny-path`.
- `permission_denials: int` -- above zero is `tool-refusal`.
- `budget_exceeded: bool` -- true is `budget`; an optional `budget_detail`
  string names the bound (the budget, `--max-turns`, the agent wall timeout).
- `self_flag: str` -- the agent's "I cannot do this honestly" line;
  non-blank is `self-flag`, and an empty or blank string means no flag.
  `None` is a gap like any other fact (AC3), never "no flag": the harness
  writes `""` when the agent raised none.
- `check_rc: int` and `live: {"pr_exists": bool, "head_sha_matches": bool}`
  -- a non-zero `check_rc`, a missing PR, or a PR whose head is not the SHA
  that was checked is `check-fail` (G8: completion is verified against live
  GitHub). CI is not read here: `reduce` decides pre-PR eligibility only, and
  CI is `merge_authorization`'s input (harmonic-forge#1195).
- `kill: list[{"ac": str, "base_rc": int, "head_rc": int}]` -- passes only
  when the list is non-empty and every check failed on the base
  (`base_rc != 0`) and passed on the head (`head_rc == 0`); otherwise
  `kill-check-fail`.

No model verdict is an input (harmonic-forge#1195): the refuter panel and the
cross-family check are out of the accept path, and `self_flag` can only set an
item aside, never make it green.

`merge_authorization(state, detail)` is the second, separate decision: whether
a PR that is open may be merged. Only gate_ci's `green` authorizes; `pending`,
`absent` and `unknown` leave the item open and unauthorized, and `red` also
sets it aside as `check-fail`. The runner never merges; BATCH's own
concluded-green check (harmonic-forge#1178) stays the merge gate.

`detail` is one line of at most 200 characters, the telemetry `attrs` limit
(`tools/telemetry/schema_v1.json`).
"""
from __future__ import annotations

SET_ASIDE_REASONS: tuple[str, ...] = (
    "ineligible", "deny-path", "check-fail", "kill-check-fail",
    "budget", "tool-refusal", "self-flag",
)

#: The order clauses are tried in; the first hit wins.
PRECEDENCE: tuple[str, ...] = (
    "ineligible", "deny-path", "tool-refusal", "budget", "self-flag",
    "check-fail", "kill-check-fail",
)

DETAIL_LIMIT = 200
#: `tools/gh/gate_ci`'s five states. Restated, not imported, so this module
#: stays pure.
CI_STATES = ("green", "pending", "red", "absent", "unknown")


class _Gap(Exception):
    """A fact a clause needs is missing or malformed."""


def _bool(facts: dict, key: str) -> bool:
    value = facts.get(key)
    if not isinstance(value, bool):
        raise _Gap(f"{key} missing or not a bool")
    return value


def _int(container: dict, key: str, label: str | None = None) -> int:
    value = container.get(key)
    # bool is an int subclass; True must not pass as a return code.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Gap(f"{label or key} missing or not an int")
    return value


def _str(container: dict, key: str, label: str | None = None) -> str:
    value = container.get(key)
    if not isinstance(value, str):
        raise _Gap(f"{label or key} missing or not a string")
    return value


def _dict(facts: dict, key: str) -> dict:
    value = facts.get(key)
    if not isinstance(value, dict):
        raise _Gap(f"{key} missing or not an object")
    return value


def _ineligible(facts: dict) -> str | None:
    return None if _bool(facts, "eligible") else "eligibility check failed"


def _deny_path(facts: dict) -> str | None:
    hits = facts.get("deny_hits")
    if not isinstance(hits, list) or not all(isinstance(h, str) for h in hits):
        raise _Gap("deny_hits missing or not a list of strings")
    return f"denied path touched: {', '.join(hits)}" if hits else None


def _tool_refusal(facts: dict) -> str | None:
    denials = _int(facts, "permission_denials")
    if denials < 0:
        raise _Gap("permission_denials is negative")
    return f"{denials} permission denial(s)" if denials else None


def _budget(facts: dict) -> str | None:
    if not _bool(facts, "budget_exceeded"):
        return None
    detail = facts.get("budget_detail")  # optional: which bound was hit
    return detail if isinstance(detail, str) and detail.strip() else "budget exceeded"


def _self_flag(facts: dict) -> str | None:
    flag = _str(facts, "self_flag")
    return f"agent self-flag: {flag}" if flag.strip() else None


def _check_fail(facts: dict) -> str | None:
    rc = _int(facts, "check_rc")
    if rc:
        return f"mise run check exited {rc}"
    live = _dict(facts, "live")
    if not _bool(live, "pr_exists"):
        return "no PR exists on GitHub"
    if not _bool(live, "head_sha_matches"):
        return "PR head is not the checked SHA"
    return None


def _kill_check_fail(facts: dict) -> str | None:
    kill = facts.get("kill")
    if not isinstance(kill, list):
        raise _Gap("kill missing or not a list")
    if not kill:
        return "no kill checks ran"
    failed = []
    for number, check in enumerate(kill, 1):
        if not isinstance(check, dict):
            raise _Gap(f"kill[{number}] not an object")
        ac = _str(check, "ac", f"kill[{number}].ac")
        base = _int(check, "base_rc", f"kill[{number}].base_rc")
        head = _int(check, "head_rc", f"kill[{number}].head_rc")
        if base == 0 or head != 0:
            failed.append(f"{ac} (base {base}, head {head})")
    # harmonic-forge#1147 AC6: kill_check.py's receipt is its own check, and a
    # failed one sets the item aside even when every kill fact passed.
    receipt_ok = _bool(facts, "kill_receipt")
    if failed:
        reason = f"kill check not fail-on-base/pass-on-head: {'; '.join(failed)}"
        return reason if receipt_ok else reason + "; kill-check receipt failed"
    return None if receipt_ok else "kill-check receipt failed"



_CLAUSES = {
    "ineligible": _ineligible,
    "deny-path": _deny_path,
    "tool-refusal": _tool_refusal,
    "budget": _budget,
    "self-flag": _self_flag,
    "check-fail": _check_fail,
    "kill-check-fail": _kill_check_fail,
}


def one_line(text: str) -> str:
    """Collapse whitespace to one line and cap it at DETAIL_LIMIT characters."""
    line = " ".join(str(text).split())
    return line if len(line) <= DETAIL_LIMIT else line[:DETAIL_LIMIT - 1] + "…"


def _set_aside(reason: str, detail: str, reasons: list[dict] | None = None) -> dict:
    line = one_line(detail)
    return {"state": "set-aside", "reason": reason, "detail": line,
            "reasons": reasons if reasons is not None else [{"reason": reason, "detail": line}]}


def reduce(facts: object) -> dict:
    """The verdict for one item. Pure; see the module docstring."""
    if not isinstance(facts, dict):
        return _set_aside(PRECEDENCE[0], "facts missing or not an object")
    hits: list[dict] = []
    for reason in PRECEDENCE:
        try:
            hit = _CLAUSES[reason](facts)
        except _Gap as gap:
            # A fact gap still returns at once: the harness is incomplete, and
            # the clauses after it read facts it never wrote (harmonic-forge#1112).
            hits.append({"reason": reason, "detail": one_line(f"fact gap: {gap}")})
            return _set_aside(hits[0]["reason"], hits[0]["detail"], hits)
        if hit:
            hits.append({"reason": reason, "detail": one_line(hit)})
    if hits:
        return _set_aside(hits[0]["reason"], hits[0]["detail"], hits)
    return {"state": "green", "reason": None, "detail": "all clauses passed", "reasons": []}


def merge_authorization(state: object, detail: object) -> dict:
    """Whether an open PR may be merged, from gate_ci's CI state. Pure.

    Only `green` authorizes. A state outside `CI_STATES` is a gap and is not
    authorized. `set_aside` is true only for `red` (`check-fail`)."""
    ci = state if isinstance(state, str) else "unknown"
    if ci not in CI_STATES:
        detail, ci = f"unrecognized CI state {state!r}: {detail}", "unknown"
    return {"authorized": ci == "green", "ci": ci, "detail": one_line(str(detail)),
            "set_aside": ci == "red"}


def with_merge(outcome: dict, merge: dict) -> dict:
    """`outcome` with red CI added as `check-fail`; anything else unchanged. Pure."""
    if not merge.get("set_aside"):
        return outcome
    hit = {"reason": "check-fail", "detail": one_line(f"CI is red: {merge.get('detail', '')}")}
    reasons = [*outcome.get("reasons", []), hit]
    first = reasons[0]
    return _set_aside(first["reason"], first["detail"], reasons)
