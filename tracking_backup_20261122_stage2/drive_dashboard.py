"""Loopback browser dashboard for a Linker VehicleSession (HTTP + Server-Sent Events).

Guide session state is read-only. Desktop requests use a queue consumed by Qt.
"""
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
from pathlib import Path
import sys
import threading
import time
import webbrowser

from guide_protocol import KINDS, Mode, endpoint, utc_ms
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


class DashboardServer:
    def __init__(self, bind=DEFAULT_BIND):
        host, port = endpoint(bind)
        if host != "127.0.0.1":
            raise ValueError("The dashboard only serves on loopback")
        self._cond = threading.Condition()
        self._payload, self._version, self._closed = b"{}", 0, False
        self.token = secrets.token_urlsafe(32)
        self.commands = deque()
        self.desktop_state = {}
        self.jpeg = None
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="drive-dashboard", daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

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

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, kind, body):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                # Reject foreign Host headers so other sites cannot read this feed via DNS rebinding.
                if self.headers.get("Host") not in (f"127.0.0.1:{server.port}", f"localhost:{server.port}"):
                    self._send(403, "text/plain; charset=utf-8", b"forbidden")
                    return
                if (self.path in ("/state", "/frame.jpg") and
                        self.headers.get("X-Dashboard-Token") != server.token):
                    self._send(403, "text/plain; charset=utf-8", b"forbidden")
                    return
                if self.path == "/":
                    page = PAGE.read_text().replace("__DASHBOARD_TOKEN__", server.token)
                    self._send(200, "text/html; charset=utf-8", page.encode())
                elif self.path == "/desktop":
                    with server._cond:
                        body = json.dumps(server.desktop_state, allow_nan=False).encode()
                    self._send(200, "application/json", body)
                elif self.path == "/frame.jpg":
                    with server._cond:
                        jpeg = server.jpeg
                    self._send(200 if jpeg else 204, "image/jpeg", jpeg or b"")
                elif self.path == "/state":
                    self._send(200, "application/json", server._next(None, 0)[2])
                elif self.path == "/events":
                    self._stream()
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")

            def do_POST(self):
                host = self.headers.get("Host")
                if (host not in (f"127.0.0.1:{server.port}", f"localhost:{server.port}") or
                        self.headers.get("Origin") != f"http://{host}" or
                        self.headers.get("X-Dashboard-Token") != server.token):
                    self._send(403, "text/plain", b"forbidden")
                    return
                if self.path != "/command":
                    self._send(404, "text/plain", b"not found")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        raise ValueError("Invalid request size")
                    command = json.loads(self.rfile.read(size))
                    if not isinstance(command, dict):
                        raise ValueError("Expected an object")
                except (ValueError, TypeError):
                    self._send(400, "text/plain", b"invalid request")
                    return
                with server._cond:
                    if len(server.commands) >= 16:
                        self._send(429, "text/plain", b"queue full")
                        return
                    server.commands.append(command)
                self._send(202, "application/json", b'{"queued":true}')

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = None
                try:
                    while True:
                        closed, version, payload = server._next(seen, 10)
                        if closed:
                            return
                        if version == seen:
                            self.wfile.write(b": keepalive\n\n")
                        else:
                            self.wfile.write(b"data: " + payload + b"\n\n")
                            seen = version
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        return Handler


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
        with self.server._cond:
            commands = list(self.server.commands)
            self.server.commands.clear()
        for command in commands:
            try:
                self.bridge.execute(command)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                self.bridge.result = f"요청 실패 · {exc}"
        state = self.bridge.snapshot()
        jpeg = self.bridge.frame()
        with self.server._cond:
            self.server.desktop_state = state
            self.server.jpeg = jpeg

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
