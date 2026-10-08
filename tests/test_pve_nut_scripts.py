"""The UPS scripts against fake qm/pct/ha-manager/pvesh/upsc: final wave, tier 1, restore, request path."""

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHUTDOWN = ROOT / "bin/pve-nut-shutdown.sh"
RESTORE = ROOT / "sbin/pve-nut-restore.sh"
TIER = ROOT / "sbin/pve-nut-tier.sh"
UPSSCHED_CMD = ROOT / "bin/pve-nut-upssched-cmd.sh"
EXAMPLES = ROOT / "examples"

FAKES = {
    "qm": r"""#!/usr/bin/env bash
echo "qm $*" >> "$FAKE/calls"
case "$1" in
  list) [ -e "$FAKE/qm-fail" ] && exit 1
        echo "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID"
        for f in "$FAKE"/vm/*; do [ -e "$f" ] || continue
          printf '%10s %-20s %-10s %8s %15s %8s\n' "$(basename "$f")" "vm$(basename "$f")" "$(cat "$f")" 2048 32.00 0; done ;;
  shutdown|stop) echo stopped > "$FAKE/vm/$2" ;;
  start) echo running > "$FAKE/vm/$2" ;;
  status) echo "status: $(cat "$FAKE/vm/$2")" ;;
esac
""",
    "pct": r"""#!/usr/bin/env bash
echo "pct $*" >> "$FAKE/calls"
case "$1" in
  list) echo "VMID       Status     Lock         Name"
        for f in "$FAKE"/ct/*; do [ -e "$f" ] || continue
          printf '%-10s %-10s %-12s %s\n' "$(basename "$f")" "$(cat "$f")" "" "ct$(basename "$f")"; done ;;
  shutdown|stop) echo stopped > "$FAKE/ct/$2" ;;
  start) echo running > "$FAKE/ct/$2" ;;
  status) echo "status: $(cat "$FAKE/ct/$2")" ;;
esac
""",
    "ha-manager": r"""#!/usr/bin/env bash
echo "ha-manager $*" >> "$FAKE/calls"
case "$1" in
  status) if [ -e "$FAKE/no-quorum" ]; then echo "quorum No quorum"; else echo "quorum OK"; echo "master pve1 (active, Sat Sep 27 12:00:00 2026)"; fi ;;
  set) { [ -e "$FAKE/ha-fail" ] || [ -e "$FAKE/ha-fail-$2" ]; } && exit 1
       echo "$4" > "$FAKE/ha/$2"
       if [[ "$4" == started && ! -e "$FAKE/no-start-$2" ]]; then echo running > "$FAKE/${2/:/\/}"; fi ;;
esac
""",
    "pvesh": r"""#!/usr/bin/env bash
echo "pvesh $*" >> "$FAKE/calls"
python3 - "$FAKE/ha" <<'EOF'
import json, os, sys
d = sys.argv[1]
print(json.dumps([{"sid": s, "state": open(os.path.join(d, s)).read().strip()} for s in sorted(os.listdir(d))]))
EOF
""",
    # ups.status as the fake upsd reports it; no file = upsd unreachable.
    "upsc": '#!/usr/bin/env bash\necho "upsc $*" >> "$FAKE/calls"\ncat "$FAKE/ups.status" 2>/dev/null || exit 1\n',
    "guest-ready": r"""#!/usr/bin/env bash
echo "guest-ready $*" >> "$FAKE/calls"
case "$1" in
  preflight) [[ ! -e "$FAKE/storage-fail-$2" ]] ;;
  ha-state) cat "$FAKE/ha/$2" ;;
  ready) [[ ! -e "$FAKE/health-fail-$2" ]] || exit 1
         [[ "$(cat "$FAKE/${2/:/\/}")" == running ]] || exit 1
         [[ "${3:-}" != --managed || "$(cat "$FAKE/ha/$2")" == started ]] ;;
esac
""",
    "logger": "#!/usr/bin/env bash\nexit 0\n",
    "systemctl": '#!/usr/bin/env bash\necho "systemctl $*" >> "$FAKE/calls"\n',
    "etherwake": '#!/usr/bin/env bash\necho "etherwake $*" >> "$FAKE/calls"\n',
    # Up while $FAKE/up/<host> exists.
    "ping": '#!/usr/bin/env bash\n[ -e "$FAKE/up/${!#}" ]\n',
    "node-online": "#!/usr/bin/env bash\nexit 0\n",
    "halt": '#!/usr/bin/env bash\necho halted > "$FAKE/halted"\n',
}

PEER = "192.0.2.13"


class Fixture:
    def __init__(self, role="client", shed="201 204 209", halt_node=0, nas_ports=(), peers="", wait=180, wol=""):
        self.root = Path(tempfile.mkdtemp())
        self.fake = self.root / "fake"
        self.bin = self.root / "bin"
        for d in ("vm", "ct", "ha", "up"):
            (self.fake / d).mkdir(parents=True)
        self.bin.mkdir()
        for name, body in FAKES.items():
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.state = self.root / "state"
        self.req = self.root / "req"
        self.req.mkdir()
        self.log = self.root / "log"
        self.conf = self.root / "pve-nut.conf"
        lines = [f"NUT_ROLE={role}", "UPS_SYS=ups@127.0.0.1", 'EDGE_VMIDS="101 102"', f'SHED_VMIDS="{shed}"', f"SHED_HALT_NODE={halt_node}"]
        if role == "server":
            lines += ["NAS_HOST=127.0.0.1", f'NAS_PORTS="{" ".join(str(p) for p in nas_ports)}"', f'PEER_HOSTS="{peers}"',
                      f"KILLPOWER_WAIT={wait}", "WOL_IFACE=vmbr0.2", f'WOL_TARGETS="{wol}"']
        self.conf.write_text("\n".join(lines) + "\n")
        self.ups("OL")

    def env(self, **extra):
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "PVE_NUT_STATE_DIR": str(self.state),
            "PVE_NUT_LOG": str(self.log),
            "PVE_NUT_HALT_CMD": str(self.bin / "halt"),
            "PVE_NUT_CONF": str(self.conf),
            "PVE_NUT_NAS_POLL": "1",
            "PVE_NUT_NODE_ONLINE": str(self.bin / "node-online"),
            "PVE_NUT_GUEST_READY": str(self.bin / "guest-ready"),
            "PVE_NUT_STATE_WRITER": str(ROOT / "sbin/pve-nut-state.py"),
            "PVE_NUT_GATE_RETRIES": "1",
            "PVE_NUT_GATE_DELAY": "0",
            "PVE_NUT_LOCK_WAIT": "1",
            "PVE_NUT_REQ_DIR": str(self.req),
            "PVE_NUT_SHUTDOWN_CMD": str(SHUTDOWN),
            "PVE_NUT_RESTORE_CMD": str(RESTORE),
            "PVE_NUT_WOL_ROUNDS": "3",
            "PVE_NUT_WOL_DELAY": "0",
            **extra,
        }

    def guest(self, kind, vmid, status="running", ha=None):
        (self.fake / kind / str(vmid)).write_text(status + "\n")
        if ha:
            (self.fake / "ha" / f"{kind}:{vmid}").write_text(ha + "\n")

    def status(self, kind, vmid):
        return (self.fake / kind / str(vmid)).read_text().strip()

    def ha(self, sid):
        return (self.fake / "ha" / sid).read_text().strip()

    def ups(self, status):
        (self.fake / "ups.status").write_text(status + "\n")

    def peer_up(self, host, up=True):
        p = self.fake / "up" / host
        if up:
            p.write_text("")
        else:
            p.unlink(missing_ok=True)

    def calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def wait_for_call(self, prefix, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(c.startswith(prefix) for c in self.calls()):
                return True
            time.sleep(0.1)
        return False

    def halted(self):
        return (self.fake / "halted").exists()

    def log_second(self, text):
        """The +Ns stamp of the first log line containing text."""
        for line in self.log.read_text().splitlines():
            if text in line:
                return int(re.search(r" \+(\d+)s ", line).group(1))
        raise AssertionError(f"{text!r} not logged:\n{self.log.read_text()}")

    def run(self, script, *args, check=True, **extra):
        return subprocess.run([str(script), *args], env=self.env(**extra), capture_output=True, text=True, check=check)

    def cleanup(self):
        shutil.rmtree(self.root)


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


class FinalWaveTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.fx.cleanup())

    def test_primary_parks_error_rows_edge_last_then_waits_for_nas_and_peers(self):
        s1, p1 = listener()
        s2, p2 = listener()
        self.fx = fx = Fixture(role="server", nas_ports=(p1, p2), peers=PEER)
        fx.peer_up(PEER)
        fx.guest("vm", 101)
        fx.guest("vm", 201, ha="started")
        fx.guest("vm", 205, ha="error")
        fx.guest("vm", 210, "stopped", ha="started")
        fx.guest("ct", 204)

        # The NAS and the peer go away 2 s after the edge stop begins: step 4b has to wait for them.
        def release():
            fx.wait_for_call("qm shutdown 101")
            time.sleep(2)
            s1.close()
            s2.close()
            fx.peer_up(PEER, up=False)

        threading.Thread(target=release, daemon=True).start()
        fx.run(SHUTDOWN)
        self.assertTrue(fx.halted())
        parked = (fx.state / "parked").read_text().split()
        self.assertEqual(sorted(parked), ["vm:201", "vm:205", "vm:210"])
        self.assertEqual(fx.ha("vm:205"), "ignored")
        for kind, vmid in (("vm", 101), ("vm", 201), ("vm", 205), ("ct", 204)):
            self.assertEqual(fx.status(kind, vmid), "stopped")
        calls = fx.calls()
        self.assertGreater(calls.index("qm shutdown 101 --forceStop 1 --timeout 45"),
                           calls.index("qm shutdown 201 --forceStop 1 --timeout 90"))
        release = calls.index("systemctl stop pve-ha-lrm pve-ha-crm")
        self.assertGreater(release, calls.index("ha-manager set vm:210 --state ignored"))
        self.assertLess(release, calls.index("qm shutdown 201 --forceStop 1 --timeout 90"))
        log = fx.log.read_text()
        self.assertIn(f"step 4b done: NAS 127.0.0.1 closed {p1},{p2}, peers down: {PEER}", log)
        self.assertIn("all guests stopped, halting", log)
        self.assertGreaterEqual(fx.log_second("step 4b done") - fx.log_second("VM 101 stopped"), 2)
        self.assertIn("script bound 552 s", log)

    def test_primary_halts_when_the_wait_bound_expires(self):
        sock, port = listener()
        self.addCleanup(sock.close)
        self.fx = fx = Fixture(role="server", nas_ports=(port,), peers=PEER, wait=2)
        fx.peer_up(PEER)
        fx.guest("vm", 201)
        fx.run(SHUTDOWN)
        self.assertTrue(fx.halted())
        self.assertIn("WARN: NAS/peer readiness not confirmed within 2 s:", fx.log.read_text())

    def test_secondary_skips_the_wait(self):
        self.fx = fx = Fixture(role="client")
        fx.guest("vm", 102)
        fx.guest("ct", 211, ha="started")
        fx.run(SHUTDOWN)
        self.assertTrue(fx.halted())
        log = fx.log.read_text()
        self.assertNotIn("step 4b", log)
        self.assertIn("script bound 370 s", log)
        self.assertIn("systemctl stop pve-ha-lrm pve-ha-crm", fx.calls())
        self.assertEqual((fx.state / "parked").read_text().split(), ["ct:211"])

    def test_dry_run_changes_nothing(self):
        self.fx = fx = Fixture(role="server", nas_ports=(1,), peers=PEER)
        fx.guest("vm", 201, ha="started")
        out = fx.run(SHUTDOWN, "--dry-run").stdout
        self.assertIn("ha-manager set vm:201 --state ignored", out)
        self.assertIn("step 4b NAS shares closed and peers down (primary only, bound 180 s)", out)
        self.assertFalse(fx.halted())
        self.assertEqual(fx.status("vm", 201), "running")
        self.assertFalse(fx.state.exists())
        out = fx.run(SHUTDOWN, "--dry-run", "--shed").stdout
        self.assertIn("record vm:201 ha", out)
        self.assertNotIn("step 4b", out)


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.fx.cleanup())

    def test_drill_keeps_the_edge_and_node_up_and_hands_over_to_the_restore(self):
        self.fx = fx = Fixture(role="server", peers=PEER)
        fx.state.mkdir()
        (fx.state / "drill").write_text("")
        fx.guest("vm", 101)
        fx.guest("vm", 201, ha="started")
        fx.guest("vm", 210)
        fx.guest("ct", 204, ha="started")
        out = fx.run(SHUTDOWN, "--dry-run").stdout
        self.assertIn("DRILL marker present", out)
        self.assertIn("step 4  edge VM: kept running (drill)", out)
        self.assertIn("record vm:210 plain", out)
        self.assertTrue((fx.state / "drill").exists())
        fx.run(SHUTDOWN)
        self.assertFalse(fx.halted())
        self.assertEqual(fx.status("vm", 101), "running")
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertEqual(fx.status("vm", 210), "stopped")
        self.assertEqual(fx.status("ct", 204), "stopped")
        self.assertEqual(sorted((fx.state / "parked").read_text().split()), ["ct:204", "vm:201"])
        self.assertEqual((fx.state / "shed").read_text().split(), ["vm:210", "plain"])
        self.assertFalse((fx.state / "drill").exists())
        self.assertTrue((fx.state / "tier1").exists())
        self.assertTrue((fx.state / "nut-reset").exists())
        calls = fx.calls()
        self.assertIn("systemctl stop pve-ha-lrm pve-ha-crm", calls)
        self.assertIn("systemctl start pve-ha-crm pve-ha-lrm", calls)
        self.assertIn("systemctl start --no-block pve-nut-restore.service", calls)
        self.assertNotIn("qm shutdown 101 --forceStop 1 --timeout 45", calls)
        log = fx.log.read_text()
        self.assertIn("DRILL on", log)
        self.assertIn("drill done: killpower skipped", log)

    def test_drill_marker_means_nothing_on_a_secondary(self):
        self.fx = fx = Fixture(role="client")
        fx.state.mkdir()
        (fx.state / "drill").write_text("")
        fx.guest("vm", 102)
        fx.run(SHUTDOWN)
        self.assertTrue(fx.halted())
        self.assertEqual(fx.status("vm", 102), "stopped")
        self.assertTrue((fx.state / "drill").exists())


class ShedTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.fx.cleanup())

    def test_shed_stops_listed_guests_only_and_records_them(self):
        self.fx = fx = Fixture(role="client", shed="101 201 204 209")
        fx.guest("vm", 101)
        fx.guest("vm", 201, ha="started")
        fx.guest("vm", 202, ha="started")
        fx.guest("ct", 204)
        fx.guest("ct", 209, "stopped")
        fx.run(SHUTDOWN, "--shed")
        self.assertFalse(fx.halted())
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertEqual(fx.status("ct", 204), "stopped")
        self.assertEqual(fx.status("vm", 101), "running")
        self.assertEqual(fx.status("vm", 202), "running")
        self.assertEqual(fx.ha("vm:201"), "ignored")
        self.assertEqual(fx.ha("vm:202"), "started")
        self.assertEqual(sorted((fx.state / "shed").read_text().splitlines()), ["ct:204 plain", "vm:201 ha"])
        self.assertFalse((fx.state / "parked").exists())

    def test_shed_halt_node_continues_into_the_final_wave(self):
        self.fx = fx = Fixture(role="client", shed="209", halt_node=1)
        fx.guest("vm", 209, ha="started")
        fx.guest("vm", 213, ha="started")
        fx.run(SHUTDOWN, "--shed")
        self.assertTrue(fx.halted())
        self.assertEqual((fx.state / "shed").read_text().split(), ["vm:209", "ha"])
        self.assertEqual((fx.state / "parked").read_text().split(), ["vm:213"])
        self.assertEqual(fx.status("vm", 213), "stopped")

    def test_shed_with_a_broken_listing_sheds_nothing(self):
        self.fx = fx = Fixture(role="client", shed="201 204")
        fx.guest("vm", 201, ha="started")
        fx.guest("ct", 204)
        (fx.fake / "qm-fail").write_text("")
        result = fx.run(SHUTDOWN, "--shed", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fx.status("vm", 201), "running")
        self.assertEqual(fx.status("ct", 204), "running")
        self.assertEqual(fx.ha("vm:201"), "started")
        self.assertFalse((fx.state / "shed").exists())
        self.assertIn("ERROR: guest enumeration failed, nothing shed", fx.log.read_text())
        self.assertEqual(fx.run(SHUTDOWN, "--dry-run", "--shed", check=False).returncode, 1)

    def test_shed_records_ha_intent_even_when_parking_fails(self):
        self.fx = fx = Fixture(role="client", shed="201")
        fx.guest("vm", 201, ha="started")
        (fx.fake / "ha-fail").write_text("")
        fx.run(SHUTDOWN, "--shed")
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertEqual((fx.state / "shed").read_text().split(), ["vm:201", "ha"])
        self.assertIn("ERROR: park of vm:201 failed", fx.log.read_text())


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.fx.cleanup())

    def test_restores_parked_and_shed_and_moves_both_files(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:213\n")
        (fx.state / "shed").write_text("vm:209 ha\nct:204 plain\nvm:201 plain\n")
        (fx.state / "tier1").write_text("")
        fx.guest("vm", 213, "stopped", ha="ignored")
        fx.guest("vm", 209, "stopped", ha="ignored")
        fx.guest("ct", 204, "stopped")
        fx.guest("vm", 201)
        fx.run(RESTORE)
        self.assertEqual(fx.ha("vm:213"), "started")
        self.assertEqual(fx.ha("vm:209"), "started")
        self.assertEqual(fx.status("ct", 204), "running")
        self.assertNotIn("qm start 201", fx.calls())
        self.assertFalse((fx.state / "parked").exists())
        self.assertFalse((fx.state / "shed").exists())
        # The wake marker belongs to pve-nut-tier.sh.
        self.assertTrue((fx.state / "tier1").exists())
        names = sorted(p.name for p in fx.state.iterdir() if p.name.startswith("restored"))
        self.assertTrue(names[0].startswith("restored-") and names[1].startswith("restored-shed-"), names)

    def test_closed_gate_keeps_the_files(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:213\n")
        (fx.fake / "no-quorum").write_text("")
        fx.guest("vm", 213, "stopped", ha="ignored")
        result = fx.run(RESTORE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fx.ha("vm:213"), "ignored")
        self.assertEqual((fx.state / "parked").read_text(), "vm:213\n")

    def test_partial_failure_keeps_the_unfinished_lines(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:213\nvm:210\n")
        (fx.state / "shed").write_text("vm:209 ha\nct:204 plain\n")
        (fx.fake / "ha-fail-vm:210").write_text("")
        for vmid in (213, 210, 209):
            fx.guest("vm", vmid, "stopped", ha="ignored")
        fx.guest("ct", 204, "stopped")
        result = fx.run(RESTORE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fx.ha("vm:213"), "started")
        self.assertEqual(fx.ha("vm:210"), "ignored")
        self.assertEqual((fx.state / "parked").read_text(), "vm:210\n")
        self.assertFalse((fx.state / "shed").exists())
        self.assertEqual(fx.status("ct", 204), "running")

    def test_shed_request_aborts_the_gate_wait(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:213\n")
        (fx.fake / "no-quorum").write_text("")
        fx.guest("vm", 213, "stopped", ha="ignored")
        abort = fx.req / "shed.request"
        abort.write_text("")
        result = fx.run(RESTORE, check=False, PVE_NUT_ABORT_FILE=str(abort))
        self.assertEqual(result.returncode, 1)
        self.assertIn("restore abandoned", result.stdout)
        self.assertEqual([c for c in fx.calls() if c.startswith("ha-manager")], [])
        self.assertEqual((fx.state / "parked").read_text(), "vm:213\n")

    def test_held_lock_keeps_the_files(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:213\n")
        fx.guest("vm", 213, "stopped", ha="ignored")
        holder = subprocess.Popen(["flock", str(fx.state / ".lock"), "sleep", "10"])
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        result = fx.run(RESTORE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".lock held for 1 s", result.stdout)
        self.assertEqual(fx.ha("vm:213"), "ignored")
        self.assertEqual((fx.state / "parked").read_text(), "vm:213\n")

    def test_nothing_pending_exits_clean(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "shed").write_text("")
        fx.run(RESTORE)
        self.assertTrue((fx.state / "shed").exists())

    def test_accepted_start_stays_pending_until_runtime_and_application_are_healthy(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        (fx.fake / "no-start-vm:201").touch()
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertEqual(fx.ha("vm:201"), "started")
        self.assertTrue((fx.state / "parked").exists())
        (fx.fake / "no-start-vm:201").unlink()
        (fx.fake / "health-fail-vm:201").touch()
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertEqual(fx.status("vm", 201), "running")
        self.assertTrue((fx.state / "parked").exists())
        (fx.fake / "health-fail-vm:201").unlink()
        fx.run(RESTORE)
        self.assertFalse((fx.state / "parked").exists())

    def test_storage_unavailable_does_not_consume_ha_start_retries(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        (fx.fake / "storage-fail-vm:201").touch()
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertEqual(fx.ha("vm:201"), "ignored")
        self.assertFalse(any(c.startswith("ha-manager set") for c in fx.calls()))

    def test_error_recovery_is_bounded_to_one_disabled_transition(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="error")
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertEqual(fx.ha("vm:201"), "disabled")
        fx.run(RESTORE)
        self.assertFalse((fx.state / "parked").exists())
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="error")
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertEqual(fx.ha("vm:201"), "error")
        self.assertEqual(fx.calls().count("ha-manager set vm:201 --state disabled"), 1)

    def test_final_wave_or_unknown_ups_blocks_direct_restore(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        for status in ("", "FSD OL CHRG LB", "OB", "OL LB"):
            with self.subTest(status=status):
                fx.ups(status)
                self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
                self.assertEqual(fx.ha("vm:201"), "ignored")
        fx.ups("OL")
        (fx.state / "final-wave").write_text("same-boot\n")
        self.assertEqual(fx.run(RESTORE, check=False, PVE_NUT_BOOT_ID="same-boot").returncode, 1)
        fx.run(RESTORE, PVE_NUT_BOOT_ID="next-boot")
        self.assertFalse((fx.state / "parked").exists())

    def test_failed_error_intent_write_does_not_mutate_ha(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="error")
        result = fx.run(RESTORE, check=False, PVE_NUT_STATE_WRITER=shutil.which("false"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fx.ha("vm:201"), "error")
        self.assertFalse(any(c.startswith("ha-manager set") for c in fx.calls()))

    def test_final_wave_during_readiness_preserves_old_and_new_pending_rows(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "running", ha="started")
        checker = fx.bin / "guest-ready"
        checker.write_text('#!/usr/bin/env bash\nprintf "same-boot\\n" > "$PVE_NUT_STATE_DIR/final-wave"\n'
                           'printf "vm:202\\n" >> "$PVE_NUT_STATE_DIR/parked"\nexit 0\n')
        result = fx.run(RESTORE, check=False, PVE_NUT_BOOT_ID="same-boot")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((fx.state / "parked").read_text(), "vm:201\nvm:202\n")
        self.assertFalse(list(fx.state.glob("restored-*")))

    def test_unknown_ha_state_never_requests_start(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "parked").write_text("vm:201\n")
        fx.guest("vm", 201, "stopped", ha="unknown")
        self.assertEqual(fx.run(RESTORE, check=False).returncode, 1)
        self.assertFalse(any(c.startswith("ha-manager set") for c in fx.calls()))


WOL = "aa:bb:cc:dd:ee:01=192.0.2.13 aa:bb:cc:dd:ee:02=192.0.2.13"
WAKES = ["etherwake -i vmbr0.2 aa:bb:cc:dd:ee:01", "etherwake -i vmbr0.2 aa:bb:cc:dd:ee:02"]


def fake_restore(fx, drop=None, fail_first=False):
    """A RESTORE_CMD that records its run and drops a request file on the way; fail_first exits 1 on
    its first run, as a gate that closed while the peers held the quorum would."""
    script = fx.bin / "fake-restore"
    body = '#!/usr/bin/env bash\necho "restore" >> "$FAKE/calls"\n'
    if drop:
        body += f': > "{fx.req / drop}"\n'
        if drop == "shed.request":
            body += 'echo OB > "$FAKE/ups.status"\n'
    if fail_first:
        body += '[ -e "$FAKE/restore-failed" ] || { : > "$FAKE/restore-failed"; exit 1; }\n'
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


class ExamplesTest(unittest.TestCase):
    """The shipped examples wire together: every key the scripts read is set, and upssched sees each event it acts on."""

    def test_example_conf_sources_every_key(self):
        keys = ["NUT_ROLE", "UPS_SYS", "EDGE_VMIDS", "SHED_VMIDS", "SHED_HALT_NODE", "NAS_HOST", "NAS_PORTS",
                "PEER_HOSTS", "KILLPOWER_WAIT", "WOL_IFACE", "WOL_TARGETS"]
        shell = 'source "$1"; shift; for key in "$@"; do [[ -v $key ]] || echo "$key"; done'
        result = subprocess.run(["bash", "-c", shell, "nut-test", str(EXAMPLES / "pve-nut.conf"), *keys],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "")

    def test_example_dry_run_holds_back_the_edge(self):
        self.fx = fx = Fixture(role="server")
        self.addCleanup(fx.cleanup)
        fx.guest("vm", 101)
        fx.guest("vm", 201)
        out = fx.run(SHUTDOWN, "--dry-run", PVE_NUT_CONF=str(EXAMPLES / "pve-nut.conf")).stdout
        self.assertLess(out.index("qm shutdown 201"), out.index("qm shutdown 101"))
        self.assertIn("step 4  edge VM last", out)

    def test_upsmon_execs_every_event_upssched_acts_on(self):
        """upssched sees only EXEC-flagged events: LOWBATT without EXEC let the shed timer outlive the final wave."""
        sched = (EXAMPLES / "upssched.conf").read_text()
        at = re.findall(r"^AT (\S+) \* (\S+) (\S+)", sched, re.M)
        self.assertIn(("LOWBATT", "CANCEL-TIMER", "shed"), at)
        self.assertIn(("FSD", "CANCEL-TIMER", "shed"), at)
        mon = (EXAMPLES / "upsmon.conf").read_text()
        self.assertIn("NOTIFYCMD /usr/sbin/upssched", mon)
        self.assertIn('SHUTDOWNCMD "/usr/local/bin/pve-nut-shutdown.sh"', mon)
        self.assertIn(f"CMDSCRIPT /usr/local/bin/{UPSSCHED_CMD.name}", sched)
        flags = dict(re.findall(r"^NOTIFYFLAG (\S+)\s+(\S+)", mon, re.M))
        for event in {e for e, _, _ in at}:
            self.assertIn("EXEC", flags.get(event, "").split("+"), event)


class TierPathTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.fx.cleanup())

    def test_request_file_drives_shed_then_restore_with_wake(self):
        self.fx = fx = Fixture(role="server", shed="201", wol=WOL)
        fx.guest("vm", 201, ha="started")
        fx.ups("OB DISCHRG")
        fx.run(UPSSCHED_CMD, "shed")
        self.assertTrue((fx.req / "shed.request").exists())
        fx.run(TIER, "consume")
        self.assertFalse((fx.req / "shed.request").exists())
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertTrue((fx.state / "tier1").exists())
        self.assertFalse(fx.halted())

        # The peer (both MACs, one address) is down at round 1 and back at round 2: one packet per MAC, then done.
        fx.ups("OL CHRG")
        (fx.bin / "ping").write_text(
            '#!/usr/bin/env bash\nn=$(cat "$FAKE/pings" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE/pings"; [ "$n" -gt 2 ]\n')
        fx.run(UPSSCHED_CMD, "restore")
        out = fx.run(TIER, "consume").stdout
        self.assertEqual(fx.ha("vm:201"), "started")
        self.assertEqual([c for c in fx.calls() if c.startswith("etherwake")], WAKES)
        self.assertIn("wake done after 2 round(s)", out)
        self.assertFalse((fx.state / "tier1").exists())
        self.assertFalse((fx.state / "shed").exists())

    def test_restore_without_prior_shed_wakes_nobody(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.run(UPSSCHED_CMD, "restore")
        fx.run(TIER, "consume")
        self.assertEqual([c for c in fx.calls() if c.startswith("etherwake")], [])

    def test_unsafe_ups_never_wakes_or_consumes_retry_budget(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").touch()
        for status in ("", "FSD OL CHRG LB", "OB", "OL LB"):
            with self.subTest(status=status):
                fx.ups(status)
                result = fx.run(TIER, "boot", check=False, PVE_NUT_RESTORE_CMD=fake_restore(fx))
                self.assertEqual(result.returncode, 1)
                self.assertFalse(any(c.startswith(("etherwake", "restore")) for c in fx.calls()))
                self.assertFalse((fx.state / "restore-attempts").exists())
                self.assertTrue((fx.state / "tier1").exists())

    def test_failed_restore_stops_after_persisted_attempt_limit(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "shed").write_text("vm:201 ha\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        (fx.fake / "health-fail-vm:201").touch()
        for _ in range(3):
            result = fx.run(TIER, "boot", check=False, PVE_NUT_RESTORE_ATTEMPTS="2")
            self.assertEqual(result.returncode, 1)
        self.assertIn("attempt limit 2 reached", result.stdout)
        self.assertEqual((fx.state / "restore-attempts").read_text(), "2\n")
        self.assertEqual(fx.calls().count("ha-manager set vm:201 --state started"), 2)
        self.assertTrue((fx.state / "shed").exists())

    def test_attempt_persistence_failure_prevents_wake_or_restore(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").touch()
        result = fx.run(TIER, "boot", check=False, PVE_NUT_STATE_WRITER=shutil.which("false"))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(c.startswith(("etherwake", "ha-manager set")) for c in fx.calls()))

    def test_a_new_shutdown_starts_a_new_recovery_budget(self):
        self.fx = fx = Fixture()
        fx.state.mkdir()
        (fx.state / "restore-attempts").write_text("10\n")
        (fx.state / "error-reset-vm:201").touch()
        fx.guest("vm", 201, "running", ha="started")
        fx.run(SHUTDOWN)
        self.assertFalse((fx.state / "restore-attempts").exists())
        self.assertFalse((fx.state / "error-reset-vm:201").exists())
        self.assertTrue((fx.state / "parked").exists())

    def test_shed_is_skipped_while_the_ups_reports_online(self):
        self.fx = fx = Fixture(role="client", shed="201")
        fx.guest("vm", 201, ha="started")
        fx.ups("OL")
        fx.run(UPSSCHED_CMD, "shed")
        out = fx.run(TIER, "consume").stdout
        self.assertIn("shed requested but ups@127.0.0.1 reports 'OL', skipped", out)
        self.assertEqual(fx.status("vm", 201), "running")
        self.assertFalse(fx.state.exists())

    def test_shed_stands_down_once_the_final_wave_is_under_way(self):
        self.fx = fx = Fixture(role="server", shed="201", wol=WOL)
        fx.guest("vm", 201, ha="started")
        for status in ("OB LB", "FSD OB LB", "FSD OB"):
            with self.subTest(status=status):
                fx.ups(status)
                fx.run(UPSSCHED_CMD, "shed")
                out = fx.run(TIER, "consume").stdout
                self.assertIn(f"shed requested but ups@127.0.0.1 reports '{status}', skipped", out)
                self.assertFalse((fx.req / "shed.request").exists())
                self.assertEqual(fx.status("vm", 201), "running")
                self.assertEqual(fx.ha("vm:201"), "started")
                # No tier1 marker: the boot-time restore must not wake peers for a shed that never ran.
                self.assertFalse(fx.state.exists())

    def test_restore_is_skipped_while_the_ups_reports_battery(self):
        self.fx = fx = Fixture(role="client")
        fx.state.mkdir()
        (fx.state / "shed").write_text("vm:201 ha\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        fx.ups("OB")
        fx.run(UPSSCHED_CMD, "restore")
        out = fx.run(TIER, "consume").stdout
        self.assertIn("restore gate closed", out)
        self.assertEqual(fx.ha("vm:201"), "ignored")
        self.assertTrue((fx.state / "shed").exists())

    def test_request_arriving_during_a_restore_is_consumed_not_swept(self):
        self.fx = fx = Fixture(role="client", shed="201")
        fx.guest("vm", 201, ha="started")
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        fx.run(UPSSCHED_CMD, "restore")
        out = fx.run(TIER, "consume", PVE_NUT_RESTORE_CMD=fake_restore(fx, drop="shed.request")).stdout
        self.assertNotIn("unknown request", out)
        self.assertEqual(fx.calls().count("restore"), 1)
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertTrue((fx.state / "tier1").exists())
        self.assertFalse((fx.req / "shed.request").exists())

    def test_shed_request_during_the_wake_aborts_it_and_keeps_the_marker(self):
        self.fx = fx = Fixture(role="server", shed="201", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        fx.guest("vm", 201, ha="started")
        fx.run(UPSSCHED_CMD, "restore")
        # Round 2 sleeps 1 s beside the restore, which drops the request at once.
        out = fx.run(TIER, "consume", PVE_NUT_RESTORE_CMD=fake_restore(fx, drop="shed.request"), PVE_NUT_WOL_DELAY="1").stdout
        self.assertIn("wake abandoned", out)
        self.assertIn("restore incomplete", out)
        self.assertEqual([c for c in fx.calls() if c.startswith("etherwake")], WAKES)
        self.assertEqual(fx.status("vm", 201), "stopped")
        self.assertTrue((fx.state / "tier1").exists())

    def test_exhausted_wake_rounds_keep_the_marker_for_retry(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        result = fx.run(TIER, "restore", check=False, PVE_NUT_RESTORE_CMD=fake_restore(fx))
        self.assertEqual(result.returncode, 1)
        out = result.stdout
        self.assertEqual([c for c in fx.calls() if c.startswith("etherwake")], WAKES * 3)
        calls = fx.calls()
        self.assertLess(calls.index(WAKES[0]), calls.index("restore"))
        self.assertIn("WARN: wake rounds exhausted, not seen back: " + WOL, out)
        self.assertTrue((fx.state / "tier1").exists())

    def test_restore_that_gave_up_before_the_peers_answered_runs_once_more(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        (fx.state / "shed").write_text("vm:201 ha\n")
        fx.guest("vm", 201, "stopped", ha="ignored")
        # Two pings per round (one per MAC): down through round 2, up from round 3.
        (fx.bin / "ping").write_text(
            '#!/usr/bin/env bash\nn=$(cat "$FAKE/pings" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE/pings"; [ "$n" -gt 4 ]\n')
        out = fx.run(TIER, "restore", PVE_NUT_RESTORE_CMD=fake_restore(fx, fail_first=True)).stdout
        calls = fx.calls()
        self.assertEqual(calls.count("restore"), 2)
        self.assertEqual([c for c in calls if c.startswith("etherwake")], WAKES * 2)
        self.assertIn("the peers are back, restore once more", out)
        self.assertNotIn("WARN", out)
        self.assertFalse((fx.state / "tier1").exists())

    def test_answering_peer_gets_no_packet(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        fx.peer_up(PEER)
        out = fx.run(TIER, "boot", PVE_NUT_RESTORE_CMD=fake_restore(fx)).stdout
        self.assertIn("every peer answers", out)
        self.assertEqual([c for c in fx.calls() if c.startswith("etherwake")], [])
        self.assertFalse((fx.state / "tier1").exists())
        self.assertEqual([c for c in fx.calls() if "restart" in c], [])

    def test_drill_boot_restarts_upsd_then_upsmon_before_the_wake_and_drops_a_stale_killpower_flag(self):
        self.fx = fx = Fixture(role="server", wol=WOL)
        fx.state.mkdir()
        (fx.state / "tier1").write_text("")
        (fx.state / "nut-reset").write_text("")
        flag = fx.fake / "killpower"
        flag.write_text("0000-00-00\n")
        upsmon_conf = fx.root / "upsmon.conf"
        upsmon_conf.write_text(f'MONITOR ups@127.0.0.1 1 u p primary\nPOWERDOWNFLAG "{flag}"\n')
        (fx.bin / "ping").write_text(
            '#!/usr/bin/env bash\nn=$(cat "$FAKE/pings" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE/pings"; [ "$n" -gt 2 ]\n')
        out = fx.run(TIER, "boot", PVE_NUT_RESTORE_CMD=fake_restore(fx), PVE_NUT_UPSMON_CONF=str(upsmon_conf)).stdout
        calls = fx.calls()
        restarts = [c for c in calls if "restart" in c]
        self.assertEqual(restarts, ["systemctl restart nut-server", "systemctl restart nut-monitor"])
        self.assertLess(calls.index(restarts[1]), calls.index(WAKES[0]))
        self.assertIn(f"WARN: {flag} outlived the upsmon restart, removed", out)
        self.assertFalse(flag.exists())
        self.assertFalse((fx.state / "nut-reset").exists())
        self.assertFalse((fx.state / "tier1").exists())

    def test_unknown_timer_and_request_are_refused(self):
        self.fx = fx = Fixture()
        self.assertEqual(fx.run(UPSSCHED_CMD, "bogus", check=False).returncode, 1)
        (fx.req / "bogus.request").write_text("")
        out = fx.run(TIER, "consume").stdout
        self.assertIn("unknown request bogus.request removed", out)
        self.assertFalse((fx.req / "bogus.request").exists())


if __name__ == "__main__":
    unittest.main()
