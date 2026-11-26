"""harmonic-forge#1565 TC 35 (#894 TC 13, removed by 19afe38): an AE released by
a `ci-outage` grant is the existing l1_post AE mode, so its own dry run is the
precheck, and neither the ci-outage grant nor a release record in the force
store's `ci-outage/` directory is read as a gate grant."""
from __future__ import annotations

import json
import shlex
import unittest

from test_gate import GateCase


class CiOutageAe(GateCase):
    REAL = ("python3 tools/gh/l1_post.py --issue 1565 --kind ae --sha HEAD "
            "--branch feat/1565-x --file ae.md")

    def test_35_the_first_real_ci_outage_ae_passes_after_its_dry_run(self) -> None:
        store = self.root / "force"
        (store / "ci-outage").mkdir(parents=True)
        (store / "F1565.json").write_text(json.dumps(
            {"key": "F1565", "mode": "ci-outage", "status": "usable", "sha_prefix": "5" * 40}))
        (store / "ci-outage" / f"F1565-{'5' * 40}-9.json").write_text(json.dumps({"mode": "ci-outage"}))
        (self.repo / "ae.md").write_text("## Lane 1 AE\n")
        self.assertIsNotNone(self.decide(self.REAL))
        self.record("tools/gh/l1_post.py", shlex.split(self.REAL)[2:] + ["--dry-run"])
        self.assertIsNone(self.decide(self.REAL))


if __name__ == "__main__":
    unittest.main()
