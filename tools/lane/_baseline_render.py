"""CI-rendered visual baselines for lane1-rebase (harmonic-forge#1058).

A conflict in a path matching the target project's `[project.protocol]
rebase_visual_baselines.glob` takes origin/main's side at every rebase stop
(main's file, or absent when main has none). Only when such a conflict
happened, push 1 is the rebased commits plus an empty marker carrying
`Render-Pending: undispatched` (lane1_rebase), then one forward pass:

  list the workflow's `workflow_dispatch` run ids -> `gh workflow run
  <workflow> -f ref=<rendered>` (the rebased SHA, the marker's parent) -> the
  single NEW id is the render run (zero or several: exit 6, head stays
  `undispatched`) -> a second empty marker `Render-Pending: <run-id>` pushed
  within the lease -> a wall-clock-bounded wait (20 min; every gh/git call
  has a timeout) -> the run must have succeeded and carry
  `visual-baselines-<rendered>` -> the fetch task runs with HEAD at `rendered` -> the
  fetched files are carried onto the marker, only declared paths committed
  (no trailer) -> pushed within the lease on the marker. Exit 0 then prints
  `render-run=<id>`.

Forward-only (sticky-wicket REFORGE 6244217307, operator ruling 6244229339):
no file in .git and no resume. Any failure after push 1 goes through the one
handler in `render` and is exit 6 with a fixed manual-recovery recipe; a
`Render-Pending` head on entry is exit 6 with the same recipe and nothing
touched. F1066's readers refuse a marked head, so no unrendered head reaches
Lane 3. A run's `head_sha` is never read (on a dispatch it is main's).

Every helper takes `lr`, the lane1_rebase module itself, so its `Exit`, git
helpers and test stubs are the ones used here (no second import of it).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path

RENDER_TIMEOUT = 20 * 60  # seconds: the wall-clock bound on the wait
CALL_TIMEOUT = 120  # seconds: every gh/git call in the render path
FETCH_TIMEOUT = 15 * 60  # seconds: the fetch task (downloads the artifact)
POLL = 15
FIND_ATTEMPTS = 8  # about two minutes for a dispatched run to be listed
_sleep = time.sleep
_clock = time.monotonic


def config(lr, repo_dir: str) -> dict[str, str] | None:
    """The declared `{glob, workflow, fetch_task}`, or None (the project
    declares none, or the manifest is unreadable: today's behavior)."""
    sys.path.insert(0, str(lr._HERE.parent / "onboard"))
    try:
        import manifest  # noqa: PLC0415 -- lazy, as lane1_rebase.regenerate_config
        from manifest_protocol import normalize_repo  # noqa: PLC0415
        url = lr.git(repo_dir, "remote", "get-url", "origin").stdout.strip()
        project = manifest.by_repo().get(normalize_repo(url))
    except Exception:  # noqa: BLE001 -- an unreadable manifest declares nothing
        return None
    protocol = getattr(project, "protocol", None)
    decl = getattr(protocol, "rebase_visual_baselines", None) if protocol else None
    return dict(decl) if decl else None


def is_baseline(path: str, glob: str | None) -> bool:
    """`path` matches the declared glob component by component: `*` never
    crosses a `/`, and the component counts must agree."""
    if not glob:
        return False
    parts, pattern = path.split("/"), glob.split("/")
    return len(parts) == len(pattern) and all(
        fnmatchcase(part, pat) for part, pat in zip(parts, pattern))


def take_baselines(lr, prep: Path, paths: list[str]) -> None:
    """Each declared baseline takes stage :2 (origin/main plus the commits
    already replayed), add/add included; absent when that side has none."""
    for path in paths:
        if lr._show_raw(prep, f":2:{path}") is None:
            lr.git(prep, "rm", "-q", "-f", "--", path)
        else:
            lr.git(prep, "checkout", "--ours", "--", path)
            lr.git(prep, "add", "--", path)


def marker_of(lr, cwd, sha: str) -> str | None:
    """The `Render-Pending` run id on commit `sha`, read with gate_ci's own
    recognizer (harmonic-forge#1066), never a second parser."""
    gate_ci = lr._gate_ci()
    message = lr.git(cwd, "log", "-1", "--format=%B", sha, timeout=CALL_TIMEOUT).stdout
    try:
        return gate_ci.render_pending_from_message(message)
    except gate_ci.RenderPendingUnreadable as exc:
        raise lr.Exit(1, f"cannot read {gate_ci.RENDER_PENDING} on {sha[:12]}: {exc}; "
                         "nothing done") from exc


# ---------------------------------------------------------------- gh calls

def _gh(lr, *args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=CALL_TIMEOUT)
    if proc.returncode:
        raise lr.Exit(1, f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def list_dispatch_ids(lr, slug: str, workflow: str) -> set[str]:
    """The ids of the workflow's recent `workflow_dispatch` runs (never
    head_sha, never created_at: no clock is compared)."""
    out = _gh(lr, "api", f"repos/{slug}/actions/workflows/{workflow}/runs"
                         "?event=workflow_dispatch&per_page=30",
              "--jq", "[.workflow_runs[] | select(.event == \"workflow_dispatch\") | .id]")
    return {str(i) for i in json.loads(out or "[]")}


def dispatch(lr, slug: str, workflow: str, sha: str) -> None:
    _gh(lr, "workflow", "run", workflow, "-R", slug, "-f", f"ref={sha}")


def read_run(lr, slug: str, run_id: str) -> dict:
    return json.loads(_gh(lr, "api", f"repos/{slug}/actions/runs/{run_id}",
                          "--jq", "{status, conclusion}"))


def run_artifacts(lr, slug: str, run_id: str) -> list[str]:
    out = _gh(lr, "api", f"repos/{slug}/actions/runs/{run_id}/artifacts?per_page=100",
              "--jq", "[.artifacts[].name]")
    return json.loads(out or "[]")


def wait_run(lr, slug: str, run_id: str) -> dict | None:
    """Poll until the run completes; None once RENDER_TIMEOUT of wall clock
    has passed (a deadline, not an iteration count)."""
    deadline = _clock() + RENDER_TIMEOUT
    while True:
        run = read_run(lr, slug, run_id)
        if run.get("status") == "completed":
            return run
        if _clock() >= deadline:
            return None
        _sleep(POLL)


def find_new_run(lr, slug: str, workflow: str, before: set[str]) -> str:
    """Operator ruling 6244229339: the single `workflow_dispatch` id listed
    after the dispatch that was not listed before it. Zero or several is
    exit 6 with no marker pushed."""
    new: set[str] = set()
    for attempt in range(FIND_ATTEMPTS):
        new = list_dispatch_ids(lr, slug, workflow) - before
        if new:
            break
        if attempt + 1 < FIND_ATTEMPTS:
            _sleep(POLL)
    if len(new) != 1:
        raise lr.Exit(6, f"{len(new)} new workflow_dispatch runs of {workflow} after the "
                         f"dispatch ({', '.join(sorted(new)) or 'none'}); cannot tell which "
                         "renders this push; head stays Render-Pending: undispatched")
    return next(iter(new))


# ---------------------------------------------------------------- the pass

def recovery(branch: str, pushed: str, rendered: str, run_id: str | None,
             cfg: dict[str, str] | None) -> str:
    """The fixed manual-recovery recipe every exit 6 prints."""
    workflow = cfg["workflow"] if cfg else "<render workflow>"
    known = run_id not in (None, "undispatched")
    fetch = (cfg["fetch_task"].format(run_id=run_id if known else "<id>") if cfg
             else "<fetch task> --run <id>")
    start = ("wait for that run (or re-dispatch it: " if known
             else "no render run was identified (marker `undispatched`); dispatch one ("
             ) + f"`gh workflow run {workflow} -f ref={rendered}`)"
    return (f"origin/{branch} is at {pushed}; render run {run_id or 'none'}. "
            f"Recover by hand: {start}, then in a worktree at "
            f"{rendered[:12]} (the marker's parent) run `mise run {fetch}` "
            "(fetch_visual_baselines.py --run <id>), carry the files onto "
            f"origin/{branch}'s head, commit the declared baselines, and push with a "
            "lease; or hand the branch to Lane 2 with a rework.")


def refuse_marked_head(lr, repo_dir: str, branch: str, head: str) -> None:
    """Before any work: a `Render-Pending` head on origin is exit 6, nothing touched."""
    run_id = marker_of(lr, repo_dir, head)
    if run_id is None:
        return
    rendered = lr.rev(repo_dir, f"{head}^") or "<marker parent>"
    raise lr.Exit(6, f"origin/{branch} head {head[:12]} carries Render-Pending: {run_id}; "
                     "nothing done. " + recovery(branch, head, rendered, run_id,
                                                 config(lr, repo_dir)))


def render(lr, repo_dir: str, issue: int, branch: str, prep: Path,
           cfg: dict[str, str], rendered: str, pushed: str,
           passed: str | None) -> tuple[str, str]:
    """From push 1 (`pushed`: the `undispatched` marker on `rendered`) to a
    pushed baseline commit; returns (final SHA, run id). The ONE handler: any
    failure in here, its own cleanup's included, is exit 6."""
    state: dict[str, str | None] = {"pushed": pushed, "run_id": None}
    try:
        return _forward(lr, repo_dir, issue, branch, prep, cfg, rendered, passed, state)
    except BaseException as exc:  # noqa: BLE001 -- every failure after push 1 is exit 6
        detail = (f"exit {exc.code}: {exc.message}" if isinstance(exc, lr.Exit)
                  else f"{type(exc).__name__}: {exc}")
        for step in (("reset", "-q", "--hard"), ("clean", "-fdq")):
            try:  # a cleanup failure is reported, never raised past here
                lr.git(prep, *step, check=False, timeout=CALL_TIMEOUT)
            except BaseException as cleanup:  # noqa: BLE001
                detail += f"\n(cleanup git {step[0]} failed: {type(cleanup).__name__}: {cleanup})"
        raise lr.Exit(6, f"pushed, visual baselines pending; {detail}\n"
                         + recovery(branch, state["pushed"] or pushed, rendered,
                                    state["run_id"], cfg)) from exc


def _push(lr, repo_dir, issue, prep, branch, passed, lease, target, state) -> None:
    if passed:  # the Lane 3 PASS receipt is extended before every push (F1034)
        lr.write_receipt(repo_dir, issue, passed, lease, target)
    lr.push(prep, branch, lease, timeout=CALL_TIMEOUT)
    state["pushed"] = target


def _forward(lr, repo_dir, issue, branch, prep, cfg, rendered, passed, state):
    slug = lr.repo_slug(repo_dir)
    workflow = cfg["workflow"]
    before = list_dispatch_ids(lr, slug, workflow)
    dispatch(lr, slug, workflow, rendered)
    run_id = find_new_run(lr, slug, workflow, before)
    state["run_id"] = run_id
    trailer = lr._gate_ci().RENDER_PENDING
    pending = state["pushed"]  # the `undispatched` marker push 1 left
    lr.git(prep, "commit", "-q", "--no-verify", "--allow-empty", "-m",
           f"visual baselines pending render run {run_id} for {rendered} (lane1-rebase)\n\n"
           f"{trailer}: {run_id}", timeout=CALL_TIMEOUT)
    marker = lr.git(prep, "rev-parse", "HEAD", timeout=CALL_TIMEOUT).stdout.strip()
    if marker_of(lr, prep, marker) != run_id:
        raise lr.Exit(6, f"the marker commit does not carry {trailer}: {run_id}")
    _push(lr, repo_dir, issue, prep, branch, passed, pending, marker, state)
    run = wait_run(lr, slug, run_id)
    if run is None:
        raise lr.Exit(6, f"render run {run_id} did not complete within "
                         f"{RENDER_TIMEOUT // 60} minutes")
    if run.get("conclusion") != "success":
        raise lr.Exit(6, f"render run {run_id} concluded {run.get('conclusion')!r}")
    name = f"visual-baselines-{rendered}"
    if name not in run_artifacts(lr, slug, run_id):
        raise lr.Exit(6, f"render run {run_id} has no artifact {name}")
    # The fetch checks HEAD against the rendered SHA: run it at `rendered`.
    lr.git(prep, "checkout", "-q", "--detach", rendered, timeout=CALL_TIMEOUT)
    task = cfg["fetch_task"].format(run_id=run_id)
    proc = lr.run_task(prep, task, timeout=FETCH_TIMEOUT)
    if proc.returncode:
        raise lr.Exit(6, f"fetch `mise run {task}` failed (exit {proc.returncode}):\n"
                         f"{(proc.stdout + proc.stderr).strip()[-2000:]}")
    # The markers are empty, so the fetched working-tree changes carry across.
    lr.git(prep, "checkout", "-q", "--detach", marker, timeout=CALL_TIMEOUT)
    entries = lr.git(prep, "status", "--porcelain", "-z", "--untracked-files=all",
                     timeout=CALL_TIMEOUT).stdout
    paths = [entry[3:] for entry in entries.split("\0") if entry]
    stray = [path for path in paths if not is_baseline(path, cfg["glob"])]
    if stray:
        raise lr.Exit(6, "the fetch changed paths outside the declared baselines:\n"
                         + "\n".join(f"  {path}" for path in stray))
    if paths:
        lr.git(prep, "add", "-A", "--", *paths, timeout=CALL_TIMEOUT)
    lr.git(prep, "commit", "-q", "--no-verify", "--allow-empty", "-m",
           f"visual baselines rendered by run {run_id} for {rendered} (lane1-rebase)",
           timeout=CALL_TIMEOUT)
    final = lr.git(prep, "rev-parse", "HEAD", timeout=CALL_TIMEOUT).stdout.strip()
    _push(lr, repo_dir, issue, prep, branch, passed, marker, final, state)
    return final, run_id
