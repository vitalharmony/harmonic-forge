#!/usr/bin/env python3
"""harmonic-forge#1230 (semantic routing A1): `route set` and `route show`.

`route set <KEY> --path N --reason "..."` records an operator override, and only
ever tightens: a path looser than the route in force is refused. Loosening is
the digest's operator-typed batch line (child D of harmonic-forge#1229).
`route show <KEY>` prints the route in force and what decided it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route_store  # noqa: E402
from gates import Route  # noqa: E402


def cmd_set(key: str, path: int, reason: str, root: Optional[Path] = None) -> int:
    if not reason.strip():
        print("route set: --reason is required", file=sys.stderr)
        return 2
    prior = route_store.current(key, root)
    if prior is not None and path > int(prior["attrs"]["path"]):
        print(f"route set: refused. {key} is path {prior['attrs']['path']} "
              f"({prior['attrs']['gate_id']}); path {path} is looser, and loosening is the "
              "operator's digest batch line, not route set.", file=sys.stderr)
        return 1
    route_store.record(key, Route(path, "override", reason.strip()), decider="override",
                       checkpoint="override", actor="operator", root=root)
    print(f"route set: {key} -> path {path} (override)")
    return 0


def cmd_show(key: str, root: Optional[Path] = None) -> int:
    event = route_store.current(key, root)
    if event is None:
        print(f"{key}: no route recorded")
        return 0
    a = event["attrs"]
    shadow = f"; shadow path {a['shadow_path']} ({a['shadow_gate']})" if "shadow_path" in a else ""
    print(f"{key}: path {a['path']} — gate {a['gate_id']} — decider {a['decider']} "
          f"at {a['checkpoint']} — {a['reason']}{shadow}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("--path", type=int, choices=(1, 2, 3), required=True)
    s.add_argument("--reason", required=True)
    w = sub.add_parser("show")
    w.add_argument("key")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "set":
            return cmd_set(args.key, args.path, args.reason)
        return cmd_show(args.key)
    except ValueError as exc:
        print(f"route: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
