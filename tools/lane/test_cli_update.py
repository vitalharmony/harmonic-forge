#!/usr/bin/env python3
"""The CLI updater on fake commands and fixture /proc trees (harmonic-forge#1305).
No test touches the network, a real CLI or the real npm root: every binary the
checked-in registry names is a fake under a temporary home."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cli_update  # noqa: E402

SERVICE = HERE / "systemd" / "forge-cli-update.service"

FAKE = r'''#!/usr/bin/env node
// A fake CLI and a fake npm in one file. Written for node, like the real ones,
// so a PATH without node fails a run the way it would on the machine.
const fs = require("fs"), path = require("path");
const self = fs.realpathSync(process.argv[1]);
const home = process.env.FAKE_HOME || path.dirname(path.dirname(path.dirname(self)));
const file = path.join(home, "state.json");
const state = JSON.parse(fs.readFileSync(file, "utf8"));
const name = path.basename(process.argv[1]), args = process.argv.slice(2);
const save = () => fs.writeFileSync(file, JSON.stringify(state));
const is = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const has = (key, who) => (state[key] || []).includes(who);
const heal = (bin) => {  // an install that puts the binary back
  const target = (state.heal || {})[bin];
  if (target) { fs.mkdirSync(path.dirname(target), {recursive: true}); fs.copyFileSync(self, target); fs.chmodSync(target, 0o755); }
};
const die = (msg) => { process.stderr.write(msg + "\n"); process.exit(1); };
if (name === "claude") {
  if (is(args, ["--version"])) console.log(state.installed.claude + " (Claude Code)");
  else if (is(args, ["update"])) {
    if (has("fail", "claude")) die("update failed");
    if (!has("stuck", "claude")) { state.installed.claude = state.latest.claude; save(); heal("claude"); }
  }
} else if (name === "codex") console.log("codex-cli " + state.installed["@openai/codex"]);
else if (name === "gemini") console.log(state.installed["@google/gemini-cli"]);
else if (name === "npm") {
  if (is(args, ["root", "-g"])) {
    if (state.root_warning) process.stderr.write(state.root_warning + "\n");
    console.log(state.root_out || path.join(home, ".npm-global", "lib", "node_modules"));
  } else if (args[0] === "view") {
    const key = args[1] === "@anthropic-ai/claude-code" ? "claude" : args[1];
    if (has("no_view", key)) die("npm ERR! offline");
    console.log(state.latest[key]);
  } else if (is(args.slice(0, 2), ["install", "-g"])) {
    const pkg = args[2].replace(/@latest$/, "");
    if (has("fail", pkg)) die("npm ERR! boom");
    if (!has("stuck", pkg)) { state.installed[pkg] = state.latest[pkg]; save(); heal({"@google/gemini-cli": "gemini", "@openai/codex": "codex"}[pkg]); }
  }
}
'''


class Home:
    """A temporary home holding fake claude, codex, gemini and npm."""

    def __init__(self, installed, latest, fail=(), **extra):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "state.json").write_text(json.dumps(
            {"installed": installed, "latest": latest, "fail": list(fail), **extra}))
        for rel in (".local/bin/claude", ".npm-global/bin/codex", ".npm-global/bin/gemini", ".npm-global/bin/npm"):
            path = self.dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.root = self.dir / ".npm-global" / "lib" / "node_modules"
        for pkg in ("@openai/codex", "@google/gemini-cli", "npm"):
            (self.root / pkg / "bin").mkdir(parents=True, exist_ok=True)
        self.proc = self.dir / "proc"
        self.proc.mkdir()
        self.log = self.dir / "Harmonic_Projects" / "logs" / "cli-versions.jsonl"

    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    def set_state(self, **extra):
        (self.dir / "state.json").write_text(json.dumps({**self.state(), **extra}))

    def process(self, pid, exe, argv=(), cwd="/"):
        entry = self.proc / str(pid)
        entry.mkdir()
        os.symlink(exe, entry / "exe")
        os.symlink(cwd, entry / "cwd")
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def run(self, **kw):
        env = {**os.environ, "FAKE_HOME": str(self.dir)}
        with mock.patch.dict(os.environ, env):
            return cli_update.main(["--home", str(self.dir)], proc_root=self.proc, **kw)

    def lines(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def last(self):
        return {row["name"]: row for row in self.lines()[-3:]}


CURRENT = {"claude": "2.1.322", "@openai/codex": "0.167.0", "@google/gemini-cli": "0.67.0"}


class Statuses(unittest.TestCase):

    def test_a_side_by_side_cli_always_updates_and_reports_from_its_versions(self):
        home = Home({**CURRENT, "claude": "2.1.319"}, {**CURRENT, "claude": "2.1.322"})
        self.assertEqual(home.run(), 0)
        row = home.last()["claude"]
        self.assertEqual((row["before"], row["after"], row["status"]), ("2.1.319", "2.1.322", "updated"))
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["claude"]["status"], "current")

    def test_an_npm_cli_at_the_published_version_is_current_and_not_reinstalled(self):
        home = Home(dict(CURRENT), dict(CURRENT), fail=("@openai/codex",))
        self.assertEqual(home.run(), 0)
        row = home.last()["codex"]
        self.assertEqual((row["before"], row["after"], row["status"]), ("0.167.0", "0.167.0", "current"))

    def test_a_stale_npm_cli_with_nothing_running_is_updated(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        self.assertEqual(home.run(), 0)
        row = home.last()["gemini"]
        self.assertEqual((row["before"], row["after"], row["status"]), ("0.66.0", "0.67.0", "updated"))
        self.assertEqual(home.state()["installed"]["@google/gemini-cli"], "0.67.0")

    def test_an_absent_binary_is_logged_and_skipped_never_an_error(self):
        home = Home(dict(CURRENT), dict(CURRENT))
        (home.dir / ".npm-global" / "bin" / "gemini").unlink()
        shutil.rmtree(home.root / "@google" / "gemini-cli")
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["gemini"], {**home.last()["gemini"], "before": None, "after": None,
                                                "status": "absent"})

    def test_a_failing_update_is_logged_the_rest_still_run_and_the_run_exits_non_zero(self):
        home = Home({**CURRENT, "@openai/codex": "0.166.0", "@google/gemini-cli": "0.66.0"},
                    dict(CURRENT), fail=("@openai/codex",))
        self.assertEqual(home.run(), 1)
        rows = home.last()
        self.assertEqual(rows["codex"]["status"], "failed")
        self.assertIn("boom", rows["codex"]["detail"])
        self.assertEqual(rows["gemini"]["status"], "updated")

    def test_an_entry_that_raises_is_isolated(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        real = cli_update.update_one

        def boom(entry, *a, **kw):
            if entry["name"] == "codex":
                raise RuntimeError("bug in codex handling")
            return real(entry, *a, **kw)

        with mock.patch.object(cli_update, "update_one", boom):
            self.assertEqual(home.run(), 1)
        rows = home.last()
        self.assertEqual(rows["codex"]["status"], "failed")
        self.assertEqual(rows["gemini"]["status"], "updated")

    def test_an_update_that_exits_zero_without_moving_is_failed_not_current(self):
        home = Home({**CURRENT, "claude": "2.1.319"}, dict(CURRENT), stuck=["claude", "@google/gemini-cli"])
        home.run()
        self.assertEqual(home.run(), 1)
        row = home.last()["claude"]
        self.assertEqual((row["before"], row["after"], row["status"]), ("2.1.319", "2.1.319", "failed"))
        self.assertIn("2.1.322 is published", row["detail"])

    def test_an_npm_update_that_exits_zero_without_moving_is_failed(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT), stuck=["@google/gemini-cli"])
        self.assertEqual(home.run(), 1)
        self.assertEqual(home.last()["gemini"]["status"], "failed")

    def test_a_side_by_side_cli_whose_published_version_is_unreadable_keeps_before_and_after(self):
        home = Home({**CURRENT, "claude": "2.1.319"}, dict(CURRENT), no_view=["claude"])
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["claude"]["status"], "updated")
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["claude"]["status"], "current")

    def test_a_warning_on_npm_roots_stderr_does_not_become_the_root(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT),
                    root_warning="npm warn Unknown user config \"foo\"")
        script = home.root / "@google/gemini-cli" / "bundle" / "gemini.js"
        home.process(4600, "/usr/bin/node", ["node", str(script)])
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["gemini"]["status"], "deferred")
        self.assertEqual(home.state()["installed"]["@google/gemini-cli"], "0.66.0")

    def test_an_npm_root_that_is_not_an_existing_absolute_directory_is_unreadable(self):
        for bad in ("relative/root", "/no/such/directory"):
            with self.subTest(root=bad):
                home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT), root_out=bad)
                self.assertEqual(home.run(), 1)
                row = home.last()["gemini"]
                self.assertEqual((row["status"], row["detail"]), ("failed", "npm root unreadable"))
                self.assertEqual(home.state()["installed"]["@google/gemini-cli"], "0.66.0")

    def test_a_dangling_link_with_nothing_running_is_updated_not_absent(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        link = home.dir / ".npm-global" / "bin" / "gemini"
        target = home.root / "@google" / "gemini-cli" / "bin" / "gemini"
        link.unlink()
        link.symlink_to(target)
        home.set_state(heal={"gemini": str(target)})
        self.assertEqual(home.run(), 0)
        row = home.last()["gemini"]
        self.assertEqual((row["before"], row["after"], row["status"]), (None, "0.67.0", "updated"))

    def test_a_dangling_link_with_the_cli_running_is_deferred(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        link = home.dir / ".npm-global" / "bin" / "gemini"
        link.unlink()
        link.symlink_to(home.root / "@google" / "gemini-cli" / "bin" / "gemini")
        home.process(4601, "/usr/bin/node", ["node", str(home.root / "@google/gemini-cli" / "bundle" / "gemini.js")])
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["gemini"]["status"], "deferred")

    def test_a_missing_link_beside_the_package_folder_is_updated_not_absent(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        link = home.dir / ".npm-global" / "bin" / "gemini"
        link.unlink()
        home.set_state(heal={"gemini": str(link)})
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["gemini"]["status"], "updated")

    def test_a_side_by_side_cli_with_a_dangling_link_is_failed(self):
        home = Home(dict(CURRENT), dict(CURRENT))
        link = home.dir / ".local" / "bin" / "claude"
        link.unlink()
        link.symlink_to(home.dir / "gone")
        self.assertEqual(home.run(), 1)
        self.assertEqual(home.last()["claude"]["status"], "failed")

    def test_every_log_line_has_the_contract_fields(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        home.run()
        for row in home.lines():
            self.assertTrue({"ts", "name", "before", "after", "status"} <= set(row), row)
            self.assertIn(row["status"], cli_update.STATUSES)
            self.assertRegex(row["ts"], r"\+00:00$")


class Deferral(unittest.TestCase):

    def stale(self):
        return Home({**CURRENT, "@openai/codex": "0.166.0", "@google/gemini-cli": "0.66.0"}, dict(CURRENT))

    def test_a_node_process_running_the_cli_defers_it(self):
        home = self.stale()
        script = home.root / "@google/gemini-cli" / "bundle" / "gemini.js"
        home.process(4100, "/usr/bin/node", ["node", str(script), "-p", "hi"])
        self.assertEqual(home.run(), 0)
        row = home.last()["gemini"]
        self.assertEqual((row["before"], row["after"], row["status"]), ("0.66.0", "0.66.0", "deferred"))
        self.assertEqual(home.state()["installed"]["@google/gemini-cli"], "0.66.0", "not updated while running")
        self.assertEqual(home.last()["codex"]["status"], "updated")

    def test_a_node_flag_before_the_script_does_not_hide_a_running_cli(self):
        home = self.stale()
        script = home.root / "@google/gemini-cli" / "bundle" / "gemini.js"
        home.process(4700, "/usr/bin/node", ["node", "--max-old-space-size=8192", str(script)])
        home.run()
        self.assertEqual(home.last()["gemini"]["status"], "deferred")

    def test_a_node_process_launched_through_a_symlinked_shim_defers_the_cli(self):
        home = self.stale()
        script = home.root / "@google/gemini-cli" / "bundle" / "gemini.js"
        script.parent.mkdir(parents=True)
        script.write_text("")
        shim = home.dir / ".npm-global" / "bin" / "gemini-shim"
        shim.symlink_to(script)
        home.process(4701, "/usr/bin/node", ["node", str(shim)])
        home.run()
        self.assertEqual(home.last()["gemini"]["status"], "deferred")

    def test_a_non_node_process_whose_first_argument_is_in_the_package_does_not_defer(self):
        home = self.stale()
        home.process(4702, "/usr/bin/less", ["less", str(home.root / "@google/gemini-cli" / "README.md")])
        home.run()
        self.assertEqual({home.last()[n]["status"] for n in ("codex", "gemini")}, {"updated"})

    def test_a_side_by_side_cli_with_a_running_process_still_updates(self):
        home = Home({**CURRENT, "claude": "2.1.319"}, dict(CURRENT))
        home.process(4703, str(home.dir / ".local" / "bin" / "claude"), [str(home.dir / ".local" / "bin" / "claude")])
        self.assertEqual(home.run(), 0)
        self.assertEqual(home.last()["claude"]["status"], "updated")

    def test_a_relative_script_resolves_against_the_process_cwd(self):
        home = self.stale()
        home.process(4101, "/usr/bin/node", ["node", "bundle/gemini.js"], cwd=str(home.root / "@google/gemini-cli"))
        home.run()
        self.assertEqual(home.last()["gemini"]["status"], "deferred")

    def test_the_native_codex_child_defers_codex(self):
        home = self.stale()
        native = home.root / "@openai/codex" / "node_modules" / "@openai/codex-linux-x64" / "vendor" / "codex"
        native.parent.mkdir(parents=True)
        native.write_text("")
        home.process(4200, str(native), [str(native)])
        home.run()
        self.assertEqual(home.last()["codex"]["status"], "deferred")

    def test_a_shell_that_mentions_the_cli_does_not_defer_it(self):
        home = self.stale()
        home.process(4300, "/usr/bin/bash", ["bash", "-c", "codex --version; gemini -p hi"])
        home.run()
        self.assertEqual({home.last()[n]["status"] for n in ("codex", "gemini")}, {"updated"})

    def test_an_npm_child_of_this_run_does_not_defer_anything(self):
        home = self.stale()
        home.process(4400, "/usr/bin/node", ["node", str(home.root / "npm" / "bin" / "npm-cli.js"), "view"])
        home.run()
        self.assertEqual({home.last()[n]["status"] for n in ("codex", "gemini")}, {"updated"})

    def test_six_deferrals_in_a_row_exit_non_zero_and_five_do_not(self):
        home = self.stale()
        script = home.root / "@google/gemini-cli" / "bundle" / "gemini.js"
        home.process(4500, "/usr/bin/node", ["node", str(script)])
        codes = [home.run() for _ in range(6)]
        self.assertEqual(codes, [0, 0, 0, 0, 0, 1])
        self.assertEqual(cli_update.deferred_streak(home.log, "gemini"), 6)

    def test_an_update_between_deferrals_resets_the_streak(self):
        home = self.stale()
        lines = [{"name": "gemini", "status": "deferred"}] * 5 + [{"name": "gemini", "status": "updated"}]
        home.log.parent.mkdir(parents=True)
        home.log.write_text("".join(json.dumps(x) + "\n" for x in lines))
        self.assertEqual(cli_update.deferred_streak(home.log, "gemini"), 0)


class UnitPath(unittest.TestCase):

    def unit_path(self):
        lines = [l for l in SERVICE.read_text().splitlines() if l.startswith("Environment=PATH=")]
        self.assertEqual(len(lines), 1, "the service sets its own PATH: the user manager's has no CLI on it")
        return lines[0].split("=", 2)[2]

    def test_under_the_units_path_no_installed_cli_is_absent(self):
        home = Home({**CURRENT, "@google/gemini-cli": "0.66.0"}, dict(CURRENT))
        path = self.unit_path().replace("%h", str(home.dir))
        done = subprocess.run(
            [sys.executable, str(HERE / "cli_update.py"), "--home", str(home.dir)],
            env={"PATH": path, "HOME": str(home.dir), "FAKE_HOME": str(home.dir)},
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        statuses = {row["name"]: row["status"] for row in home.lines()}
        self.assertNotIn("absent", statuses.values())
        self.assertEqual(statuses["gemini"], "updated")

    def test_the_units_path_covers_every_registry_binary_directory(self):
        reg = cli_update.load_registry(cli_update.REGISTRY, "%h")
        dirs = {str(Path(e["binary"]).parent) for e in reg["cli"]}
        self.assertTrue(dirs <= set(self.unit_path().split(":")), dirs)


class Registry(unittest.TestCase):

    def test_the_seed_entries_are_claude_codex_and_gemini(self):
        reg = cli_update.load_registry(cli_update.REGISTRY, "/home/x")
        self.assertEqual([e["name"] for e in reg["cli"]], ["claude", "codex", "gemini"])
        self.assertEqual({e["kind"] for e in reg["cli"]}, {"side-by-side", "npm-global"})
        self.assertTrue(all(isinstance(e["update_cmd"], list) for e in reg["cli"]), "argv lists, never a shell string")

    def test_a_new_family_is_one_entry_and_skipped_when_not_installed(self):
        home = Home(dict(CURRENT), dict(CURRENT))
        extra = cli_update.REGISTRY.read_text() + (
            '\n[[cli]]\nname = "grok"\nkind = "npm-global"\npackage = "grok-cli"\n'
            'binary = "%h/.npm-global/bin/grok"\nversion_cmd = ["%h/.npm-global/bin/grok", "--version"]\n'
            'latest_cmd = ["%h/.npm-global/bin/npm", "view", "grok-cli", "version"]\n'
            'update_cmd = ["%h/.npm-global/bin/npm", "install", "-g", "grok-cli@latest"]\n')
        reg = home.dir / "reg.toml"
        reg.write_text(extra)
        with mock.patch.dict(os.environ, {"FAKE_HOME": str(home.dir)}):
            code = cli_update.main(["--home", str(home.dir), "--registry", str(reg)], proc_root=home.proc)
        self.assertEqual(code, 0)
        self.assertEqual({r["name"]: r["status"] for r in home.lines()}["grok"], "absent")

    def test_a_shell_metacharacter_in_a_field_is_never_interpreted(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        hostile = f"$(touch {marker}); touch {marker} #"
        code, out = cli_update.run([sys.executable, "-c", "import sys; print(sys.argv[1])", hostile], 30)
        self.assertEqual((code, out.strip()), (0, hostile))
        self.assertFalse(marker.exists(), "a field reached a shell")
        code, out = cli_update.run_stdout([sys.executable, "-c", "import sys; print(sys.argv[1])", hostile], 30)
        self.assertEqual((code, out.strip()), (0, hostile))
        self.assertFalse(marker.exists(), "a field reached a shell")

    def test_every_entry_names_a_latest_cmd(self):
        reg = cli_update.load_registry(cli_update.REGISTRY, "/home/x")
        self.assertTrue(all(e.get("latest_cmd") for e in reg["cli"]))
        text = cli_update.REGISTRY.read_text().replace('latest_cmd = ["%h/.npm-global/bin/npm", "view", "@openai/codex", "version"]\n', "")
        scratch = Path(tempfile.mkdtemp()) / "reg.toml"
        scratch.write_text(text)
        with self.assertRaisesRegex(ValueError, "latest_cmd"):
            cli_update.load_registry(scratch, "/home/x")

    def test_version_parsing(self):
        for text, want in (("2.1.322 (Claude Code)", "2.1.322"), ("codex-cli 0.167.0", "0.167.0"),
                           ("0.67.0-nightly.1\n", "0.67.0-nightly.1"), ("no version", None)):
            self.assertEqual(cli_update.parse_version(text), want)


if __name__ == "__main__":
    unittest.main()
