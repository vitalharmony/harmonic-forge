#!/usr/bin/env python3
"""Test stand-in for bwrap (harmonic-forge#1098): records its argv, then runs
the command after the boundary's `--chdir <dir> --` in that directory, with no
namespaces. Real confinement is exercised by the tests that use real bwrap."""
import json
import os
import sys

args = sys.argv[1:]
log = os.environ.get("STUB_BWRAP_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(args) + "\n")
at = args.index("--chdir")
os.chdir(args[at + 1])
command = args[at + 3:]
os.execvp(command[0], command)
