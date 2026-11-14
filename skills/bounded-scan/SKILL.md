---
name: bounded-scan
description: "Run a bounded gh list scan (every open issue, open PR, a milestone or a label of one vitalharmony repo) under the operator's SCAN grant, and read its snapshot. Use when work genuinely needs a full list the gh shim refuses as a scan (a wayfinder comparing against every open issue, an approved lane sweep). Do NOT use for one named issue or PR; read it by number."
---

# Bounded scan (harmonic-forge#1363)

The protection is the tool and the grant, not this page. `rules/lane-shorthand.md`
§ SCAN (R-0427) is the authority.

## When to ask

Only when the task needs a whole list, and no read by number or by name can
answer it. Say why in one sentence and ask the operator for the line.

## The grant

The operator types, as the prompt's first line:

    SCAN <letter> <scope>: <reason>

`<letter>` is H, F, I or O. `<scope>` is `open-issues`, `open-prs`,
`milestone=<number>` or `label=<name>`. The grant lasts 2 hours, for this
session, this repo and this scope.

## The run

1. `python3 ~/harmonic-forge/tools/gh/bounded_scan.py --repo <letter> --scope <scope> --dry-run`
   checks the grant and both budgets and prints the plan. Read it.
2. The same command without `--dry-run` prints the snapshot path. Add
   `--fields number,title,labels` to keep the snapshot small.
3. Read the snapshot JSON: `pages[].items[]`. `truncated: true` means
   `--max-pages` cut it short. A snapshot under 15 minutes old is served again
   with no call; an older one is refreshed with free conditional requests.

## Never

- Never use the `gh_scan_override` touch file for this. It is the operator's
  emergency fallback.
- Never ask for a cross-account letter (K, Y, D, P): the scan returns empty.
