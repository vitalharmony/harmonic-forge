#!/usr/bin/env python3
"""Decide a cross-family review's provenance label from its envelope
(harmonic-forge#598 AC2/AC3, preclose finding 1).

The first version of this mechanism left the label to the agent's own reading
of the result, with three states written out in prose. Preclose inspection
found the state the prose had no name for: the call **runs, exits 0, returns
`status: ok`, and checks nothing** -- every assumption normalized down to
`uncheckable` because the reviewer could not reach the evidence from the `cwd`
it was given. The rule then told the reader `uncheckable` is "a real and
expected answer, not a failed call", and the report went out labelled
`cross-family`. One pass spent, zero cross-family checking, full cross-family
credit. That is exactly AC3's prohibited state, reached by a route AC3 did not
anticipate.

A label an agent derives by reading prose is a label that will sometimes be
derived wrong. So the label is computed here, from the envelope, and pasted.

**`uncheckable` is not failure -- but ALL-`uncheckable` is.** A single
uncheckable verdict beside a confirmed one is the mechanism working: the
reviewer said what it could not reach instead of confabulating. A report where
nothing at all was reached is indistinguishable in content from no call having
been made, so it gets the label that says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_CHECKED = {"confirmed", "refuted"}

#: Substrings that must never appear in a label except where THIS module put
#: them. Found live by the cross-family reviewer on this file's own second
#: invocation (harmonic-forge#598): the failure branch interpolates the
#: envelope's `stderr` -- which is the sibling CLI's output, not ours -- into
#: the reason text, so a process whose error message happened to contain
#: `cross-family (` produced a FALLBACK label carrying the cross-family
#: marker. Every downstream reader of these labels is a human eye or a grep,
#: and both are fooled by that. The reviewer demonstrated it rather than
#: arguing it, which is the difference the mechanism exists to buy.
_RESERVED_MARKERS = ("cross-family", "provenance:", "in-family")


def _redact(text: str) -> str:
    """Untrusted text, made safe to sit inside a label line."""
    flat = " ".join(str(text).split())
    for marker in _RESERVED_MARKERS:
        lowered = flat.lower()
        start = lowered.find(marker)
        while start != -1:
            flat = flat[:start] + "[redacted]" + flat[start + len(marker):]
            lowered = flat.lower()
            start = lowered.find(marker)
    return flat

CROSS_FAMILY = "Red-team provenance: cross-family (codex / {model})"
FALLBACK = "Red-team provenance: in-family fallback ({own_model}) — {reason}"
NOT_TRIGGERED = (
    "Red-team provenance: in-family only ({own_model}) — cross-family branch "
    "not triggered"
)


def iter_envelopes(text: str):
    """Each JSON object in the stream, one per invoked family.

    A streaming decode, not `splitlines()`. ADR-007 calls this a "JSON-lines
    envelope" and the dispatch loop does emit one record per family -- but it
    builds them with `jq -n`, which PRETTY-PRINTS by default, so a real
    envelope spans a dozen-odd lines and a line-at-a-time parse reports every
    one of them as malformed. Measured against a live envelope, which is how
    this was found. `raw_decode` handles the pretty-printed form and the
    compact one identically, so this keeps working if the script ever gains
    `jq -c`.
    """
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            return
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            return
        if isinstance(value, dict):
            yield value


def classify(envelope: dict, model: str, own_model: str) -> str:
    """The provenance line for one envelope."""
    status = envelope.get("status")
    if status != "ok" or not isinstance(envelope.get("report"), dict):
        detail = _redact(f"{status or 'no envelope'}, "
                         f"exit {envelope.get('exit_code', '?')}")
        stderr = envelope.get("stderr")
        if stderr:
            detail += f" -- {_redact(stderr)[:300]}"
        return FALLBACK.format(own_model=own_model,
                               reason=f"cross-family call did not run: {detail}")

    assumptions = envelope["report"].get("assumptions")
    if not isinstance(assumptions, list) or not assumptions:
        return FALLBACK.format(
            own_model=own_model,
            reason="cross-family call returned no verdicts, so it produced "
                   "nothing to weigh",
        )

    checked = [a for a in assumptions
               if isinstance(a, dict) and a.get("verdict") in _CHECKED]
    if not checked:
        return FALLBACK.format(
            own_model=own_model,
            reason=f"cross-family call ran but checked nothing: all "
                   f"{len(assumptions)} verdict(s) uncheckable. The reviewer "
                   f"could not reach the evidence -- check that --cwd named "
                   f"the repository the artifact lives in, not an empty "
                   f"scratch directory",
        )

    label = CROSS_FAMILY.format(model=model)
    unchecked = len(assumptions) - len(checked)
    return (f"{label} — {len(checked)} of {len(assumptions)} assumption(s) "
            f"checked with executed evidence"
            + (f", {unchecked} uncheckable" if unchecked else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", required=True,
                        help="path to the JSON-lines envelope cross_family_call.sh wrote")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--own-model", default="claude-opus-5")
    parser.add_argument("--not-triggered", action="store_true",
                        help="print the not-triggered label and exit; --envelope is ignored")
    args = parser.parse_args(argv)

    if args.not_triggered:
        print(NOT_TRIGGERED.format(own_model=args.own_model))
        return 0

    text = Path(args.envelope).read_text().strip()
    if not text:
        print(FALLBACK.format(own_model=args.own_model,
                              reason="cross-family call did not run: empty envelope"))
        return 0
    envelopes = list(iter_envelopes(text))
    if not envelopes:
        print(FALLBACK.format(
            own_model=args.own_model,
            reason="cross-family call did not run: envelope was not JSON"))
        return 0
    for envelope in envelopes:
        print(classify(envelope, args.model, args.own_model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
