"""harmonic-forge#858: the code guard behind R-0374 (the operator's standing
production AE grant) -- what an AE claiming the grant must show before it
posts, and that a grant AE never carries forward to a new SHA."""
import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import _standing_grant as grant  # noqa: E402
import check_lane3_ready as clr  # noqa: E402
import l1_post  # noqa: E402

REPO, ISSUE = "vitalharmony/hrse", 42
GATED = "a" * 40
LATER = "b" * 40


def _footered(prefix: str, kind: str, extra: str = "") -> str:
    digest = hashlib.sha256(prefix.rstrip("\n").encode()).hexdigest()
    return f"{prefix}\n\n<!-- l1-post v1; kind={kind};{extra} body-sha256={digest} -->"


def gate(comment_id: int = 100, verdict: str = "PASS", tier: str = "R",
         head: str | None = GATED, edited: bool = False, kind: str = "gate-result",
         marker: bool = True) -> dict:
    lines = [f"## Lane 3 Gate Results — H{ISSUE}", "", f"**Verdict:** {verdict}. All cases ran.", ""]
    if tier:
        lines.append(f"Write tier {tier} throughout.")
    if head:
        lines.append(f"**Head-SHA:** {head}")  # the bolded form real reports use
    body = _footered("\n".join(lines), kind, " posted-by=LANE3;")
    if not marker:
        body = body.split("\n\n<!--")[0]
    if edited:
        body = body.replace("All cases ran.", "All cases ran, edited.")
    return {"id": comment_id, "body": body,
            "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{comment_id}"}


def ae_body(comment_id: int = 100, sha: str = GATED, cite: bool = True, apply: str = "") -> str:
    link = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{comment_id}"
    authorized = (f"**Authorized:** the operator's standing AE grant, R-0374 — gate-result "
                  f"{link}, gated SHA {sha}." if cite else "**Authorized:** the operator, in chat.")
    extra = f"\nApply path: {apply}" if apply else ""
    return f"## AE — H{ISSUE}\n\n{authorized}{extra}\n\nSequence: apply, then verify counts."


def fake_git(merge_base: int = 0, files: str = "scripts/apply.py\n", diff: int = 0):
    def run(*args, cwd=None):
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, merge_base, "c" * 40 if merge_base == 0 else "", "")
        if args[0] == "diff" and args[1] == "--name-only":
            return subprocess.CompletedProcess(args, 0, files, "")
        return subprocess.CompletedProcess(args, diff, "", "")
    return run


class CitesGrantTests(unittest.TestCase):
    def test_a_citation_claims_the_grant(self):
        self.assertTrue(grant.cites_grant(ae_body()))

    def test_a_citation_only_inside_a_fence_is_quoted_evidence(self):
        self.assertFalse(grant.cites_grant("## AE — H42\n\n```\nAuthorized: ... R-0374\n```\n"))

    def test_a_mention_off_the_authorized_line_is_not_a_claim(self):
        body = "## AE — H42\n\n**Authorized:** the operator, in chat.\n\nR-0374 does not apply here."
        self.assertFalse(grant.cites_grant(body))

    def test_no_citation_is_not_a_claim(self):
        self.assertFalse(grant.cites_grant(ae_body(cite=False)))


class GrantRefusalTests(unittest.TestCase):
    def refusal(self, body=None, comments=None, sha=GATED, git=None):
        return grant.grant_refusal(body or ae_body(), REPO, ISSUE, sha,
                                   comments if comments is not None else [gate()],
                                   git=git or fake_git())

    def test_a_linked_passing_tier_r_gate_at_this_sha_is_accepted(self):
        self.assertIsNone(self.refusal())

    def test_no_linked_gate_result_refuses(self):
        self.assertIn("links no gate-result", self.refusal(comments=[]))

    def test_a_link_to_another_issue_does_not_count(self):
        body = ae_body().replace(f"/issues/{ISSUE}#", "/issues/7#")
        self.assertIn("links no gate-result", self.refusal(body=body))

    def test_a_gate_report_without_a_body_sha256_marker_refuses(self):
        self.assertIn("no body-sha256", self.refusal(comments=[gate(marker=False)]))

    def test_a_gate_report_footered_as_another_kind_is_recognized_by_its_heading(self):
        self.assertIsNone(self.refusal(comments=[gate(kind="discussion")]))

    def test_an_unbolded_head_sha_also_parses(self):
        g = gate()
        g["body"] = g["body"].replace("**Head-SHA:**", "Head-SHA:")
        g["body"] = _footered(g["body"].split("\n\n<!--")[0], "gate-result", " posted-by=LANE3;")
        self.assertIsNone(self.refusal(comments=[g]))

    def test_a_later_commit_changing_a_named_apply_file_refuses(self):
        seen = {}

        def git(*args, cwd=None):
            if args[0] == "merge-base":
                return subprocess.CompletedProcess(args, 0, "c" * 40, "")
            if args[1] == "--name-only":
                return subprocess.CompletedProcess(args, 0, "scripts/apply.py\n", "")
            seen["files"] = args[args.index("--") + 1:]
            return subprocess.CompletedProcess(args, 1 if "scripts/_lib.py" in seen["files"] else 0, "", "")
        reason = self.refusal(body=ae_body(apply="scripts/_lib.py"), sha=LATER, git=git)
        self.assertIn("differs from the gated commit", reason)
        self.assertEqual(sorted(seen["files"]), ["scripts/_lib.py", "scripts/apply.py"])

    def test_an_edited_gate_result_refuses(self):
        self.assertIn("edited", self.refusal(comments=[gate(edited=True)]))

    def test_a_failed_gate_refuses(self):
        self.assertIn("not PASS", self.refusal(comments=[gate(verdict="FAIL")]))

    def test_a_tier_w_gate_refuses(self):
        self.assertIn("tier W", self.refusal(comments=[gate(tier="W")]))

    def test_an_unstated_tier_refuses(self):
        self.assertIn("unstated", self.refusal(comments=[gate(tier="")]))

    def test_the_tier_falls_back_to_the_sweep_before_the_gate(self):
        sweep = {"id": 90, "body": _footered("## Gate-readiness sweep — H42\n\nWrite tier R throughout.",
                                             "sweep", f" sha={GATED};")}
        self.assertIsNone(self.refusal(comments=[sweep, gate(tier="")]))

    def test_no_head_sha_refuses(self):
        self.assertIn("Head-SHA", self.refusal(comments=[gate(head=None)]))

    def test_the_ae_must_name_the_gated_sha(self):
        self.assertIn("does not name the gated SHA", self.refusal(body=ae_body(sha="d" * 40)))

    def test_a_later_commit_tree_identical_in_the_touched_files_is_accepted(self):
        self.assertIsNone(self.refusal(sha=LATER, git=fake_git(diff=0)))

    def test_a_later_commit_that_differs_in_the_touched_files_refuses(self):
        self.assertIn("differs from the gated commit", self.refusal(sha=LATER, git=fake_git(diff=1)))

    def test_an_unresolvable_gated_commit_refuses(self):
        self.assertIn("cannot resolve", self.refusal(sha=LATER, git=fake_git(merge_base=128)))


class CarryForwardTests(unittest.TestCase):
    def ready(self, comment_id: int, sha: str) -> dict:
        return {"id": comment_id, "body": _footered("## Ready for Lane 3", "ready-for-l3", f" sha={sha};")}

    def test_a_grant_ae_never_carries_forward(self):
        authority = {"id": 10, "body": _footered(ae_body(), "ae", f" sha={GATED};")}
        comments = [authority, self.ready(11, LATER)]
        self.assertIsNone(clr.carry_forward(comments, authority, LATER))

    def test_an_operator_ae_still_carries_forward(self):
        authority = {"id": 10, "body": _footered(ae_body(cite=False), "ae", f" sha={GATED};")}
        comments = [authority, self.ready(11, LATER)]
        self.assertEqual(clr.carry_forward(comments, authority, LATER)["id"], 11)


class ValidateGrantAeTests(unittest.TestCase):
    def test_a_grant_ae_cannot_carry_a_no_pr_override(self):
        with patch.object(clr, "fetch_comments", return_value=[gate()]), \
             self.assertRaises(SystemExit):
            l1_post.validate_grant_ae(ae_body(), REPO, ISSUE, GATED, "merged already")

    def test_a_non_grant_ae_reads_nothing(self):
        with patch.object(clr, "fetch_comments", side_effect=AssertionError("no fetch")):
            l1_post.validate_grant_ae(ae_body(cite=False), REPO, ISSUE, GATED, None)

    def test_a_grant_ae_on_main_needs_no_open_pr(self):
        with patch.object(l1_post, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertTrue(l1_post.grant_on_main(ae_body(), GATED))
            self.assertFalse(l1_post.grant_on_main(ae_body(cite=False), GATED))
        with patch.object(l1_post, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertFalse(l1_post.grant_on_main(ae_body(), GATED))

    def test_a_grant_ae_without_its_gate_refuses(self):
        with patch.object(clr, "fetch_comments", return_value=[]), \
             self.assertRaises(SystemExit):
            l1_post.validate_grant_ae(ae_body(), REPO, ISSUE, GATED, None)


if __name__ == "__main__":
    unittest.main()
