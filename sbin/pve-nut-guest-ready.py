#!/usr/bin/env python3
"""Read-only PVE restore gates. A start request is not evidence of a running guest."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(path, *args):
    result = subprocess.run(
        ["pvesh", "get", path, *args, "--output-format", "json"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout)


def locate(sid):
    kind, vmid = sid.split(":", 1)
    expected = {"vm": "qemu", "ct": "lxc"}[kind]
    rows = api("/cluster/resources", "--type", "vm")
    matches = [r for r in rows if str(r.get("vmid")) == vmid and r.get("type") == expected]
    if len(matches) != 1:
        raise ValueError(f"{sid}: guest placement is missing or ambiguous")
    return matches[0], f"/nodes/{matches[0]['node']}/{expected}/{vmid}"


def ha_state(sid):
    rows = api("/cluster/ha/status/current")
    matches = [r for r in rows if r.get("type") == "service" and r.get("sid") == sid]
    if len(matches) != 1:
        raise ValueError(f"{sid}: HA runtime state missing or ambiguous")
    return matches[0]["state"]


def preflight(sid):
    """Check every referenced PVE store and exact custom snippet before accepting a start."""
    row, path = locate(sid)
    config = api(path + "/config")
    if config.get("lock"):
        raise ValueError(f"{sid}: guest configuration is locked")
    stores = set()
    snippets = []
    for key, value in config.items():
        if re.fullmatch(r"(?:scsi|sata|ide|virtio|efidisk|tpmstate|mp)\d+|rootfs", key):
            volume = str(value).split(",", 1)[0]
            if re.match(r"^[A-Za-z0-9_-]+:", volume):
                stores.add(volume.split(":", 1)[0])
        if key == "cicustom":
            for entry in str(value).split(","):
                if not re.fullmatch(r"(?:user|vendor|meta|network)=[A-Za-z0-9_-]+:snippets/[^,\s]+", entry):
                    raise ValueError(f"{sid}: malformed custom snippet reference")
                volume = entry.split("=", 1)[1]
                store = volume.split(":", 1)[0]
                stores.add(store)
                snippets.append((store, volume))
    for store in sorted(stores):
        status = api(f"/nodes/{row['node']}/storage/{store}/status")
        if not status.get("active") or not status.get("enabled"):
            raise ValueError(f"{sid}: storage {store} is not active and enabled")
    for store, volume in snippets:
        contents = api(f"/nodes/{row['node']}/storage/{store}/content", "--content", "snippets")
        if not any(r.get("volid") == volume for r in contents):
            raise ValueError(f"{sid}: snippet {volume} is unavailable")


def ready(sid, managed):
    row, path = locate(sid)
    if row.get("status") != "running" or api(path + "/status/current").get("status") != "running":
        raise ValueError(f"{sid}: guest is not running")
    if managed and ha_state(sid) != "started":
        raise ValueError(f"{sid}: HA has not confirmed started")
    # No credentials in these URLs. Repeated sid entries require ALL of its endpoints.
    for entry in os.environ.get("PVE_NUT_HEALTH_CHECKS", "").split():
        target, url = entry.split("=", 1)
        if target != sid:
            continue
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{sid}: health URL must use HTTP(S)")
        # Explicitly ignore proxy environment: these are local infrastructure probes.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=5) as response:
            if response.status != 200:
                raise ValueError(f"{sid}: health probe returned {response.status}")
    # An internal service without a host port can be graded by its existing
    # Prometheus up series. Require actual fresh samples, never HTTP200 alone.
    for entry in os.environ.get("PVE_NUT_METRIC_CHECKS", "").split():
        target, url = entry.split("=", 1)
        if target != sid:
            continue
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{sid}: metric URL must use HTTP(S)")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=5) as response:
            payload = json.load(response)
        data = payload.get("data", {})
        results = data.get("result", [])
        if payload.get("status") != "success" or data.get("resultType") != "vector" or len(results) != 1:
            raise ValueError(f"{sid}: readiness metric is missing or invalid")
        now = time.time()
        for sample in results:
            timestamp, value = sample["value"]
            if float(value) != 1 or not 0 <= now - float(timestamp) <= 90:
                raise ValueError(f"{sid}: readiness metric is unhealthy or stale")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "ready", "ha-state"])
    parser.add_argument("sid", type=lambda value: value if re.fullmatch(r"(?:vm|ct):[1-9][0-9]*", value) else parser.error("invalid sid"))
    parser.add_argument("--managed", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "preflight":
            preflight(args.sid)
        elif args.mode == "ha-state":
            print(ha_state(args.sid))
        else:
            ready(args.sid, args.managed)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
