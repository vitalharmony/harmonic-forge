#!/usr/bin/env python3
"""harmonic-forge#1565 TCs 31-34: the `FORCE <KEY> ci-outage <40-hex sha>` line.

The grant store is a temp dir; nothing here reaches ~/.claude/state/force/.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import expand_lane_shorthand as m  # noqa: E402
import force_grant  # noqa: E402
import imc_waiver  # noqa: E402

SHA = "5" * 40


class CiOutageLine(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        # An hrse issue has no preclose receipt, so there is no covered head.
        for target, attr, value in ((force_grant, "FORCE_STATE_DIR", self.state),
                                    (force_grant, "covered_head", lambda repo, issue: None)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def grant(self, key: str = "H12") -> dict | None:
        path = self.state / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_31_a_ci_outage_line_needs_no_preclose_receipt(self) -> None:
        receipt = m.authorize_force(f"FORCE H12 ci-outage {SHA}: Actions outage", "sess-1", "1")
        self.assertIn("pending ci-outage grant written for H12", receipt)
        self.assertIn("the AE at that SHA, beside its check-full receipt", receipt)
        grant = self.grant()
        self.assertEqual((grant["mode"], grant["sha_prefix"], grant["status"], grant["repo"]),
                         ("ci-outage", SHA, "pending", "vitalharmony/hrse"))

    def test_32_a_12_hex_ci_outage_line_is_refused(self) -> None:
        receipt = m.authorize_force(f"FORCE H12 ci-outage {SHA[:12]}: outage", "sess-1", "1")
        self.assertIn("FORCE REFUSED for H12", receipt)
        self.assertIn("full 40-character", receipt)
        self.assertIsNone(self.grant())


class CiOutageGrantBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(force_grant, "FORCE_STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def usable(self, mode: str) -> None:
        # Both bindings name SHA, so only the mode compare can refuse.
        now = imc_waiver._now()
        grant = force_grant.write_pending("H12", "vitalharmony/hrse", 12, SHA, "r", "s", "line",
                                          now, mode=mode, sha_prefix=SHA)
        imc_waiver.write_json_atomic(force_grant.grant_path("H12"),
                                     {**grant, "status": "usable", "comment_id": "9",
                                      "comment_url": "https://x/9"})

    def refused(self, mode: str) -> str:
        with self.assertRaises(force_grant.GrantRefused) as ctx:
            force_grant.require("vitalharmony/hrse", 12, SHA, mode=mode)
        return str(ctx.exception)

    def test_33_modes_never_cross(self) -> None:
        self.usable("plain")
        self.assertIn("authorizes a plain force, not a ci-outage one", self.refused("ci-outage"))
        self.usable("ci-outage")
        for other in ("plain", "route"):
            with self.subTest(other=other):
                self.assertIn(f"authorizes a ci-outage force, not a {other} one", self.refused(other))
        self.assertEqual(force_grant.require("vitalharmony/hrse", 12, SHA, mode="ci-outage")["mode"],
                         "ci-outage")

    def test_34_the_grant_comment_names_the_full_sha_and_never_a_merge(self) -> None:
        grant = force_grant.write_pending("H12", "vitalharmony/hrse", 12, None, "outage", "s", "line",
                                          mode="ci-outage", sha_prefix=SHA)
        text = force_grant.comment_body(grant)
        self.assertIn(f"sha={SHA} mode=ci-outage", text)
        self.assertIn("never a merge", text)


if __name__ == "__main__":
    unittest.main()
