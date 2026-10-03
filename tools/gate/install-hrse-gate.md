# Installing the hrse-gate grant broker (harmonic-forge#878)

One-time host setup, run by the operator. It needs root, which no lane has.
After it, a Lane 3 Tier P run needs a grant you mint for that exact action, and
each grant buys one run.

## Why a separate account

Every earlier design kept the authorization somewhere the agent's own uid could
write: a GitHub comment, a file under `$HOME`, a hook's regex. Two preclose
passes broke each one. Here the grants live in `/var/lib/hrse-gate`, owned by a
system account with no login, so a lane gets `EACCES` from the kernel. The only
thing a lane can do is run `consume` through one password-free sudo rule, and
`consume` spends a grant: it cannot create one.

## Install from a reviewed, pushed SHA

Use the commit the operator approved, not a local working tree. Replace
`<SHA>` with it.

```bash
sudo useradd --system --user-group --home-dir /var/lib/hrse-gate --shell /usr/sbin/nologin hrse-gate
sudo install -d -o hrse-gate -g hrse-gate -m 0700 /var/lib/hrse-gate /var/lib/hrse-gate/grants /var/lib/hrse-gate/receipts
git -C ~/harmonic-forge fetch -q origin && git -C ~/harmonic-forge show <SHA>:tools/gate/hrse_gate_broker.py > /tmp/hrse-gate.py
sudo install -o root -g root -m 0755 /tmp/hrse-gate.py /usr/local/libexec/hrse-gate && rm /tmp/hrse-gate.py
printf '%s\n' 'mmangus ALL=(hrse-gate) NOPASSWD: /usr/local/libexec/hrse-gate consume *' > /tmp/hrse-gate.sudoers && visudo -cf /tmp/hrse-gate.sudoers
sudo install -o root -g root -m 0440 /tmp/hrse-gate.sudoers /etc/sudoers.d/hrse-gate && sudo visudo -c && rm /tmp/hrse-gate.sudoers
```

The `consume *` wildcard is safe only because `consume` takes three positional
arguments and nothing else. It has no path-valued option, abbreviations are off,
and any extra token is refused. sudoers matches the arguments as one string,
and `*` spans spaces, so the parser is the rule's real scope; the broker's
tests assert both. `/usr/local/libexec/hrse-gate --version` prints the
installed file's sha256. HRSE2's `scripts/gate_production_run.py` compares it
with the tracked source and refuses to run against a stale install.

Check that exactly one new rule appeared:

```bash
sudo -n -l | grep hrse-gate
```

## Minting a grant (each production run)

Use the issue, the full gated SHA and the action exactly as the AE declares
them (`l1_post.py --prod-run`):

```bash
sudo /usr/local/libexec/hrse-gate grant 1892 <40-hex sha> script=scripts/1-1892-revive-falsely-lapsed.py,apply --ttl-minutes 60
sudo /usr/local/libexec/hrse-gate grant 1867 <40-hex sha> count-label=Task
```

This host's `Defaults targetpw` makes `sudo` ask for the root password. A grant
expires after `--ttl-minutes` (default 60, max 1440) whether or not it is used.

## Updating the broker

Repeat the `git show` and `install` steps from a newer reviewed SHA. Until you
do, `gate_production_run.py` refuses with a version-drift message. That is
intended.

## Limits, stated

The broker controls the sanctioned entrypoint only. The HRSE2 backend still
runs as `mmangus` and reads production credentials from `backend/.env`, and
`mmangus` already holds a password-free `neo4j-admin dump`. A raw credential
read is still guarded only by the auto-mode classifier, as it was before #878.
Closing that gap means running the backend under its own account. It is out of
scope here and is recorded for the operator.
