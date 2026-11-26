#!/usr/bin/env python3
"""harmonic-forge#1565, Option A (design amendment issuecomment-7197572224):
the AE-only ci-outage release. AC1 (one AE, one SHA, beside a sidecar, never
reused), AC2 (Lane 3's PASS is unchanged: pending CI still refuses) and AC3
(the closed allow-set and the --prod-run refusal, real post and --dry-run).

Real stores throughout: a temp FORCE_STATE_DIR, a temp check_receipt.STORE, a
temp forge root for `check_receipt.forge_sha()` and a temp hrse source repo
whose HEAD is the AE's SHA. Only GitHub is stubbed: `comment_body`, the S0 CI
reader, and the PR/world/sticky reads, which are not this issue's rules.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import l1_post as L  # noqa: E402
import post_lane_discussion as P  # noqa: E402
import check_receipt  # noqa: E402
import ci_outage  # noqa: E402
import force_grant  # noqa: E402
import gate_ci  # noqa: E402
import imc_waiver  # noqa: E402
from test_gate_ci import check, fake_gh  # noqa: E402

REPO, ISSUE, KEY, SLUG = "vitalharmony/hrse", 12, "H12", "vitalharmony__hrse"
S2 = "6" * 40
_SAME = object()


def git_repo(root: Path, origin: str | None = None) -> str:
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    if not (root / ".git").exists():
        run("init", "-q")
        if origin:
            run("remote", "add", "origin", origin)
    (root / "f").write_text(str(len(list(root.iterdir()))) + os.urandom(4).hex())
    run("add", "f")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def job(name: str, conclusion: str | None = "success", status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "completed_at": "2026-11-25T00:00:00Z"}


def s0(jobs=(), statuses=()):
    """An S0 reader: one in-flight pull_request run of ci.yml carrying `jobs`, or no run at all."""
    runs = ([{"id": 1, "name": "ci", "status": "in_progress", "conclusion": None, "event": "pull_request",
              "path": ".github/workflows/ci.yml"}] if jobs else [])

    def read(path: str):
        if "/actions/runs?" in path:
            return 200, json.dumps({"total_count": len(runs), "workflow_runs": runs})
        if "/actions/runs/1/jobs" in path:
            return 200, json.dumps({"total_count": len(jobs), "jobs": list(jobs)})
        if "/statuses" in path:
            return 200, json.dumps(list(statuses))
        return 404, None
    return read


def status(context: str, state: str) -> dict:
    return {"context": context, "state": state, "updated_at": "2026-11-25T00:00:00Z",
            "creator": {"login": "github-actions[bot]"}}


ABSENT, PENDING = s0(), s0([job("verify", None, "in_progress")])
RED_STATUS = s0(statuses=[status("verify", "failure")])


class World:
    def build(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.state, self.store, self.forge_root, self.src = (base / n for n in ("force", "store", "forge", "src"))
        for path in (self.forge_root, self.src):
            path.mkdir()
        self.forge = git_repo(self.forge_root)
        self.S = git_repo(self.src, "https://github.com/vitalharmony/hrse.git")
        for target, attr, value in ((force_grant, "FORCE_STATE_DIR", self.state),
                                    (check_receipt, "STORE", self.store),
                                    (check_receipt, "FORGE_ROOT", self.forge_root)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in (check_receipt.SOURCE_ENV, check_receipt.FORGE_SHA_ENV):
            os.environ.pop(name, None)
        ci_outage._RELEASES.clear()
        self.addCleanup(ci_outage._RELEASES.clear)
        self.comments: list[dict] = []
        self.next_id = 1000
        self.receipts, self.tasklist = mock.Mock(), mock.Mock()

    def grant(self, prefix: str | None = None, cid: str = "900", mode: str = "ci-outage", later: int = 0) -> dict:
        pending = force_grant.write_pending(KEY, REPO, ISSUE, None, "Actions outage", "s", "line",
                                            imc_waiver._now() + timedelta(minutes=later),
                                            mode=mode, sha_prefix=prefix or self.S)
        usable = {**pending, "status": "usable", "comment_id": cid,
                  "comment_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{cid}"}
        imc_waiver.write_json_atomic(force_grant.grant_path(KEY), usable)
        return usable

    def check_full(self) -> None:
        """`check-full` as hrse#2889 runs it: the forge HEAD captured at its start exported (P5)."""
        with mock.patch.dict(os.environ, {check_receipt.FORGE_SHA_ENV: check_receipt.forge_sha() or ""}):
            self.assertEqual(check_receipt.record(str(self.src), source=check_receipt.CHECK_FULL_SOURCE), 0)

    def sidecar(self, sha: str, forge, body_forge=_SAME) -> Path:
        path = check_receipt.check_full_path(SLUG, sha, forge)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"repo": SLUG, "sha": sha, "source": "check-full",
                                    "forge_sha": forge if body_forge is _SAME else body_forge}))
        return path

    def add(self, body: str, cid: int | None = None) -> dict:
        cid = cid or self.next_id
        self.next_id = max(self.next_id, cid) + 10
        comment = {"id": cid, "body": body, "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{cid}"}
        self.comments.append(comment)
        return comment

    def footered(self, body: str, kind: str, sha: str, tail: str = "") -> str:
        digest = hashlib.sha256(body.rstrip("\n").encode()).hexdigest()
        return f"{body.rstrip()}\n\n<!-- l1-post v1; kind={kind}; sha={sha}; body-sha256={digest}; checks=x{tail} -->\n"

    def sweep(self, sha: str | None = None, tier: str = "W") -> dict:
        return self.add(self.footered(f"## Gate-readiness sweep — {KEY}\n\nWrite tier: {tier}\n", "sweep", sha or self.S))

    def ci(self, stack: ExitStack, reader) -> None:
        for target, attr, value in ((L, "_cwd_repo_from_git", lambda _: REPO), (L, "_ci_reader", lambda _: reader),
                                    (L.gate_ci, "required_checks", lambda *a, **k: {"verify"}),
                                    (L, "world_checks", lambda *a, **k: ([], [])), (L, "refresh_main", lambda: None),
                                    (L, "require_open_pr", lambda *a, **k: (["pr-open #7"], [], []))):
            stack.enter_context(mock.patch.object(target, attr, value))

    def post_ae(self, reader=ABSENT, fail_post: bool = False, before_reserve=None, prod_run=None):
        cid = self.next_id

        def comment(repo, issue, text):
            if fail_post:
                raise SystemExit("[l1-post] GitHub comment failed: 502")
            return self.add(text, cid)["html_url"], cid
        with ExitStack() as stack:
            self.ci(stack, reader)
            for attr, value in (("comment_body", comment), ("validate_sticky_wicket", before_reserve or (lambda *a, **k: None)),
                                ("write_receipt", self.receipts), ("record_overlap_rulings", lambda *a, **k: None),
                                ("active_worktree_branches", lambda: []), ("_afk_toggle", None),
                                ("_tasklist_owed", self.tasklist)):
                stack.enter_context(mock.patch.object(L, attr, value))
            return L._post_kind(REPO, ISSUE, "ae", f"## Lane 1 AE — {KEY}\n\nAuthorized.\n", self.S, "feat/12-x",
                                prod_run=prod_run)

    def exits(self, **kw) -> tuple[object, str]:
        with self.assertRaises(SystemExit) as ctx:
            self.post_ae(**kw)
        return ctx.exception.code, str(ctx.exception)


class AeSide(World, unittest.TestCase):
    def setUp(self) -> None:
        self.build()

    def test_01_a_grant_beside_a_current_sidecar_posts_one_ae_and_consumes(self) -> None:
        self.grant()
        self.check_full()
        self.post_ae()
        [comment] = self.comments
        self.assertIn("ci-outage-grant", comment["body"].rsplit("checks=", 1)[1])
        self.assertIn(f"**CI substitute:** check-full receipt for {self.S} (forge {self.forge[:12]})", comment["body"])
        self.assertEqual(force_grant.read_grant(KEY)["status"], "consumed")
        self.assertFalse((self.state / "ci-outage").exists())  # no release record (Option A)

    def refused(self, needle: str, **kw) -> None:
        code, message = self.exits(**kw)
        self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
        self.assertIn(needle, message)
        self.assertEqual(self.comments, [])

    def test_02_a_grant_without_a_sidecar_refuses(self) -> None:
        self.grant()
        self.refused("run `mise run check-full`")
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_03_a_sidecar_for_another_forge_or_naming_another_forge_refuses(self) -> None:
        self.grant()
        self.sidecar(self.S, "f" * 40)
        self.refused("run `mise run check-full`")  # (a) none at the current HEAD
        self.sidecar(self.S, self.forge, body_forge="f" * 40)
        self.refused("run `mise run check-full`")  # (b) the current key, another body

    def test_04_a_slot_only_check_full_receipt_refuses(self) -> None:
        self.grant()
        slot = check_receipt.receipt_path(SLUG, self.S)
        slot.parent.mkdir(parents=True)
        slot.write_text(json.dumps({"repo": SLUG, "sha": self.S, "forge_sha": self.forge, "source": "check-full"}))
        self.refused("run `mise run check-full`")

    def test_05_a_grant_for_another_sha_or_a_12_hex_prefix_releases_nothing(self) -> None:
        self.check_full()
        for prefix in (S2, self.S[:12]):
            with self.subTest(prefix=prefix):
                self.grant(prefix=prefix)
                self.refused("CI is absent")

    def test_06a_red_ci_under_a_grant_refuses(self) -> None:
        """AC3's closed allow-set: gate_ci reads `red`, which is not pending or absent."""
        self.grant()
        self.check_full()
        self.refused("CI is red on", reader=RED_STATUS)
        self.assertIn("a ci-outage release needs pending or absent", self.exits(reader=RED_STATUS)[1])
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_06b_unknown_ci_under_a_grant_refuses(self) -> None:
        """Operator D10: an unrecognised conclusion reads `unknown`, which never releases."""
        self.grant()
        self.check_full()
        self.refused("CI is unknown on", reader=s0([job("verify", "stale")]))
        self.assertIn("a ci-outage release needs pending or absent",
                      self.exits(reader=s0([job("verify", "stale")]))[1])
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_07_a_second_ae_under_the_same_grant_exits_1_naming_a_new_force_line(self) -> None:
        self.grant()
        self.check_full()
        self.post_ae(reader=PENDING)
        code, message = self.exits(reader=PENDING)
        self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
        self.assertIn("already consumed", message)
        self.assertIn(f"`FORCE {KEY} ci-outage {self.S}:", message)
        self.assertEqual(len(self.comments), 1)

    def test_08_a_failed_post_after_reserve_leaves_the_grant_spent(self) -> None:
        self.grant()
        self.check_full()
        self.exits(reader=PENDING, fail_post=True)
        self.assertEqual(force_grant.read_grant(KEY)["status"], "consumed")
        code, message = self.exits(reader=PENDING)
        self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
        self.assertIn("already consumed", message)

    def test_09_a_force_line_replacing_the_slot_before_reserve_posts_nothing(self) -> None:
        self.grant()
        self.check_full()
        later = lambda *a, **k: self.grant(cid="901", later=1)  # noqa: E731 - a later FORCE line, same SHA
        self.refused("could not be consumed", before_reserve=later)
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def dry_run(self, reader=ABSENT, prod_run=None) -> None:
        with ExitStack() as stack:
            self.ci(stack, reader)
            for step in L._publish_checks(REPO, ISSUE, "ae", self.S, "feat/12-x", ack_overlap=None,
                                          ack_no_pr_required=None, grant_on_main=False, prod_run=prod_run):
                step()

    def test_10_the_dry_run_never_consumes_and_the_post_consumes_once(self) -> None:
        self.grant()
        self.check_full()
        self.dry_run()
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")
        self.post_ae()
        self.assertEqual(force_grant.read_grant(KEY)["status"], "consumed")

    def test_41_prod_run_refuses_the_release_on_the_real_post(self) -> None:
        """AC3: a production step is R-0374's grant path, never this one."""
        self.grant()
        self.check_full()
        self.refused("declares --prod-run", prod_run={"script": "x"})
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_44_a_no_pr_ack_never_takes_the_outage_release(self) -> None:
        """Kept from reforge round 1: with --ack-no-pr-required applied, CI is absent because no
        PR exists, not because of an outage; the grant is never consulted, nor spent."""
        self.grant()
        self.check_full()
        with ExitStack() as stack:
            self.ci(stack, ABSENT)
            stack.enter_context(mock.patch.object(L.gate_ci, "no_pr_ack_release", lambda *a, **k: False))
            with self.assertRaises(SystemExit) as ctx:
                L._ae_ci_gate(REPO, ISSUE, self.S, "no PR: a backfill")
        self.assertNotIn("ci-outage", str(ctx.exception))
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_45_a_cross_repo_ae_refuses(self) -> None:
        """Kept from reforge round 1: the grant and the sidecar name the issue repo's SHA."""
        self.grant()
        self.check_full()
        with ExitStack() as stack:
            self.ci(stack, ABSENT)
            stack.enter_context(mock.patch.object(L, "_cwd_repo_from_git", lambda _: "vitalharmony/other"))
            with self.assertRaises(SystemExit) as ctx:
                L._ae_ci_gate(REPO, ISSUE, self.S, None)
        self.assertIn("cross-repo AE", str(ctx.exception))
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_42_prod_run_refuses_the_release_on_the_dry_run(self) -> None:
        """R-0419: the dry run refuses what the real post refuses."""
        self.grant()
        self.check_full()
        with self.assertRaises(SystemExit) as ctx:
            self.dry_run(prod_run={"script": "x"})
        self.assertIn("declares --prod-run", str(ctx.exception))
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_36_a_dirty_forge_refuses_naming_why(self) -> None:
        self.grant()
        self.check_full()
        (self.forge_root / "f").write_text("dirty")
        self.sidecar(self.S, None, body_forge=None)  # what an unguarded reader would find
        self.refused("forge tree dirty: f")



class PassUnchanged(World, unittest.TestCase):
    """AC2: a consumed ci-outage grant and its AE at S, CI pending at S: Lane 3's PASS
    refuses with gate_ci's unfinished-CI refusal and never reads the grant."""

    def test_43_a_pass_on_pending_ci_refuses_after_an_outage_ae(self) -> None:
        self.build()
        self.grant()
        self.check_full()
        self.post_ae(reader=PENDING)
        self.sweep()
        self.assertEqual(force_grant.read_grant(KEY)["status"], "consumed")
        before = force_grant.grant_path(KEY).read_bytes()
        body = f"## Lane 3 Gate Results — {KEY}\n\n**Verdict:** PASS\n**Head-SHA:** {self.S}\n\nAll cases ran.\n"
        run = fake_gh(required={"verify"}, checks=[check("verify", None, "in_progress")],
                      pulls=[{"number": 7, "head": self.S, "ref": "feat/12-x", "base": "main", "state": "open"}])
        with mock.patch.object(gate_ci, "_run", run), \
             mock.patch.object(P.check_lane3_ready, "fetch_comments", lambda r, i: list(self.comments)):
            with self.assertRaises(SystemExit) as ctx:
                P.require_green_ci("gate-result", REPO, body, issue=ISSUE)
        self.assertIn("CI has not completed for the SHA this PASS names", str(ctx.exception))
        self.assertEqual(force_grant.grant_path(KEY).read_bytes(), before)
        tree = ast.parse(Path(P.__file__).read_text())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names} | {node.module for node in ast.walk(tree)
                                                if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("ci_outage", imported)
        self.assertNotIn("ci_outage", vars(P))


if __name__ == "__main__":
    unittest.main()
