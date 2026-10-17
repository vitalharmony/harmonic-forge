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

Clauses run in a fixed order and the first hit wins:

    ineligible, deny-path, tool-refusal, budget, self-flag, check-fail,
    kill-check-fail, preclose-survivor

`green` only when no clause hits. Every clause fails closed: a fact that is
missing, `None` or of the wrong type sets the item aside with the reason of
the clause that reads it, so an incomplete harness can never produce `green`.

Facts (a plain dict; unknown keys are ignored):

- `eligible: bool` -- false is `ineligible`.
- `deny_hits: list[str]` -- non-empty is `deny-path`.
- `permission_denials: int` -- above zero is `tool-refusal`.
- `budget_exceeded: bool` -- true is `budget`.
- `self_flag: str | None` -- the agent's "I cannot do this honestly" line;
  non-blank is `self-flag`. `None` means no flag (the one fact where `None`
  is a value, not a gap).
- `check_rc: int` and `live: {"pr_exists": bool, "head_sha_matches": bool,
  "ci": "green"|"pending"|"red"}` -- a non-zero `check_rc`, a missing PR, a
  PR whose head is not the SHA that was checked, or `ci == "red"` is
  `check-fail` (G8: completion is verified against live GitHub). `pending`
  passes: upstream Actions wait for the operator's approval on a fork PR, so
  it surfaces on the review card and CI is re-read live at BATCH time.
- `kill: list[{"ac": str, "base_rc": int, "head_rc": int}]` -- passes only
  when the list is non-empty and every check failed on the base
  (`base_rc != 0`) and passed on the head (`head_rc == 0`); otherwise
  `kill-check-fail`.
- `preclose: {"status": str, "survivors": int, "provenance": str}` -- passes
  only when `status == "complete"`, `survivors == 0` and `provenance` starts
  with `Red-team provenance: cross-family (`. An in-family fallback or a
  missing receipt is `preclose-survivor`: IMC's rule (R-0384) applied to the
  runner, since nothing but the operator settles its output.

`detail` is one line of at most 200 characters, the telemetry `attrs` limit
(`tools/telemetry/schema_v1.json`).
"""
from __future__ import annotations

SET_ASIDE_REASONS: tuple[str, ...] = (
    "ineligible", "deny-path", "check-fail", "kill-check-fail",
    "preclose-survivor", "budget", "tool-refusal", "self-flag",
)

#: The order clauses are tried in; the first hit wins.
PRECEDENCE: tuple[str, ...] = (
    "ineligible", "deny-path", "tool-refusal", "budget", "self-flag",
    "check-fail", "kill-check-fail", "preclose-survivor",
)

#: Mirrors `tools/gh/preclose_check.py`'s `PROVENANCE_TRIGGERED[0]`. Restated,
#: not imported, so this module stays pure (importing preclose_check runs its
#: own path and hook imports).
CROSS_FAMILY_PREFIX = "Red-team provenance: cross-family ("

DETAIL_LIMIT = 200
CI_STATES = ("green", "pending", "red")


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
    return "budget exceeded" if _bool(facts, "budget_exceeded") else None


def _self_flag(facts: dict) -> str | None:
    if "self_flag" not in facts:
        raise _Gap("self_flag missing")
    flag = facts["self_flag"]
    if flag is None:
        return None
    if not isinstance(flag, str):
        raise _Gap("self_flag not a string or None")
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
    ci = _str(live, "ci", "live.ci")
    if ci not in CI_STATES:
        raise _Gap(f"live.ci is not one of {', '.join(CI_STATES)}")
    return "CI is red" if ci == "red" else None


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
    return f"kill check not fail-on-base/pass-on-head: {'; '.join(failed)}" if failed else None


def _preclose_survivor(facts: dict) -> str | None:
    preclose = _dict(facts, "preclose")
    status = _str(preclose, "status", "preclose.status")
    survivors = _int(preclose, "survivors", "preclose.survivors")
    provenance = _str(preclose, "provenance", "preclose.provenance")
    if status != "complete":
        return f"preclose pass not complete (status {status})"
    if survivors:
        return f"{survivors} preclose finding(s) survived"
    if not provenance.startswith(CROSS_FAMILY_PREFIX):
        return "preclose pass is not cross-family"
    return None


_CLAUSES = {
    "ineligible": _ineligible,
    "deny-path": _deny_path,
    "tool-refusal": _tool_refusal,
    "budget": _budget,
    "self-flag": _self_flag,
    "check-fail": _check_fail,
    "kill-check-fail": _kill_check_fail,
    "preclose-survivor": _preclose_survivor,
}


def one_line(text: str) -> str:
    """Collapse whitespace to one line and cap it at DETAIL_LIMIT characters."""
    line = " ".join(str(text).split())
    return line if len(line) <= DETAIL_LIMIT else line[:DETAIL_LIMIT - 1] + "…"


def _set_aside(reason: str, detail: str) -> dict:
    return {"state": "set-aside", "reason": reason, "detail": one_line(detail)}


def reduce(facts: object) -> dict:
    """The verdict for one item. Pure; see the module docstring."""
    if not isinstance(facts, dict):
        return _set_aside(PRECEDENCE[0], "facts missing or not an object")
    for reason in PRECEDENCE:
        try:
            hit = _CLAUSES[reason](facts)
        except _Gap as gap:
            return _set_aside(reason, f"fact gap: {gap}")
        if hit:
            return _set_aside(reason, hit)
    return {"state": "green", "reason": None, "detail": "all clauses passed"}
