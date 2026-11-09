# tools/precheck: the replay harness and the never-refuse sample

A local acceptance tool for precheck layer sets (harmonic-forge#1289, issue B
of the harmonic-forge#1279 design). It answers one question: does a candidate
layer set meet its pass bars, measured on the replay set (the slips a layer
should have refused) and on a sample of ordinary commands (which no layer may
refuse)?

It is a measuring tool. Nothing here is wired into a hook, and nothing here
gates a command.

## Where the data lives: never here

harmonic-forge is public. The failure records, the replay overlays, the store
and the never-refuse sample all hold local data, and all of it stays in
`~/Harmonic_Projects/operator-memory/`:

| Data | Path |
|---|---|
| Failure store (rebuilt from the logs) | `operator-memory/failure-store.sqlite` |
| Replay overlays | `operator-memory/failure-overlays/*.jsonl` |
| Never-refuse sample and manifest | `operator-memory/never-refuse/never-refuse-sample-v1.jsonl` (+ `.manifest.json`) |

`derive` refuses an `--out` inside a harmonic-forge checkout or worktree, and
`failure_store.build` refuses a `--db` there. `.gitignore` covers
`never-refuse-sample*` as a second line of defence. Add `never-refuse/` to
operator-memory's own `.gitignore` by hand. The tests use synthetic fixtures
only.

Neither tool prints a command, a result text or a stored probe: only counts,
owners, coarse bucket labels and a layer's own refusal reasons.

## `never_refuse_sample.py derive`

```
python3 tools/precheck/never_refuse_sample.py derive [--root ~/.claude/projects]
    [--out <operator-memory>/never-refuse/never-refuse-sample-v1.jsonl]
    [--size 10000] [--seed 1279] [--until <ISO timestamp, default now>]
```

1. Reads `*/*.jsonl` and `*/*/subagents/*.jsonl` under `--root` in sorted path
   order, and joins each Bash `tool_use` to its `tool_result`s by
   `tool_use_id`.
2. **Shape guard** (exit 3): any Bash result whose `content` is neither a
   string nor a list of text items, or whose `is_error` is not a bool; or any
   Claude Code `version` with no joined result or no non-error result carrying
   a `toolUseResult.stdout`.
3. **Ran-check**, positive: a call is kept only when it has a result, no
   joined decision is `reject` or `cancelled`, and every result is non-error
   or starts `Exit code `. Everything else is excluded and counted by a coarse
   label in the manifest's `excluded_by_bucket`.
4. De-duplicates exact strings, keeping the earliest record by
   `(timestamp, path, line)`.
5. **Secret scan**: blanks the record's own `cwd`, `$HOME` and
   `/tmp/claude-<uid>/<project-dir>` (exact strings), runs
   `tools/memory/secret_scan.findings()` unchanged, and drops the string on any
   hit. The stored command is the original string.
6. **Ordinary classifier** over `tools/hooks/shell_parse.simple_commands()`:
   every simple command must be a read, a probe or a scratch write (a path
   under `/tmp/` after normalization). Anything else is excluded with a counted
   reason; a string that does not parse is counted as `unparseable`.
7. Samples `--size` strings with `random.Random(seed)` over the sorted pool
   (exit 3 if the pool is smaller), re-scans every row before writing (exit 3
   on any hit), and writes the manifest temp, then the sample temp, then
   renames the sample, then the manifest.

Exit codes: 0 written; 2 refused, nothing written; 3 nothing written for any
other reason. Never 1.

### Orphan recovery

The sample is frozen: `derive` never overwrites one. A crash leaves one of
these, and the next run refuses naming it:

| Left behind | Message | Recovery |
|---|---|---|
| `never-refuse-sample*.tmp` | leftover temp from an interrupted derive | delete the temp files, re-run |
| the sample without its manifest | orphan sample from an interrupted derive | delete the sample, re-run (`score` refuses it too) |
| the manifest without the sample | orphan manifest | delete the manifest, re-run |
| both | already derived | nothing: the sample is frozen; delete both to re-derive |

`score` also refuses a sample whose sha256 no longer matches its manifest.

## `replay_harness.py score`

```
python3 tools/precheck/replay_harness.py score --layers <layer-set.json>
    [--sample <path>] [--db/--memory-log/--testplan-log/--overlays as failure_store.py]
    [--json] [--strict] [--control]
```

- **Replay**: rebuilds the failure store if stale and reads
  `failure_keys WHERE overlaid ORDER BY ts, log, line`. Each slip goes to the
  first layer whose claims match, else to `existing[class]`, else to
  `accepted-uncovered:<class>`, else `UNCOVERED`. `repeat_owned` and
  `repeat_unowned` count slips whose effective `tool|class` appeared earlier,
  by whether a layer owns them.
- **Sample**: every layer with a `module` runs over every row.
  `--control` adds a `null` layer (never refuses) and an `all` layer (always
  refuses).
- **Bars**: a replay bar holds when claimed/total >= `min_share`; a sample bar
  holds when refusals <= `sample_max_refusals` and the layer raised no error. A
  layer with no module reports its sample bar `not-measurable`.

Exit codes: 0 every measurable bar holds (`--strict`: a not-measurable bar also
fails); 4 a bar fails; 2 refused; 3 any other error. Never 1.

## The layer-set contract

```json
{
  "version": 1,
  "layers": [
    {"name": "lint", "claims": ["*|shell"], "module": null,
     "bar": {"replay": {"classes": ["shell"], "min_share": 1.0}, "sample_max_refusals": 0}}
  ],
  "existing": {"policy": "existing deny hooks"},
  "accepted_uncovered": ["exists"]
}
```

- A claim is `*|<class>` (any tool) or `<tool>|<class>` (one tool's mode). Every
  class must be in `tools/memory/failure_classes.json`'s current version; the
  loader refuses (exit 2) anything else.
- Layers are scored in file order and the first claiming layer owns a slip. A
  slip claimed by two layers is counted as `multi_claimed`.
- `module` is a path (relative to the layer-set file) to a Python file defining

  ```python
  def refuses(command: str, cwd: str | None, env_names: frozenset[str]) -> str | None:
      """A short refusal reason, or None to allow."""
  ```

  `env_names` is the harness process's live environment names. A layer that
  raises fails its sample bar.

`layersets/design-v1.json` is the design record's layer set (lint, gate,
provenance) with no modules yet; issues D, E and F supply them.
