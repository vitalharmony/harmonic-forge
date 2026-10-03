#!/usr/bin/env python3
"""SessionStart hook: inject the transaction-log view, rendered from git now
(harmonic-forge#883).

A repo opts in by registering this in its own `.claude/settings.json` with
its own `--boundary` (see tools/transaction-log/transaction_log.py for the
boundary grammar). The view is emitted as `hookSpecificOutput.additionalContext`.

Never blocks session start: any failure — not a repo, a bad boundary, git
missing — exits 0 with no output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

_RENDERER = Path(__file__).resolve().parents[1] / "transaction-log" / "transaction_log.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("transaction_log", _RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_context(repo: Path, boundary: str, max_entries: int, max_chars: int) -> str:
    renderer = _load_renderer()
    _, description = renderer.resolve_boundary(repo, boundary)
    body = renderer.render(repo, boundary, max_entries=max_entries, max_chars=max_chars)
    return (f"[TRANSACTION LOG] {description}, newest first (generated now from git "
            f"— there is no transaction-log.md; `mise run transaction-log --all` for "
            f"the uncapped view):\n{body or '- (no commits since the boundary)'}")


class _SilentParser(argparse.ArgumentParser):
    """argparse prints usage to stderr before exiting; this hook stays silent."""

    def error(self, message):
        raise ValueError(message)


def main() -> int:
    try:
        parser = _SilentParser(add_help=False)
        parser.add_argument("--boundary", required=True)
        parser.add_argument("--max-entries", type=int, default=40)
        parser.add_argument("--max-chars", type=int, default=6000)
        args = parser.parse_args()
        repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        context = build_context(repo, args.boundary, args.max_entries, args.max_chars)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": context}}))
    except BaseException:  # noqa: BLE001 — never block session start, argparse exits included
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
