"""Per-issue preclose pass accounting (harmonic-forge#834).

Operator ruling, 2026-09-30: **at most two preclose passes per issue.** A
rebase, or any head change whose diff is patch-identical to a reviewed one,
is not a new pass. A third pass is never run:

- both passes left surviving findings: the sticky-wicket agent decides patch
  (the operator's ``--force`` covers the final head) or reforge (a new branch,
  and the count restarts);
- anything else goes to the operator.

**Reforge is the operator's instruction, never Lane 1's own** (F834 pass 2,
sticky-wicket PATCH verdict): "sticky-wicket ruled reforge" is an off-machine
authorization no local check can establish, and branch names are renameable
and absent under a detached HEAD, so a branch-name fence was self-service.
``--reforge`` therefore requires ``--force``, and even then refuses a diff a
completed pass already covers. It starts a new epoch; the old passes stay in
``pass_history`` as the record.

The count rides in the receipt itself (``pass_history``), carried forward by
every receipt write, planned or complete. It never depends on the telemetry
archive (harmonic-forge#826), which is best-effort and swallows every error.
Shared by ``preclose_check.py`` and ``block_missing_preclose_inspection.py``
so both enforcement points say exactly the same thing (AC4).

Issue #845 adds an earlier circuit breaker: when pass 1 has two surviving
findings with the same author-declared mechanism, sticky-wicket rules on the
approach before Lane 1 fixes either symptom.

**Change identity, not patch-id (harmonic-forge#1077).** Every acceptance --
``covered`` here, the merge hook, the kill-check receipt and
``--post-verdict``'s base tie -- goes through ``change_identity``/``accepts``:
a head is the reviewed change when (a) it is the reviewed head SHA, or (b) a
``git merge-tree`` replay of the reviewed commit onto the head's own base is
clean and yields exactly the head's tree (only package.json ``version`` and
package-lock.json ``version``/``packages[""].version`` are exempt, and only
in (b)). patch-id discarded hunk position, so the same edit to a second
identical block hashed as reviewed.

Migration note: a pass recorded before #1077 carries no ``change_identity``.
It is accepted only at its exact recorded SHA; a rebased head under such a
receipt needs a new pass (or the operator's ``--force``), and the merge hook
says so naming #1077.
"""
from __future__ import annotations

import json
import os
import subprocess

MAX_PASSES = 2

STICKY_WICKET = (
    "Two preclose passes are complete, and both left surviving findings. Do not run a "
    "third. Invoke the sticky-wicket agent on this issue: 'patch' means one cross-family "
    "post-verdict check reads just the patch (preclose_check.py's --post-verdict), then the "
    "operator --forces the final head; 'reforge' means a new branch, and the pass count "
    "restarts."
)
OPERATOR = (
    "Two preclose passes are complete. Do not run a third. Escalate to the operator; "
    "--force is their instruction, not yours."
)
CLUSTER_ROUTE = (
    "Pass 1 has multiple surviving findings from the same mechanism: {mechanisms}. "
    "Invoke the sticky-wicket agent now, before fixing anything, then record its "
    "verdict with preclose_check.py --repo {repo} --issue {issue} --cluster-verdict "
    "PATCH --comment-url <url> (or REFORGE, which proceeds only with the operator's "
    "--force --reforge). The operator may bypass an unresolved cluster with --force."
)


def normalize_mechanism(mechanism: str) -> str:
    """The deliberately narrow comparison promised by F845.

    This is not inference over prose: it only casefolds and collapses
    whitespace in the author-declared key. Near-synonyms remain distinct.
    """
    return " ".join(mechanism.casefold().split())


def patch_id(diff_text: str | None) -> str | None:
    """``git patch-id --verbatim`` of a unified diff, or None when it cannot be
    computed. Since harmonic-forge#1077 it is never an acceptance basis; it
    only labels the post-verdict check's patch.

    ``--verbatim``, never ``--stable``: ``--stable`` strips whitespace, so a
    Python or YAML re-indent that changes behavior hashed as "the same diff"
    and merged unreviewed (harmonic-forge#834 preclose, three refuters)."""
    if not diff_text:
        return None
    try:
        result = subprocess.run(["git", "patch-id", "--verbatim"], input=diff_text,
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.split()
    return fields[0] if result.returncode == 0 and fields else None


#: The ref ``change_identity`` takes its merge base against by default.
MAIN_REF = "origin/main"
#: (b)'s only exemption: these JSON keys, in these files (by basename), and
#: nothing else -- never a nested ``"version"`` (harmonic-forge#1077 AC4).
VERSION_EXEMPT = {
    "package.json": (("version",),),
    "package-lock.json": (("version",), ("packages", "", "version")),
}
LEGACY_RECEIPT = (
    "This receipt predates harmonic-forge#1077: its pass records no change identity, "
    "only a patch-id, and patch-id is no longer an acceptance basis. It is accepted "
    "only at its exact reviewed head {reviewed}. A re-review of this head is required "
    "(a new pass, or the operator's --force)."
)


def run_git(repo_dir: str | os.PathLike | None, *args: str,
            timeout: float = 60) -> subprocess.CompletedProcess:
    """Every git call this module makes. Module-level so a test can replace it."""
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _git_out(repo_dir, *args: str) -> str:
    """Stdout of a git call that must succeed; raises otherwise."""
    result = run_git(repo_dir, *args)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def merge_base(repo_dir, main_ref: str, head_sha: str) -> str:
    return _git_out(repo_dir, "merge-base", main_ref, head_sha)


def change_identity(repo_dir, head_sha: str, main_ref: str = MAIN_REF) -> dict:
    """harmonic-forge#1077 AC1: ``{head_sha, merge_base_sha, tree}`` of the
    commit ``head_sha`` from local git -- never from ``gh pr diff``. Raises
    when git cannot resolve it."""
    head = _git_out(repo_dir, "rev-parse", "--verify", f"{head_sha}^{{commit}}")
    return {"head_sha": head, "merge_base_sha": merge_base(repo_dir, main_ref, head),
            "tree": _git_out(repo_dir, "rev-parse", "--verify", f"{head}^{{tree}}")}


def _strict_json(text: str):
    def pairs(items):
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate JSON key")
        return dict(items)
    return json.loads(text, object_pairs_hook=pairs)


def _without_exempt(document, paths: tuple[tuple[str, ...], ...]):
    for path in paths:
        node = document
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return document


def _strict_equal(a, b) -> bool:
    """JSON equality with exact types (harmonic-forge#1077 preclose pass 1):
    Python ``==`` equates ``true``, ``1`` and ``1.0``; this does not. Key
    sets and list order must match exactly."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _equal_but_exempt(repo_dir, replay_tree: str, head_tree: str,
                      skip: set[str] = frozenset()) -> bool:
    """True when the two trees differ only in the exempt JSON keys. Paths in
    ``skip`` (a version-only conflict, harmonic-forge#1086) are not compared."""
    raw = _git_out(repo_dir, "diff-tree", "-r", "-z", "--no-renames", replay_tree, head_tree)
    fields = [field for field in raw.split("\0") if field != ""] if raw else []
    if len(fields) % 2:
        return False
    for meta, path in zip(fields[0::2], fields[1::2]):
        if path in skip:
            continue
        old_mode, new_mode, _old, _new, status = meta.lstrip(":").split()
        exempt = VERSION_EXEMPT.get(os.path.basename(path))
        if status != "M" or old_mode != new_mode or exempt is None:
            return False
        before = _strict_json(_git_out(repo_dir, "cat-file", "blob", f"{replay_tree}:{path}"))
        after = _strict_json(_git_out(repo_dir, "cat-file", "blob", f"{head_tree}:{path}"))
        if not _strict_equal(_without_exempt(before, exempt), _without_exempt(after, exempt)):
            return False
    return True


def accepts(recorded: dict | None, cur_head: str, cur_base: str | None = None,
            repo_dir=None, main_ref: str = MAIN_REF) -> bool:
    """harmonic-forge#1077: is ``cur_head`` the change ``recorded`` (a
    ``change_identity``) identifies?

    (a) ``cur_head`` is the recorded head; else (b) replaying the recorded
    commit onto ``cur_base`` -- merge-base(main, cur_head), never main's tip,
    so an unrebased PR still matches when main moves -- is clean and gives
    exactly ``cur_head``'s tree, the JSON version exemption aside.

    ``repo_dir`` may be a zero-argument callable returning the directory (or
    None), so the merge hook fetches its cache clone only when (a) fails.
    Every error returns False: the preflight and the merge hook must agree,
    and an error must never read as acceptance."""
    try:
        if not isinstance(recorded, dict) or not cur_head:
            return False
        if cur_head == recorded.get("head_sha"):
            return True
        directory = repo_dir() if callable(repo_dir) else repo_dir
        if callable(repo_dir) and directory is None:
            return False
        rec_head, rec_base = recorded.get("head_sha"), recorded.get("merge_base_sha")
        if not rec_head or not rec_base:
            return False
        base = cur_base or merge_base(directory, main_ref, cur_head)
        replay = run_git(directory, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                         "-z", "--merge-base", rec_base, base, rec_head)
        if replay.returncode not in (0, 1):
            return False
        # -z: NUL-separated; field 1 is the tree, the rest exactly the conflicted paths.
        fields = [f for f in replay.stdout.split("\0") if f]
        if not fields:
            return False
        replay_tree, conflicted = fields[0], fields[1:]
        head_tree = _git_out(directory, "rev-parse", "--verify", f"{cur_head}^{{tree}}")
        if replay.returncode == 0:
            return replay_tree == head_tree or _equal_but_exempt(directory, replay_tree, head_tree)
        # harmonic-forge#1086: a conflict is accepted only when it is version-only,
        # and then only the conflicted paths are skipped in the tree comparison.
        import _exempt_conflict  # noqa: PLC0415 -- it imports this module
        if not _exempt_conflict.version_only_conflict(directory, conflicted, rec_base, rec_head,
                                                      base, cur_head):
            return False
        return _equal_but_exempt(directory, replay_tree, head_tree, skip=set(conflicted))
    except Exception:  # noqa: BLE001 -- fail closed, see the docstring
        return False


def entry_identity(entry: dict | None) -> dict | None:
    """The change identity a pass entry (or kill receipt) recorded, or None
    for a legacy one."""
    ident = (entry or {}).get("change_identity")
    return ident if isinstance(ident, dict) and ident.get("head_sha") else None


def accepts_entry(entry: dict | None, cur_head: str, repo_dir=None,
                  main_ref: str = MAIN_REF, sha_key: str = "sha") -> bool:
    """``accepts`` against a recorded pass. A legacy entry (no identity) is
    accepted only at its exact recorded SHA (harmonic-forge#1077 AC6)."""
    try:
        if not entry or not cur_head:
            return False
        ident = entry_identity(entry)
        if ident is None:
            return bool(entry.get(sha_key)) and entry.get(sha_key) == cur_head
        return accepts(ident, cur_head, None, repo_dir, main_ref)
    except Exception:  # noqa: BLE001
        return False


def legacy_message(entry: dict | None, cur_head: str) -> str | None:
    """AC6's re-review message when ``entry`` is a legacy pass that does not
    name ``cur_head`` exactly, else None."""
    if not entry or entry_identity(entry) is not None or entry.get("sha") == cur_head:
        return None
    return LEGACY_RECEIPT.format(reviewed=str(entry.get("sha"))[:12])


def history(receipt: dict | None) -> list[dict]:
    """The completed passes on record, oldest first. A legacy complete receipt
    written before #834 carries no history and counts as one pass."""
    if not receipt:
        return []
    carried = receipt.get("pass_history")
    if isinstance(carried, list):
        return [entry for entry in carried if isinstance(entry, dict)]
    if receipt.get("status") == "complete":
        return [{"sha": receipt.get("reviewed_sha"),
                 "surviving": int(receipt.get("surviving_findings") or 0)}]
    return []


def current(passes: list[dict]) -> list[dict]:
    """The passes of the latest reforge epoch -- the ones the cap counts."""
    if not passes:
        return []
    epoch = max(int(p.get("epoch") or 0) for p in passes)
    return [p for p in passes if int(p.get("epoch") or 0) == epoch]


def reviewed(receipt: dict | None) -> dict | None:
    """The last completed pass of the current epoch, whatever the receipt's
    own status: an abandoned ``--plan`` for the next pass must not un-review
    the diff the last pass covered (harmonic-forge#834 preclose)."""
    passes = current(history(receipt))
    return passes[-1] if passes else None


def cap_message(passes: list[dict]) -> str:
    """AC2/AC4's case split, read from the last two completed passes."""
    last_two = passes[-MAX_PASSES:]
    if len(last_two) == MAX_PASSES and all(int(p.get("surviving") or 0) > 0 for p in last_two):
        return STICKY_WICKET
    return OPERATOR


def covered(passes: list[dict], sha: str, repo_dir=None, main_ref: str = MAIN_REF) -> bool:
    """True when the last completed pass already reviewed this change: the
    same head, or a clean replay of it (a rebase) -- harmonic-forge#1077."""
    if not passes:
        return False
    return accepts_entry(passes[-1], sha, repo_dir, main_ref)


def refusal(receipt: dict | None, sha: str, repo_dir=None, main_ref: str = MAIN_REF,
            reforge: bool = False, force: bool = False, *, repo: str,
            issue: int | str) -> str | None:
    """Why a new pass must not run, or None when it may. ``repo``/``issue``
    are required: the cluster route it may return is a runnable command."""
    passes = current(history(receipt))
    if reforge and not force:
        return ("--reforge is the operator's instruction, never Lane 1's own: it runs only "
                "with --force, after sticky-wicket's reforge verdict.")
    cluster = cluster_message(receipt, repo, issue)
    if force and not reforge:
        if (cluster
                and (receipt or {}).get("mechanism_cluster", {}).get("verdict") == "REFORGE"):
            return cluster
        return None
    if covered(passes, sha, repo_dir, main_ref):
        if reforge:
            return "--reforge needs a changed diff; a completed pass already reviewed this one."
        return ("A completed pass already covers this change (same head, or a clean replay "
                "of it: a rebase is not a pass). Nothing new to review. If a finding is disputed, "
                "escalate to the operator rather than re-running.")
    if force:
        return None
    if cluster:
        return cluster
    if len(passes) >= MAX_PASSES:
        return cap_message(passes)
    return None


def record(receipt: dict | None, sha: str, identity: dict | None, surviving: int,
           mechanisms: list[str] | None = None, branch: str | None = None,
           reforge: bool = False, cost: dict | None = None) -> dict:
    """The receipt fields for a newly completed pass (AC1). ``cost`` is the
    pass's measured cost and counts (harmonic-forge#889), merged into its
    ``pass_history`` entry."""
    everything = history(receipt)
    passes = current(everything)
    epoch = max((int(p.get("epoch") or 0) for p in everything), default=0) + (1 if reforge else 0)
    prior_surviving = int(passes[-1].get("surviving") or 0) if passes and not reforge else None
    distinct = sorted(set(mechanisms or []))
    everything = everything + [{"sha": sha, "change_identity": identity, "surviving": int(surviving),
                                "mechanisms": distinct, "branch": branch, "epoch": epoch,
                                **(cost or {})}]
    extra = _post_verdict(receipt)
    existing = _mechanism_cluster(receipt, epoch)
    if existing:
        extra["mechanism_cluster"] = existing
    elif len(current(everything)) == 1:
        clustered = sorted({mechanism for mechanism in distinct
                            if (mechanisms or []).count(mechanism) >= 2})
        if clustered:
            extra["mechanism_cluster"] = {"pass_sha": sha, "mechanisms": clustered,
                                          "verdict": None, "comment_url": None,
                                          "epoch": epoch}
    return {"pass_history": everything, "pass_count": len(current(everything)),
            "surviving_findings": int(surviving), "prior_surviving_findings": prior_surviving,
            "reviewed_change_identity": identity, **extra}


def pass_cost_view(entry: dict) -> dict:
    """The one reading of a pass entry's cost (harmonic-forge#889 sticky-wicket
    PATCH). Every reader -- the stdout line, the telemetry event, the report --
    goes through here, so no reader re-invents the tri-state.

    `panel` is `measured` (both figures recorded), `unavailable` (the runtime
    reported no usage; `--cost-unavailable` recorded why) or `unknown` (a pass
    recorded before #889). `codex` is `ran`, `fallback` (the call ran and spent
    its time but returned no verdict), `not-run` or `unknown`. A figure is set
    only when its state says it was measured."""
    tokens, ms = entry.get("panel_tokens"), entry.get("panel_ms")
    if isinstance(tokens, int) and isinstance(ms, int):
        panel = "measured"
    elif entry.get("cost_unavailable"):
        panel = "unavailable"
    else:
        panel = "unknown"
    ran = entry.get("cross_family_ran")
    if ran is True:
        codex = "fallback" if entry.get("cross_family_fallback") else "ran"
    elif ran is False:
        codex = "not-run"
    else:
        codex = "unknown"
    return {"panel": panel,
            "tokens": tokens if panel == "measured" else None,
            "ms": ms if panel == "measured" else None,
            "unavailable": entry.get("cost_unavailable") if panel == "unavailable" else None,
            "codex": codex,
            "codex_ms": entry.get("cross_family_ms") if codex in ("ran", "fallback") else None}


def last_tier(receipt: dict | None) -> str | None:
    """The tier the newest recorded pass was sized at, carried on the pass
    entry itself, so a later re-plan without --tier cannot lose it."""
    for entry in reversed(history(receipt)):
        if entry.get("tier") and entry.get("tier") != "unset":
            return entry["tier"]
    return None


def carried(receipt: dict | None) -> dict:
    """The fields a non-completing write (``plan``) must preserve."""
    passes = history(receipt)
    fields = {"pass_history": passes, "pass_count": len(current(passes))} if passes else {}
    epoch = max((int(p.get("epoch") or 0) for p in passes), default=0)
    cluster = _mechanism_cluster(receipt, epoch)
    return {**fields, **_post_verdict(receipt),
            **({"mechanism_cluster": cluster} if cluster else {})}


def _mechanism_cluster(receipt: dict | None, epoch: int) -> dict | None:
    cluster = (receipt or {}).get("mechanism_cluster")
    if isinstance(cluster, dict) and int(cluster.get("epoch") or 0) == epoch:
        return cluster
    return None


def cluster_message(receipt: dict | None, repo: str, issue: int | str) -> str | None:
    """Route an unresolved/current-epoch cluster, or enforce its REFORGE verdict."""
    passes = history(receipt)
    epoch = max((int(p.get("epoch") or 0) for p in passes), default=0)
    cluster = _mechanism_cluster(receipt, epoch)
    if not cluster:
        return None
    mechanisms = ", ".join(cluster.get("mechanisms") or [])
    verdict = cluster.get("verdict")
    if verdict == "PATCH":
        return None
    if verdict == "REFORGE":
        return (f"Sticky-wicket ruled REFORGE for the pass 1 mechanism cluster ({mechanisms}). "
                "Only the operator's --force --reforge starts the new epoch.")
    return CLUSTER_ROUTE.format(mechanisms=mechanisms, repo=repo, issue=issue)


# harmonic-forge#838 AC5: after a sticky-wicket PATCH verdict, one cross-family
# refuter reads just the patch (pass-2 head...final head) before the operator's
# --force. It is recorded beside the passes, never in ``pass_history``, so it
# never counts toward the cap. Both receipt writers rebuild the payload from
# ``record``/``carried``, so a field neither carries would vanish on the next
# write -- which is why both carry it.
POST_VERDICT_REQUIRED = (
    "Two passes both left surviving findings (the sticky-wicket case). Before the "
    "operator's --force covers this head, one cross-family refuter must read the patch "
    "since pass 2: preclose_check.py --repo {repo} --issue {issue} --post-verdict "
    "--base <pass-2 head> --envelope <path> --findings <file> "
    "--own-model <your session's model> --cross-family-ms <ms>. It never counts as a pass "
    "(harmonic-forge#838)."
)

#: Every printed hint that is a runnable preclose_check.py command. Each is
#: formatted with the real repo and issue before printing, and
#: test_preclose_check asserts every member parses exactly as printed and
#: that no other module-level string here carries such a command
#: (harmonic-forge#852 preclose, the class of the #848 follow-up).
COMMAND_HINTS = (CLUSTER_ROUTE, POST_VERDICT_REQUIRED)


def _post_verdict(receipt: dict | None) -> dict:
    check = (receipt or {}).get("post_verdict_check")
    return {"post_verdict_check": check} if isinstance(check, dict) else {}


def post_verdict_refusal(receipt: dict | None, sha: str, repo: str, issue: int) -> str | None:
    """Why a forced receipt at ``sha`` must not be written yet, or None.
    Only the sticky-wicket case needs the check; the operator case (not both
    passes with survivors) is unchanged."""
    passes = current(history(receipt))
    if len(passes) < MAX_PASSES or cap_message(passes) != STICKY_WICKET:
        return None
    check = _post_verdict(receipt).get("post_verdict_check") or {}
    if check.get("head_sha") == sha:
        return None
    # The hint is a runnable command, so it names this repo and issue
    # (harmonic-forge#848 follow-up, folded into #852).
    return POST_VERDICT_REQUIRED.format(repo=repo, issue=issue)


def reviewed_head(receipt: dict | None) -> str | None:
    """The head the receipt currently vouches for. The post-verdict write keeps
    it unchanged, so recording the check never lets the merge hook accept the
    final head on its own: the operator's --force still has to."""
    return (receipt or {}).get("reviewed_sha")


def reviewed_identity(receipt: dict | None) -> dict | None:
    """The change identity of the head the receipt vouches for, or None for a
    legacy receipt (harmonic-forge#1077; replaces ``reviewed_patch_id``)."""
    ident = (receipt or {}).get("reviewed_change_identity")
    return ident if isinstance(ident, dict) and ident.get("head_sha") else None


def post_verdict_fields(receipt: dict | None, base_sha: str, head_sha: str,
                        current_patch_id: str | None, provenance: str, surviving: int,
                        cross_family_ms: int | None = None) -> dict:
    """The receipt with the check added and every pass left exactly as it was.
    The check is one cross-family refuter, so its only cost figure is that
    call's wall-clock (harmonic-forge#889); Codex reports no tokens."""
    check = {"base_sha": base_sha, "head_sha": head_sha, "patch_id": current_patch_id,
             "provenance": provenance, "surviving": int(surviving)}
    if cross_family_ms is not None:
        check.update({"cross_family_ms": int(cross_family_ms),
                      "cross_family_tokens": "unavailable"})
    return {**carried(receipt), "post_verdict_check": check}
