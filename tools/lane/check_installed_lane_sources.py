#!/usr/bin/env python3
"""Verify that the installed lane commands share one launcher source (F645).

The lane commands are user-local symlinks rather than tracked repository files.
That is appropriate for putting them on PATH, but it means a retarget can make
one lane consume a detached platform checkout while the other two consume main.
This checker reports that drift and never repairs it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

LANES = ("lane1", "lane2", "lane3")
def check(
    expected_root: Path | None = None,
    bin_dir: Path | None = None,
    skip_if_absent: bool = False,
) -> int:
    """Return 0 only when every installed lane resolves under one root.

    Without an explicit root, installed lane1 is authoritative. That lets this
    run from any worktree without treating the caller's path as configuration.
    """
    canonical_root = expected_root.resolve() if expected_root else None
    failures: list[str] = []
    missing = 0
    for lane in LANES:
        if bin_dir:
            installed = bin_dir / lane
        else:
            found = shutil.which(lane)
            if not found:
                failures.append(f"{lane}: MISSING from PATH")
                missing += 1
                continue
            installed = Path(found)
        if not installed.exists():
            failures.append(f"{lane}: MISSING from {'--bin-dir' if bin_dir else 'PATH'}")
            missing += 1
            continue
        if not installed.is_symlink():
            failures.append(f"{lane}: not a symlink ({installed})")
            continue
        try:
            resolved = installed.resolve(strict=True)
        except OSError as exc:
            failures.append(f"{lane}: BROKEN ({installed}: {exc})")
            continue
        if canonical_root is None:
            if lane != "lane1":
                failures.append(f"{lane}: cannot establish canonical root; lane1 is unavailable")
                continue
            canonical_root = resolved.parent
            print(f"[lane-source] canonical root inferred from lane1: {canonical_root}")
        expected = canonical_root / lane
        print(f"[lane-source] {lane}: {installed} -> {resolved}")
        if resolved != expected:
            failures.append(
                f"{lane}: DRIFT (expected {expected}, got {resolved})")

    if skip_if_absent and missing == len(LANES):
        print("[lane-source] SKIP — no installed lane commands found")
        return 0
    if failures:
        for failure in failures:
            print(f"[lane-source] {failure}", file=sys.stderr)
        return 1
    print(f"[lane-source] OK — all lanes resolve under {canonical_root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check installed lane1/lane2/lane3 launcher source consistency")
    parser.add_argument(
        "--expected-root", type=Path,
        help="Canonical tools/lane directory (default: infer from installed lane1)")
    parser.add_argument(
        "--bin-dir", type=Path,
        help="Directory holding lane symlinks (default: search PATH)")
    parser.add_argument(
        "--skip-if-absent", action="store_true",
        help="Pass only when all three commands are absent (for clean CI hosts)")
    args = parser.parse_args(argv)
    return check(args.expected_root, args.bin_dir, args.skip_if_absent)


if __name__ == "__main__":
    raise SystemExit(main())
