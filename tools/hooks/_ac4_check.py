#!/usr/bin/env python3
"""AC4's check, as logic rather than one clever regex (harmonic-forge#509).

Three regex attempts were wrong in three different ways, which is the argument
for not using one:

1. `"permissionDecision"\\s*:\\s*"allow"` matched only a string **literal**, and
   missed `batch_gate.py:32`'s `"permissionDecision": decision` — a variable,
   and precisely the idiom a future author copies from the working example next
   door.
2. Matching the bare key flagged every deny hook, whose entire job is to emit
   that key with `"deny"`.
3. `permissionDecision["']` also matches inside `permissionDecisionReason"`,
   because the former is a prefix of the latter.

The property is small and stateable, so state it: **a hook that reads batch
state may refuse, and may decide nothing else.**
"""
from __future__ import annotations

import re

#: The exact key, not a prefix of a longer one.
_KEY = re.compile(r'["\']permissionDecision["\']\s*:\s*(?P<value>[^,\n}]+)')

#: The only value a batch-aware hook may emit.
_DENY_LITERAL = re.compile(r'^["\']deny["\']$')


def offending_decisions(source: str) -> list[str]:
    """Values assigned to `permissionDecision` that are not a literal deny."""
    bad: list[str] = []
    for match in _KEY.finditer(source):
        value = match.group("value").strip().rstrip(",")
        if not _DENY_LITERAL.match(value):
            bad.append(value)
    return bad


def reads_batch_state(source: str) -> bool:
    return "batch_auth" in source or "batch_context" in source
