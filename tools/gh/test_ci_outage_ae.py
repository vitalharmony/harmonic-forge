#!/usr/bin/env python3
"""harmonic-forge#1565 AE side (AC1): TCs 1-10, 36, 37 and TC 40's AE half.

Real stores throughout: a temp FORCE_STATE_DIR, a temp check_receipt.STORE, a
temp forge root for `check_receipt.forge_sha()` and a temp hrse source repo
whose HEAD is the AE's SHA. Only GitHub is stubbed: `comment_body`, the S0 CI
reader, and the PR/world/sticky reads, which are not this issue's rules.
`World` is a mixin, imported by test_ci_outage_pass.py.
"""
from __future__ import annotations

import hashlib
import io
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
import check_receipt  # noqa: E402
import ci_outage  # noqa: E402
import force_grant  # noqa: E402
import imc_waiver  # noqa: E402

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

    def records(self) -> list[dict]:
        folder = self.state / "ci-outage"
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def ci(self, stack: ExitStack, reader) -> None:
        for target, attr, value in ((L, "_cwd_repo_from_git", lambda _: REPO), (L, "_ci_reader", lambda _: reader),
                                    (L.gate_ci, "required_checks", lambda *a, **k: {"verify"}),
                                    (L, "world_checks", lambda *a, **k: ([], [])), (L, "refresh_main", lambda: None),
                                    (L, "require_open_pr", lambda *a, **k: (["pr-open #7"], [], []))):
            stack.enter_context(mock.patch.object(target, attr, value))

    def post_ae(self, reader=ABSENT, fail_post: bool = False, before_reserve=None):
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
            return L._post_kind(REPO, ISSUE, "ae", f"## Lane 1 AE — {KEY}\n\nAuthorized.\n", self.S, "feat/12-x")

    def exits(self, **kw) -> tuple[object, str]:
        with self.assertRaises(SystemExit) as ctx:
            self.post_ae(**kw)
        return ctx.exception.code, str(ctx.exception)


class AeSide(World, unittest.TestCase):
    def setUp(self) -> None:
        self.build()

    def test_01_a_grant_beside_a_current_sidecar_posts_and_binds(self) -> None:
        self.grant()
        self.check_full()
        url, cid = self.post_ae()
        text = self.comments[0]["body"]
        self.assertIn("ci-outage-grant", text.rsplit("checks=", 1)[1])
        self.assertIn(f"**CI substitute:** check-full receipt for {self.S} (forge {self.forge[:12]})", text)
        [record] = self.records()
        self.assertEqual((record["ae_comment_id"], record["ae_url"], record["sha"]), (cid, url, self.S))
        self.assertEqual(record["posted_sha256"], hashlib.sha256(text.rstrip("\n").encode()).hexdigest())
        self.assertEqual(record["receipt"], {"source_slug": SLUG, "forge_sha": self.forge})
        self.assertEqual(force_grant.read_grant(KEY)["status"], "consumed")

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

    def test_06_red_or_unknown_ci_under_a_grant_refuses(self) -> None:
        """E4 (`gate_ci.OUTAGE_STATES`, operator D10): `unknown` here is an unrecognised
        conclusion on a complete, red-free read, so E4 alone refuses it."""
        self.grant()
        self.check_full()
        self.refused("CI is unknown; a ci-outage release needs pending or absent",
                     reader=s0([job("verify", "stale")]))
        self.refused("CI is red", reader=RED_STATUS)
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_07_a_second_ae_under_the_same_grant_exits_1_naming_the_bound_ae(self) -> None:
        self.grant()
        self.check_full()
        url, _ = self.post_ae(reader=PENDING)
        code, message = self.exits(reader=PENDING)
        self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
        self.assertIn(f"it is bound to the AE {url}", message)
        self.assertIn(f"`FORCE {KEY} ci-outage {self.S}:", message)
        self.assertEqual(len(self.comments), 1)

    def test_08_a_failed_post_after_reserve_leaves_no_ae_bound(self) -> None:
        self.grant()
        self.check_full()
        self.exits(reader=PENDING, fail_post=True)
        self.assertEqual((force_grant.read_grant(KEY)["status"], self.records()), ("consumed", []))
        code, message = self.exits(reader=PENDING)
        self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
        self.assertIn("no AE was bound", message)
        self.assertIn(f"`FORCE {KEY} ci-outage {self.S}:", message)

    def test_09_a_force_line_replacing_the_slot_before_reserve_posts_nothing(self) -> None:
        self.grant()
        self.check_full()
        later = lambda *a, **k: self.grant(cid="901", later=1)  # noqa: E731 - a later FORCE line, same SHA
        self.refused("could not be consumed", before_reserve=later)
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_10_the_dry_run_never_consumes_and_the_post_binds_once(self) -> None:
        self.grant()
        self.check_full()
        with ExitStack() as stack:
            self.ci(stack, ABSENT)
            for step in L._publish_checks(REPO, ISSUE, "ae", self.S, "feat/12-x", ack_overlap=None,
                                          ack_no_pr_required=None, grant_on_main=False):
                step()
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")
        self.post_ae()
        self.assertEqual(len(self.records()), 1)

    def test_36_a_dirty_forge_refuses_naming_why(self) -> None:
        self.grant()
        self.check_full()
        (self.forge_root / "f").write_text("dirty")
        self.sidecar(self.S, None, body_forge=None)  # what an unguarded reader would find
        self.refused("forge tree dirty: f")

    def test_37_a_failed_bind_prints_and_the_post_completes(self) -> None:
        self.grant()
        self.check_full()
        self.state.mkdir(exist_ok=True)
        (self.state / "ci-outage").write_text("not a directory")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            url, _ = self.post_ae()
        self.receipts.assert_called_once()
        self.tasklist.record_post.assert_called_once_with(REPO, ISSUE, "ae")
        for needle in (url, "release was not recorded", f"`FORCE {KEY} ci-outage {self.S}:"):
            self.assertIn(needle, err.getvalue())
        self.sweep()
        body = f"## Lane 3 Gate Results — {KEY}\n\n**Verdict:** PASS\n**Head-SHA:** {self.S}\n"
        with mock.patch.object(ci_outage.check_lane3_ready, "fetch_comments", lambda r, i: self.comments):
            self.assertEqual(ci_outage.pass_release(REPO, ISSUE, body, "refused"), (False, "refused"))


class AeVisibleRed(World, unittest.TestCase):
    """TC 40, AE half: a required check running beside a failed job (pending), and a
    required check not yet created beside a failed job (absent)."""
    SHAPES = {"pending": s0([job("verify", None, "in_progress"), job("lint", "failure")]),
              "absent": s0([job("lint", "failure")])}

    def setUp(self) -> None:
        self.build()
        self.check_full()

    def test_40_no_grant_waits_or_refuses_as_today(self) -> None:
        for state, want in (("pending", L._carry_forward.WAIT_EXIT), ("absent", None)):
            with self.subTest(state=state):
                code, message = self.exits(reader=self.SHAPES[state])
                if want is None:
                    self.assertIn("CI is absent", message)
                else:
                    self.assertEqual(code, want)
        self.assertEqual(self.comments, [])

    def test_40_a_grant_refuses_a_visible_red_job(self) -> None:
        self.grant()
        for state, reader in self.SHAPES.items():
            with self.subTest(state=state):
                code, message = self.exits(reader=reader)
                self.assertNotEqual(code, L._carry_forward.WAIT_EXIT)
                self.assertIn("visibly red (lint:failure)", message)
        self.assertEqual(self.comments, [])


if __name__ == "__main__":
    unittest.main()
