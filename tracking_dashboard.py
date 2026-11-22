"""Linker Tracking / Telemetry browser dashboard (loopback only).

The HTTP threads only read cached bytes published by the Qt thread and append validated
commands to a bounded queue. They never touch Qt objects, the tracker or a vehicle.
There is no generic MAVLink API: the only vehicle-affecting request is a flight mode
change from the firmware's offered list, executed after an explicit confirmation ticket
and re-validated on the Qt thread against the currently selected vehicle.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

from dashboard_server import LoopbackServer

PAGE = Path(__file__).resolve().with_name("tracking_dashboard.html")
DEFAULT_BIND = ("127.0.0.1", 8766)
FRAME_INTEREST_S = 3.0

SIMPLE = {"begin_selection", "cancel_selection", "stop", "intervention", "simulate", "revoke_simulation",
          "video_connect", "video_disconnect", "demo", "mode_cancel"}
HEX = re.compile(r"^[0-9a-f]{1,64}$")
MODE = re.compile(r"^[A-Z][A-Z_]{1,19}$")
VEHICLE = re.compile(r"^\d{1,6}_\d{1,3}$")
TICKET = re.compile(r"^[0-9a-f]{32}$")


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("integer out of range")
    return value


def _text(value, pattern):
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError("invalid text field")
    return value


def validate(command):
    """Return a normalised command or raise ValueError. Unknown keys are rejected."""
    if not isinstance(command, dict) or not isinstance(command.get("action"), str):
        raise ValueError("expected an object with an action")
    action = command["action"]

    def keys(*allowed):
        if set(command) != {"action", *allowed}:
            raise ValueError("unexpected fields")
    if action in SIMPLE:
        keys()
        return dict(action=action)
    if action == "select_roi":
        keys("ref", "roi")
        ref, roi = command["ref"], command["roi"]
        if not isinstance(ref, dict) or set(ref) != {"source_id", "session_id", "frame_seq", "width", "height"}:
            raise ValueError("invalid frame reference")
        ref = (_text(ref["source_id"], HEX), _text(ref["session_id"], HEX),
               _int(ref["frame_seq"], 0, 2 ** 53), _int(ref["width"], 1, 16384), _int(ref["height"], 1, 16384))
        if (not isinstance(roi, list) or len(roi) != 4
                or not all(type(v) in (int, float) and math.isfinite(v) for v in roi)
                or roi[0] < 0 or roi[1] < 0 or roi[2] <= 0 or roi[3] <= 0
                or roi[0] + roi[2] > ref[3] or roi[1] + roi[3] > ref[4]):
            raise ValueError("invalid roi")
        return dict(action=action, ref=ref, roi=tuple(float(v) for v in roi))
    if action == "mode_prepare":
        keys("mode", "vehicle")
        return dict(action=action, mode=_text(command["mode"], MODE), vehicle=_text(command["vehicle"], VEHICLE))
    if action == "mode_commit":
        keys("ticket", "vehicle")
        return dict(action=action, ticket=_text(command["ticket"], TICKET),
                    vehicle=_text(command["vehicle"], VEHICLE))
    raise ValueError("unsupported action")


class TrackingDashboard(LoopbackServer):
    PROTECTED = ("/state", "/frame.jpg")
    THREAD_NAME = "tracking-dashboard"

    def __init__(self, bind=DEFAULT_BIND):
        self._state = b'{"standby":true}'
        self._jpeg, self._meta, self._received = None, None, None
        self._frame_request = 0.
        self.frames_published = 0   # JPEG payloads handed over by the Qt thread (one per encode)
        super().__init__(bind)

    def validate_command(self, command):
        return validate(command)

    def publish_state(self, data):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        with self._cond:
            self._state = payload

    def publish_frame(self, jpeg, meta):
        header = None if meta is None else json.dumps(meta, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        with self._cond:
            self._jpeg, self._meta = jpeg, header
            self._received = None if meta is None else meta.get("received")
            if jpeg is not None:
                self.frames_published += 1

    def frame_wanted(self, now):
        return now - self._frame_request < FRAME_INTEREST_S

    def handle_get(self, handler, path):
        if path == "/":
            page = PAGE.read_text(encoding="utf-8").replace("__DASHBOARD_TOKEN__", self.token)
            handler.send(200, "text/html; charset=utf-8", page.encode())
        elif path == "/state":
            with self._cond:
                body = self._state
            handler.send(200, "application/json", body)
        elif path == "/frame.jpg":
            self._frame_request = time.monotonic()
            with self._cond:
                jpeg, meta, received = self._jpeg, self._meta, self._received
            if jpeg is None:
                handler.send(204, "image/jpeg", b"")
            else:
                # Age on the app's monotonic clock at serve time (decoder receipt → this response).
                age = "" if received is None else str(round((time.monotonic() - received) * 1000))
                handler.send(200, "image/jpeg", jpeg, {"X-Frame-Meta": meta, "X-Frame-Age-Ms": age})
        else:
            return False
        return True


def main(argv=None):
    """Standalone: Tracking dashboard with the shared service but no Fleet / vehicle."""
    parser = argparse.ArgumentParser(description="Linker Tracking / Telemetry dashboard (no vehicle link).")
    parser.add_argument("--demo", action="store_true", help="start the synthetic SIMULATION source")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--legacy", action="store_true", help="open the Sentinel Legacy drive dashboard instead")
    args, rest = parser.parse_known_args(argv)
    if args.legacy:
        from drive_dashboard import main as legacy_main
        return legacy_main(rest + (["--no-browser"] if args.no_browser else []))
    import signal
    from PyQt5.QtCore import QCoreApplication, QTimer
    from tracking_service import TrackingService
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    service = TrackingService()
    url = service.open_dashboard()
    if args.demo:
        service.start_demo()
    print(f"Linker Tracking dashboard · {url} · 기체 연결 없음 · MONITOR ONLY · Ctrl+C로 종료", flush=True)
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keep = QTimer()
    keep.timeout.connect(lambda: None)   # let Python handle SIGINT
    keep.start(200)
    try:
        return app.exec_()
    finally:
        service.shutdown()


if __name__ == "__main__":
    sys.exit(main())
