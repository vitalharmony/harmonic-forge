"""Tests for wrapper_parity (harmonic-forge#290).

The acceptance criterion that matters here is bidirectional: it is not enough
that the check passes on a correct wrapper, it must *fail* on a newly
unexposed flag. A parity check that only ever passes is the same class of
defect it exists to catch.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent / "wrapper_parity.py"
_spec = importlib.util.spec_from_file_location("wrapper_parity", _MODULE_PATH)
assert _spec and _spec.loader
wp = importlib.util.module_from_spec(_spec)
sys.modules["wrapper_parity"] = wp
_spec.loader.exec_module(wp)


def _script(tmp_path: Path, flags: list[str], name: str = "fake.py") -> Path:
    """A real argparse script, so we exercise genuine --help output.

    Dedent the template *before* interpolating: multi-line substituted text
    shifts dedent's common-prefix calculation and silently corrupts the
    generated source.
    """
    template = textwrap.dedent("""\
        import argparse
        def main():
            p = argparse.ArgumentParser(description="fake")
        __ADDS__
            p.parse_args()
        if __name__ == "__main__":
            main()
        """)
    adds = "\n".join(f'    p.add_argument("{f}")' for f in flags)
    path = tmp_path / name
    path.write_text(template.replace("__ADDS__", adds), encoding="utf-8")
    return path


def _mise(tmp_path: Path, task: str, flags: list[str]) -> Path:
    template = textwrap.dedent("""\
        [tasks.__TASK__]
        description = "fake"
        usage = '''
        __DECL__
        '''
        run = "true"
        """)
    decl = "\n".join(f'flag "{f} <v>" help="x"' for f in flags)
    path = tmp_path / "mise.toml"
    path.write_text(
        template.replace("__TASK__", task).replace("__DECL__", decl),
        encoding="utf-8",
    )
    return path


def test_parity_holds_when_wrapper_exposes_everything(tmp_path):
    script = _script(tmp_path, ["--alpha", "--beta"])
    mise = _mise(tmp_path, "t", ["--alpha", "--beta"])
    assert wp.check(mise, "t", script, set()) == []


def test_unexposed_flag_is_reported(tmp_path):
    """The failing direction — this is the whole point of the check."""
    script = _script(tmp_path, ["--alpha", "--beta"])
    mise = _mise(tmp_path, "t", ["--alpha"])
    assert wp.check(mise, "t", script, set()) == ["--beta"]


def test_declared_omission_is_accepted(tmp_path):
    script = _script(tmp_path, ["--alpha", "--beta"])
    mise = _mise(tmp_path, "t", ["--alpha"])
    assert wp.check(mise, "t", script, {"--beta"}) == []


def test_removing_a_flag_from_allow_list_reopens_the_finding(tmp_path):
    """Declaring an omission must not permanently silence it."""
    script = _script(tmp_path, ["--alpha", "--beta"])
    mise = _mise(tmp_path, "t", ["--alpha"])
    assert wp.check(mise, "t", script, {"--beta"}) == []
    assert wp.check(mise, "t", script, set()) == ["--beta"]


def test_allow_missing_accepts_bare_or_dashed_names(tmp_path):
    """argparse rejects a value starting with `--`, so both spellings work."""
    script = _script(tmp_path, ["--alpha", "--beta"])
    mise = _mise(tmp_path, "t", ["--alpha"])
    assert wp.check(mise, "t", script, {"beta"}) == []
    assert wp.check(mise, "t", script, {"--beta"}) == []


def test_help_text_mentions_are_not_counted_as_flags(tmp_path):
    """Why the usage block is parsed instead of the options section.

    gh_issue.py's --milestone help cites --tier; --body-file's cites --body.
    Scraping the options section would invent flags and fail spuriously.
    """
    src = textwrap.dedent("""\
        import argparse
        def main():
            p = argparse.ArgumentParser(description="fake")
            p.add_argument("--alpha", help="prefer --ghost over --phantom here")
            p.parse_args()
        if __name__ == "__main__":
            main()
        """)
    script = tmp_path / "mentions.py"
    script.write_text(src, encoding="utf-8")
    assert wp.script_flags(script) == {"--alpha"}


def test_help_is_never_treated_as_a_wrapped_flag(tmp_path):
    script = _script(tmp_path, ["--alpha"])
    assert "--help" not in wp.script_flags(script)


def test_missing_task_is_a_run_error_not_a_pass(tmp_path):
    """A check that cannot run must not look like a check that passed."""
    script = _script(tmp_path, ["--alpha"])
    mise = _mise(tmp_path, "other", ["--alpha"])
    with pytest.raises(wp.ParityError):
        wp.check(mise, "t", script, set())


def test_unrunnable_script_is_a_run_error(tmp_path):
    mise = _mise(tmp_path, "t", [])
    with pytest.raises(wp.ParityError):
        wp.check(mise, "t", tmp_path / "does-not-exist.py", set())


def test_live_gh_issue_usage_block_parses(tmp_path):
    """Guards the real parse against argparse formatting changes."""
    real = Path(__file__).resolve().parent / "gh_issue.py"
    flags = wp.script_flags(real)
    assert {"--repo", "--title", "--tier", "--milestone"} <= flags
