"""Sentinel Legacy loopback browser dashboard for a Linker VehicleSession (HTTP + SSE).

Guide session state is read-only. Desktop requests use a queue consumed by Qt.
"""
import argparse
from collections import deque
import json
import math
from pathlib import Path
import sys
import time
import webbrowser

from dashboard_server import LoopbackServer
from guide_protocol import KINDS, Mode, utc_ms
from obstacle_geo import local_xy

PAGE = Path(__file__).resolve().with_name("drive_dashboard.html")
DEFAULT_BIND = ("127.0.0.1", 8765)
PUBLISH_S = 0.1
STANDBY = {"standby": True}
SEVERITY = ("INFO", "CAUTION", "WARNING", "CRITICAL")
SIDES = ("UNKNOWN", "LEFT", "RIGHT", "BOTH", "NONE")
STATES = ("NEW", "UPDATE", "CLEARED")
LOCAL_EVENTS = {"snapshot_synced", "link_degraded", "expired", "passed", "rejected",
                "gps_rejected", "hazard_decision"}


def num(value, digits=3):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def bearing(origin, target):
    east, north = local_xy(*target, origin)
    if math.hypot(east, north) < 1.0:
        return None
    return math.degrees(math.atan2(east, north)) % 360


def describe(kind, data):
    if kind == "OBSTACLE_ALERT":
        return (f'#{data["obstacle_id"]} {STATES[data["state"]]} {KINDS[data["type"]]} '
                f'{SEVERITY[data["severity"]]} lat {data["lateral_m"]:+.1f} m · 여유 {SIDES[data["free_side"]]}')
    if kind == "HAZARD_CLEAR":
        return f'#{data["obstacle_id"]} CLEARED'
    if kind == "DRIVE_ADVICE":
        return (f'{Mode(data["advice_mode"]).name} · 목표 {data["target_speed_mps"]:.1f} m/s · '
                f'offset {data["lateral_offset_m"]:+.1f} m · 원인 #{data["reason_obstacle_id"]}')
    if kind == "MODE_ACK":
        return (f'{Mode(data["applied_mode"]).name} · {"수락" if data["accepted"] else "거부"} · '
                f'msg {data["ack_msg_id"]}')
    return ""


def describe_local(event, fields):
    if event == "snapshot_synced":
        return "SYNC", f'동기화 완료 · epoch {fields["epoch"]} · 장애물 {fields["count"]}건'
    if event == "link_degraded":
        return "LINK", "heartbeat 끊김 · 재동기화 요청"
    if event == "expired":
        return "EXPIRE", f'#{fields["obstacle_id"]} TTL 만료'
    if event == "passed":
        return "PASS", f'#{fields["obstacle_id"]} 차량 통과'
    if event == "rejected":
        return "REJECT", f'경로 {fields["path"] + 1} 거부 · {fields["reason"]}'
    if event == "gps_rejected":
        return "GPS", f'GPS 거부 · {fields["reason"]}'
    return "DECISION", f'{fields["mode"]} · 탐지→판단 {fields["detect_to_decision_ms"]} ms'


class _LogTap:
    def __init__(self, inner, hook):
        self.inner, self.hook = inner, hook

    def __call__(self, event, **fields):
        self.inner(event, **fields)
        self.hook(event, fields)

    def close(self):
        self.inner.close()


class SessionTap:
    """Records protocol traffic of a VehicleSession for display."""

    def __init__(self, session, size=12):
        self.events = deque(maxlen=size)
        self.times = {}
        self.last_key = {}
        self.delay_ms = None
        receive, send = session.receive, session.link.send
        log = _LogTap(session.log, self._on_log)
        session.log = session.link.log = log

        def on_receive(path, packet, now):
            if not packet.duplicate:
                self._count(packet.kind)
                if packet.kind not in ("GUIDE_HEARTBEAT", "DRONE_STATE") and not (
                        packet.kind == "OBSTACLE_ALERT" and STATES[packet.data["state"]] == "UPDATE"
                ) and self._changed(packet.kind, packet.data):
                    self._event("S", packet.kind, packet.data["seq"], describe(packet.kind, packet.data))
            receive(path, packet, now)

        def on_send(packet):
            send(packet)
            self._count("TX_" + packet.kind)
            if packet.kind == "MODE_ACK" and self._changed(packet.kind, packet.data):
                self._event("L", packet.kind, packet.data["seq"], describe(packet.kind, packet.data))

        session.receive, session.link.send = on_receive, on_send

    def _count(self, kind):
        self.times.setdefault(kind, deque(maxlen=200)).append(time.monotonic())

    def rate(self, kind, window=2.0):
        cutoff = time.monotonic() - window
        return sum(stamp >= cutoff for stamp in self.times.get(kind, ())) / window

    def _event(self, direction, kind, seq, text):
        self.events.appendleft(dict(utc_ms=utc_ms(), dir=direction, kind=kind, seq=seq, text=text))

    def _changed(self, kind, data):
        # Advice and ACKs repeat at the alert update rate; show only changes.
        if kind == "DRIVE_ADVICE":
            key = (data["advice_mode"], data["reason_obstacle_id"])
        elif kind == "MODE_ACK":
            key = (data["applied_mode"], data["accepted"])
        else:
            return True
        if self.last_key.get(kind) == key:
            return False
        self.last_key[kind] = key
        return True

    def _on_log(self, event, fields):
        if event == "clock_observed":
            self.delay_ms = fields["apparent_delay_ms"]
        elif event == "hazard_decision" and not fields["initial"]:
            return
        elif event in LOCAL_EVENTS:
            kind, text = describe_local(event, fields)
            self._event("local", kind, None, text)


def snapshot(session, tap):
    c = session.c
    now = time.monotonic()
    car = session.live_car()
    decision = session.decision if car else None
    now_ms = utc_ms()
    hazards = []
    if car:
        for view in session.hazards(car):
            data = view.data
            hazards.append(dict(id=data["obstacle_id"], remaining_m=num(view.remaining_m), tta_s=num(view.tta_s),
                                route_known=view.route_known, **_hazard_fields(data, now_ms)))
    else:
        for data in session.route.values():
            if data["t_detect_utc_ms"] + data["ttl_ms"] > now_ms:
                hazards.append(dict(id=data["obstacle_id"], remaining_m=None, tta_s=None,
                                    route_known=False, **_hazard_fields(data, now_ms)))
    view_range = min(160.0, max(40.0, c["lead_distance_max_m"] * 4))
    course_ahead = drone_bearing = None
    drone = session.drone
    if car and session.course and car["track_s_m"] is not None:
        ahead = car["track_s_m"] + view_range / 2
        course_ahead = bearing(session.course.point_at(ahead), session.course.point_at(ahead + 2))
    if car and drone:
        drone_bearing = bearing((car["lat"], car["lon"]), (drone["lat"], drone["lon"]))
    paths = []
    for index in range(len(session.link.channels)):
        stamp = session.heartbeats.get(index)
        paths.append(None if stamp is None else round((now - stamp) * 1000))
    return dict(
        t_utc_ms=now_ms,
        circuit=None if not session.course else dict(
            points=[[num(x, 2), num(y, 2)] for x, y in session.course.xy],
            length_m=num(session.course.length, 2), closed=session.course.closed),
        profile=dict(speed_limit_mps=c["vehicle_speed_limit_mps"], nominal_mps=c["car_speed_nominal_mps"],
                     course_width_m=c["course_width_m"], view_range_m=view_range,
                     link_degraded_ms=round(c["link_degraded_s"] * 1000), vehicle_kind=c["vehicle_kind"]),
        car=None if not car else dict(
            speed_mps=num(car["speed_mps"]), heading=num(car["heading"], 1), track_s_m=num(car["track_s_m"], 1),
            lateral_m=num(car["lateral_m"]), manual=car["manual"], age_ms=now_ms - car["t_utc_ms"],
            telemetry={key: num(value, 1) for key, value in session.telemetry.items()}),
        decision=None if not decision else dict(
            mode=decision.mode.name, target_speed_mps=num(decision.target_speed_mps),
            lateral_offset_m=num(decision.lateral_offset_m), requested_offset_m=num(decision.requested_offset_m),
            stopping_m=num(decision.stopping_m, 2), margin_short=decision.margin_short, reason=decision.reason,
            hazard_id=decision.hazard.data["obstacle_id"] if decision.hazard else None),
        hazards=hazards,
        drone=None if not drone else dict(
            lead_distance_m=num(drone["lead_distance_m"], 1), alt=num(drone["alt"], 1), speed=num(drone["speed"], 1),
            battery_pct=num(drone["battery_pct"], 0), sensor_ok=bool(drone["sensor_ok"])),
        course_heading_ahead=num(course_ahead, 1), drone_bearing=num(drone_bearing, 1),
        link=dict(paths_age_ms=paths, degraded=session.link_age(now) >= c["link_degraded_s"],
                  synchronized=session.synchronized, epoch=session.snapshot_epoch,
                  remote_health=session.remote_health),
        rates={kind: round(tap.rate(kind), 1) for kind in ("GUIDE_HEARTBEAT", "DRONE_STATE", "TX_CAR_STATE")},
        delay_ms=tap.delay_ms,
        log=list(tap.events),
    )


def _hazard_fields(data, now_ms):
    return dict(type=data["type"], severity=data["severity"], state=data["state"], lateral_m=num(data["lateral_m"]),
                free_side=data["free_side"], size_m=num(data["size_m"], 2), confidence=num(data["confidence"], 2),
                ttl_left_ms=max(0, data["t_detect_utc_ms"] + data["ttl_ms"] - now_ms))


class DashboardServer(LoopbackServer):
    """Sentinel Legacy drive dashboard: SSE state feed, shared desktop view and command queue."""
    PROTECTED = ("/state", "/frame.jpg")
    THREAD_NAME = "drive-dashboard"

    def __init__(self, bind=DEFAULT_BIND):
        self._payload, self._version = b"{}", 0
        self.desktop_state = {}
        self.jpeg = None
        self.jpeg_frame_at = None     # VideoCanvas.frame_at (monotonic) of the cached JPEG
        self.frame_request = 0.
        self.encodes = 0
        super().__init__(bind)

    def frame_wanted(self, now, interest=3.0):
        return now - self.frame_request < interest

    def publish(self, data):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        with self._cond:
            self._payload = payload
            self._version += 1
            self._cond.notify_all()

    def _next(self, seen, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._version != seen, timeout)
            return self._closed, self._version, self._payload

    def handle_get(self, handler, path):
        if path == "/":
            page = PAGE.read_text().replace("__DASHBOARD_TOKEN__", self.token)
            handler.send(200, "text/html; charset=utf-8", page.encode())
        elif path == "/desktop":
            with self._cond:
                body = json.dumps(self.desktop_state, allow_nan=False).encode()
            handler.send(200, "application/json", body)
        elif path == "/frame.jpg":
            self.frame_request = time.monotonic()
            with self._cond:
                jpeg, frame_at = self.jpeg, self.jpeg_frame_at
            age = "" if not jpeg or frame_at is None else str(round((time.monotonic() - frame_at) * 1000))
            handler.send(200 if jpeg else 204, "image/jpeg", jpeg or b"", {"X-Frame-Age-Ms": age})
        elif path == "/state":
            handler.send(200, "application/json", self._next(None, 0)[2])
        elif path == "/events":
            self._stream(handler)
        else:
            return False
        return True

    def _stream(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        seen = None
        try:
            while True:
                closed, version, payload = self._next(seen, 10)
                if closed:
                    return
                if version == seen:
                    handler.wfile.write(b": keepalive\n\n")
                else:
                    handler.wfile.write(b"data: " + payload + b"\n\n")
                    seen = version
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def bind_from_profile(path):
    try:
        config = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return tuple(config["linker"]["dashboard"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_BIND


class Dashboard:
    """Serves the page without a session (standby → page simulates); attach() switches it to live data."""

    def __init__(self, bind=DEFAULT_BIND):
        self.server = DashboardServer(bind)
        self.session = self.tap = None
        self.next_publish = 0.0
        self.server.publish(STANDBY)
        self.bridge = None

    def update_desktop(self):
        if self.bridge is None:
            return
        for command in self.server.drain():
            try:
                self.bridge.execute(command)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                self.bridge.result = f"요청 실패 · {exc}"
        state = self.bridge.snapshot()
        with self.server._cond:
            self.server.desktop_state = state
        # Encode only while a browser tab is reading frames; one cached JPEG serves every tab.
        if not self.server.frame_wanted(time.monotonic()):
            return
        jpeg = self.bridge.frame()
        with self.server._cond:
            self.server.jpeg = jpeg
            self.server.jpeg_frame_at = getattr(self.bridge, "frame_at", None)
            self.server.encodes += 1

    @property
    def url(self):
        return self.server.url

    def attach(self, session):
        self.session, self.tap = session, SessionTap(session)
        self.next_publish = 0.0

    def detach(self):
        self.session = self.tap = None
        self.server.publish(STANDBY)

    def update(self):
        now = time.monotonic()
        if self.session and now >= self.next_publish:
            self.next_publish = now + PUBLISH_S
            self.server.publish(snapshot(self.session, self.tap))

    def close(self):
        self.server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the drive dashboard without a guide link (simulation).")
    parser.add_argument("--config", default=str(Path(__file__).resolve().with_name("config") / "stage1.json"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        dashboard = Dashboard(bind_from_profile(args.config))
    except (OSError, ValueError) as exc:
        print(f"Drive dashboard: {exc}", file=sys.stderr)
        return 1
    print(f"Drive dashboard · {dashboard.url} · 실제 연결 없음 (시뮬레이션) · Ctrl+C로 종료", flush=True)
    if not args.no_browser:
        webbrowser.open(dashboard.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        dashboard.close()


if __name__ == "__main__":
    sys.exit(main())
