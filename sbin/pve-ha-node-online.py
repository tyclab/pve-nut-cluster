#!/usr/bin/env python3
"""Exit 0 once HA can take NODE's parked rows back, else 1.

Usage: pve-ha-node-online.py NODE (run on NODE: its boot time dates the LRM status)

A row restored before the HA manager counts NODE online is recovered onto a peer (measured in a reboot drill). An
online node_status can predate a manager round that still reads the LRM's pre-shutdown maintenance status and
flips NODE back, so the LRM status must also carry this boot's active mode. The LRM's own state is no gate: with
nothing to run it idles.
"""
import json
import subprocess
import sys

node = sys.argv[1]
ha = json.loads(subprocess.check_output(["pvesh", "get", "/cluster/ha/status/manager_status", "--output-format", "json"]))
with open("/proc/stat") as stat:
    booted = next(int(line.split()[1]) for line in stat if line.startswith("btime "))
lrm = ha.get("lrm_status", {}).get(node, {})
sys.exit(
    not (
        ha.get("manager_status", {}).get("node_status", {}).get(node) == "online"
        and lrm.get("mode") == "active"
        and lrm.get("timestamp", 0) > booted
    )
)
