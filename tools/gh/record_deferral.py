#!/usr/bin/env python3
"""Backfill recorded R-0352 deferrals for Lane 1's belt (harmonic-forge#1564).

`gh_issue.py --handoff-exception` records each deferral it files. An issue
deferred before that landed is recorded here, by explicit key:

    python3 tools/gh/record_deferral.py --repo vitalharmony/hrse --issue 2757 --issue 2778 \
        --operator-login <the deferral comment's login>

`--operator-login` is required: it is the one login whose `go` releases the
deferral, stored casefolded. The repo must be onboarded (`projects.toml`), and
every key must pass `deferral_store.valid_key` (an issue >= 1) before anything
is written: a rejected key exits 2 (harmonic-forge#1564 sticky-wicket fix (c)). A local write only: no `gh` call and no network. At most
`deferral_store.MAX_BACKFILL_KEYS` keys per run. The belt verifies each
entry's deferral comment on its next tick, and discharges a backfilled entry
whose thread carries none (a mistyped key), with one stderr line.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deferral_store  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
from manifest import ManifestError, require_onboarded_repo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record open R-0352 deferrals for Lane 1's belt, by explicit key "
                    "(local write, no gh call).")
    parser.add_argument("--repo", required=True,
                        help="owner/name of the deferred issues' repo (no default)")
    parser.add_argument("--issue", type=int, action="append", required=True,
                        help="an issue number carrying a deferral comment; repeat for each "
                             f"(at most {deferral_store.MAX_BACKFILL_KEYS})")
    parser.add_argument("--operator-login", required=True,
                        help="the login that posted the deferral comment; only its `go` "
                             "releases the deferral")
    args = parser.parse_args(argv)
    try:
        repo = require_onboarded_repo(args.repo).repo
    except ManifestError as exc:
        print(f"record_deferral: {exc}", file=sys.stderr)
        return 2
    issues = list(dict.fromkeys(args.issue))
    if len(args.issue) > deferral_store.MAX_BACKFILL_KEYS:
        print(f"record_deferral: {len(args.issue)} keys given; at most "
              f"{deferral_store.MAX_BACKFILL_KEYS} per run", file=sys.stderr)
        return 2
    rejected = [n for n in issues if deferral_store.valid_key(repo, n) is None]
    if rejected:
        print(f"record_deferral: invalid key(s) {rejected}: an issue number must be "
              ">= 1; nothing recorded", file=sys.stderr)
        return 2
    failed = 0
    for number in issues:
        if not deferral_store.record_deferral(repo, number, "backfill",
                                              operator_login=args.operator_login):
            print(f"record_deferral: could not record {repo}#{number}", file=sys.stderr)
            failed += 1
            continue
        print(f"recorded deferral {repo}#{number}; the belt verifies its deferral "
              "comment on its next tick")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
