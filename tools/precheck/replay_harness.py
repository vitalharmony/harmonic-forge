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
bar also fails; without it the verdict line names how many bars were not
measured); 4 a bar fails, including a replay bar with no slips to score; 2 refused (a malformed layer set, including
an unknown key at any level, a layer with no bar and a set with no layers; a
sample without its manifest, with a changed sha256, or not exactly 10,000 rows;
a store path inside forge);
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


#: Every key, per level, and every one is required (harmonic-forge#1289
#: epoch-3 REFORGE, "parse, don't validate"): a level's keys must be exactly
#: this set. An unknown key refuses, and so does an absent one: there are no
#: defaults anywhere in the layer set, because every default this loader had
#: leaned toward PASS. An absent value is written as `null` (a module, a bar
#: half) or an empty container (`existing`, `accepted_uncovered`).
LAYER_SET_KEYS = frozenset({"version", "layers", "existing", "accepted_uncovered"})
LAYER_KEYS = frozenset({"name", "claims", "claims_basis", "module", "bar"})
BAR_KEYS = frozenset({"replay", "sample_max_refusals"})
REPLAY_KEYS = frozenset({"classes", "min_share"})
CLAIMS_BASES = ("validated", "declared")
#: Owner labels the report reserves; an `existing` label may not take one.
UNCOVERED = "UNCOVERED"
ACCEPTED_PREFIX = "accepted-uncovered:"


def _exact_keys(value, allowed: frozenset, where: str) -> None:
    """Refuses a non-object, an unknown key and an absent key alike."""
    if not isinstance(value, dict):
        raise Refused(f"{where} must be an object")
    unknown, absent = sorted(set(value) - allowed), sorted(allowed - set(value))
    if unknown:
        raise Refused(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")
    if absent:
        raise Refused(f"{where}: absent key(s) {', '.join(map(repr, absent))}; every key is required, "
                      "with null or an empty container for none")


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_bar(bar, classes: set[str], where: str) -> tuple[dict | None, int | None]:
    if bar is None or bar == {}:
        raise Refused(f"{where} needs a bar: a replay bar, a sample_max_refusals bar or both")
    _exact_keys(bar, BAR_KEYS, f"{where} bar")
    replay = bar["replay"]
    if replay is not None:
        _exact_keys(replay, REPLAY_KEYS, f"{where} replay bar")
        share = replay["min_share"]
        if (not isinstance(replay["classes"], list) or not replay["classes"]
                or any(not isinstance(c, str) or c not in classes for c in replay["classes"])
                or len(set(replay["classes"])) != len(replay["classes"])
                or isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 1):
            raise Refused(f"{where}: replay bar needs distinct known classes and a min_share in 0..1")
    max_refusals = bar["sample_max_refusals"]
    if max_refusals is not None and (not _is_count(max_refusals) or max_refusals < 0):
        raise Refused(f"{where}: sample_max_refusals must be a non-negative integer")
    if replay is None and max_refusals is None:
        raise Refused(f"{where}: bar sets neither a replay bar nor sample_max_refusals")
    return replay, max_refusals


def _parse_layer(index: int, layer, classes: set[str], base: Path) -> dict:
    where = f"layer {index}"
    # refuses: an absent name reads as None here and is refused.
    if not isinstance(layer, dict) or not isinstance(layer.get("name"), str) or not layer["name"]:
        raise Refused(f"{where} needs a name")
    where = f"layer {index} ({layer['name']})"
    _exact_keys(layer, LAYER_KEYS, where)
    claims = layer["claims"]
    if not isinstance(claims, list) or not claims:
        raise Refused(f"{where} needs a non-empty claims list")
    parsed_claims = [_claim(c, classes, where) for c in claims]
    if len(set(parsed_claims)) != len(parsed_claims):
        raise Refused(f"{where}: a claim appears twice")
    module = layer["module"]
    if module is not None:
        if not isinstance(module, str) or not module:
            raise Refused(f"{where}: module must be a path string or null")
        module_path = Path(module).expanduser()
        if not module_path.is_absolute():
            module_path = base / module_path
        if not module_path.is_file():
            raise Refused(f"{where}: module {module_path} does not exist")
        module = module_path
    replay, max_refusals = _parse_bar(layer["bar"], classes, where)
    # A module with no sample bar would run over the sample and have its
    # errors and refusals ignored (codex #10): a module needs its bar.
    if module is not None and max_refusals is None:
        raise Refused(f"{where} has a module, so its bar must set sample_max_refusals")
    # "declared": the layer's claims are what it says it guards, not what a
    # dry run was shown to validate (operator ruling, harmonic-forge#1289:
    # design-v1's gate claims stay declared until issue C's per-tool
    # registry replaces them). Its replay bar is labeled, never bare. No
    # default: an absent claims_basis refuses above.
    if layer["claims_basis"] not in CLAIMS_BASES:
        raise Refused(f"{where}: claims_basis must be validated or declared")
    return {"index": index, "name": layer["name"], "claims": parsed_claims, "module": module,
            "replay_bar": replay, "sample_max_refusals": max_refusals, "claims_basis": layer["claims_basis"]}


def load_layer_set(path: Path) -> dict:
    """Parse, never validate: the result is fully specified, or this raises
    `Refused`. Nothing downstream reads the raw JSON."""
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"layer set {path} is unreadable: {type(exc).__name__}") from exc
    classes = current_classes()
    _exact_keys(data, LAYER_SET_KEYS, f"layer set {path}")
    if data["version"] != 1 or isinstance(data["version"], bool) or not isinstance(data["layers"], list):
        raise Refused(f"layer set {path} needs version 1 and a layers list")
    layers = [_parse_layer(index, layer, classes, path.parent) for index, layer in enumerate(data["layers"])]
    if not layers:
        raise Refused(f"layer set {path} has no layers, so no bars: nothing scored is never a pass")
    names = [layer["name"] for layer in layers]
    # Layer names key the report and the sample results: a duplicate refuses.
    if len(set(names)) != len(names):
        raise Refused(f"layer set {path}: layer names must be unique")
    existing, accepted = data["existing"], data["accepted_uncovered"]
    if (not isinstance(existing, dict) or any(k not in classes for k in existing)
            or not isinstance(accepted, list) or any(not isinstance(c, str) or c not in classes for c in accepted)):
        raise Refused(f"layer set {path}: existing and accepted_uncovered must name known classes")
    if len(set(accepted)) != len(accepted):
        raise Refused(f"layer set {path}: accepted_uncovered names a class twice")
    if set(existing) & set(accepted):
        raise Refused(f"layer set {path}: a class is both existing and accepted_uncovered")
    for label in existing.values():
        # Ownership is an identity, never this string (score_replay); the
        # label is display only, so it may not read as a layer or a reserved owner.
        if (not isinstance(label, str) or not label.strip() or label in names
                or label == UNCOVERED or label.startswith(ACCEPTED_PREFIX)):
            raise Refused(f"layer set {path}: an existing owner must be a non-empty label that is "
                          "not a layer name, UNCOVERED or accepted-uncovered:*")
    return {"layers": layers, "existing": dict(existing), "accepted_uncovered": list(accepted)}


def claims(layer: dict, tool: str, klass: str) -> bool:
    return any(c_class == klass and c_tool in ("*", tool) for c_tool, c_class in layer["claims"])


# --------------------------------------------------------------------------
# Replay.

def _overlay_counts(con: sqlite3.Connection) -> dict:
    """The build's overlay accounting, parsed: refuses any overlay row the
    store dropped as malformed and any overlay row that joins no failure
    record. Either one would drop a slip from the replay, and a dropped slip
    can only raise a bar's share (harmonic-forge#1289 epoch 3). Read-only, on
    F1288's schema as it is."""
    row = con.execute("SELECT value FROM meta WHERE key = 'counts'").fetchone()
    try:
        counts = json.loads(row[0]) if row else None
    except ValueError:
        counts = None
    # refuses: no counts, or a count that is not an integer, is never zero.
    if (not isinstance(counts, dict) or not _is_count(counts.get("overlay_malformed"))
            or not _is_count(counts.get("skipped"))):
        raise Refused("the failure store records no overlay counts; rebuild it")
    if counts["overlay_malformed"] > 0:
        raise Refused(f"{counts['overlay_malformed']} malformed overlay row(s): fix the overlay; "
                      "a dropped row is never scored as absent")
    unjoined = con.execute("SELECT count(*) FROM overlays o WHERE NOT EXISTS "
                           "(SELECT 1 FROM failures f WHERE f.log = o.log AND f.line = o.line)").fetchone()[0]
    if unjoined:
        raise Refused(f"{unjoined} overlay row(s) match no failure record: fix the overlay's log_line")
    return {"skipped": counts["skipped"]}


def read_replay(db: Path, logs: dict, overlays: Path) -> tuple[list[tuple], int, int]:
    """`(rows, keyed_not_overlaid, skipped)`: the overlay-keyed slips in time
    order, each `(tool, class, own_tool, own_class)`; the effective key is the
    first pair. `skipped` counts overlay rows with an empty `key_v1`, which
    F1288 documents as deliberate (re-keys nothing)."""
    failure_store.build(db, logs, overlays, if_stale=True)
    con = sqlite3.connect(f"{Path(db).expanduser().resolve().as_uri()}?mode=ro", uri=True)
    try:
        accounting = _overlay_counts(con)
        rows = con.execute("SELECT tool, class, own_tool, own_class FROM failure_keys WHERE overlaid "
                           "ORDER BY ts, log, line").fetchall()
        other = con.execute("SELECT count(*) FROM failure_keys WHERE NOT overlaid "
                            "AND class IS NOT NULL").fetchone()[0]
    finally:
        con.close()
    return rows, other, accounting["skipped"]


def owner_label(owner: tuple, layer_set: dict) -> str:
    """Display only. Scoring compares the identity tuple, never this string."""
    kind, value = owner
    if kind == "layer":
        return layer_set["layers"][value]["name"]
    if kind == "existing":
        return layer_set["existing"][value]
    if kind == "accepted":
        return f"{ACCEPTED_PREFIX}{value}"
    return UNCOVERED


def score_replay(rows: list[tuple], layer_set: dict) -> dict:
    """Each slip's owner is an identity: `("layer", index)`,
    `("existing", class)`, `("accepted", class)` or `("uncovered", None)`.
    A name string is never compared, so no layer, existing guard or
    accepted class can stand in for another (harmonic-forge#1289 epoch 3)."""
    owners: collections.Counter = collections.Counter()
    # fail-direction: display counts keyed by class and owner identity; scoring reads _rows.
    per_class: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    owned_by: list[tuple] = []
    seen: set[str] = set()
    repeat_owned = repeat_unowned = multi = 0
    for tool, klass, _own_tool, _own_class in rows:
        claimers = [("layer", layer["index"]) for layer in layer_set["layers"] if claims(layer, tool, klass)]
        if len(claimers) > 1:
            multi += 1
        if claimers:
            owner = claimers[0]
        elif klass in layer_set["existing"]:
            owner = ("existing", klass)
        elif klass in layer_set["accepted_uncovered"]:
            owner = ("accepted", klass)
        else:
            owner = ("uncovered", None)
        # Display counts, keyed by identity; the labels are unique by the
        # parser (unique names; existing labels never a layer or reserved name).
        owners[owner] += 1
        per_class[klass][owner] += 1
        owned_by.append(owner)
        key = f"{tool}|{klass}"
        if key in seen:
            if owner[0] == "layer":
                repeat_owned += 1
            else:
                repeat_unowned += 1
        seen.add(key)
    def labelled(counter: collections.Counter) -> dict:
        # Two existing classes may share one label: summed, never overwritten.
        out: collections.Counter = collections.Counter()
        for owner, count in counter.items():
            out[owner_label(owner, layer_set)] += count
        return dict(out.most_common())

    return {"total": len(rows), "owners": labelled(owners),
            "per_class": {k: labelled(v) for k, v in sorted(per_class.items())},
            "repeat_owned": repeat_owned, "repeat_unowned": repeat_unowned,
            "multi_claimed": multi, "_rows": [((r[0], r[1]), o) for r, o in zip(rows, owned_by)]}


def replay_bar(layer: dict, replay: dict) -> dict | None:
    bar = layer["replay_bar"]
    if bar is None:
        return None  # refuses: a layer with no replay bar has a sample bar (parser)
    relevant = [owner for (_tool, klass), owner in replay["_rows"] if klass in bar["classes"]]
    # Identity, never the name: ("layer", index) is this layer and no other.
    total, claimed = len(relevant), sum(1 for owner in relevant if owner == ("layer", layer["index"]))
    name = f"{layer['name']}.replay" + (" (declared claims)" if layer["claims_basis"] == "declared" else "")
    if total == 0:
        # Nothing to score is a failure, never a pass: an empty or mistyped
        # --overlays leaves every replay bar here (harmonic-forge#1289 pass 1).
        return {"bar": name, "status": "fails", "claimed": 0, "total": 0}
    holds = claimed / total >= bar["min_share"]
    return {"bar": name, "status": "holds" if holds else "fails", "claimed": claimed, "total": total}


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
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    data = path.read_bytes()
    # refuses: an absent sha256 reads as None and never equals a digest.
    if not isinstance(meta, dict) or hashlib.sha256(data).hexdigest() != meta.get("sha256"):
        raise Refused(f"sample {path} does not match its manifest's sha256; it was edited")
    if data and not data.endswith(b"\n"):
        raise Refused(f"sample {path} does not end in a newline; derive writes one per row")
    # Every line is a row: no blank line or unparsed line is skipped.
    rows = [parse_sample_row(line, index) for index, line in enumerate(data.decode("utf-8").split("\n")[:-1])]
    if len({row["n"] for row in rows}) != len(rows):
        raise Refused(f"sample {path}: a row number n appears twice")
    # Absent input refuses (harmonic-forge#1289 epoch 2): a sample of any
    # other size, however it was made, holds a bar over fewer rows than 10,000.
    # refuses: an absent manifest size reads as None and never equals SIZE.
    size = never_refuse_sample.SIZE
    if meta.get("size") != size or len(rows) != size:
        raise Refused(f"sample {path} has {len(rows)} rows (manifest size {meta.get('size')!r}); "
                      f"a sample is exactly {size} rows")
    return rows


#: Exactly the keys `never_refuse_sample.py derive` writes per row.
SAMPLE_ROW_KEYS = frozenset({"n", "id", "ts", "session", "sidechain", "version", "cwd", "class", "command"})


def parse_sample_row(line: str, index: int) -> dict:
    """One sample row, parsed (harmonic-forge#1289 epoch 3): a JSON object
    with exactly derive's keys, `n` an int, `command` a non-empty str and
    `cwd` a str or null. Anything else refuses, including a blank line."""
    where = f"sample row {index + 1}"
    try:
        row = json.loads(line)
    except ValueError as exc:
        raise Refused(f"{where} is not JSON") from exc
    _exact_keys(row, SAMPLE_ROW_KEYS, where)
    if not _is_count(row["n"]):
        raise Refused(f"{where}: n must be an integer")
    if not isinstance(row["command"], str) or not row["command"]:
        raise Refused(f"{where}: command must be a non-empty string")
    if row["cwd"] is not None and not isinstance(row["cwd"], str):
        raise Refused(f"{where}: cwd must be a string or null")
    return row


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_precheck_layer_{Path(path).stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.refuses


def run_layer(refuses, rows: list[dict], env_names: frozenset[str]) -> dict:
    """A layer's return is parsed too (harmonic-forge#1289 epoch 3, codex #9):
    `None` allows, a non-empty `str` refuses, and anything else (`""`, a
    bool, a number) is an error, which fails the sample bar."""
    refusals, errors, reasons = 0, 0, []
    for row in rows:
        try:
            reason = refuses(row["command"], row["cwd"], env_names)
        except Exception:  # noqa: BLE001 - a layer that raises is counted, never trusted
            errors += 1
            continue  # fail-direction: counted as an error, and errors fail the bar
        if reason is None:
            continue  # fail-direction: only None allows; every other non-str value is an error below
        if not isinstance(reason, str) or not reason:
            errors += 1  # fail-direction: outside the contract is an error, never an allow
            continue
        refusals += 1
        if len(reasons) < MAX_REASONS:  # display cap only; every refusal is counted above
            reasons.append((row["n"], reason[:80]))
    return {"refusals": refusals, "size": len(rows), "errors": errors, "reasons": reasons}


#: Constant controls, keyed by fixed names; never input.
CONTROLS = {"null": lambda command, cwd, env_names: None,
            "all": lambda command, cwd, env_names: "control: refuses every row"}


# --------------------------------------------------------------------------

def score(layer_file: Path, sample: Path, db: Path, logs: dict, overlays: Path,
          control: bool = False) -> dict:
    layer_set = load_layer_set(layer_file)
    try:
        replay_rows, other, skipped = read_replay(db, logs, overlays)
    except failure_store.Refused as exc:
        raise Refused(str(exc)) from exc
    replay = score_replay(replay_rows, layer_set)
    replay["keyed_not_overlaid"] = other
    replay["overlay_skipped"] = skipped
    rows = read_sample(sample)
    env_names = frozenset(os.environ)
    bars, layers = [], {}
    for layer in layer_set["layers"]:
        bar = replay_bar(layer, replay)
        if bar is not None:  # refuses: a layer with neither bar was refused by the parser
            bars.append(bar)
        result = None  # fail-direction: no module is not-measurable, a FAIL under --strict, never a bare PASS
        if layer["module"] is not None:
            result = run_layer(load_module(layer["module"]), rows, env_names)
        # refuses: keyed by name, and duplicate names were refused by the parser.
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
    lines = [(f"replay: {replay['total']} overlay-keyed slips "
              f"({replay['keyed_not_overlaid']} keyed, not overlaid, not scored; "
              f"{replay['overlay_skipped']} overlay row(s) skipped, empty key_v1)")]
    lines += [f"  {owner}: {count}" for owner, count in replay["owners"].items()]
    lines.append(f"  repeat_owned {replay['repeat_owned']}, repeat_unowned {replay['repeat_unowned']}, "
                 f"multi_claimed {replay['multi_claimed']}")
    lines.append(f"sample: {report['sample_rows']} rows")
    for name, result in [*report["layers"].items(), *(("control " + k, v) for k, v in report["controls"].items())]:
        if result is None:
            lines.append(f"  {name}: no module, not measurable on the sample")
            continue  # fail-direction: display only; the bar itself is not-measurable
        lines.append(f"  {name}: {result['refusals']}/{result['size']} refused, {result['errors']} errors")
        lines += [f"    n={n}: {reason}" for n, reason in result["reasons"]]
    unmeasured = sum(1 for b in report["bars"] if b["status"] == "not-measurable")
    verdict = "FAIL" if code != EXIT_OK else (
        f"PASS with {unmeasured} bar(s) not measured" if unmeasured else "PASS")
    lines.append(f"verdict: {verdict}: " + "; ".join(_bar_text(b) for b in report["bars"]))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("score", help="Score a layer set.")
    s.add_argument("--layers", type=Path, required=True)
    # refuses: each default is the real store; an absent sample refuses, and an absent
    # overlays folder leaves every replay bar 0/0, which fails.
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
