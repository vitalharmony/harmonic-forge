#!/usr/bin/env python3
"""Report what will halt a batch, before it starts (harmonic-forge#509 AC1).

`#502` made BATCH create its authorization. It did not stop the run halting:
three of the operator's interruptions during the `#493` batch were **hook
denials**, not permission prompts, and every one of them was knowable in
advance.

The worst is `block_missing_preclose_inspection.py`. It arms on the
`tooling-exception` label — which is exactly what every issue in a Tooling
Exception batch carries — and fires on **each close**. A batch of N such issues
is guaranteed to halt N times unless preclose has already run for all N. That
does not make unattended tooling batches fragile; it makes them **structurally
impossible**, and this tool is how that stops being true.

## It reports. It does not satisfy.

Specifically it does **not** run preclose-inspection and tick the box. Preclose
is Lane 1 work with its own output and its own findings — a preflight that
"satisfied" it would be forging a review. AC1 says so explicitly. What this does
is tell you, in one pass, which of N issues still need one, so the answer is
known at the top of the run rather than discovered one denial at a time.

## Exit codes

0 — nothing will halt this batch.
1 — at least one precondition is unmet; the report says which and how to fix it.
2 — the run could not happen (no keys, unreadable state, `gh` unavailable).

Three-valued deliberately: a tool returning non-zero for both "found a problem"
and "could not look" leaves a caller unable to tell a finding from its own
misconfiguration. `memory_lint`'s missing-store case is the local precedent.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: The guard whose precondition is per-issue and therefore the expensive one:
#: it fires once per close, so a batch of N halts N times.
PRECLOSE_LABEL = "tooling-exception"
PRECLOSE_SATISFIED_LABEL = "preclose-inspected"

OK, UNMET, UNKNOWN = "ok", "UNMET", "unknown"


@dataclass
class Finding:
    key: str
    guard: str
    status: str
    detail: str

    def line(self) -> str:
        mark = {OK: "  ok  ", UNMET: " UNMET", UNKNOWN: "  ??  "}[self.status]
        return f"[{mark}] {self.key:<8} {self.guard:<34} {self.detail}"


def _gh(*args: str) -> str | None:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_repo(key: str) -> tuple[str, str] | None:
    """`H1631` -> `("vitalharmony/hrse", "1631")`, via the shorthand prefixes."""
    match = re.fullmatch(r"([A-Za-z])(\d+)", key.strip())
    if not match:
        return None
    try:
        from batch_auth import REPO_PREFIXES  # noqa: PLC0415

        for repo, prefix in REPO_PREFIXES.items():
            if prefix.upper() == match.group(1).upper():
                return repo, match.group(2)
    except Exception:
        return None
    return None


def check_preclose(key: str) -> Finding:
    """Will `block_missing_preclose_inspection.py` halt the close of this issue?

    Mirrors that hook's own condition — labelled `tooling-exception` and NOT
    labelled `preclose-inspected` — rather than reimplementing its judgement.
    It checks label presence only, not whether a lane actually reviewed
    anything, and this deliberately reproduces that same shallowness: a
    preflight that were stricter than the guard would report halts that will
    not happen.
    """
    target = resolve_repo(key)
    if target is None:
        return Finding(key, "preclose-inspection", UNKNOWN,
                       "no shorthand prefix maps this key to a repo")
    repo, number = target
    raw = _gh("issue", "view", number, "--repo", repo, "--json", "labels,state")
    if raw is None:
        return Finding(key, "preclose-inspection", UNKNOWN,
                       f"could not read {repo}#{number}")
    try:
        data = json.loads(raw)
        labels = {label["name"] for label in data.get("labels", [])}
    except (ValueError, KeyError, TypeError):
        return Finding(key, "preclose-inspection", UNKNOWN, "unparseable issue JSON")

    if data.get("state") == "CLOSED":
        return Finding(key, "preclose-inspection", OK, "already closed")
    if PRECLOSE_LABEL not in labels:
        return Finding(key, "preclose-inspection", OK,
                       f"not labelled {PRECLOSE_LABEL}; guard does not arm")
    if PRECLOSE_SATISFIED_LABEL in labels:
        return Finding(key, "preclose-inspection", OK,
                       f"{PRECLOSE_SATISFIED_LABEL} present")
    return Finding(
        key, "preclose-inspection", UNMET,
        f"labelled {PRECLOSE_LABEL} without {PRECLOSE_SATISFIED_LABEL} — the "
        f"close WILL halt. Run preclose-inspection, then: gh issue edit "
        f"{number} --repo {repo} --add-label {PRECLOSE_SATISFIED_LABEL}")


def check_authorization(key: str) -> Finding:
    """Is a BATCH authorization actually live for this key?

    Not a deny-surface guard, but the same class of knowable-in-advance halt:
    `#502` made BATCH self-executing, and a key typed with a typo, or one whose
    grant has expired mid-run, still produces a prompt on every merge and close.
    Cheap to check here, invisible until it bites otherwise.
    """
    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        from batch_context import live_batch_keys  # noqa: PLC0415

        live = {k.upper() for k in live_batch_keys(datetime.now(timezone.utc))}
    except Exception:
        return Finding(key, "batch authorization", UNKNOWN,
                       "could not read the authorization state")
    if key.upper() in live:
        return Finding(key, "batch authorization", OK, "live")
    return Finding(key, "batch authorization", UNMET,
                   f"no live authorization — every merge and close for {key} "
                   f"will prompt. Send a chat message containing `BATCH {key}`")


#: Guards whose precondition is not per-issue: they depend on how a command is
#: composed, not on any issue's state, so nothing can be checked in advance.
#: Listed so the report says so rather than silently omitting them.
COMPOSITION_GUARDS = (
    ("block_lane1_status_claims", "denies GitHub auto-close syntax (`Closes #N`) "
     "in a PR body — compose bodies without it"),
    ("block_inline_prose", "denies long/metacharacter-bearing prose in `-m`/"
     "`--body` — use `--body-file`"),
    ("block_irreversible_ops", "owns the merge/close class end to end; "
     "`#502` governs its authorization path"),
)

#: Check names, resolved at CALL time rather than held as references.
#:
#: A tuple of function objects froze its bindings at import, so patching
#: `batch_preflight.check_preclose` had no effect on what `report()` ran —
#: caught by this module's own tests, which appeared to pass a clean fixture
#: while silently exercising the live `gh` path. A frozen reference is also the
#: reason such a tuple silently ignores a later monkeypatch or subclass.
CHECK_NAMES = ("check_authorization", "check_preclose")


def report(keys: list[str]) -> tuple[str, int]:
    checks = [globals()[name] for name in CHECK_NAMES]
    findings = [check(key) for key in keys for check in checks]
    lines = [f"batch preflight — {len(keys)} issue(s): {', '.join(keys)}", ""]
    lines += [f.line() for f in findings]

    unmet = [f for f in findings if f.status == UNMET]
    unknown = [f for f in findings if f.status == UNKNOWN]

    lines += ["", "Guards that CANNOT be pre-satisfied — they depend on how each",
              "command is composed, not on issue state:"]
    lines += [f"  - {name}: {why}" for name, why in COMPOSITION_GUARDS]

    lines += ["", f"{len(unmet)} unmet, {len(unknown)} unknown, "
                  f"{len(findings) - len(unmet) - len(unknown)} ok."]
    if unmet:
        lines += ["", "This batch WILL halt. Clear the UNMET rows above first."]
    elif unknown:
        lines += ["", "No known blocker, but some checks could not run — treat "
                      "this as unverified rather than clear."]
    else:
        lines += ["", "Nothing known will halt this batch."]
    return "\n".join(lines), (1 if unmet else 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--key", action="append", default=[], metavar="KEY",
                   help="Issue key, e.g. F509. Repeatable.")
    p.add_argument("--from-state", action="store_true",
                   help="Take the keys from every live BATCH authorization "
                        "instead of --key.")
    args = p.parse_args(argv)

    keys = list(args.key)
    if args.from_state:
        try:
            from datetime import datetime, timezone  # noqa: PLC0415

            from batch_context import live_batch_keys  # noqa: PLC0415

            keys += [k for k in live_batch_keys(datetime.now(timezone.utc))
                     if k not in keys]
        except Exception as exc:  # noqa: BLE001
            print(f"batch-preflight: cannot read the authorization state: {exc}",
                  file=sys.stderr)
            return 2
    if not keys:
        print("batch-preflight: no keys — pass --key or --from-state",
              file=sys.stderr)
        return 2

    text, code = report(keys)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
