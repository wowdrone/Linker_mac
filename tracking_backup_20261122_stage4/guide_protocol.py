"""Signed MAVLink 2 transport shared by the drone and vehicle."""
from collections import OrderedDict
from dataclasses import dataclass
from enum import IntEnum
import json
import math
import os
from pathlib import Path
import secrets
import socket
import time

from wow_guide_mavlink import MAVLink

MASK = 2**32 - 1
VERSION = 2
HEALTHY, SYNC_REQUEST = 1, 2
CAMERA_FRESH, POSE_FRESH, GPS_3D, CLOCK_OK, COURSE_LOADED = (1 << n for n in range(2, 7))
SENSOR_HEALTH_MASK = CAMERA_FRESH | POSE_FRESH | GPS_3D | CLOCK_OK | COURSE_LOADED
KINDS = ("UNKNOWN", "DEBRIS", "VEHICLE", "PERSON", "ANIMAL", "SURFACE_HAZARD")
LEFT, RIGHT, BOTH, BLOCKED = 1, 2, 3, 4
INFO, CAUTION, WARNING, CRITICAL = range(4)
NEW, UPDATE, CLEARED = range(3)
SENTINEL, LINKER = 1, 2
INCOMING = {
    SENTINEL: {"GUIDE_HEARTBEAT", "CAR_STATE", "MODE_ACK"},
    LINKER: {"GUIDE_HEARTBEAT", "DRONE_STATE", "OBSTACLE_ALERT", "HAZARD_CLEAR", "DRIVE_ADVICE"},
}


class Mode(IntEnum):
    NORMAL = 0
    CAUTION = 1
    AVOID = 2
    STOP = 3
    MANUAL = 4
    LINK_DEGRADED = 5


def utc_ms():
    return time.time_ns() // 1_000_000


def checked_lat_lon(lat, lon):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid WGS84 coordinates")
    return lat, lon


def newer(candidate, current):
    return 0 < ((candidate - current) & MASK) < 2**31


def load_settings(path):
    path = Path(path).expanduser().resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("stage") != "STAGE1":
        raise ValueError("Select a configured STAGE1 profile")
    for key in (
        "a_max", "a_lat_max", "a_accel", "offset_rate_mps", "course_width_m",
        "vehicle_speed_limit_mps", "drone_speed_limit_mps", "drone_alt_agl_m", "heartbeat_s",
        "car_state_s", "drone_state_s", "retry_s", "alert_update_s", "hazard_ttl_ms",
        "gps_max_age_ms", "max_age_ms", "pose_max_age_ms", "unseen_clear_s",
    ):
        if not math.isfinite(config[key]) or config[key] <= 0:
            raise ValueError(f"{key} must be positive and finite")
    if not 0 < config["link_degraded_s"] < config["link_slow_s"] < config["link_stop_s"]:
        raise ValueError("Invalid vehicle link timeouts")
    if not 0 < config["lead_distance_min_m"] <= config["lead_distance_m"] <= config["lead_distance_max_m"]:
        raise ValueError("Invalid lead-distance profile")
    if (config["drone_escape_side"] not in ("LEFT", "RIGHT")
            or not config["drone_escape_offset_m"] > config["course_width_m"] / 2
            or not config["drone_escape_alt_m"] >= config["drone_alt_agl_m"]):
        raise ValueError("Escape profile must reach above and outside the course")
    if not 0 <= config["vehicle_half_width_m"] < config["course_width_m"] / 2:
        raise ValueError("Vehicle profile exceeds the course width")
    if not 0 <= config["safe_speed_mps"] <= config["car_speed_nominal_mps"] <= config["vehicle_speed_limit_mps"]:
        raise ValueError("Invalid nominal and safe vehicle speeds")
    if not 0 < config["minimum_tta_speed_mps"] <= config["vehicle_speed_limit_mps"]:
        raise ValueError("Invalid minimum TTA speed")
    return path.parent, config


def signing_key(base, config):
    raw_path = os.environ.get("WOW_GUIDE_SIGNING_KEY_FILE") or config["signing_key_file"]
    key = bytes.fromhex((base / Path(raw_path).expanduser()).read_text(encoding="ascii").strip())
    if len(key) != 32:
        raise ValueError("MAVLink signing key must be 32 bytes in hexadecimal")
    return key


def endpoint(values):
    return str(values[0]), int(values[1])


def udp(bind, peer=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(endpoint(bind))
        if peer:
            sock.connect(endpoint(peer))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def packets(sock, size=8192, limit=64):
    for _ in range(limit):
        try:
            yield sock.recv(size)
        except OSError:
            return


class JsonLog:
    def __init__(self, path=None):
        self.file = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = path.open("a", encoding="utf-8", buffering=1)

    def __call__(self, event, **fields):
        if self.file:
            self.file.write(json.dumps(dict(event=event, utc_ms=utc_ms(), **fields),
                                       ensure_ascii=False, allow_nan=False) + "\n")

    def close(self):
        if self.file:
            self.file.close()


def validate(kind, data, now, profile):
    if kind in {"CAR_STATE", "DRONE_STATE", "OBSTACLE_ALERT"}:
        checked_lat_lon(data["lat"], data["lon"])
        if not math.isfinite(data["track_s_m"]) or not -1 <= data["track_s_m"] <= profile["station_limit_m"]:
            raise ValueError("Invalid course station")
    if kind == "CAR_STATE":
        Mode(data["drive_mode"])
        if not (0 <= data["speed_mps"] <= profile["vehicle_speed_limit_mps"]
                and 0 <= data["heading"] < 360 and math.isfinite(data["lateral_m"])):
            raise ValueError("Invalid vehicle state")
    elif kind == "DRONE_STATE":
        if not (0 <= data["alt"] <= profile["alt_limit_m"] and 0 <= data["speed"] <= profile["drone_speed_limit_mps"]
                and -1 <= data["battery_pct"] <= 100 and data["sensor_ok"] in (0, 1)
                and math.isfinite(data["lead_distance_m"])):
            raise ValueError("Invalid drone state")
    elif kind == "OBSTACLE_ALERT":
        if not (data["obstacle_id"] > 0 and 0 <= data["type"] < len(KINDS)
                and 0 <= data["confidence"] <= 1 and 0 <= data["pos_err_m"] <= profile["position_limit_m"]
                and -1 <= data["size_m"] <= profile["position_limit_m"] and math.isfinite(data["lateral_m"])
                and data["free_side"] in range(5) and data["severity"] in range(4)
                and data["state"] in range(3) and 0 < data["ttl_ms"] <= profile["hazard_ttl_ms"]
                and 0 < data["t_detect_utc_ms"] <= data["t_send_utc_ms"]
                and now <= data["t_detect_utc_ms"] + data["ttl_ms"] + profile["future_skew_ms"]):
            raise ValueError("Invalid or expired advisory")
    elif kind == "HAZARD_CLEAR":
        if not 0 < data["t_utc_ms"] <= data["t_send_utc_ms"] or data["obstacle_id"] <= 0:
            raise ValueError("Invalid hazard clear")
    elif kind == "DRIVE_ADVICE":
        Mode(data["advice_mode"])
        if not (0 <= data["target_speed_mps"] <= profile["vehicle_speed_limit_mps"]
                and abs(data["lateral_offset_m"]) <= profile["course_width_m"] / 2
                and now <= data["valid_until_ms"] <= now + profile["hazard_ttl_ms"]
                and data["reason_obstacle_id"] > 0):
            raise ValueError("Invalid driving advice")
    elif kind == "GUIDE_HEARTBEAT":
        Mode(data["mode"])
        if not (0 <= data["link_quality"] <= 1 and not data["health"] & ~(HEALTHY | SYNC_REQUEST | SENSOR_HEALTH_MASK)
                and 0 < data["t_utc_ms"] <= data["t_send_utc_ms"]
                and data["snapshot_count"] <= profile["max_hazards"]):
            raise ValueError("Invalid heartbeat")
    elif kind == "MODE_ACK":
        Mode(data["applied_mode"])
        if data["accepted"] not in (0, 1) or not data["ack_msg_id"]:
            raise ValueError("Invalid mode acknowledgment")


@dataclass(frozen=True)
class Packet:
    kind: str
    data: dict
    duplicate: bool = False


class GuideLink:
    def __init__(self, node, config, key, log):
        self.node, self.config, self.log = node, config, log
        self.peer = LINKER if node == SENTINEL else SENTINEL
        self.session = secrets.randbelow(MASK) + 1
        self.remote_session = None
        self.retired_sessions = OrderedDict()
        self.last_peer_stamp = 0
        self.msg_id = secrets.randbelow(MASK) + 1
        self.streams, self.received, self.seen = {}, OrderedDict(), OrderedDict()
        self.channels = []
        try:
            for index, path in enumerate(config["paths"]):
                sock = udp(path["bind"], path["peer"])
                mav = MAVLink(None, srcSystem=node, srcComponent=190)
                mav.signing.secret_key = key
                mav.signing.sign_outgoing = True
                mav.signing.link_id = node * 4 + index
                mav.signing.timestamp = int((time.time() - 1420070400) * 100_000)
                self.channels.append((sock, mav))
            if len(self.channels) != 2:
                raise ValueError("Configure exactly two guide paths")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _stream(kind, data):
        if kind in ("OBSTACLE_ALERT", "HAZARD_CLEAR"):
            return "HAZARD", data["obstacle_id"]
        return kind

    def make(self, kind, **data):
        stream = self._stream(kind, data)
        seq = self.streams.get(stream, -1)
        seq = (seq + 1) & MASK
        self._remember(self.streams, stream, seq)
        self.msg_id = self.msg_id % MASK + 1
        return Packet(kind, dict(proto_ver=VERSION, msg_id=self.msg_id, seq=seq,
                                session_id=self.session, t_send_utc_ms=utc_ms(), **data))

    def send(self, packet):
        stamp = utc_ms()
        for sock, mav in self.channels:
            fields = {**packet.data, "t_send_utc_ms": stamp}
            encoder = getattr(mav, packet.kind.lower() + "_encode")
            frame = encoder(**fields).pack(mav)
            mav.seq = (mav.seq + 1) & 255
            try:
                sock.send(frame)
            except OSError as exc:
                self.log("send_error", kind=packet.kind, reason=str(exc))

    def _decode(self, raw, mav):
        if len(raw) < 25 or raw[0] != 0xFD or raw[2] != 1 or len(raw) != 25 + raw[1]:
            raise ValueError("Expected a single signed MAVLink 2 frame")
        try:
            message = mav.decode(bytearray(raw))
        except Exception as exc:
            raise ValueError("Invalid MAVLink signature, counter or checksum") from exc
        if not message.get_signed() or message.get_srcSystem() != self.peer:
            raise ValueError("Invalid peer identity")
        kind = message.get_type()
        if kind not in INCOMING[self.node]:
            raise ValueError("Unexpected message direction")
        data = {name: getattr(message, name) for name in message.fieldnames}
        now = utc_ms()
        stamp = data["t_send_utc_ms"]
        if (data["proto_ver"] != VERSION or not data["msg_id"] or not data["session_id"]
                or not -self.config["future_skew_ms"] <= now - stamp < self.config["max_age_ms"]):
            raise ValueError("Invalid protocol version, ID or send time")
        validate(kind, data, now, self.config)
        if kind == "GUIDE_HEARTBEAT" and data["node_id"] != self.peer:
            raise ValueError("Invalid heartbeat node")
        session = data["session_id"]
        if session != self.remote_session:
            if kind != "GUIDE_HEARTBEAT" or session in self.retired_sessions or stamp < self.last_peer_stamp:
                raise ValueError("Unknown or retired peer session")
            if self.remote_session is not None:
                self._remember(self.retired_sessions, self.remote_session, stamp)
            self.remote_session = session
            self.received.clear()
            self.seen.clear()
        self.last_peer_stamp = max(stamp, self.last_peer_stamp)
        key = data["msg_id"]
        if key in self.seen:
            if self.seen[key] != (kind, data["seq"]):
                raise ValueError("Conflicting duplicate ID")
            return Packet(kind, data, True)
        stream = self._stream(kind, data)
        previous = self.received.get(stream)
        if previous is not None and not newer(data["seq"], previous):
            raise ValueError("Out-of-order guide sequence")
        self._remember(self.received, stream, data["seq"])
        self._remember(self.seen, key, (kind, data["seq"]))
        if kind == "GUIDE_HEARTBEAT":
            self.log("clock_observed", apparent_delay_ms=now - data["t_utc_ms"])
        return Packet(kind, data)

    @staticmethod
    def _remember(cache, key, value):
        cache[key] = value
        if isinstance(cache, OrderedDict):
            cache.move_to_end(key)
            if len(cache) > 4096:
                cache.popitem(last=False)
        elif len(cache) > 4096:
            cache.pop(next(iter(cache)))

    def receive(self):
        for index, (sock, mav) in enumerate(self.channels):
            for raw in packets(sock, 281):
                try:
                    yield index, self._decode(raw, mav)
                except ValueError as exc:
                    self.log("rejected", path=index, reason=str(exc))

    def close(self):
        for sock, _ in self.channels:
            sock.close()
