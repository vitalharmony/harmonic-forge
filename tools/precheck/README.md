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
    [--seed 1279] [--until <ISO timestamp, default now>]
```

1. Reads `*/*.jsonl` and `*/*/subagents/*.jsonl` under `--root` in sorted path
   order, and joins each Bash `tool_use` to its `tool_result`s by
   `tool_use_id`.
2. **Shape guard** (exit 3): any Bash result whose `content` is neither a
   string nor a list of text items, or whose `is_error` is not a bool. Per
   Claude Code `version` the manifest reports `calls`, `joined`, `no_result`
   and `ok_with_stdout`; there is no per-version floor, because a version with
   no joined result (a still-running call on a just-updated version) already
   fails safe: the ran-check excludes each such call as `no_result`.
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
6. **Ordinary classifier**, a recognizer of literal shapes, not an
   interpreter (harmonic-forge#1289 epoch-2 REFORGE): nothing computes a shell
   effect. It refuses before lexing any backtick, any unquoted `#`, any quoted
   span holding `;&|()<>#` or a newline, a backslash-escaped blank, an
   unquoted-delimiter heredoc in a string holding `$(` or whose body holds a
   backtick, a `$` or a backslash (an unquoted body must be literal text,
   epoch-3 PATCH), and `<<<WORD`. Then
   every simple command from `tools/hooks/shell_parse.simple_commands()` must
   match one row: a READ program or git/gh read under its option allowlist;
   a SCRATCH_WRITE program whose every operand is a scratch target; `mktemp`
   under its own option row; or a probe, exactly
   `[prog, "--help"]` with `prog` on a READ or SCRATCH_WRITE row or git/gh.
   `argv[0]` is matched as written: an assignment, a path or a wrapper
   (`env`, `sudo`, `nice`, `timeout`, `command`, `xargs`, ...) is refused, and
   no shell `-c` string is unwrapped. A scratch target is a literal
   `/tmp/<segment>/...` (no `..`, `.`, braces, globs, `$`, `~` or blanks;
   never relative, so `cd` is never consulted). Before any of this the raw text
   must pass one character-class check: outside single quotes it holds none of
   backslash, backtick, `$`, `#`, `{` or `}`. Every loop (`while`, `until`, `for`)
   is excluded.
   Anything else is excluded with a counted reason; a string that does not
   parse is counted as `unparseable`.
7. Samples exactly 10,000 strings (pinned; there is no `--size`) with
   `random.Random(seed)` over the sorted pool
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

`score` also refuses a sample whose sha256 no longer matches its manifest,
and a sample that is not exactly 10,000 rows (by count and by manifest
`size`). Each row is parsed: a JSON object with exactly the keys derive
writes, `n` a unique integer, `command` a non-empty string and `cwd` a string
or null; a blank or unparseable line refuses rather than being skipped.

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
  by whether a layer owns them. The build's overlay accounting is read
  back: any malformed overlay row, or any overlay row that matches no failure
  record, refuses (a dropped row could only raise a share). Rows with an
  empty-string `key_v1` are deliberate and are printed as `skipped`; an
  absent, null or false `key_v1` is malformed and refuses.
- **Sample**: every layer with a `module` runs over every row.
  `--control` adds a `null` layer (never refuses) and an `all` layer (always
  refuses).
- **Bars**: a replay bar reports as `<layer>.coverage`, declaration coverage: it
  holds when claimed/total >= `min_share`, and no layer runs on a slip, because
  a failure record stores no command, and every class it names must have a
  slip. Only a sample bar executes a layer. A sample bar
  holds when refusals <= `sample_max_refusals` and the layer raised no error. A
  replay bar with no slips in its classes fails: an empty or mistyped
  `--overlays` scores nothing, and nothing scored is never a pass. A layer with
  no module reports its sample bar `not-measurable`, and the verdict line then
  reads `PASS with N bar(s) not measured`, never a bare `PASS`; when no bar was
  measured at all, the run fails.

Exit codes: 0 every measurable bar holds (`--strict`: a not-measurable bar also
fails); 4 a bar fails; 2 refused; 3 any other error. Never 1.

## The layer-set contract

```json
{
  "version": 1,
  "layers": [
    {"name": "lint", "claims": ["*|shell"], "claims_basis": "validated", "module": null,
     "bar": {"replay": {"classes": ["shell"], "min_share": 1.0}, "sample_max_refusals": 0}}
  ],
  "existing": {"policy": "existing deny hooks"},
  "accepted_uncovered": ["exists"]
}
```

- Parsed, never validated (harmonic-forge#1289 epoch 3): at every level the
  keys must be exactly the ones shown, and every one is required. An unknown
  key and an absent key both refuse (exit 2); there are no defaults. Write
  `null` for no module or for an unused bar half, and `{}`/`[]` for an empty
  `existing`/`accepted_uncovered`. Every layer needs a `bar` setting a
  `replay` bar, a `sample_max_refusals` bar or both, a layer with a `module`
  must set `sample_max_refusals`, and a set with no layers refuses: nothing
  scored is never a pass.
- Layer names are unique. A claim appears once per layer, a class is never
  both `existing` and `accepted_uncovered`, and an `existing` owner is a
  non-empty label that is not a layer name, `UNCOVERED` or
  `accepted-uncovered:*`. Ownership is scored as an identity (a layer's
  index, an existing class, an accepted class), never by comparing labels.
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

  `env_names` is always the empty set: the sample rows are historical, so
  the harness never passes its own live environment to them, and the shell
  it runs from cannot change a verdict. A layer that raises (including
  `SystemExit`; `KeyboardInterrupt` still stops the run), or returns anything
  but `None` or a non-empty `str` (`""`, `False`, `0`), is counted as an
  error, and an error fails its sample bar. A module that exits while loading
  is exit 3.

- `claims_basis` (required) is `validated` (each claim is a class the
  layer's check was shown to validate) or `declared` (the layer says it
  guards these classes, not yet shown). A declared layer's replay bar prints
  as `<name>.coverage (declared claims)`, so its holds is never read as measured
  coverage.

`layersets/design-v1.json` is the design record's layer set (lint, gate,
provenance) with no modules yet; issues D, E and F supply them. Its gate
claims are `declared` (operator ruling, harmonic-forge#1289): decision 3 wants
each registry mode to claim only the classes its dry run validates, and that
registry is issue C, which replaces the wildcard claims with per-tool ones.
