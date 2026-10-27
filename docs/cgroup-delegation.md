# cgroup delegation for per-lane resource limits (harmonic-forge#1181 AC6)

A `systemd-run --user --scope -p MemoryHigh=… -p CPUQuota=…` only limits
anything when the systemd user manager has the `cpu` and `memory` controllers.
On this machine it is delegated only `pids` by default, so such a scope runs
with **no limit and no error**: the scope simply has no `memory.high` or
`cpu.max` file.

## Check

```
python3 ~/harmonic-forge/tools/lane/check_cgroup_delegation.py
```

Exit 0 means `cpu` and `memory` are delegated. Exit 1 names what is missing and
the drop-in below. An unreadable controllers file counts as missing (it fails
closed).

## The one operator step (root)

Write `/etc/systemd/system/user@.service.d/delegate.conf`:

```
[Service]
Delegate=cpu memory pids
```

Then re-log (or `systemctl daemon-reexec` followed by a restart of the user
manager), and run the check again. The per-lane and per-check scopes
(AC7–AC8) refuse to launch until the check passes, rather than running
unlimited.
