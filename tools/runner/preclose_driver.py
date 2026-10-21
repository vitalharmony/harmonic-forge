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
   Steps 1-2 and the reads after them are `READ_ONLY_STEPS`, run first and
   every refusal collected (`read_only_preconditions`); the te-runner's
   preflight runs the same list per queued item (harmonic-forge#1112 AC12);
3. one `claude -p --json-schema` refuter per planned lens, in a scratch clone
   of the head (a git repository, AC14), briefed with the issue's acceptance
   criteria, the diff and the lens only, returning its findings as structured
   output;
4. one cross-family `verify` call, briefed with one assumption per acceptance
   criterion and the harness's own results (`--harness-results`: the kill
   receipt, the base-relative check, the deny check);
5. `preclose_check.py --complete --cross-family` with every finding undismissed.

Exit 0 when the pass was recorded (survivors or not), 2 when it was not, and
4 when the receipt shows a pass recorded but the run failed afterwards (or the
receipt could not be re-read): never retry on 4.
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
sys.path.insert(0, str(HERE))
import acceptance  # noqa: E402
import sandbox  # noqa: E402

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
#: The exported head's `origin` (harmonic-forge#1112 AC14, as AC3).
UPSTREAM_URL = "https://github.com/vitalharmony/harmonic-forge.git"
PROVENANCE_TOOL = HERE.parent / "lane" / "cross_family_provenance.py"
#: The receipt gained a pass but the run failed afterwards (or the receipt
#: could not be re-read): never retry, the item is set aside.
RECORDED_INCOMPLETE = 4


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


def issue_body(repo: str, issue: int, env: dict[str, str]) -> str:
    result = call(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body"], env=env)
    if result.returncode:
        raise DriverError(f"cannot read {repo}#{issue}: {result.stderr.strip()}")
    return result.stdout


def acceptance_criteria(repo: str, issue: int, env: dict[str, str]) -> str:
    """The acceptance section through the shared matcher (harmonic-forge#1112 AC11)."""
    text = acceptance.section(issue_body(repo, issue, env))
    if not text:
        raise DriverError(f"{repo}#{issue} has no acceptance criteria section")
    # A section with no criterion items is refused, never one blanket
    # assumption (harmonic-forge#1112 sticky-wicket patch 1).
    if not acceptance.items(text):
        raise DriverError(f"{repo}#{issue}'s acceptance section lists no criteria")
    return text


def _passes(receipt: dict | None) -> int:
    return len(preclose_passes.current(preclose_passes.history(receipt or {})))


def passes_held(repo: str, issue: int) -> int:
    return _passes(preclose_check.find_receipt(repo, issue))


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
    """A git repository at the head (harmonic-forge#1112 AC14): a `git clone
    --shared --no-checkout` of SOURCE, the head checked out detached, and
    `origin` set to GitHub as the agent's clone is (AC3). No worktree is added
    to the source repo and no agent checkout is read; objects are borrowed
    through alternates, so each boundary names SOURCE's object store."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise DriverError(f"cannot export {head[:12]}: {dest} already exists")
    git(source, "clone", "-q", "--shared", "--no-checkout", str(source), str(dest))
    git(dest, "checkout", "-q", "--detach", head)
    git(dest, "remote", "set-url", "origin", UPSTREAM_URL)


def source_objects(source: Path) -> Path:
    """SOURCE's object store, which an exported head borrows from."""
    return (source / git(source, "rev-parse", "--git-path", "objects").strip()).resolve()


# --- the read-only preconditions (harmonic-forge#1112 AC12) --------------------------
#
# Every step the driver checks before it spends, as one list. The driver runs
# them first; the preflight runs the same list per queued item before any
# agent spend. Each step reads `ctx` and adds what later steps need; a step
# whose input an earlier step failed to produce is skipped, since that failure
# is already listed. The plan writes a planned receipt and needs a kill-check
# receipt at the head, so it runs only for a real head and only when nothing
# before it failed.

def _step_out_dir(ctx: dict) -> None:
    # A fresh --out per run: anything already there would read as this run's
    # output whenever this run fails before replacing it.
    out = ctx.get("out")
    if out is not None and out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise DriverError(f"--out {out} is not an empty directory")


def _step_head(ctx: dict) -> None:
    if ctx.get("head_ref"):
        ctx["head"] = git(ctx["source"], "rev-parse", "--verify",
                          f"{ctx['head_ref']}^{{commit}}").strip()


def _step_passes(ctx: dict) -> None:
    held = passes_held(ctx["repo"], ctx["issue"])
    if held >= preclose_passes.MAX_PASSES:
        raise DriverError(f"{ctx['repo']}#{ctx['issue']} already holds {held} passes; "
                          "a third never runs")


def plan_refusals_without_head(repo: str, issue: int, source: Path) -> None:
    """The subset of `preclose_check.plan`'s refusals that needs no head: the
    receipt dir is writable, no IMC waiver receipt is unconfirmed, no live
    IMC hook record lacks its receipt, and `source` has no uncommitted or
    untracked path (the interactive plan's dirty-tree refusal). Raises DriverError naming the refusal,
    or naming that the plan could not be checked (harmonic-forge#1112
    sticky-wicket patch 3). The full plan, which needs a kill-check receipt at
    the head and writes a planned receipt, runs once the head exists."""
    try:
        preclose_check.require_writable(preclose_check.receipt_dir())
        imc = preclose_check.imc_waiver
        held = imc.waiver_receipt(repo, issue)
        if held is not None and not imc.waiver_satisfied(held):
            raise DriverError(f"plan refused: {repo}#{issue} holds an IMC waiver receipt that "
                              f"is not recorded (status {held.get('status', 'unreadable')!r})")
        unmatched = imc.unmatched_hook_record(repo, issue)
        if unmatched is not None:
            raise DriverError(f"plan refused: {repo}#{issue} holds a live IMC hook record "
                              f"(granted {unmatched.get('granted_at')}) but no matching "
                              "waiver receipt")
        status = call(["git", "-c", "core.quotePath=false", "status", "--porcelain", "-z"],
                      cwd=source)
        if status.returncode:
            raise DriverError(f"plan not checked before spend: `git status` failed in {source} "
                              f"(exit {status.returncode})")
        dirty = [entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3]
        if dirty:
            raise DriverError(f"plan refused: {len(dirty)} uncommitted/untracked path(s) in "
                              f"{source}: {', '.join(dirty[:5])}")
    except DriverError:
        raise
    except SystemExit as exc:
        raise DriverError(f"plan refused: receipt dir not writable (exit {exc.code})") from exc
    except Exception as exc:  # noqa: BLE001 -- a check that cannot run is a failure
        raise DriverError(f"plan not checked before spend: {exc}") from exc


def _step_plan(ctx: dict) -> None:
    # With no head (the preflight, te_queue add) the plan never returns
    # silently: its head-independent refusals run, and one that cannot run is
    # listed as a failure (harmonic-forge#1112 sticky-wicket patch 3).
    if "head" not in ctx:
        plan_refusals_without_head(ctx["repo"], ctx["issue"], ctx["source"])
        return
    if ctx["errors"]:
        return
    planned = call([*ctx["check"], "--tier", ctx["tier"]], cwd=ctx["source"], env=ctx["env"])
    if planned.returncode:
        raise DriverError(f"plan refused: {(planned.stderr or planned.stdout).strip()[-400:]}")
    ctx["plan_stdout"] = planned.stdout


def _step_lenses(ctx: dict) -> None:
    if "plan_stdout" in ctx:
        ctx["lenses"] = planned_lenses(ctx["plan_stdout"])


def _step_acceptance(ctx: dict) -> None:
    ctx["criteria"] = acceptance_criteria(ctx["repo"], ctx["issue"], ctx["env"])


def _step_export(ctx: dict) -> None:
    export_head(ctx["source"], ctx.get("head") or ctx["base"], ctx["scratch"] / "head")
    ctx["tree"] = ctx["scratch"] / "head"


READ_ONLY_STEPS = (_step_out_dir, _step_head, _step_passes, _step_plan, _step_lenses,
                   _step_acceptance, _step_export)


def preconditions_context(repo: str, issue: int, base: str, source: Path, scratch: Path,
                          env: dict[str, str], *, head: str | None = None,
                          out: Path | None = None, tier: str | None = None) -> dict:
    ctx = {"repo": repo, "issue": issue, "base": base, "source": Path(source).resolve(),
           "scratch": Path(scratch), "env": env, "head_ref": head, "out": out, "tier": tier,
           "errors": []}
    return ctx


def read_only_preconditions(ctx: dict) -> list[str]:
    """Every step in READ_ONLY_STEPS, each refusal collected, never only the
    first. With no head (the preflight, before any agent runs) the head is the
    base and the plan runs only its head-independent refusals."""
    for step in READ_ONLY_STEPS:
        if ctx.get("head") and "check" not in ctx:
            ctx["check"] = [sys.executable, str(PRECLOSE), "--repo", ctx["repo"],
                            "--issue", str(ctx["issue"]), "--base", ctx["base"],
                            "--head", ctx["head"]]
        try:
            step(ctx)
        except DriverError as exc:
            ctx["errors"].append(str(exc))
    return list(ctx["errors"])


#: The structured output each refuter must return (`claude -p --json-schema`).
#: additionalProperties false keeps a refuter from adding a `dismissed` field.
FINDINGS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["anchor", "scenario", "mechanism"],
        "properties": {k: {"type": "string"} for k in ("anchor", "scenario", "mechanism")}}}},
}


def refuter_prompt(lens: str, criteria: str, diff: str) -> str:
    """The inspection instructions inline: `--json-schema` does not carry
    through `--agent`, so the refuter is a plain run (operator ruling,
    harmonic-forge#1059)."""
    return (f"Lens: {lens}\n\n"
            "You are one refuter on an adversarial preclose panel, reviewing a finished change "
            "against its acceptance criteria. The working directory is the head of the change. "
            "Find concrete defects through this lens. Every finding needs a file:line anchor, "
            "a failure scenario (inputs or state, then the wrong behavior) and the mechanism "
            "(the approach that fails, not the symptom). Discard anything without a concrete "
            "scenario. Read the code; do not trust the diff's comments.\n\n"
            f"## Acceptance criteria\n\n{criteria}\n\n## Diff\n\n```diff\n{diff}\n```\n\n"
            "Return the findings in the structured output; an empty list when none meets the bar.")


def parse_findings(structured: object) -> list[dict]:
    """A refuter's structured output as findings, or DriverError. Only the
    three fields are kept, so a refuter can never add a `dismissed` field (AC4)."""
    data = structured.get("findings") if isinstance(structured, dict) else None
    if not isinstance(data, list):
        raise DriverError("refuter returned no structured findings list")
    out = []
    for item in data:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and item[k].strip()
                for k in ("anchor", "scenario", "mechanism")):
            raise DriverError(f"refuter finding malformed: {item!r}")
        out.append({k: item[k] for k in ("anchor", "scenario", "mechanism")})
    return out


def run_refuter(lens: str, prompt: str, cwd: Path, env: dict[str, str], model: str,
                budget: str, inputs: tuple[Path, ...] = ()) -> tuple[list[dict], int | None]:
    argv = ["claude", "-p", "--model", model,
            "--permission-prompts", "none", "--permission-mode", "dontAsk",
            "--max-budget-usd", budget, "--output-format", "json",
            "--json-schema", json.dumps(FINDINGS_SCHEMA)]
    try:
        # A refuter may run the head's (the agent's) tests, so it runs inside the
        # bwrap boundary: only its export of the head and a private transcripts
        # dir are writable (harmonic-forge#1098).
        argv = sandbox.wrap(argv, cwd, Path(tempfile.mkdtemp(prefix="transcripts-", dir=cwd.parent)),
                            profile="agent", inputs=inputs)
    except sandbox.SandboxError as exc:
        raise DriverError(str(exc)) from exc
    result = call(argv, cwd=cwd, env=env, stdin=prompt, timeout=REFUTER_TIMEOUT)
    if result.returncode:
        raise DriverError(f"refuter {lens} exited {result.returncode}: {result.stderr.strip()[:300]}")
    try:
        envelope = json.loads(result.stdout)
    except ValueError as exc:
        raise DriverError(f"refuter {lens} printed no JSON envelope") from exc
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise DriverError(f"refuter {lens} returned an error envelope")
    usage = envelope.get("usage")
    tokens = None
    if isinstance(usage, dict):
        parts = [usage.get(k) for k in ("input_tokens", "output_tokens",
                                        "cache_creation_input_tokens", "cache_read_input_tokens")]
        if all(isinstance(p, int) for p in parts):
            tokens = sum(parts)
    return [{**f, "lens": lens} for f in parse_findings(envelope.get("structured_output"))], tokens


def assumptions(criteria: str) -> list[str]:
    """One assumption per acceptance criterion (harmonic-forge#1112 AC14), read
    through the shared matcher's item list; a section with no list items is
    one criterion."""
    found = acceptance.items(criteria) or [" ".join(criteria.split())]
    return [f"Acceptance criterion {n} holds at the working directory's HEAD: {text}"
            for n, text in enumerate(found, 1)]


def harness_results(path: Path | None) -> str:
    """The harness's own results for this head (AC14): the kill receipt (each
    AC's base and head rc), the base-relative check result (AC4) and the
    deny-check result, rendered for the brief. The runner writes them as JSON."""
    if path is None:
        return "No harness results were supplied for this head (the driver ran on its own).\n"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DriverError(f"--harness-results {path} unreadable: {exc}") from exc
    kill = data.get("kill") or []
    lines = ["Results the harness obtained for this head, before this review.", "",
             "Kill receipt (each acceptance criterion's check, base rc then head rc; a "
             "kill check must fail on the base and pass on the head):"]
    lines += [f"- {k.get('ac')}: base rc {k.get('base_rc')}, head rc {k.get('head_rc')}"
              for k in kill if isinstance(k, dict)] or ["- none recorded"]
    check = data.get("check") or {}
    lines += ["", f"Base-relative `mise run check`: check_rc {check.get('check_rc')}; "
                  f"new failures on the head: {check.get('new_failures') or 'none'}; "
                  f"failures already on the base: {check.get('base_failures') or 'none'}."]
    hits = data.get("deny_hits")
    lines += ["", f"Deny check: {'no hits' if not hits else 'hits: ' + '; '.join(hits)}."]
    return "\n".join(lines) + "\n"


def build_brief(scratch: Path, repo: str, issue: int, criteria: str, diff: str,
                harness: str, env: dict[str, str]) -> Path:
    brief = scratch / "brief.md"
    for name, text in (("diff.patch", diff), ("criteria.md", criteria),
                       ("harness-results.md", harness)):
        (scratch / name).write_text(text, encoding="utf-8")
    argv = [sys.executable, str(BRIEF_BUILDER),
            "--artifact", str(scratch / "criteria.md"),
            "--artifact", str(scratch / "diff.patch"),
            "--artifact", str(scratch / "harness-results.md"),
            "--intent", f"Satisfy the acceptance criteria of {repo}#{issue}.",
            "--question", "Does this change satisfy each acceptance criterion? One verdict per "
                          "assumption, from the evidence below.",
            "--evidence", ". (a git repository at the head; `git log -1`, `git show HEAD`)",
            "--evidence", "harness-results.md, embedded below: the kill receipt, the "
                          "base-relative check result and the deny-check result for this head",
            "--out", str(brief)]
    for assumption in assumptions(criteria):
        argv[argv.index("--out"):argv.index("--out")] = ["--assumption", assumption]
    built = call(argv, env=env)
    if built.returncode:
        raise DriverError(f"cross-family brief refused: {built.stderr.strip()[-300:]}")
    return brief


def cross_family_verify(tree: Path, scratch: Path, brief: Path, env: dict[str, str],
                        inputs: tuple[Path, ...] = ()) -> tuple[Path, int]:
    """One cross-family `verify` call in the reviewer boundary: (envelope, ms).
    The reviewers' own workspace config in the head (`.codex/`) is
    the agent's to write: it is removed from the export first."""
    for config in (".codex",):
        if (tree / config).is_symlink() or (tree / config).is_file():
            (tree / config).unlink()
        else:
            shutil.rmtree(tree / config, ignore_errors=True)
    envelope = scratch / "envelope.jsonl"
    try:
        cross_argv = sandbox.wrap(
            ["bash", str(CROSS_FAMILY), "--caller", "claude", "--families", "2",
             "--posture", "verify", "--brief", str(brief), "--cwd", str(tree),
             "--out", str(envelope)], scratch, profile="reviewer",
            inputs=tuple(dict.fromkeys((Path(CROSS_FAMILY).resolve().parent, HERE.parent,
                                        *inputs))))
    except sandbox.SandboxError as exc:
        raise DriverError(str(exc)) from exc
    started = time.monotonic()
    crossed = call(cross_argv, cwd=scratch, env=env)
    # The envelope lives in this run's scratch directory, so one that exists
    # was written by this call.
    if not envelope.is_file():
        raise DriverError(f"cross-family call left no envelope (exit {crossed.returncode})")
    return envelope, max(1, int((time.monotonic() - started) * 1000))


def provenance_label(envelope: Path, own_model: str, env: dict[str, str]) -> str:
    """The platform classifier's label for an envelope, as --complete computes it."""
    result = call([sys.executable, str(PROVENANCE_TOOL), "--envelope", str(envelope),
                   "--own-model", own_model], env=env)
    label = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if result.returncode or not label:
        raise DriverError(f"provenance classifier failed ({result.returncode}): "
                          f"{result.stderr.strip()[-200:]}")
    return label


def run_pass(args: argparse.Namespace, env: dict[str, str], progress: dict | None = None) -> int:
    """`progress` records the two facts main() needs after a failure: that
    --complete was started, and that it exited 0."""
    progress = {} if progress is None else progress
    source, out = Path(args.source_repo).resolve(), Path(args.out).resolve()
    with tempfile.TemporaryDirectory(prefix="preclose-driver-") as scratch_dir:
        scratch = Path(scratch_dir)
        ctx = preconditions_context(args.repo, args.issue, args.base, source, scratch, env,
                                    head=args.head, out=out, tier=args.tier)
        errors = read_only_preconditions(ctx)
        if errors:
            raise DriverError("; ".join(errors))
        head, lenses, criteria, tree = ctx["head"], ctx["lenses"], ctx["criteria"], ctx["tree"]
        check = ctx["check"]
        diff = git(source, "diff", f"{args.base}...{head}")
        harness = harness_results(Path(args.harness_results) if args.harness_results else None)
        objects = (source_objects(source),)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(lenses)) as pool:
            futures = [pool.submit(run_refuter, lens, refuter_prompt(lens, criteria, diff), tree,
                                   env, args.model, args.budget_usd, objects) for lens in lenses]
            results = [f.result() for f in futures]
        panel_ms = max(1, int((time.monotonic() - started) * 1000))
        findings = [f for found, _tokens in results for f in found]
        tokens = [t for _found, t in results]
        findings_path = scratch / "findings.json"
        findings_path.write_text(json.dumps(findings, indent=1) + "\n", encoding="utf-8")

        brief = build_brief(scratch, args.repo, args.issue, criteria, diff, harness, env)
        # A failed or fallback call is still recorded when it left an envelope:
        # --complete labels it, and the reducer sets the item aside on that label.
        envelope, cross_ms = cross_family_verify(tree, scratch, brief, env, objects)

        cost = (["--panel-tokens", str(sum(tokens)), "--panel-ms", str(panel_ms)]
                if all(isinstance(t, int) for t in tokens)
                else ["--cost-unavailable", "a refuter's envelope carried no usage"])
        progress["complete_started"] = True
        completed = call([*check, "--complete", "--cross-family", "--own-model", args.model,
                          "--findings", str(findings_path), "--envelope", str(envelope),
                          "--cross-family-ms", str(cross_ms), *cost], cwd=source, env=env)
        if completed.returncode:
            raise DriverError(f"--complete refused: "
                              f"{(completed.stderr or completed.stdout).strip()[-400:]}")
        progress["complete_ok"] = True
        provenance = next((line.strip() for line in completed.stdout.splitlines()
                           if line.strip().startswith("Red-team provenance:")), "")
        survivors = len(preclose_check.surviving_findings(findings))
        fact = {"status": "complete", "survivors": survivors, "provenance": provenance}
        # Outputs reach --out only once the pass is recorded.
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(findings_path, out / "findings.json")
        shutil.copy2(envelope, out / "envelope.jsonl")
        shutil.copy2(brief, out / "brief.md")
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
    run.add_argument("--harness-results", default=None,
                     help="JSON of the harness's kill receipt, check and deny results (AC14)")
    args = parser.parse_args(argv)
    before, progress = None, {}
    try:
        before = preclose_check.find_receipt(args.repo, args.issue)
        return run_pass(args, runner_env(), progress)
    except Exception as exc:  # noqa: BLE001
        print(f"preclose-driver: {exc}", file=sys.stderr)
    return 2 if not recorded_or_unknown(args, before, progress) else RECORDED_INCOMPLETE


def recorded_or_unknown(args: argparse.Namespace, before: dict | None, progress: dict) -> bool:
    """After a failure: may a pass have been recorded? Facts only, never how
    far this code got. --complete exiting 0 is a recorded pass; so is a
    receipt whose pass count rose. A receipt that cannot be read (preclose's
    reader returns None for it) once --complete was started could be one.
    Any yes is exit 4: retrying could spend the issue's last pass."""
    reason = "--complete exited 0" if progress.get("complete_ok") else None
    if reason is None:
        try:
            after = preclose_check.find_receipt(args.repo, args.issue)
        except Exception as exc:  # noqa: BLE001
            after, reason = None, f"the receipt could not be re-read ({exc})"
        if reason is None and after is not None and _passes(after) > _passes(before):
            reason = "the receipt gained a pass"
        if reason is None and after is None and progress.get("complete_started"):
            reason = "--complete was started and the receipt is now unreadable"
    if reason is None:
        return False
    print(f"preclose-driver: a pass may be recorded ({reason}); outputs are incomplete. "
          "Do not retry.", file=sys.stderr)
    return True

if __name__ == "__main__":
    sys.exit(main())
