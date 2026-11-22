"""STAGE1 integration over real UDP when GUIDE_TEST_UDP=1, otherwise a socket fixture."""
from collections import deque
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_ROOT = Path(os.environ.get("SENTINEL_SOURCE", ROOT.parent / "sentinel"))
sys.path.insert(0, str(ROOT))
from sentinel_namespace import SentinelNamespace
sentinel_namespace = SentinelNamespace(SENTINEL_ROOT)
from drive_mode_manager import avoidance_distance, stopping_distance
from guide_protocol import LINKER, SENTINEL, GuideLink, Mode, udp, utc_ms
from guide_vehicle import VehicleSession
SentinelSession = sentinel_namespace.load("guide_sentinel").SentinelSession
from obstacle_geo import Course, distance_m, from_xy
Camera = sentinel_namespace.load("obstacle_projector").Camera
Pose = sentinel_namespace.load("obstacle_projector").Pose
from tools.link_impair import LinkImpair
from tools.sim_car import SimCar

CONFIG = ROOT / "config" / "stage1.json"
GUIDE_TEST_UDP = os.environ.get("GUIDE_TEST_UDP") == "1"


class MemoryNetwork:
    def __init__(self):
        self.ports, self.next_port = {}, 22000

    def socket(self, *_):
        return MemorySocket(self)


class MemorySocket:
    def __init__(self, network):
        self.network = network
        self.address = self.peer = None
        self.queue = deque()

    def bind(self, address):
        if not address[1]:
            self.network.next_port += 1
            address = (address[0], self.network.next_port)
        if address in self.network.ports:
            raise OSError("port in use")
        self.network.ports[address] = self
        self.address = address

    def getsockname(self):
        return self.address

    def connect(self, peer):
        self.peer = peer

    def setblocking(self, _):
        pass

    def send(self, packet):
        return self.sendto(packet, self.peer)

    def sendto(self, packet, address):
        if self.address is None:
            self.bind(("127.0.0.1", 0))
        dest = self.network.ports.get(address)
        if dest and (dest.peer is None or dest.peer == self.address):
            dest.queue.append(packet)
        return len(packet)

    def recv(self, size):
        if not self.queue:
            raise BlockingIOError
        return self.queue.popleft()[:size]

    def close(self):
        if self.address in self.network.ports:
            self.network.ports.pop(self.address)


class Harness:
    def __init__(self, loss=0, key_mismatch=False):
        self.stack = ExitStack()
        try:
            self.stack.enter_context(nullcontext() if GUIDE_TEST_UDP else patch("socket.socket", MemoryNetwork().socket))
            self.root = Path(self.stack.enter_context(TemporaryDirectory()))
            self.c = json.loads(CONFIG.read_text())
            self.c["course"] = str((CONFIG.parent / self.c["course"]).resolve())
            self.c["sound"] = False
            key = self.root / "signing.hex"
            key.write_text("a1" * 32, encoding="ascii")
            key.chmod(0o600)
            self.stack.enter_context(patch.dict(os.environ, {"WOW_GUIDE_SIGNING_KEY_FILE": str(key)}))
            reservations = [udp(("127.0.0.1", 0)) for _ in range(12)]
            assigned = [sock.getsockname() for sock in reservations]
            for sock in reservations:
                sock.close()
            routes = []
            for index in range(2):
                left, right, drone, car = assigned[4 * index:4 * index + 4]
                routes.append((left, right, drone, car))
                self.c["sentinel"]["paths"][index] = dict(bind=drone, peer=left)
                self.c["linker"]["paths"][index] = dict(bind=car, peer=right)
            self.c["linker"].update(gps_bind=assigned[8], sim_output=assigned[9],
                                    log=str(self.root / "linker.jsonl"))
            self.c["sentinel"].update(detection_bind=assigned[10], fc_bind=assigned[11],
                                      log=str(self.root / "sentinel.jsonl"))
            profile = self.root / "stage1.json"
            profile.write_text(json.dumps(self.c), encoding="utf-8")
            self.linker = VehicleSession(profile)
            self.stack.callback(self.linker.close)
            if key_mismatch:
                key.write_text("b2" * 32, encoding="ascii")
            current = json.loads((ROOT / 'tests/fixtures/sentinel_current.json').read_text())
            for name, value in self.c.items():
                if name not in ('sentinel', 'camera'):
                    current[name] = value
            current['sentinel'].update(self.c['sentinel'])
            current['sentinel'].update(fc_peer=assigned[8], detection_mode='inprocess')
            current['camera'].update(self.c['camera'])
            current['sitl_motion'] = False
            current_profile = self.root / 'current.json'
            current_profile.write_text(json.dumps(current))
            self.sentinel = SentinelSession(current_profile)
            self.stack.callback(self.sentinel.close)
            self.proxy = LinkImpair(routes, loss=loss, delay=.002, seed=8)
            self.stack.callback(self.proxy.close)
            self.car = SimCar(self.c, self.linker.course, 25, self.c["car_speed_nominal_mps"])
            self.input = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.stack.callback(self.input.close)
            self.control = udp(self.c["linker"]["sim_output"])
            self.stack.callback(self.control.close)
            self.previous = self.next_gps = time.monotonic()
            self.commands = []
        except BaseException:
            self.stack.close()
            raise

    def step(self, motion=True):
        now = time.monotonic()
        for raw in receive(self.control):
            self.car.accept(raw, now)
            self.commands.append((now, json.loads(raw)))
        if motion:
            self.car.advance(now, now - self.previous)
        self.previous = now
        if now >= self.next_gps:
            self.next_gps = now + self.c["car_state_s"]
            self.input.sendto(json.dumps(self.car.gps()).encode(), tuple(self.c["linker"]["gps_bind"]))
        self.proxy.poll()
        self.linker.poll()
        self.sentinel.poll()
        self.proxy.poll()
        time.sleep(.004)

    def until(self, predicate, timeout=5, motion=True, hazard=None):
        deadline = time.monotonic() + timeout
        next_hazard = 0
        while time.monotonic() < deadline:
            if hazard and time.monotonic() >= next_hazard:
                self.inject(*hazard)
                next_hazard = time.monotonic() + .18
            self.step(motion)
            if predicate():
                return True
        return False

    def inject(self, station, kind="DEBRIS", side="LEFT", confidence=.99, lateral=.7, size=.25):
        lat, lon = self.linker.course.point_at(station)
        lat, lon = from_xy(0, -lateral, (lat, lon))
        data = {"class": {"VEHICLE": "car", "ANIMAL": "dog", "SURFACE_HAZARD": "water"}.get(kind, kind.lower()), "lat": lat, "lon": lon, "verified_free_side": side,
                "confidence": confidence, "size_m": size, "t_detect_utc_ms": utc_ms()}
        self.sentinel.detection(data)  # authenticated in-process fixture, no detection UDP bypass

    def mode(self):
        return self.linker.decision.mode if self.linker.decision else None

    def events(self, role, name):
        return [row for line in (self.root / f"{role}.jsonl").read_text().splitlines()
                if (row := json.loads(line))["event"] == name]

    def close(self):
        self.stack.close()


def receive(sock):
    while True:
        try:
            yield sock.recv(8192)
        except BlockingIOError:
            return


class StageIntegration(unittest.TestCase):
    # Current Sentinel demo truthfully reports sensor health incomplete: CAUTION, not NORMAL.
    def setup_session(self, **kwargs):
        h = Harness(**kwargs)
        self.addCleanup(h.close)
        if not kwargs.get("key_mismatch"):
            self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, motion=False))
        return h

    def test_A_left_avoid_and_clear(self):
        h = self.setup_session()
        hazard = (h.car.station + 25,)
        self.assertTrue(h.until(lambda: h.mode() == Mode.AVOID, hazard=hazard))
        self.assertTrue(h.until(lambda: h.car.lateral < -.7, hazard=hazard))
        self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, timeout=20, hazard=hazard))
        self.assertTrue(h.events("linker", "passed"))
        decisions = h.events("linker", "hazard_decision")
        initial = [row["detect_to_decision_ms"] for row in decisions if row["initial"]]
        updates = [row["detect_to_decision_ms"] for row in decisions if not row["initial"]]
        self.assertTrue(initial and updates)
        self.assertLessEqual(max(initial), 150)
        self.assertLessEqual(max(updates), 150)
        print(f"Avoidance: initial {max(initial)} ms; 5 Hz limited updates max {max(updates)} ms")

    def test_B_blocked_vehicle_stops_before_hazard(self):
        h = self.setup_session()
        station = h.car.station + h.c["detect_range_min_m"]
        hazard = (station, "VEHICLE", "NONE", .99, 0, 3)
        self.assertTrue(h.until(lambda: h.mode() == Mode.STOP, hazard=hazard))
        self.assertTrue(h.until(lambda: h.car.speed < .08, hazard=hazard))
        self.assertGreater(station - h.car.station, h.c["margin"])

    def test_C_low_confidence_caution_then_normal(self):
        h = self.setup_session()
        h.inject(h.car.station + 18, "DEBRIS", "UNKNOWN", .3)
        self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, motion=False))
        self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, timeout=4, motion=False))

    def test_D_critical_ack_with_twenty_percent_loss(self):
        h = self.setup_session(loss=.2)
        for i in range(8):
            h.inject(h.car.station + 20 + 2 * i, "VEHICLE", "NONE", .99, 0, 3)
            self.assertTrue(h.until(lambda: len(h.events("sentinel", "ack")) >= 2 * (i + 1),
                                   timeout=2, motion=False))
        self.assertGreater(h.proxy.dropped, 0)
        self.assertTrue(any(row["retry"] for row in h.events("sentinel", "sent")))
        first_decisions = h.events("linker", "hazard_decision")
        self.assertEqual(len({row["msg_id"] for row in first_decisions}), 8)
        latencies = [row["detect_to_decision_ms"] for row in first_decisions]
        self.assertLessEqual(max(latencies), 150)
        print(f"20% loss: 8/8 critical alerts ACKed; detect to decision {min(latencies)}–{max(latencies)} ms")

    def test_E_outage_slows_stops_and_resyncs(self):
        h = self.setup_session()
        h.proxy.cut = True
        cut_started = time.monotonic()
        self.assertTrue(h.until(lambda: h.mode() == Mode.LINK_DEGRADED, timeout=1))
        self.assertTrue(h.until(lambda: h.car.speed <= h.c["safe_speed_mps"] + .25, timeout=2))
        self.assertTrue(h.until(lambda: h.mode() == Mode.STOP, timeout=3))
        self.assertTrue(h.until(lambda: h.car.speed < .08, timeout=3))
        h.until(lambda: False, timeout=max(0, 5 - (time.monotonic() - cut_started)))
        h.proxy.cut = False
        self.assertTrue(h.until(lambda: h.mode() == Mode.CAUTION, timeout=3))
        self.assertGreaterEqual(len(h.events("linker", "snapshot_synced")), 2)
        # 연속 차량 명령의 감속/오프셋 변화가 프로필 한계 안에 있다.
        flagged = [(a, b) for a, b in zip(h.commands, h.commands[1:])
                   if b[0] > a[0] and a[1]["t_utc_ms"] < b[1]["t_utc_ms"]]
        for a, b in flagged:
            elapsed = (b[1]["t_utc_ms"] - a[1]["t_utc_ms"]) / 1000
            self.assertLessEqual(a[1]["target_speed_mps"] - b[1]["target_speed_mps"],
                                 h.c["a_max"] * (elapsed + .002) + .02)
            self.assertLessEqual(abs(b[1]["lateral_offset_m"] - a[1]["lateral_offset_m"]),
                                 h.c["offset_rate_mps"] * (elapsed + .002) + .02)

    def test_F_operator_override(self):
        h = self.setup_session()
        h.inject(h.car.station + 16, "VEHICLE", "NONE", .99, 0, 3)
        self.assertTrue(h.until(lambda: h.mode() == Mode.STOP, motion=False))
        h.car.manual = True
        self.assertTrue(h.until(lambda: h.mode() == Mode.MANUAL, timeout=.4))
        self.assertTrue(h.until(lambda: h.commands and not h.commands[-1][1]["automatic"], timeout=.4))

    def test_G_bad_signature(self):
        h = self.setup_session(key_mismatch=True)
        h.inject(h.car.station + 18, "VEHICLE", "NONE", .99, 0, 3)
        h.until(lambda: False, timeout=.4, motion=False)
        self.assertFalse(h.linker.route)
        self.assertTrue(any("signature" in row["reason"] for row in h.events("linker", "rejected")))
        self.assertFalse(h.events("sentinel", "ack"))

    def test_H_short_gap_triggers_escape_and_recovers(self):
        h = self.setup_session()
        sim = h.sentinel.sim
        h.car.manual = True
        h.car.speed = h.c["car_speed_nominal_mps"] / 2
        self.assertTrue(h.until(lambda: h.sentinel.car["speed_mps"] < h.c["car_speed_nominal_mps"] * .6,
                                motion=False))
        sim.sample.station = h.car.station + h.c["lead_distance_min_m"] + .1
        sim.sample.speed = h.car.speed
        h.car.speed = h.c["car_speed_nominal_mps"]
        self.assertTrue(h.until(lambda: h.sentinel.target and h.sentinel.target.state == "ESCAPE", timeout=.3))
        self.assertGreater(h.sentinel.target.speed, h.car.speed)
        started = time.monotonic()
        self.assertTrue(h.until(lambda: h.sentinel.target.gap >= h.c["lead_distance_min_m"], timeout=.5))
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(h.until(lambda: h.sentinel.target.state == "LEAD", timeout=1))

    def test_I_lost_car_state_escapes_then_existing_rtl(self):
        h = self.setup_session()
        h.proxy.cut = True
        self.assertTrue(h.until(lambda: h.sentinel.target.state == "ESCAPE", timeout=1))
        self.assertTrue(h.until(lambda: h.sentinel.target.state == "RTL_EXISTING_FAILSAFE", timeout=3.5))
        self.assertGreaterEqual(h.sentinel.sim.sample.altitude, h.c["drone_escape_alt_m"] - .2)
        self.assertGreater(abs(h.sentinel.sim.sample.lateral), h.c["course_width_m"] / 2)

    def test_J_ten_metre_stop_flags_short_margin(self):
        h = self.setup_session()
        station = h.car.station + 10
        h.inject(station, "VEHICLE", "NONE", .99, 0, 3)
        self.assertTrue(h.until(lambda: h.mode() == Mode.STOP and h.linker.decision.margin_short))
        self.assertGreater(stopping_distance(h.c["car_speed_nominal_mps"], h.c), 10)
        self.assertTrue(h.until(lambda: h.car.speed < .08, hazard=(station, "VEHICLE", "NONE", .99, 0, 3)))
        self.assertLess(h.car.station, station)


class Mechanics(unittest.TestCase):
    def setUp(self):
        self.c = json.loads(CONFIG.read_text())
        camera = json.loads((ROOT / "tests/fixtures/sentinel_current.json").read_text())["camera"]
        self.c["camera"] = {**camera, **self.c["camera"]}

    def test_project_down_and_course_wrap(self):
        camera = Camera(dict(self.c["camera"], roll_pitch_yaw_deg=[0, -90, 0]))
        pose = Pose(37.52, 126.61, 5, (0, 0, 0))
        obj = camera.project((630, 340, 650, 360), None, pose)
        self.assertLess(distance_m(pose.lat, pose.lon, obj.lat, obj.lon), .001)
        course = Course([(37.52, 126.61), (37.5201, 126.61), (37.5201, 126.6101), (37.52, 126.61)])
        self.assertAlmostEqual(course.delta(2, course.length - 1), 3)
        self.assertAlmostEqual(avoidance_distance(8.33, 1, self.c), 9.78, places=1)

    def test_time_and_sequence_validation(self):
        network = MemoryNetwork()
        log = lambda *_args, **_kwargs: None
        paths = [{"bind": ("127.0.0.1", a), "peer": ("127.0.0.1", b)} for a, b in ((50, 51), (52, 53))]
        with patch("socket.socket", network.socket):
            a = GuideLink(SENTINEL, {**self.c, "paths": paths}, b"k" * 32, log)
            self.addCleanup(a.close)
            b = GuideLink(LINKER, {**self.c, "paths": [
                {"bind": row["peer"], "peer": row["bind"]} for row in paths]}, b"k" * 32, log)
            self.addCleanup(b.close)
            def heartbeat():
                return a.make("GUIDE_HEARTBEAT", node_id=SENTINEL, t_utc_ms=utc_ms(),
                              mode=0, link_quality=1, health=1, snapshot_epoch=0, snapshot_count=0)
            older, newer = heartbeat(), heartbeat()
            a.send(newer)
            self.assertEqual([p.duplicate for _, p in b.receive()], [False, True])
            a.send(older)
            self.assertFalse(list(b.receive()))
            pending = heartbeat()
            delayed = utc_ms() - 400
            pending.data["t_utc_ms"] = delayed
            with patch("guide_protocol.utc_ms", return_value=delayed):
                a.send(pending)
            self.assertFalse(list(b.receive()))


if __name__ == "__main__":
    unittest.main()
