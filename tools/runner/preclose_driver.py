#!/usr/bin/env python3
"""Headless preclose driver for the te-runner (harmonic-forge#1059, G5).

An interactive Lane 1 session runs the refuter panel that
`tools/gh/preclose_check.py` plans. The unattended runner has no such
session, so this module runs exactly one pass in its place:

    preclose_driver.py run --repo R --issue N --base REF --head SHA \\
        --source-repo PATH --tier fast|standard|deep --out DIR

1. refuse when the issue's receipt already holds `MAX_PASSES` passes;
2. `preclose_check.py` plan, run from `--source-repo` (never an agent's
   worktree). Plan refuses without a passing `kill_check.py` receipt; for
   runner items Lane 1 writes those checks (operator ruling, 2026-10-19);
3. one `claude -p --agent preclose-inspection` refuter per planned lens, in a
   scratch export of the head, briefed with the issue's acceptance criteria,
   the diff and the lens only;
4. one cross-family `verify` call;
5. `preclose_check.py --complete --cross-family` with every finding undismissed.

Exit 0 when the pass was recorded (survivors or not), 2 when it was not.
Writes `findings.json`, `envelope.jsonl`, `preclose.json` (the reducer's
`preclose` fact, harmonic-forge#1051) and `summary.md` under `--out`. It
never posts to the issue, labels anything, or passes `--force`, `--reforge`,
`--allow-dirty`, `--not-triggered`, `--cluster-verdict` or
`--allow-repo-mismatch`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
GH_TOOLS = HERE.parent / "gh"
sys.path.insert(0, str(GH_TOOLS))
import preclose_check  # noqa: E402
import preclose_passes  # noqa: E402

PRECLOSE = GH_TOOLS / "preclose_check.py"
CROSS_FAMILY = HERE.parent / "lane" / "cross_family_call.sh"
BRIEF_BUILDER = HERE.parent / "lane" / "build_cross_family_brief.py"
RUNNER_GH = Path.home() / ".config" / "te-runner" / "gh"
RUNNER_KEY = Path.home() / ".config" / "te-runner" / "anthropic-key"
STRIPPED = ("GH_TOKEN", "GITHUB_TOKEN")
FORBIDDEN = ("--force", "--reforge", "--allow-dirty", "--not-triggered", "--cluster-verdict",
             "--allow-repo-mismatch")
DEFAULT_MODEL = "claude-opus-5-5"
REFUTER_TIMEOUT = 1800


class DriverError(Exception):
    """The pass could not be recorded; the message says why."""


def runner_env() -> dict[str, str]:
    """F1055 AC6's env: the runner's own key and gh config, the operator's
    tokens stripped, for this process tree. A missing or loosely permissioned
    key refuses the run; there is no fallback to the operator's login."""
    try:
        mode = stat.S_IMODE(RUNNER_KEY.stat().st_mode)
    except OSError as exc:
        raise DriverError(f"runner key {RUNNER_KEY} unreadable: {exc}") from exc
    if mode != 0o600:
        raise DriverError(f"runner key {RUNNER_KEY} has mode {oct(mode)}, not 0o600")
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED}
    env["ANTHROPIC_API_KEY"] = RUNNER_KEY.read_text(encoding="utf-8").strip()
    env["GH_CONFIG_DIR"] = str(RUNNER_GH)
    return env


def call(argv: list[str], *, cwd: str | Path | None = None, env: dict[str, str] | None = None,
         stdin: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Every subprocess the driver starts goes through here, so the refusals
    are checked in one place (AC2)."""
    bad = [flag for flag in FORBIDDEN if flag in argv]
    if bad:
        raise DriverError(f"refusing to run with {', '.join(bad)}: {argv[:3]}")
    try:
        return subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriverError(f"{argv[0]} failed: {exc}") from exc


def git(source: Path, *args: str) -> str:
    result = call(["git", "-C", str(source), *args])
    if result.returncode:
        raise DriverError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def acceptance_criteria(repo: str, issue: int, env: dict[str, str]) -> str:
    result = call(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body"], env=env)
    if result.returncode:
        raise DriverError(f"cannot read {repo}#{issue}: {result.stderr.strip()}")
    match = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", result.stdout,
                      re.MULTILINE | re.DOTALL)
    if not match or not match.group(1).strip():
        raise DriverError(f"{repo}#{issue} has no '## Acceptance criteria' section")
    return match.group(1).strip()


def passes_held(repo: str, issue: int) -> int:
    receipt = preclose_check.find_receipt(repo, issue)
    return len(preclose_passes.current(preclose_passes.history(receipt or {})))


def planned_lenses(stdout: str) -> list[str]:
    lenses, inside = [], False
    for line in stdout.splitlines():
        if line.strip() == "lenses:":
            inside = True
        elif inside and line.startswith("    - "):
            lenses.append(line[6:].strip())
        elif inside:
            break
    if not lenses or any(lens not in preclose_check.LENSES for lens in lenses):
        raise DriverError(f"plan printed no recognizable lenses: {lenses!r}")
    return lenses


def export_head(source: Path, head: str, dest: Path) -> None:
    """A fresh tree of the head, made with `git -C <source>`: no worktree is
    added to the source repo and no agent checkout is read."""
    dest.mkdir(parents=True)
    tarball = dest.parent / "head.tar"
    git(source, "archive", "-o", str(tarball), head)
    if call(["tar", "-x", "-f", str(tarball), "-C", str(dest)]).returncode:
        raise DriverError(f"cannot unpack {head[:12]} from {source}")


def refuter_prompt(lens: str, criteria: str, diff: str) -> str:
    return (f"Lens: {lens}\n\n"
            "You are one refuter on a preclose panel. The working directory is the head of the "
            "change. Find a concrete defect: a file:line anchor and a failure scenario a test "
            "could reproduce. Read the code; do not trust the diff's comments.\n\n"
            f"## Acceptance criteria\n\n{criteria}\n\n## Diff\n\n```diff\n{diff}\n```\n\n"
            "Reply with ONLY a JSON list, no prose and no fence: "
            '[{"anchor": "path:line", "scenario": "...", "mechanism": "..."}], or [] for none.')


def last_json_list(text: str) -> list | None:
    """The outermost JSON list that ends last in `text`, or None. The
    preclose-inspection agent writes its report to a file and closes with
    prose, so the list is found in the reply, not required to be all of it
    (operator ruling, harmonic-forge#1059). A list nested inside another, such
    as a bracket in a scenario string, never wins over its container."""
    decoder, best = json.JSONDecoder(), None
    for start in (i for i, ch in enumerate(text) if ch == "["):
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, list) and (best is None or end > best[1]
                                        or (end == best[1] and start < best[0])):
            best = (start, end, value)
    return best[2] if best else None


def parse_findings(text: str) -> list[dict]:
    """A refuter's reply as findings, or DriverError. Only the three fields are
    kept, so a refuter can never smuggle in a `dismissed` field (AC4)."""
    data = last_json_list(text)
    if data is None:
        raise DriverError("refuter output holds no JSON list")
    out = []
    for item in data:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and item[k].strip()
                for k in ("anchor", "scenario", "mechanism")):
            raise DriverError(f"refuter finding malformed: {item!r}")
        out.append({k: item[k] for k in ("anchor", "scenario", "mechanism")})
    return out


def run_refuter(lens: str, prompt: str, cwd: Path, env: dict[str, str], model: str,
                budget: str) -> tuple[list[dict], int | None]:
    result = call(["claude", "-p", "--agent", "preclose-inspection", "--model", model,
                   "--permission-prompts", "none", "--permission-mode", "dontAsk",
                   "--max-budget-usd", budget, "--output-format", "json"],
                  cwd=cwd, env=env, stdin=prompt, timeout=REFUTER_TIMEOUT)
    if result.returncode:
        raise DriverError(f"refuter {lens} exited {result.returncode}: {result.stderr.strip()[:300]}")
    try:
        envelope = json.loads(result.stdout)
    except ValueError as exc:
        raise DriverError(f"refuter {lens} printed no JSON envelope") from exc
    if not isinstance(envelope, dict) or envelope.get("is_error") or not isinstance(
            envelope.get("result"), str):
        raise DriverError(f"refuter {lens} returned an error envelope")
    usage = envelope.get("usage")
    tokens = None
    if isinstance(usage, dict):
        parts = [usage.get(k) for k in ("input_tokens", "output_tokens",
                                        "cache_creation_input_tokens", "cache_read_input_tokens")]
        if all(isinstance(p, int) for p in parts):
            tokens = sum(parts)
    return [{**f, "lens": lens} for f in parse_findings(envelope["result"])], tokens


def run_pass(args: argparse.Namespace, env: dict[str, str]) -> int:
    source, out = Path(args.source_repo).resolve(), Path(args.out).resolve()
    # A fresh --out per run: anything already there would read as this run's
    # output whenever this run fails before replacing it.
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise DriverError(f"--out {out} is not an empty directory")
    head = git(source, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
    held = passes_held(args.repo, args.issue)
    if held >= preclose_passes.MAX_PASSES:
        raise DriverError(f"{args.repo}#{args.issue} already holds {held} passes; a third never runs")
    check = [sys.executable, str(PRECLOSE), "--repo", args.repo,
             "--issue", str(args.issue), "--base", args.base, "--head", head]
    planned = call([*check, "--tier", args.tier], cwd=source, env=env)
    if planned.returncode:
        raise DriverError(f"plan refused: {(planned.stderr or planned.stdout).strip()[-400:]}")
    lenses = planned_lenses(planned.stdout)
    criteria = acceptance_criteria(args.repo, args.issue, env)
    diff = git(source, "diff", f"{args.base}...{head}")

    with tempfile.TemporaryDirectory(prefix="preclose-driver-") as scratch:
        tree = Path(scratch) / "head"
        export_head(source, head, tree)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(lenses)) as pool:
            futures = [pool.submit(run_refuter, lens, refuter_prompt(lens, criteria, diff), tree,
                                   env, args.model, args.budget_usd) for lens in lenses]
            results = [f.result() for f in futures]
        panel_ms = max(1, int((time.monotonic() - started) * 1000))
        findings = [f for found, _tokens in results for f in found]
        tokens = [t for _found, t in results]
        findings_path = Path(scratch) / "findings.json"
        findings_path.write_text(json.dumps(findings, indent=1) + "\n", encoding="utf-8")

        brief, envelope = Path(scratch) / "brief.md", Path(scratch) / "envelope.jsonl"
        (Path(scratch) / "diff.patch").write_text(diff, encoding="utf-8")
        (Path(scratch) / "criteria.md").write_text(criteria, encoding="utf-8")
        built = call([sys.executable, str(BRIEF_BUILDER),
                      "--artifact", str(Path(scratch) / "criteria.md"),
                      "--artifact", str(Path(scratch) / "diff.patch"),
                      "--intent", f"Satisfy the acceptance criteria of {args.repo}#{args.issue}.",
                      "--question", "Does this change contain a defect against its acceptance "
                                    "criteria that the in-family panel missed?",
                      "--assumption", "The change at the working directory satisfies every "
                                      "acceptance criterion with no concrete failing scenario.",
                      "--evidence", ".", "--out", str(brief)], env=env)
        if built.returncode:
            raise DriverError(f"cross-family brief refused: {built.stderr.strip()[-300:]}")
        started = time.monotonic()
        crossed = call(["bash", str(CROSS_FAMILY), "--caller", "claude", "--families", "2",
                        "--posture", "verify", "--brief", str(brief), "--cwd", str(tree),
                        "--out", str(envelope)], env=env)
        cross_ms = max(1, int((time.monotonic() - started) * 1000))
        # The envelope lives in this run's scratch directory, so one that exists
        # was written by this call. A failed or fallback call is still recorded
        # when it left one: --complete labels it, and the reducer sets the item
        # aside on that label.
        if not envelope.is_file():
            raise DriverError(f"cross-family call left no envelope (exit {crossed.returncode})")

        cost = (["--panel-tokens", str(sum(tokens)), "--panel-ms", str(panel_ms)]
                if all(isinstance(t, int) for t in tokens)
                else ["--cost-unavailable", "a refuter's envelope carried no usage"])
        completed = call([*check, "--complete", "--cross-family", "--own-model", args.model,
                          "--findings", str(findings_path), "--envelope", str(envelope),
                          "--cross-family-ms", str(cross_ms), *cost], cwd=source, env=env)
        if completed.returncode:
            raise DriverError(f"--complete refused: "
                              f"{(completed.stderr or completed.stdout).strip()[-400:]}")
        provenance = next((line.strip() for line in completed.stdout.splitlines()
                           if line.strip().startswith("Red-team provenance:")), "")
        survivors = len(preclose_check.surviving_findings(findings))
        fact = {"status": "complete", "survivors": survivors, "provenance": provenance}
        # Outputs reach --out only once the pass is recorded.
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(findings_path, out / "findings.json")
        shutil.copy2(envelope, out / "envelope.jsonl")
        (out / "preclose.json").write_text(json.dumps(fact, indent=1) + "\n", encoding="utf-8")
        write_summary(out / "summary.md", args, head, lenses, findings, fact)
    print(json.dumps(fact))
    return 0


def write_summary(path: Path, args: argparse.Namespace, head: str, lenses: list[str],
                  findings: list[dict], fact: dict) -> None:
    rows = "\n".join(f"| {i} | {f['lens']} | `{f['anchor']}` | {f['scenario']} | {f['mechanism']} |"
                     for i, f in enumerate(findings, 1)) or "| - | - | - | none | - |"
    path.write_text(
        f"## Preclose pass — {args.repo}#{args.issue} at `{head[:12]}` (Tier {args.tier})\n\n"
        f"Headless driver: {len(lenses)} refuters ({', '.join(lenses)}), every finding "
        f"undismissed. **Surviving: {fact['survivors']}.** {fact['provenance']}\n\n"
        "| # | Lens | Anchor | Scenario | Mechanism |\n|---|---|---|---|---|\n"
        f"{rows}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--issue", type=int, required=True)
    run.add_argument("--base", required=True)
    run.add_argument("--head", required=True)
    run.add_argument("--source-repo", required=True)
    run.add_argument("--tier", required=True, choices=("fast", "standard", "deep"))
    run.add_argument("--out", required=True)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--budget-usd", default="5")
    args = parser.parse_args(argv)
    try:
        return run_pass(args, runner_env())
    except Exception as exc:  # noqa: BLE001 -- anything raised means no pass was recorded
        print(f"preclose-driver: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
