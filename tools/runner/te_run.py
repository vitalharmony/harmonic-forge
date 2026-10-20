#!/usr/bin/env python3
"""The te-runner's serial driver (harmonic-forge#1055, G1, G3).

    te_run.py run --queue FILE --max-budget-usd N [--max-steps 5]

Works a `te_queue.py` queue one item at a time. The harness alone writes item
statuses: the agent is never given the queue path. Per item:

1. re-check eligibility (`te_queue.first_failure`); an ineligible item is
   reduced and set aside without a checkout;
2. create `fix/<N>-te-runner` from `origin/main` in a fresh `git clone
   --shared` (an existing branch or checkout of that name blocks the item,
   never reused). The agent and every run of its code (`mise run check`, the
   kill-check tests, the preclose refuters) run inside `sandbox.py`'s `bwrap`
   boundary (harmonic-forge#1098);
3. run `claude -p` in it with `te-runner.settings.json`, the runner's own key
   and gh config (`preclose_driver.runner_env`), and stream-json with hook
   events. G3: the run aborts unless `SessionStart`, `UserPromptSubmit` and
   `Stop` hook events each appear and `init.apiKeySource` is not `none`;
4. `mise run check`, `kill_runner.py run`, then `kill_check.py` over checks
   generated from the bundle and the agent's own diff reversed (operator
   ruling on this issue), `deny_check.py diff`, and `preclose_driver.py run`
   exactly once (any non-zero exit sets the item aside; never retried);
5. `verdict.reduce` over the facts. Only `green` is pushed to the
   `runner-fork` remote and opened as a PR from the runner identity;
   `pushed_sha` is saved before the push. The live PR is then read into the
   facts and reduced again, and the preclose summary is attached as a PR
   comment. The runner never merges, closes, labels or comments on an issue.

`deny_check.py boundary` runs once at start: a failure aborts the run. Every
oracle test runs with `PYTHONSAFEPATH=1` (`python3 -P`), so a head-authored
module never shadows pytest or the standard library (operator ruling).

Exit 0 when the queue was worked, 3 when the run aborted (boundary, G3, or a
blocked item), 2 on an error before any item ran.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
sys.path.insert(0, str(HERE.parent / "telemetry"))
import deny_check  # noqa: E402
import preclose_driver  # noqa: E402
import sandbox  # noqa: E402
import queue_state  # noqa: E402
import te_queue  # noqa: E402
import verdict  # noqa: E402

SOURCE = HERE.parent.parent
SETTINGS = HERE / "te-runner.settings.json"
PRECLOSE_DRIVER = HERE / "preclose_driver.py"
WORKTREE_ROOT = Path.home() / "Harmonic_Projects" / ".worktrees"
RUNNER_LOGIN = deny_check.RUNNER_LOGIN
FORK_URL = f"https://github.com/{RUNNER_LOGIN}/harmonic-forge.git"
REQUIRED_HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")
SELF_FLAG = "TE-RUNNER-SELF-FLAG:"
SAFE_PATH = {"PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1"}
CLAUDE = "claude"
RECONCILED = "recovery (push attempted):"
#: Every `done` item carries these keys (AC3); work_item fills what it learned.
DONE_KEYS = {"reason": "", "verdict": None, "facts": None, "ac_titles": [], "diff_stat": "",
             "head_sha": "", "branch": "", "pr": "", "pushed_sha": "", "preclose_summary": "",
             "live_read_at": ""}


class Abort(Exception):
    """The whole run stops; the current item is blocked with this note."""


class Blocked(Exception):
    """This item cannot proceed; the note says why."""


def log(step: str, message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} te-run {step}: {message}", file=sys.stderr, flush=True)


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        # errors="replace": an agent's non-UTF-8 output must never escape the
        # per-item boundary as UnicodeDecodeError. The reverse patch, whose
        # bytes matter, is read separately (kill_check_checks).
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc}")


def git(*args: str, cwd: Path | None = None) -> str:
    # SOURCE is read at call time, never bound as a default: a default would
    # freeze the module's value and ignore any later change to it.
    result = run(["git", *args], cwd=cwd or SOURCE)
    if result.returncode:
        raise Blocked(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def oracle_env(env: dict[str, str]) -> dict[str, str]:
    return {**env, **SAFE_PATH}


# --- the queue -------------------------------------------------------------------

def reconcile(state: dict) -> bool:
    """A kill between push and save leaves `pushed_sha` without `pr`: make the
    pushed branch visible, then `queue_state.recover` blocks the rest."""
    changed = False
    for item in state["items"]:
        if item["status"] in ("blocked", "in_progress") and item.get("pushed_sha") \
                and not item.get("pr"):
            if item["note"].startswith(RECONCILED):
                continue
            # pushed_sha is saved before the push, so it proves an attempt, not
            # a push: say so, and keep what the run itself recorded.
            item["status"] = "blocked"
            item["note"] = (f"{RECONCILED} branch {item.get('branch')} may have been pushed to "
                            f"runner-fork at {item['pushed_sha'][:12]}; no PR was recorded. "
                            f"Earlier note: {item['note'] or 'none'}")
            changed = True
    return queue_state.recover(state) or changed


def save(path: Path, state: dict) -> None:
    queue_state.save(path, state)


# --- the agent run ---------------------------------------------------------------

def agent_prompt(issue: int, body: str, handoff: str) -> str:
    """The issue and its handoff only: never the queue, never a status."""
    return (f"Implement harmonic-forge#{issue} in this checkout, following the Lane 1 handoff "
            "below exactly. Commit your work with `git commit` when it is done; do not push, "
            "and do not open a pull request. If you cannot complete the issue honestly, end "
            f"your final message with one line `{SELF_FLAG} <reason>`.\n\n"
            f"## Issue\n\n{body}\n\n## Handoff\n\n{handoff}\n")


def claude_argv(budget: str) -> list[str]:
    return [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
            "--include-hook-events", "--settings", str(SETTINGS),
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-budget-usd", budget]


def parse_stream(text: str) -> dict:
    hooks: collections.Counter = collections.Counter()
    init, result = None, None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype in ("hook_started", "hook_response"):
            name = event.get("hook_event") or event.get("hook_name")
            if isinstance(name, str):
                hooks[(subtype, name)] += 1
        elif kind == "system" and subtype == "init":
            init = event
        elif kind == "result":
            result = event
    return {"hooks": hooks, "init": init, "result": result}


def g3_problems(parsed: dict) -> list[str]:
    problems = [f"no {hook} hook events" for hook in REQUIRED_HOOKS
                if not (parsed["hooks"][("hook_started", hook)]
                        and parsed["hooks"][("hook_response", hook)])]
    source = (parsed["init"] or {}).get("apiKeySource")
    if not parsed["init"]:
        problems.append("no init event")
    elif source in (None, "none"):
        problems.append(f"init.apiKeySource is {source!r}: not the runner's own key")
    if not isinstance(parsed["result"], dict):
        problems.append("no result event")
    return problems


def run_agent(clone: Path, prompt: str, budget: str, env: dict[str, str],
              log_path: Path, transcripts: Path) -> dict:
    if not SETTINGS.is_file():
        raise Abort(f"settings file {SETTINGS} is missing")
    try:
        # The agent runs inside the bwrap boundary (harmonic-forge#1098): only its
        # clone and a private transcripts dir are writable.
        argv = sandbox.wrap(claude_argv(budget), clone, transcripts)
    except sandbox.SandboxError as exc:
        raise Abort(str(exc)) from exc
    try:
        done = subprocess.run(argv, cwd=clone, env=env, input=prompt,
                              capture_output=True, text=True)
    except OSError as exc:
        raise Blocked(f"claude did not start: {exc}") from exc
    log_path.write_text(done.stdout, encoding="utf-8")
    parsed = parse_stream(done.stdout)
    problems = g3_problems(parsed)
    if problems:
        raise Abort("G3: " + "; ".join(problems))
    return parsed


# --- the oracle --------------------------------------------------------------------

def snapshot_bundle(issue: int, pin: str, scratch: Path) -> Path:
    """The bundle copied into this run's private scratch BEFORE the agent runs,
    and verified against the handoff's pin. The oracle uses only this copy, so
    nothing the agent does to the bundle on disk afterwards reaches it."""
    snapshot = scratch / "bundle"
    try:
        shutil.copytree(te_queue.bundle_dir(issue), snapshot, symlinks=True)
    except OSError as exc:
        raise Blocked(f"cannot snapshot the kill-check bundle: {exc}") from exc
    # A symlink anywhere in the copy (checks.json included) would point the
    # oracle at a file the agent can still write, so none is accepted. The copy
    # is what is scanned: it is exactly what the oracle will use.
    links = [str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_symlink()]
    if links:
        raise Blocked(f"the kill-check bundle holds a symlink ({', '.join(links[:3])}); refused")
    try:
        digest = te_queue.kill_runner.bundle_digest(snapshot)
    except te_queue.kill_runner.RunnerError as exc:
        raise Blocked(f"cannot snapshot the kill-check bundle: {exc}") from exc
    if digest != pin:
        raise Blocked("the kill-check bundle no longer matches the handoff's kill-bundle-sha256")
    return snapshot


def kill_facts(issue: int, base: str, head: str, env: dict[str, str], bundle: Path,
               pin: str) -> list[dict]:
    result = run([sys.executable, "-P", str(HERE / "kill_runner.py"), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--bundle", str(bundle), "--source-repo", str(SOURCE)],
                 cwd=SOURCE, env=oracle_env(env))
    if result.returncode:
        log("kill", f"kill_runner could not run: {result.stderr.strip()[:200]}")
        return []
    try:
        report = json.loads(result.stdout)
        checks, digest = report["checks"], report["bundle_sha256"]
    except (ValueError, KeyError, TypeError):
        return []
    if digest != pin:  # the bundle kill_runner actually ran is the pinned one
        log("kill", "kill_runner ran a bundle other than the pinned one")
        return []
    return checks


def kill_check_checks(bundle: Path, base: str, head: str, scratch: Path) -> Path:
    """One `kill_check.py` check per bundle check: the bundle's files copied
    into the scratch tree, then its test; the stub patch is the agent's change
    reversed, outside the bundle's own paths (operator ruling, harmonic-forge#1055)."""
    owned = set(te_queue.kill_runner.bundle_files(bundle))
    changed = [path for path in git("diff", "--name-only", "--no-renames", "-z", base, head)
               .split("\0") if path and path not in owned]
    patch = scratch / "revert.patch"
    reverse = subprocess.run(["git", "diff", "--binary", "--no-renames", head, base, "--",
                              *changed], cwd=SOURCE, capture_output=True) if changed else None
    if reverse is None or reverse.returncode or not reverse.stdout:
        raise Blocked("the agent's change has nothing to reverse")
    patch.write_bytes(reverse.stdout)
    checks = []
    for entry in te_queue.kill_runner.load_checks(bundle):
        overlay = f"cp -R {_quote(str(bundle / 'files'))}/. . && exec \"$@\""
        # The test executes the agent's code, so it runs inside the boundary,
        # writable = kill_check.py's own scratch tree (the cwd at test time). The
        # trusted sandbox.py is named by absolute path, never the tree's copy.
        checks.append({"ac": entry["ac"], "mechanism": "the agent's change, reversed",
                       "patch": str(patch),
                       "test": [sys.executable, str(HERE / "sandbox.py"), "here", "--",
                                "sh", "-c", overlay, "kill-check", *entry["test"]]})
    path = scratch / "checks.json"
    path.write_text(json.dumps(checks, indent=1), encoding="utf-8")
    return path


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def kill_receipt(issue: int, base: str, head: str, env: dict[str, str], scratch: Path,
                 bundle: Path) -> bool:
    try:
        checks = kill_check_checks(bundle, base, head, scratch)
    except (Blocked, te_queue.kill_runner.RunnerError) as exc:
        log("kill-check", str(exc))
        return False
    result = run([sys.executable, str(HERE.parent / "gh" / "kill_check.py"), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--checks", str(checks)], cwd=SOURCE, env=oracle_env(env))
    log("kill-check", (result.stdout.strip().splitlines() or ["no output"])[-1])
    return result.returncode == 0


def deny_hits(base: str, head: str) -> list[str]:
    result = run([sys.executable, str(HERE / "deny_check.py"), "diff", "--base", base,
                  "--head", head], cwd=SOURCE)
    if result.returncode not in (0, 3):
        return [f"deny-check could not run: {result.stderr.strip()[:150]}"]
    try:
        return json.loads(result.stdout)["hits"]
    except (ValueError, KeyError, TypeError):
        return ["deny-check printed no result"]


def preclose_fact(issue: int, base: str, head: str, tier: str, env: dict[str, str],
                  out: Path) -> dict:
    """Exactly one driver run; any non-zero exit sets the item aside (operator
    ruling, harmonic-forge#1059): the runner never retries it."""
    result = run([sys.executable, str(PRECLOSE_DRIVER), "run",
                  "--repo", te_queue.REPO, "--issue", str(issue), "--base", base, "--head", head,
                  "--source-repo", str(SOURCE), "--tier", tier, "--out", str(out)],
                 cwd=SOURCE, env=env)
    if result.returncode:
        return {"status": f"driver exit {result.returncode}", "survivors": 0, "provenance": ""}
    try:
        return json.loads((out / "preclose.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "driver wrote no preclose.json", "survivors": 0, "provenance": ""}


# --- the PR ------------------------------------------------------------------------

def live_facts(pr: str, head: str, env: dict[str, str]) -> dict:
    result = run(["gh", "pr", "view", pr, "--repo", te_queue.REPO, "--json",
                  "headRefOid,statusCheckRollup"], env=env)
    try:
        data = json.loads(result.stdout) if not result.returncode else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"pr_exists": False, "head_sha_matches": False, "ci": "pending"}
    states = [str(c.get("conclusion") or c.get("state") or "").upper()
              for c in data.get("statusCheckRollup") or [] if isinstance(c, dict)]
    ci = ("red" if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for s in states)
          else "green" if states and all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states)
          else "pending")
    return {"pr_exists": True, "head_sha_matches": data.get("headRefOid") == head, "ci": ci}


def open_pr(item: dict, state: dict, queue: Path, issue: int, head: str,
            branch: str, titles: list[str], env: dict[str, str]) -> str:
    # Pushed from SOURCE, never from the agent's clone: the clone's .git/config
    # and hooks are agent-writable and would run as the operator (#1098).
    if run(["git", "remote", "get-url", "runner-fork"], cwd=SOURCE).returncode:
        git("remote", "add", "runner-fork", FORK_URL)
    item.update({"pushed_sha": head, "branch": branch})
    save(queue, state)  # before the push: a kill after it stays visible
    pushed = run(["git", "push", "runner-fork", f"{head}:refs/heads/{branch}"], cwd=SOURCE,
                 env=env)
    if pushed.returncode:
        raise Blocked(f"push to runner-fork failed: {pushed.stderr.strip()[:200]}")
    body = (f"Unattended te-runner implementation for harmonic-forge#{issue}.\n\n"
            + "".join(f"- {title}\n" for title in titles)
            + "\nProposed for the operator's review; the runner never merges.\n")
    created = run(["gh", "pr", "create", "--repo", te_queue.REPO, "--base", "main",
                   "--head", f"{RUNNER_LOGIN}:{branch}",
                   "--title", f"te-runner: {titles[0] if titles else 'implementation'} "
                              f"(harmonic-forge#{issue})", "--body", body], env=env)
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    if created.returncode or not url.startswith("https://"):
        raise Blocked(f"pushed branch {branch} at {head[:12]}, but gh pr create failed: "
                      f"{created.stderr.strip()[:150]}")
    return url


# --- one item ----------------------------------------------------------------------

def ac_titles(body: str) -> list[str]:
    section = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    titles = []
    for line in (section.group(1) if section else "").splitlines():
        match = re.match(r"\s*\d+\.\s+(.*)", line)
        if match:
            titles.append(re.split(r"(?<=[.:])\s", match.group(1), maxsplit=1)[0][:80])
    return titles


def self_flag(result: dict | None) -> str:
    text = (result or {}).get("result") or ""
    flags = [line.split(SELF_FLAG, 1)[1].strip() for line in str(text).splitlines()
             if SELF_FLAG in line]
    return flags[-1] if flags else ""


def emit_event(issue: int, head: str, extra: dict, cost: object) -> None:
    import emit  # noqa: PLC0415 -- telemetry is best-effort and imported late
    try:
        emit.emit([{"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": "te-runner", "repo": te_queue.REPO, "issue": issue,
                    "sha": head or None, "account": "vitalharmony", "org": "vitalharmony",
                    "actor": "unset", "event_type": "te-runner.item", "subject_kind": "te-issue",
                    "subject_id": f"{te_queue.REPO}#{issue}@{(head or 'none')[:12]}",
                    "provenance": "manual",
                    "attrs": {"reason": extra.get("reason") or "green",
                              "state": (extra.get("verdict") or {}).get("state", "blocked"),
                              "total_cost_usd": cost if isinstance(cost, (int, float)) else -1,
                              "permission_denials": (extra.get("facts") or {}).get(
                                  "permission_denials", -1)}}])
    except Exception as exc:  # noqa: BLE001
        log("telemetry", f"not written: {exc}")


def work_item(item: dict, state: dict, queue: Path, budget: str, env: dict[str, str]) -> dict:
    issue = te_queue.issue_number(item["id"])
    failure = te_queue.first_failure(issue)
    body = te_queue.gh_json(f"repos/{te_queue.REPO}/issues/{issue}").get("body") or ""
    titles = ac_titles(body)
    if failure:
        facts = {"eligible": failure[0] != "ineligible",
                 "deny_hits": [failure[1]] if failure[0] == "deny-path" else []}
        return {"facts": facts, "verdict": verdict.reduce(facts), "ac_titles": titles}
    branch = f"fix/{issue}-te-runner"
    clone = WORKTREE_ROOT / f"harmonic-forge-{issue}r-impl"
    if clone.exists() or not run(["git", "rev-parse", "--verify", "--quiet",
                                  f"refs/heads/{branch}"], cwd=SOURCE).returncode:
        raise Blocked(f"branch {branch} or checkout {clone} already exists; never reused")
    git("fetch", "-q", "origin", "main")
    base = git("rev-parse", "origin/main")
    # A standalone clone, not a linked worktree: its .git lives inside the
    # boundary's one writable directory, so the agent can commit (#1098).
    git("clone", "-q", "--shared", "--no-checkout", str(SOURCE), str(clone))
    git("checkout", "-q", "-b", branch, base, cwd=clone)
    log("clone", f"{clone} on {branch} from {base[:12]}")
    with tempfile.TemporaryDirectory(prefix="te-run-") as scratch_name:
        scratch = Path(scratch_name)
        handoff = te_queue.handoff(issue) or ""
        pinned = te_queue.DIGEST_RE.search(handoff)
        if not pinned:
            raise Blocked("the handoff pins no kill-bundle-sha256")
        pin = pinned.group(1)
        bundle = snapshot_bundle(issue, pin, scratch)  # before the agent can touch it
        parsed = run_agent(clone, agent_prompt(issue, body, handoff),
                           budget, env, scratch / "stream.jsonl", scratch / "transcripts")
        result = parsed["result"]
        # The head is read through SOURCE, never the clone's own git, and pinned
        # by a ref so a gc in SOURCE cannot prune it before the push (#1098).
        git("fetch", "-q", str(clone), "HEAD")
        head = git("rev-parse", "FETCH_HEAD")
        git("update-ref", f"refs/te-runner/{issue}", head)
        log("agent", f"head {head[:12]}, cost {result.get('total_cost_usd')}")
        try:
            check_argv = sandbox.wrap(["mise", "run", "check"], clone)
        except sandbox.SandboxError as exc:
            raise Abort(str(exc)) from exc
        check_rc = run(check_argv, cwd=clone, env=oracle_env(env)).returncode
        log("check", f"exit {check_rc}")
        kill = kill_facts(issue, base, head, env, bundle, pin)
        if kill and not kill_receipt(issue, base, head, env, scratch, bundle):
            kill = []
        log("kill", f"{len(kill)} check(s)")
        hits = deny_hits(base, head)
        log("deny", f"{len(hits)} hit(s)")
        preclose = preclose_fact(issue, base, head, te_queue.tier(issue) or "standard", env,
                                 scratch / "preclose")
        log("preclose", preclose.get("status", "?"))
        summary_path = scratch / "preclose" / "summary.md"
        summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
        facts = {"eligible": True, "deny_hits": hits,
                 "permission_denials": len(result.get("permission_denials") or []),
                 "budget_exceeded": result.get("subtype") == "error_max_budget_usd",
                 "self_flag": self_flag(result), "check_rc": check_rc, "kill": kill,
                 "preclose": preclose,
                 # Every clause but the PR's: a branch is pushed only on green.
                 "live": {"pr_exists": True, "head_sha_matches": True, "ci": "pending"}}
        outcome = verdict.reduce(facts)
        extra = {"head_sha": head, "branch": branch,
                 "diff_stat": (git("diff", "--stat", base, head).splitlines() or [""])[-1].strip(),
                 "ac_titles": titles, "total_cost_usd": result.get("total_cost_usd")}
        if outcome["state"] == "green":
            pr = open_pr(item, state, queue, issue, head, branch, titles, env)
            extra["pr"] = pr
            facts["live"] = live_facts(pr, head, env)
            extra["live_read_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            outcome = verdict.reduce(facts)
            if summary:
                posted = run(["gh", "pr", "comment", pr, "--repo", te_queue.REPO, "--body",
                              summary], env=env)
                url = posted.stdout.strip().splitlines()[-1] if posted.stdout.strip() else ""
                # The report links the summary (harmonic-forge#1056 AC3).
                extra["preclose_summary"] = url if url.startswith("https://") else ""
        return {**extra, "facts": facts, "verdict": outcome}


def run_queue(queue: Path, budget: str, max_steps: int) -> int:
    with te_queue.queue_lock(queue):
        return _run_queue(queue, budget, max_steps)


def set_status(item: dict, status: str, run_id: str, **fields: object) -> None:
    """Every status change goes through here, so every item this run touches
    carries the run's id and the report can scope to it (harmonic-forge#1056
    sticky-wicket PATCH: stamping by hand at each write site missed some)."""
    item.update({**fields, "status": status, "run_id": run_id})


def _run_queue(queue: Path, budget: str, max_steps: int) -> int:
    state = queue_state.load(queue)
    # The run's id first, saved at once: a run that ends before any item (a
    # boundary failure, an empty queue) must still replace the last run's id,
    # or the report would re-propose that run's work.
    # A random suffix: two runs in the same second must never share an id.
    run_id = ("run-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "-" + secrets.token_hex(4))
    state["run_id"] = run_id
    before = {item["id"]: (item["status"], item["note"]) for item in state["items"]}
    reconcile(state)  # queue_state.recover is imported unmodified, so its changes are
    for item in state["items"]:  # detected and stamped here rather than inside it
        if (item["status"], item["note"]) != before[item["id"]]:
            set_status(item, item["status"], run_id)
    save(queue, state)
    problems = deny_check.boundary()
    if problems:
        log("boundary", "; ".join(problems))
        return 3
    env = preclose_driver.runner_env()
    blocked = False
    for _step in range(max_steps):
        item = queue_state.next_item(state)
        if item is None:
            break
        set_status(item, "in_progress", run_id)
        save(queue, state)
        log("item", f"{item['id']} in progress")
        try:
            extra = work_item(item, state, queue, budget, env)
        except Exception as exc:  # noqa: BLE001 -- nothing may leave an item in_progress
            set_status(item, "blocked", run_id, note=str(exc))
            save(queue, state)
            emit_event(te_queue.issue_number(item["id"]), item.get("head_sha", ""),
                       {"reason": "blocked"}, None)
            log("item", f"{item['id']} blocked: {exc}")
            if not isinstance(exc, Blocked):  # Abort, or anything unexpected: stop the run
                return 3
            blocked = True
            continue
        outcome = extra["verdict"]
        set_status(item, "done", run_id, **extra, reason=outcome.get("reason") or "",
                   note=outcome.get("detail", ""))
        for key, default in DONE_KEYS.items():  # never overwrites pushed_sha set before a push
            item.setdefault(key, default)
        save(queue, state)
        emit_event(te_queue.issue_number(item["id"]), extra.get("head_sha", ""), item,
                   extra.get("total_cost_usd"))
        log("item", f"{item['id']} done: {outcome['state']} {outcome.get('reason') or ''}")
    return 3 if blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    worked = sub.add_parser("run")
    worked.add_argument("--queue", required=True)
    worked.add_argument("--max-budget-usd", required=True)
    worked.add_argument("--max-steps", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if not (math.isfinite(float(args.max_budget_usd)) and float(args.max_budget_usd) > 0) \
                or args.max_steps <= 0:
            raise ValueError("--max-budget-usd must be a finite positive number and "
                             "--max-steps positive")
        return run_queue(Path(args.queue), args.max_budget_usd, args.max_steps)
    except (ValueError, OSError, te_queue.QueueError, preclose_driver.DriverError,
            deny_check.DenyError) as exc:
        log("error", str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
