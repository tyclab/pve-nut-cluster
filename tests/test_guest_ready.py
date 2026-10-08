"""Recovery acceptance against PVE responses and real local HTTP probes."""

from contextlib import contextmanager
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "guest_ready", Path(__file__).resolve().parents[1] / "sbin/pve-nut-guest-ready.py"
)
ready = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ready)


class GuestReadyTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{"vmid": 205, "type": "qemu", "node": "pve2", "status": "running"}]
        self.config = {
            "scsi0": "local-zfs:vm-205-disk-0,size=32G",
            "ide2": "local-zfs:vm-205-cloudinit,media=cdrom",
            "efidisk0": "local-zfs:vm-205-disk-1,size=1M",
            "cicustom": "user=local:snippets/critical-user.yaml",
        }
        self.runtime = {"status": "running"}
        self.ha = [{"type": "service", "sid": "vm:205", "state": "started"}]
        self.storage = {
            "local-zfs": {"active": 1, "enabled": 1},
            "local": {"active": 1, "enabled": 1},
        }
        self.contents = {"local": [{"volid": "local:snippets/critical-user.yaml"}]}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(ready, "api", side_effect=self.api).start()
        patch.dict(ready.os.environ, {"PVE_NUT_HEALTH_CHECKS": "", "PVE_NUT_METRIC_CHECKS": ""}).start()

    def api(self, path, *args):
        self.calls.append((path, args))
        if path == "/cluster/resources":
            return self.rows
        if path == "/cluster/ha/status/current":
            return self.ha
        prefix = "/nodes/pve2/qemu/205"
        if path == prefix + "/config":
            return self.config
        if path == prefix + "/status/current":
            return self.runtime
        for name, status in self.storage.items():
            if path == f"/nodes/pve2/storage/{name}/status":
                return status
            if path == f"/nodes/pve2/storage/{name}/content":
                self.assertEqual(args, ("--content", "snippets"))
                return self.contents.get(name, [])
        raise AssertionError(f"Unexpected PVE read: {path} {args}")

    def test_preflight_uses_current_placement_and_exact_local_snippet(self):
        ready.preflight("vm:205")
        self.assertIn(("/nodes/pve2/storage/local/content", ("--content", "snippets")), self.calls)

    def test_nas_storage_active_does_not_prove_required_snippet_exists(self):
        self.config["cicustom"] = "user=nas:snippets/critical-user.yaml"
        self.storage["nas"] = {"active": 1, "enabled": 1}
        self.contents["nas"] = [{"volid": "nas:snippets/some-other-guest.yaml"}]
        with self.assertRaisesRegex(ValueError, "snippet.*unavailable"):
            ready.preflight("vm:205")

    def test_every_custom_snippet_must_exist(self):
        self.config["cicustom"] += ",network=local:snippets/critical-network.yaml"
        with self.assertRaisesRegex(ValueError, "critical-network"):
            ready.preflight("vm:205")

    def test_inactive_root_storage_blocks_start_even_with_snippet_present(self):
        self.storage["local-zfs"]["active"] = 0
        with self.assertRaisesRegex(ValueError, "local-zfs"):
            ready.preflight("vm:205")

    def test_disabled_storage_blocks_start(self):
        self.storage["local"]["enabled"] = 0
        with self.assertRaises(ValueError):
            ready.preflight("vm:205")

    def test_unattached_disk_does_not_require_unused_nas(self):
        self.config["unused0"] = "retired-nas:vm-205-disk-9"
        self.config["ide0"] = "none,media=cdrom"
        ready.preflight("vm:205")
        self.assertFalse(any("retired-nas" in path for path, _ in self.calls))

    def test_guest_lock_blocks_start(self):
        self.config["lock"] = "migrate"
        with self.assertRaisesRegex(ValueError, "locked"):
            ready.preflight("vm:205")

    def test_missing_or_ambiguous_guest_placement_is_not_ready(self):
        good = self.rows[0]
        for rows in [[], [good, dict(good, node="pve3")]]:
            with self.subTest(rows=rows), patch.object(self, "rows", rows):
                with self.assertRaisesRegex(ValueError, "placement"):
                    ready.ready("vm:205", True)

    def test_accepted_ha_start_does_not_substitute_for_running_runtime(self):
        self.runtime["status"] = "stopped"
        with self.assertRaisesRegex(ValueError, "not running"):
            ready.ready("vm:205", True)

    def test_stale_cluster_state_does_not_pass_running_node_state(self):
        self.rows[0]["status"] = "stopped"
        with self.assertRaisesRegex(ValueError, "not running"):
            ready.ready("vm:205", True)

    def test_running_guest_waits_for_ha_started_acknowledgment(self):
        for state in ["starting", "error", "stopped", "disabled"]:
            with self.subTest(state=state):
                self.ha[0]["state"] = state
                with self.assertRaisesRegex(ValueError, "HA"):
                    ready.ready("vm:205", True)

    def test_unmanaged_running_guest_does_not_require_ha_registration(self):
        self.ha = []
        ready.ready("vm:205", False)
        self.assertNotIn(("/cluster/ha/status/current", ()), self.calls)

    def test_ha_status_is_exact_service_not_another_row(self):
        for rows in [
            [{"type": "service", "sid": "vm:206", "state": "started"}],
            [{"type": "lrm", "sid": "vm:205", "state": "started"}],
            self.ha * 2,
        ]:
            with self.subTest(rows=rows), patch.object(self, "ha", rows):
                with self.assertRaises(ValueError):
                    ready.ready("vm:205", True)

    @contextmanager
    def http_server(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                status = 200 if self.path.startswith("/metric-") else {
                    "/ready": 200, "/starting": 503, "/login": 200, "/redirect": 302
                }.get(self.path, 404)
                self.send_response(status)
                if self.path == "/redirect":
                    self.send_header("Location", "/login")
                self.end_headers()
                if self.path.startswith("/metric-"):
                    sample = {"metric": {"job": "loki"}, "value": [time.time(), "1"]}
                    payload = {"status": "success", "data": {"resultType": "vector", "result": [sample]}}
                    if self.path == "/metric-stale":
                        sample["value"][0] -= 180
                    elif self.path == "/metric-future":
                        sample["value"][0] += 180
                    elif self.path == "/metric-down":
                        sample["value"][1] = "0"
                    elif self.path == "/metric-nan":
                        sample["value"][1] = "NaN"
                    elif self.path == "/metric-empty":
                        payload["data"]["result"] = []
                    elif self.path == "/metric-ambiguous":
                        payload["data"]["result"].append({"metric": {"job": "loki", "instance": "old"}, "value": [time.time(), "1"]})
                    elif self.path == "/metric-error":
                        payload = {"status": "error", "errorType": "timeout", "error": "query timed out"}
                    elif self.path == "/metric-matrix":
                        payload["data"]["resultType"] = "matrix"
                    self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", seen
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_running_guest_is_not_ready_until_all_its_app_probes_pass(self):
        with self.http_server() as (url, seen):
            with patch.dict(ready.os.environ, {"PVE_NUT_HEALTH_CHECKS": f"vm:205={url}/ready vm:205={url}/starting"}):
                with self.assertRaises((ValueError, OSError)):
                    ready.ready("vm:205", True)
            self.assertEqual(seen, ["/ready", "/starting"])

    def test_successful_probe_ignores_proxy_and_other_guests(self):
        with self.http_server() as (url, seen):
            with patch.dict(ready.os.environ, {
                "PVE_NUT_HEALTH_CHECKS": f"vm:206={url}/starting vm:205={url}/ready",
                "http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": "",
            }):
                ready.ready("vm:205", True)
            self.assertEqual(seen, ["/ready"])

    def test_redirect_to_login_does_not_pass_application_health(self):
        with self.http_server() as (url, _):
            with patch.dict(ready.os.environ, {"PVE_NUT_HEALTH_CHECKS": f"vm:205={url}/redirect"}):
                with self.assertRaises((ValueError, OSError)):
                    ready.ready("vm:205", True)

    def test_fresh_single_healthy_service_metric_passes(self):
        with self.http_server() as (url, seen):
            with patch.dict(ready.os.environ, {
                "PVE_NUT_METRIC_CHECKS": f"vm:206={url}/metric-down vm:205={url}/metric-good",
                "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": "",
            }):
                ready.ready("vm:205", True)
            self.assertEqual(seen, ["/metric-good"])

    def test_http_success_does_not_make_unhealthy_metrics_ready(self):
        with self.http_server() as (url, _):
            for condition in ["stale", "future", "down", "nan", "empty", "ambiguous", "error", "matrix"]:
                with self.subTest(condition=condition), patch.dict(ready.os.environ, {
                    "PVE_NUT_METRIC_CHECKS": f"vm:205={url}/metric-{condition}",
                }):
                    with self.assertRaises(ValueError):
                        ready.ready("vm:205", True)

    def test_each_required_service_metric_must_pass(self):
        with self.http_server() as (url, seen):
            with patch.dict(ready.os.environ, {"PVE_NUT_METRIC_CHECKS": f"vm:205={url}/metric-good vm:205={url}/metric-down"}):
                with self.assertRaises(ValueError):
                    ready.ready("vm:205", True)
            self.assertEqual(seen, ["/metric-good", "/metric-down"])


if __name__ == "__main__":
    unittest.main()
