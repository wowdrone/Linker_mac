"""Drive dashboard snapshot over a STAGE1 session, and the loopback SSE server."""
import http.client
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from drive_dashboard import Dashboard, DashboardServer, SessionTap, snapshot
from guide_protocol import Mode
from test_guide_stage1 import Harness


class DashboardSnapshot(unittest.TestCase):
    def test_avoid_scenario_is_visible(self):
        h = Harness()
        self.addCleanup(h.close)
        tap = SessionTap(h.linker)
        self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, motion=False))
        hazard = (h.car.station + 25,)
        self.assertTrue(h.until(lambda: h.mode() == Mode.AVOID, hazard=hazard))
        h.until(lambda: False, timeout=.4, hazard=hazard)
        snap = json.loads(json.dumps(snapshot(h.linker, tap), allow_nan=False))
        self.assertEqual(snap["decision"]["mode"], "AVOID")
        self.assertTrue(snap["hazards"])
        ids = {item["id"] for item in snap["hazards"]}
        self.assertIn(snap["decision"]["hazard_id"], ids)
        self.assertIsNotNone(snap["hazards"][0]["remaining_m"])
        self.assertIn("rpm", snap["car"]["telemetry"])
        self.assertTrue(snap["link"]["synchronized"])
        self.assertIsNotNone(snap["drone"])
        self.assertGreater(snap["rates"]["GUIDE_HEARTBEAT"], 0)
        kinds = {row["kind"] for row in snap["log"]}
        self.assertIn("OBSTACLE_ALERT", kinds)
        self.assertEqual(h.mode(), Mode.AVOID)


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer(("127.0.0.1", 0))
        self.addCleanup(self.server.close)

    def request(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.server.port}"})
        return conn.getresponse()

    def test_page_and_stream(self):
        page = self.request("/")
        self.assertEqual(page.status, 200)
        self.assertIn(b"Linker Drive", page.read())
        self.server.publish({"profile": {}, "value": 1})
        stream = self.request("/events")
        self.assertEqual(stream.getheader("Content-Type"), "text/event-stream")
        line = stream.fp.readline()
        self.assertEqual(json.loads(line.removeprefix(b"data: ")), {"profile": {}, "value": 1})

    def test_foreign_host_rejected(self):
        self.assertEqual(self.request("/state", host="evil.example:8765").status, 403)

    def test_protected_get_requires_token(self):
        for path in ("/state", "/frame.jpg"):
            self.assertEqual(self.request(path).status, 403)
            conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
            self.addCleanup(conn.close)
            conn.request("GET", path, headers={"X-Dashboard-Token": self.server.token})
            self.assertIn(conn.getresponse().status, (200, 204))

    def test_dashboard_without_session_is_standby(self):
        dashboard = Dashboard(("127.0.0.1", 0))
        self.addCleanup(dashboard.close)
        dashboard.update()
        conn = http.client.HTTPConnection("127.0.0.1", dashboard.server.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/state", headers={"Host": f"127.0.0.1:{dashboard.server.port}",
                                                   "X-Dashboard-Token": dashboard.server.token})
        self.assertEqual(json.loads(conn.getresponse().read()), {"standby": True})

    def test_rejects_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            DashboardServer(("0.0.0.0", 0))


if __name__ == "__main__":
    unittest.main()
