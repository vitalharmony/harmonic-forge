#!/usr/bin/env python3
"""Resolve a model family to the latest model in it (harmonic-forge#938).

    resolve_model.py <family>   print the latest model ID for a family
    resolve_model.py --list     print `family<TAB>id` for every family (`-` when unresolvable)

Exit 0 resolved, 1 the family's provider could not resolve it (reason on
stderr; callers keep their own fallback pin), 2 unknown family.

Providers:
- claude: the family alias itself (`opus` ...). Claude Code resolves the alias
  to the latest model (`claude -p --model opus` reports `claude-opus-5-5`,
  verified 2026-10-07). `haiku` is unexercised in forge; kept for symmetry.
- gemini: Google's hot-swapped `-latest` aliases. `gemini-flash-latest` is
  unexercised in forge; `gemini-pro-latest` is what `cross_family_call.sh` uses.
- codex: Codex's own catalog, `codex debug models`. A family's latest is the
  `visibility == "list"` entry with no `upgrade`, lowest `priority`. The answer
  is the newest model the LOCAL `codex` binary knows about: offline, the
  command silently serves its bundled catalog, which can be older.

Usage with the operator's Sol profile (the profile file is not edited). Bind
the result first: inside `-m "$(...)"` an exit 1 is discarded and codex gets
an empty model, silently running its default.

    m=$(python3 ~/harmonic-forge/tools/models/resolve_model.py sol) && codex exec -p sol -m "$m" ...

`--list` with a hung `codex` binary: the lister's memo does not cache a
failure, so each codex family waits out its own 30 s timeout (about 120 s for
four). Accepted (operator, harmonic-forge#938): `--list` is for people, and a
fast failure costs nothing extra.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
from model_tier_gate import _codex_family  # noqa: E402

CODEX_TIMEOUT_SECONDS = 30  # a hung binary, not an offline guard


@functools.lru_cache(maxsize=1)
def list_codex() -> tuple[dict, ...]:
    """Current Codex models as `{"id", "family", "rank"}`, lower rank = newer.

    Raises on a non-zero exit, a timeout, or output that is not the catalog.
    """
    result = subprocess.run(["codex", "debug", "models"], capture_output=True,
                            text=True, timeout=CODEX_TIMEOUT_SECONDS)
    if result.returncode:
        raise RuntimeError(f"codex debug models exited {result.returncode}: "
                           f"{result.stderr.strip()}")
    models = json.loads(result.stdout)["models"]
    return tuple(
        {"id": entry["slug"], "family": _codex_family(entry["slug"]),
         "rank": entry["priority"]}
        for entry in models
        # `upgrade` is present and null on current entries: test truthiness,
        # never key presence.
        if entry.get("visibility") == "list" and not entry.get("upgrade")
    )


# A provider is either {"aliases": {family: id}} or {"lister": fn}, where fn
# returns [{"id", "family", "rank"}] and the lowest rank in a family wins.
PROVIDERS: dict[str, dict | None] = {
    "claude": {"aliases": {f: f for f in ("opus", "sonnet", "haiku", "fable")}},
    "gemini": {"aliases": {"gemini-pro": "gemini-pro-latest",
                           "gemini-flash": "gemini-flash-latest"}},
    "codex": {"lister": list_codex},
    # Adding a provider: replace this stub with one of the two shapes above,
    # e.g. {"aliases": {"newfam": "newfam-latest"}} or {"lister": list_newprov},
    # then map its families in FAMILIES below. A new family on an existing
    # provider is one FAMILIES line (plus an alias entry for alias providers).
    "example": None,
}

FAMILIES: dict[str, str] = {
    "opus": "claude", "sonnet": "claude", "haiku": "claude", "fable": "claude",
    "sol": "codex", "terra": "codex", "luna": "codex", "astra": "codex",
    "gemini-pro": "gemini", "gemini-flash": "gemini",
}


def resolve(family: str) -> str:
    """Latest model ID for `family`. KeyError unknown; LookupError unresolvable."""
    provider = PROVIDERS[FAMILIES[family]]
    if "aliases" in provider:
        return provider["aliases"][family]
    entries = [e for e in provider["lister"]() if e["family"] == family]
    if not entries:
        raise LookupError(f"no current model in family {family!r}")
    return min(entries, key=lambda e: e["rank"])["id"]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    if argv[0] == "--list":
        for family in FAMILIES:
            try:
                model = resolve(family)
            except Exception:  # one unresolvable family must not hide the rest
                model = "-"
            print(f"{family}\t{model}")
        return 0
    family = argv[0]
    if family not in FAMILIES:
        print(f"resolve_model: unknown family {family!r} "
              f"(known: {', '.join(FAMILIES)})", file=sys.stderr)
        return 2
    try:
        print(resolve(family))
    except Exception as exc:  # lister failure or no current model
        print(f"resolve_model: cannot resolve {family!r}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
