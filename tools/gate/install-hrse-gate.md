# Installing the hrse-gate broker and runner (harmonic-forge#878)

One-time host setup, run by the operator. It needs root, which no lane has.
Afterwards, a Lane 3 Tier P run needs a grant you mint for that exact action,
and each grant buys one run.

## What this guarantees, and what it does not

**Guaranteed** (kernel-enforced): through the sanctioned entrypoint, one
operator-minted grant is spent per run, matched exactly against the approved
issue, HEAD and action. The only auto-approved object is root-owned at an
absolute path. The grant store is unreachable from the lane's uid (EPERM, not
a regex). At most one live grant exists per approval (issue and action), and
the receipt records which grant was spent. A lane cannot run a production step
by accident, run the wrong action, or run one twice without a second grant
from you.

**Drift-detected, not enforced:** the code a spent grant executes. The gate
script, the migration script and the interpreter binary are checked by digest
before the run. But the lane's venv (`site-packages`, every `__pycache__`)
stays writable by the lane's own uid, so a lane *determined* to misbehave can
alter what runs. That is not a new capability: the backend already runs as
`mmangus` and reads the production credential (see Limits). Protecting
production from the agents themselves is a host-architecture change, filed
separately.

## Why root ownership

Every earlier design kept some part of the authorization where the agent's own
uid could write it: a GitHub comment, a file under `$HOME`, a hook's regex, a
relative command rule, a policy file in the repo. Two preclose passes broke
each one. In this design, every object an authorization decision reads is
either owned by root or carries no authorization load, as enumerated below.
The lane can run two things without a password: `consume` (spend a grant) and
`hrse-gate-run` (the only auto-approved command). Neither can create a grant.

## Authorization inputs

These are all the inputs an authorization decision reads, and who can write each one.

| Input | Path | Writable by `mmangus`? | Load |
|---|---|---|---|
| Grant store | `/var/lib/hrse-gate/{grants,receipts}` | No: `hrse-gate` 0700 | **The authorization.** One operator-minted grant buys one run of one exact action. |
| Broker | `/usr/local/libexec/hrse-gate` | No: root 0755 | Mints (as root) and consumes grants. |
| Sudo rules | `/etc/sudoers.d/hrse-gate` and the rest | No: root 0440 | The only password-free route to `consume`. On every run, `hrse-gate-run` compares the host's whole `sudo -n -l` rule set with the manifest's `sudo_rules`, and refuses any password-free `ALL` or wildcard rule. |
| Runner | `/usr/local/libexec/hrse-gate-run` | No: root 0755 | The only auto-approved command. It verifies everything below before spending a grant. |
| Manifest | `/usr/local/share/hrse-gate/manifest.json` | No: root 0444, root directory | Names the gate worktree and its interpreter, the reviewed sha256 of the gate script, the broker, the interpreter binary and the Lane 3 policy, and the host's reviewed sudo rule set. |
| Lane 3 Claude policy | `/usr/local/share/hrse-gate/claude-lane3.json` | No: root 0444 | One allow rule, `Bash(/usr/local/libexec/hrse-gate-run:*)`. The launcher refuses to launch unless it is root-owned, not writable by anyone else, and matches the manifest's `policy_sha256`. |
| Gate script | `HRSE2-lane3/scripts/gate_production_run.py` | Yes | Drift-detected: the runner refuses unless its sha256 equals the manifest's. |
| Migration script | `HRSE2-lane3/scripts/1-*.py` | Yes | Drift-detected: the runner refuses unless it matches HEAD's committed blob. |
| `sudo`, `git` | `/usr/bin/sudo`, `/usr/bin/git` | No: root | Run by absolute path, root-ownership checked, with `PATH=/usr/bin:/bin`. A planted `~/.local/bin/sudo` is never called. |
| `HRSE_GATE_GRANT` | the gate script's environment | Yes | **Mistake guard only.** `hrse-gate-run` sets it to the spent grant's nonce before it execs the gate script, which refuses without it. That stops a retyped, direct invocation, which spends no grant. A lane can set the variable itself, so it is no boundary against a determined agent (hrse#2188). |
| AE footer (`prod-run=`) | the GitHub comment | Yes (via the API) | **Record only.** It is inside the AE's `body-sha256` and verified, but the grant is the authorization. An edited AE refuses; a forged one still needs a grant. |
| Repo policy copy | `tools/lane/policies/claude-lane3.json` | Yes | None at runtime. It is the reviewed source the install step copies from. |
| Launcher | `tools/lane/_cli_launch.sh`, `_agent_registry.sh` | Yes | None that buys capability. A tampered launcher can at most widen auto-approval over commands the classifier never hard-blocks. That is the residual below, not a grant. |
| Interpreter | `HRSE2-lane3/backend/.venv/bin/python` | Yes | Drift-detected: its resolved binary must match the manifest's `python_sha256`. Its `site-packages` and `__pycache__` are **not** covered (see What this guarantees). |

## Install from reviewed, pushed SHAs

Use the forge SHA and the HRSE2 SHA the operator approved, never a local working
tree. Replace `<FORGE_SHA>` and `<HRSE_SHA>`.

```bash
sudo useradd --system --user-group --home-dir /var/lib/hrse-gate --shell /usr/sbin/nologin hrse-gate
sudo install -d -o hrse-gate -g hrse-gate -m 0700 /var/lib/hrse-gate /var/lib/hrse-gate/grants /var/lib/hrse-gate/receipts
git -C ~/harmonic-forge fetch -q origin
git -C ~/harmonic-forge show <FORGE_SHA>:tools/gate/hrse_gate_broker.py > /tmp/hrse-gate.py
git -C ~/harmonic-forge show <FORGE_SHA>:tools/gate/hrse_gate_run.py > /tmp/hrse-gate-run.py
git -C ~/harmonic-forge show <FORGE_SHA>:tools/lane/policies/claude-lane3.json > /tmp/claude-lane3.json
sudo install -o root -g root -m 0755 /tmp/hrse-gate.py /usr/local/libexec/hrse-gate
sudo install -o root -g root -m 0755 /tmp/hrse-gate-run.py /usr/local/libexec/hrse-gate-run
sudo install -d -o root -g root -m 0755 /usr/local/share/hrse-gate
sudo install -o root -g root -m 0444 /tmp/claude-lane3.json /usr/local/share/hrse-gate/claude-lane3.json
printf '%s\n' 'mmangus ALL=(hrse-gate) NOPASSWD: /usr/local/libexec/hrse-gate consume *' > /tmp/hrse-gate.sudoers && visudo -cf /tmp/hrse-gate.sudoers
sudo install -o root -g root -m 0440 /tmp/hrse-gate.sudoers /etc/sudoers.d/hrse-gate && sudo visudo -c
rm /tmp/hrse-gate.py /tmp/hrse-gate-run.py /tmp/claude-lane3.json /tmp/hrse-gate.sudoers
```

Then write the manifest from the reviewed HRSE2 SHA:

```bash
git -C ~/Harmonic_Projects/HRSE2 fetch -q origin
LANE3="$HOME/Harmonic_Projects/HRSE2-lane3"
python3 - "$LANE3" <HRSE_SHA> > /tmp/hrse-gate-manifest.json <<'PY'
import hashlib, json, os, subprocess, sys
lane3, hrse_sha = sys.argv[1], sys.argv[2]
sha = lambda b: hashlib.sha256(b).hexdigest()
script = subprocess.run(["git", "-C", os.path.expanduser("~/Harmonic_Projects/HRSE2"), "show",
                         f"{hrse_sha}:scripts/gate_production_run.py"], capture_output=True, check=True).stdout
python = f"{lane3}/backend/.venv/bin/python"
rules = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True).stdout.splitlines()
start = next(i for i, l in enumerate(rules) if "may run the following commands" in l)
print(json.dumps({
    "worktree": lane3, "python": python,
    "script_sha256": sha(script),
    "python_sha256": sha(open(os.path.realpath(python), "rb").read()),
    "broker_sha256": sha(open("/usr/local/libexec/hrse-gate", "rb").read()),
    "policy_sha256": sha(open("/usr/local/share/hrse-gate/claude-lane3.json", "rb").read()),
    "sudo_rules": [r.strip() for r in rules[start + 1:] if r.strip()],
}, indent=1))
PY
sudo install -o root -g root -m 0444 /tmp/hrse-gate-manifest.json /usr/local/share/hrse-gate/manifest.json && rm /tmp/hrse-gate-manifest.json
```

The `consume *` wildcard is safe only because `consume` takes three positional
arguments and nothing else: no path-valued option, no abbreviations, and any
extra token is refused with the declared refusal code. sudoers matches the
arguments as one string and `*` spans spaces, so the parser is the rule's real
scope. The broker's tests assert that; `hrse-gate-run` checks the live rule.

Check that exactly one new rule appeared:

```bash
sudo -n -l | grep hrse-gate
```

## Running it (Lane 3)

```bash
/usr/local/libexec/hrse-gate-run --issue <N> --script scripts/1-<name>.py              # dry run
/usr/local/libexec/hrse-gate-run --issue <N> --script scripts/1-<name>.py --mode apply # apply
/usr/local/libexec/hrse-gate-run --issue <N> --count-label <Label>
```

`--mode apply`, not `--apply`: HRSE2's tracked `permissions.ask` rules match
`* --apply` and outrank any allow rule. They stay, so that every ad hoc
`--apply` keeps prompting.

## Minting a grant (each production run)

Use the issue, the full SHA of the gate worktree's HEAD, and the action exactly
as the AE declares them (`l1_post.py --prod-run`):

```bash
sudo /usr/local/libexec/hrse-gate status 1892 <40-hex sha> script=scripts/1-1892-revive-falsely-lapsed.py,apply
sudo /usr/local/libexec/hrse-gate grant 1892 <40-hex sha> script=scripts/1-1892-revive-falsely-lapsed.py,apply --ttl-minutes 60
```

This host's `Defaults targetpw` makes `sudo` ask for the root password. A
second `grant` for an action that already has a live grant is refused and
names the existing one; `status` shows whether a grant is live, expired or
spent. A grant expires after `--ttl-minutes` (default 60, max 1440).

## Updating

Repeat the install steps and the manifest step from newer reviewed SHAs. Until
you do, `hrse-gate-run` refuses with a digest-drift message. That is intended.

## Limits, stated

- **Chained commands.** Claude Code's prefix rule `Bash(/usr/local/libexec/hrse-gate-run:*)`
  is not asserted here to reject `hrse-gate-run … && <other command>`. A chained
  command may ride the allow rule; whatever follows still runs as the lane's
  user, which is the same residual as below. This is deliberately documented
  rather than "tested": a reimplemented matcher would only restate an assumption.


The broker and runner control the sanctioned route only. The HRSE2 backend
still runs as `mmangus` and reads production credentials from `backend/.env`,
and `mmangus` already holds a password-free `neo4j-admin dump`. A raw
credential read is still guarded only by the auto-mode classifier, as before
#878, and the agent-writable launcher can at most widen auto-approval within
that same residual. Closing it means running the backend under its own
account. That is out of scope here and is recorded for the operator.
