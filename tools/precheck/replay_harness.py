#!/usr/bin/env python3
"""The replay harness: score a layer set against the replay set and the
never-refuse sample (harmonic-forge#1289).

  score   rebuild the failure store when stale, read its overlay-keyed slips
          (`failure_keys WHERE overlaid`) in time order, give each to the first
          layer whose claims match it, else to an existing guard, else to an
          accepted-uncovered class, else `UNCOVERED`; then run every layer
          that has a module over the frozen sample and evaluate each layer's
          bars.

A layer set is a JSON file (see README.md and `layersets/design-v1.json`).
A layer module defines `refuses(command, cwd, env_names) -> str | None`;
`env_names` is the harness process's live environment names.

The report prints counts, owners and up to 20 `(n, reason)` pairs per layer;
never a command and never a stored probe.

Exit codes: 0 every measurable bar holds (with `--strict`, a not-measurable
bar also fails); 4 a bar fails; 2 refused (a malformed layer set, a sample
without its manifest or with a changed sha256, a store path inside forge);
3 any other error. Never 1.

Stdlib only, no network and no subprocess.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "memory"))
sys.path.insert(0, str(HERE))

import failure_store  # noqa: E402
import never_refuse_sample  # noqa: E402

CLASSES_FILE = TOOLS / "memory" / "failure_classes.json"
EXIT_OK, EXIT_REFUSED, EXIT_ERROR, EXIT_BAR_FAILED = 0, 2, 3, 4
MAX_REASONS = 20


class Refused(Exception):
    """Exit 2."""


def current_classes() -> set[str]:
    data = json.loads(CLASSES_FILE.read_text(encoding="utf-8"))
    return set(data["versions"][str(data["current"])])


# --------------------------------------------------------------------------
# Layer sets.

def _claim(text, classes: set[str], where: str) -> tuple[str, str]:
    if not isinstance(text, str) or text.count("|") != 1:
        raise Refused(f"{where}: claim {text!r} is not '<tool>|<class>' or '*|<class>'")
    tool, klass = text.split("|")
    if not tool or klass not in classes:
        raise Refused(f"{where}: claim {text!r} names no tool or a class not in failure_classes.json")
    return tool, klass


def load_layer_set(path: Path) -> dict:
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"layer set {path} is unreadable: {type(exc).__name__}") from exc
    classes = current_classes()
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("layers"), list):
        raise Refused(f"layer set {path} needs version 1 and a layers list")
    layers = []
    for index, layer in enumerate(data["layers"]):
        where = f"layer {index}"
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), str) or not layer["name"]:
            raise Refused(f"{where} needs a name")
        where = f"layer {layer['name']}"
        claims = layer.get("claims")
        if not isinstance(claims, list) or not claims:
            raise Refused(f"{where} needs a non-empty claims list")
        module = layer.get("module")
        if module is not None:
            module_path = Path(module).expanduser()
            if not module_path.is_absolute():
                module_path = path.parent / module_path
            if not module_path.is_file():
                raise Refused(f"{where}: module {module_path} does not exist")
            module = module_path
        bar = layer.get("bar") or {}
        replay = bar.get("replay")
        if replay is not None:
            share = replay.get("min_share")
            if (not isinstance(replay.get("classes"), list) or not replay["classes"]
                    or any(c not in classes for c in replay["classes"])
                    or not isinstance(share, (int, float)) or not 0 <= share <= 1):
                raise Refused(f"{where}: replay bar needs known classes and a min_share in 0..1")
        max_refusals = bar.get("sample_max_refusals")
        if max_refusals is not None and (not isinstance(max_refusals, int) or max_refusals < 0):
            raise Refused(f"{where}: sample_max_refusals must be a non-negative integer")
        layers.append({"name": layer["name"], "claims": [_claim(c, classes, where) for c in claims],
                       "module": module, "replay_bar": replay, "sample_max_refusals": max_refusals})
    existing = data.get("existing") or {}
    accepted = data.get("accepted_uncovered") or []
    if (not isinstance(existing, dict) or any(k not in classes for k in existing)
            or not isinstance(accepted, list) or any(c not in classes for c in accepted)):
        raise Refused(f"layer set {path}: existing and accepted_uncovered must name known classes")
    return {"layers": layers, "existing": existing, "accepted_uncovered": list(accepted)}


def claims(layer: dict, tool: str, klass: str) -> bool:
    return any(c_class == klass and c_tool in ("*", tool) for c_tool, c_class in layer["claims"])


# --------------------------------------------------------------------------
# Replay.

def read_replay(db: Path, logs: dict, overlays: Path) -> tuple[list[tuple], int]:
    """`(rows, keyed_not_overlaid)`: the overlay-keyed slips in time order, each
    `(tool, class, own_tool, own_class)`; the effective key is the first pair."""
    failure_store.build(db, logs, overlays, if_stale=True)
    con = sqlite3.connect(f"{Path(db).expanduser().resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT tool, class, own_tool, own_class FROM failure_keys WHERE overlaid "
                           "ORDER BY ts, log, line").fetchall()
        other = con.execute("SELECT count(*) FROM failure_keys WHERE NOT overlaid "
                            "AND class IS NOT NULL").fetchone()[0]
    finally:
        con.close()
    return rows, other


def score_replay(rows: list[tuple], layer_set: dict) -> dict:
    owners: collections.Counter = collections.Counter()
    per_class: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    owned_by: list[str] = []
    seen: set[str] = set()
    repeat_owned = repeat_unowned = multi = 0
    layer_names = {layer["name"] for layer in layer_set["layers"]}
    for tool, klass, _own_tool, _own_class in rows:
        claimers = [layer["name"] for layer in layer_set["layers"] if claims(layer, tool, klass)]
        if len(claimers) > 1:
            multi += 1
        if claimers:
            owner = claimers[0]
        elif klass in layer_set["existing"]:
            owner = layer_set["existing"][klass]
        elif klass in layer_set["accepted_uncovered"]:
            owner = f"accepted-uncovered:{klass}"
        else:
            owner = "UNCOVERED"
        owners[owner] += 1
        per_class[klass][owner] += 1
        owned_by.append(owner)
        key = f"{tool}|{klass}"
        if key in seen:
            if owner in layer_names:
                repeat_owned += 1
            else:
                repeat_unowned += 1
        seen.add(key)
    return {"total": len(rows), "owners": dict(owners.most_common()),
            "per_class": {k: dict(v) for k, v in sorted(per_class.items())},
            "repeat_owned": repeat_owned, "repeat_unowned": repeat_unowned,
            "multi_claimed": multi, "_rows": [((r[0], r[1]), o) for r, o in zip(rows, owned_by)]}


def replay_bar(layer: dict, replay: dict) -> dict | None:
    bar = layer["replay_bar"]
    if bar is None:
        return None
    relevant = [owner for (_tool, klass), owner in replay["_rows"] if klass in bar["classes"]]
    total, claimed = len(relevant), sum(1 for owner in relevant if owner == layer["name"])
    if total == 0:
        return {"bar": f"{layer['name']}.replay", "status": "not-measurable", "claimed": 0, "total": 0}
    holds = claimed / total >= bar["min_share"]
    return {"bar": f"{layer['name']}.replay", "status": "holds" if holds else "fails",
            "claimed": claimed, "total": total}


# --------------------------------------------------------------------------
# Sample.

def read_sample(path: Path) -> list[dict]:
    path = Path(path).expanduser()
    orphan = never_refuse_sample.orphan_refusal(path)
    if orphan:
        raise Refused(orphan)
    manifest = never_refuse_sample.manifest_path(path)
    if not path.exists():
        raise Refused(f"no sample at {path}: run never_refuse_sample.py derive first")
    expected = json.loads(manifest.read_text(encoding="utf-8")).get("sha256")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise Refused(f"sample {path} does not match its manifest's sha256; it was edited")
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_precheck_layer_{Path(path).stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.refuses


def run_layer(refuses, rows: list[dict], env_names: frozenset[str]) -> dict:
    refusals, errors, reasons = 0, 0, []
    for row in rows:
        try:
            reason = refuses(row["command"], row.get("cwd") or None, env_names)
        except Exception:  # noqa: BLE001 - a layer that raises is counted, never trusted
            errors += 1
            continue
        if reason:
            refusals += 1
            if len(reasons) < MAX_REASONS:
                reasons.append((row["n"], str(reason)[:80]))
    return {"refusals": refusals, "size": len(rows), "errors": errors, "reasons": reasons}


CONTROLS = {"null": lambda command, cwd, env_names: None,
            "all": lambda command, cwd, env_names: "control: refuses every row"}


# --------------------------------------------------------------------------

def score(layer_file: Path, sample: Path, db: Path, logs: dict, overlays: Path,
          control: bool = False) -> dict:
    layer_set = load_layer_set(layer_file)
    try:
        replay_rows, other = read_replay(db, logs, overlays)
    except failure_store.Refused as exc:
        raise Refused(str(exc)) from exc
    replay = score_replay(replay_rows, layer_set)
    replay["keyed_not_overlaid"] = other
    rows = read_sample(sample)
    env_names = frozenset(os.environ)
    bars, layers = [], {}
    for layer in layer_set["layers"]:
        bar = replay_bar(layer, replay)
        if bar:
            bars.append(bar)
        result = None
        if layer["module"] is not None:
            result = run_layer(load_module(layer["module"]), rows, env_names)
        layers[layer["name"]] = result
        if layer["sample_max_refusals"] is not None:
            name = f"{layer['name']}.sample"
            if result is None:
                bars.append({"bar": name, "status": "not-measurable"})
            else:
                holds = result["refusals"] <= layer["sample_max_refusals"] and result["errors"] == 0
                bars.append({"bar": name, "status": "holds" if holds else "fails",
                             "refusals": result["refusals"], "size": result["size"],
                             "errors": result["errors"]})
    controls = {name: run_layer(fn, rows, env_names) for name, fn in CONTROLS.items()} if control else {}
    replay.pop("_rows")
    return {"replay": replay, "sample_rows": len(rows), "layers": layers, "controls": controls,
            "bars": bars}


def exit_code(report: dict, strict: bool) -> int:
    for bar in report["bars"]:
        if bar["status"] == "fails" or (strict and bar["status"] == "not-measurable"):
            return EXIT_BAR_FAILED
    return EXIT_OK


def _bar_text(bar: dict) -> str:
    if "total" in bar and bar["status"] != "not-measurable":
        return f"{bar['bar']} {bar['claimed']}/{bar['total']} {bar['status']}"
    if "refusals" in bar:
        return f"{bar['bar']} {bar['refusals']}/{bar['size']} errors {bar['errors']} {bar['status']}"
    return f"{bar['bar']} {bar['status']}"


def render(report: dict, code: int) -> str:
    replay = report["replay"]
    lines = [f"replay: {replay['total']} overlay-keyed slips "
             f"({replay['keyed_not_overlaid']} keyed, not overlaid, not scored)"]
    lines += [f"  {owner}: {count}" for owner, count in replay["owners"].items()]
    lines.append(f"  repeat_owned {replay['repeat_owned']}, repeat_unowned {replay['repeat_unowned']}, "
                 f"multi_claimed {replay['multi_claimed']}")
    lines.append(f"sample: {report['sample_rows']} rows")
    for name, result in [*report["layers"].items(), *(("control " + k, v) for k, v in report["controls"].items())]:
        if result is None:
            lines.append(f"  {name}: no module, not measurable on the sample")
            continue
        lines.append(f"  {name}: {result['refusals']}/{result['size']} refused, {result['errors']} errors")
        lines += [f"    n={n}: {reason}" for n, reason in result["reasons"]]
    verdict = "PASS" if code == EXIT_OK else "FAIL"
    lines.append(f"verdict: {verdict}: " + "; ".join(_bar_text(b) for b in report["bars"]))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("score", help="Score a layer set.")
    s.add_argument("--layers", type=Path, required=True)
    s.add_argument("--sample", type=Path, default=never_refuse_sample.DEFAULT_OUT)
    s.add_argument("--db", type=Path, default=failure_store.DEFAULT_DB)
    s.add_argument("--memory-log", type=Path, default=failure_store.DEFAULT_LOGS["memory"])
    s.add_argument("--testplan-log", type=Path, default=failure_store.DEFAULT_LOGS["testplan"])
    s.add_argument("--overlays", type=Path, default=failure_store.DEFAULT_OVERLAYS)
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true", help="A not-measurable bar fails.")
    s.add_argument("--control", action="store_true", help="Add the null and all control layers.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs = {"memory": args.memory_log, "testplan": args.testplan_log}
    try:
        report = score(args.layers, args.sample, args.db, logs, args.overlays, args.control)
        code = exit_code(report, args.strict)
        print(json.dumps(report, indent=2) if args.json else render(report, code))
        sys.stdout.flush()
        return code
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - every other error is exit 3, never 1
        print(f"replay-harness: error: {type(exc).__name__}", file=sys.stderr)
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
