# Contributing to harmonic-forge

Thanks for looking. This is a small, opinionated repo: the rules, tools and templates for running a three-lane (Blueprint / Muscle / Control Gate) workflow with AI coding agents. Contributions are welcome. This page is the shortest path from "I cloned it" to "I opened a pull request".

## Try it first

Run the [Quickstart](README.md#quickstart) in the README. If the Quickstart does not work as written on a fresh clone, that is itself a worthwhile issue to report.

## Report a problem or an idea

Open an issue with one of the forms in [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) (bug report or feature request). If you want something to start on, look for issues labeled `good first issue` or `help wanted`.

## Send a change

1. Fork the repo and create a branch.
2. Run `git config core.hooksPath .githooks` once in your clone (the hooks keep direct commits to `main` out and link the platform rules).
3. Make your change, then run the check CI runs:

   ```bash
   mise run ci-check
   ```

4. Open a pull request. CI runs the same `mise run ci-check` on it. For a first-time contributor a maintainer approves the first workflow run; CI is the authority on whether the change is green.

`mise run check` is the maintainers' superset of `ci-check`: it adds steps that need the maintainer's machine (platform-link drift, a cross-registry sweep, a context-budget ratchet). You do not need it, and it will not pass on a fork.

## Propose a rule

Rules are numbered `<!-- R-NNNN -->` spans inside `rules/*.md` and `3-lane-protocol.md`, registered in `tools/rules/registry.toml`.

```bash
python3 tools/rules/check_rule_drift.py --next-id   # the next free R-ID
mise run rules-check                                # registry and text must agree
```

Every rule names the incident that produced it (what went wrong, where). A rule with no incident behind it will be asked for one.

## How review works

A maintainer reviews every pull request. Changes to rules and tooling also go through this repo's own three-lane review (see [`3-lane-protocol.md`](3-lane-protocol.md)); that is run by the maintainers, so you do not run the lanes yourself. Expect questions about the incident or the failure a change prevents more than about style.

## License

Contributions are made under the repository's [MIT license](LICENSE).
