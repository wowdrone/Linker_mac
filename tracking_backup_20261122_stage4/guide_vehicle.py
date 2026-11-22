"""Linker STAGE1 vehicle session and local simulation adapter."""
import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time

from drive_mode_manager import DriveModes, Hazard
from guide_protocol import (
    CLEARED, HEALTHY, LINKER, NEW, SYNC_REQUEST, WARNING, GuideLink, JsonLog, Mode,
    checked_lat_lon, endpoint, load_settings, packets, signing_key, udp, utc_ms,
)
from obstacle_geo import Course, distance_m

TELEMETRY = ("rpm", "gear", "throttle_pct", "brake_pct", "ers_pct")

class SimVehicleInterface:
    def __init__(self, output):
        self.peer = endpoint(output)
        if self.peer[0] != "127.0.0.1":
            raise ValueError("The simulation adapter requires a loopback address")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def send(self, decision, config):
        data = {
            "mode": decision.mode.name,
            "automatic": decision.mode != Mode.MANUAL,
            "target_speed_mps": decision.target_speed_mps,
            "lateral_offset_m": decision.lateral_offset_m,
            "max_decel": config["a_max"],
            "max_lat_accel": config["a_lat_max"],
            "t_utc_ms": utc_ms(),
        }
        try:
            self.sock.sendto(json.dumps(data).encode(), self.peer)
        except OSError:
            pass

    def close(self):
        self.sock.close()


class VehicleSession:
    def __init__(self, profile):
        base, self.c = load_settings(profile)
        self.course = Course.load(base / self.c["course"]) if self.c.get("course") else None
        cfg = self.c["linker"]
        self.log = JsonLog(base / Path(cfg["log"]).expanduser())
        self.link = self.gps = self.interface = None
        try:
            self.link = GuideLink(LINKER, {**self.c, **cfg}, signing_key(base, self.c), self.log)
            self.gps = udp(cfg["gps_bind"], cfg.get("gps_peer"))
            if self.c["vehicle_kind"] == "AUTONOMOUS":
                self.interface = SimVehicleInterface(cfg["sim_output"])
            elif self.c["vehicle_kind"] != "MANNED":
                raise ValueError("Set vehicle_kind to AUTONOMOUS or MANNED")
        except BaseException:
            self.close()
            raise
        self.engine = DriveModes(self.c)
        self.started = time.monotonic()
        self.heartbeats = {}
        self.route = {}
        self.dismissed = {}
        self.acks = {}
        self.car = self.decision = self.drone = None
        self.telemetry = {}
        self.peer_session = None
        self.remote_health = False
        self.synchronized = False
        self.snapshot_epoch, self.snapshot_count = 0, 0
        self.snapshots = {}
        self.next_heartbeat = self.next_car = self.next_output = 0.0

    def update_car(self, raw):
        data = json.loads(raw)
        lat, lon = checked_lat_lon(data["lat"], data["lon"])
        stamp, speed, heading = int(data["t_utc_ms"]), float(data["speed_mps"]), float(data.get("heading", 0))
        if not -self.c["future_skew_ms"] <= utc_ms() - stamp <= self.c["gps_max_age_ms"]:
            raise ValueError("Stale GPS position")
        if not (0 <= speed <= self.c["vehicle_speed_limit_mps"] and 0 <= heading < 360):
            raise ValueError("Invalid speed or heading")
        if self.car and stamp <= self.car["t_utc_ms"]:
            return
        station = lateral = None
        if self.course:
            hint = data.get("track_s_m", self.car["track_s_m"] if self.car else None)
            if hint is not None:
                hint = float(hint)
                if not 0 <= hint <= self.course.length:
                    hint = None
            match = self.course.project(lat, lon, hint)
            if match.distance <= self.c["course_width_m"]:
                station, lateral = match.s, match.lateral
        manual = data.get("manual", False)
        if not isinstance(manual, bool):
            raise ValueError("manual must be a Boolean")
        self.car = dict(lat=lat, lon=lon, speed_mps=speed, heading=heading, track_s_m=station,
                        lateral_m=lateral if lateral is not None else 0.0, manual=manual, t_utc_ms=stamp)
        # Display-only powertrain values; decisions never read them.
        telemetry = {}
        for key in TELEMETRY:
            try:
                value = float(data[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                telemetry[key] = value
        self.telemetry = telemetry

    def live_car(self):
        return self.car if self.car and utc_ms() - self.car["t_utc_ms"] <= self.c["gps_max_age_ms"] else None

    def link_age(self, now):
        return now - max(self.heartbeats.values(), default=self.started)

    def _decision(self, now):
        car = self.live_car()
        if not car:
            self.decision = None
            return None
        self.decision = self.engine.update(now, car, self.hazards(car), self.link_age(now),
                                           self.synchronized, self.remote_health and (
            self.drone is None or (self.drone["sensor_ok"] == 1
            and (self.drone["battery_pct"] < 0 or self.drone["battery_pct"] >= self.c["battery_warning_pct"]))
        ))
        return self.decision

    def hazards(self, car):
        now_ms = utc_ms()
        views = []
        for ident, data in tuple(self.route.items()):
            expiry = data["t_detect_utc_ms"] + data["ttl_ms"]
            if now_ms >= expiry:
                self.route.pop(ident)
                self.log("expired", obstacle_id=ident)
                continue
            if ident in self.dismissed:
                continue
            station = car["track_s_m"]
            known = station is not None and self.course is not None and 0 <= data["track_s_m"] <= self.course.length
            if known:
                remaining = self.course.delta(data["track_s_m"], station)
                if remaining < -self.c["pass_margin_m"]:
                    self.dismissed[ident] = expiry
                    self.log("passed", obstacle_id=ident)
                    continue
                remaining = max(0.0, remaining)
            else:
                remaining = distance_m(data["lat"], data["lon"], car["lat"], car["lon"])
            speed = car["speed_mps"]
            tta = remaining / speed if speed >= self.c["minimum_tta_speed_mps"] and speed > 0 else None
            views.append(Hazard(data, remaining, tta, known))
        return sorted(views, key=lambda view: (-view.data["severity"], view.remaining_m))

    def _sync(self):
        if self.synchronized or not self.snapshot_epoch:
            return
        snapshot = self.snapshots.get(self.snapshot_epoch, {})
        if len(snapshot) != self.snapshot_count:
            return
        now = utc_ms()
        self.route = {key: data for key, data in snapshot.items()
                      if data["t_detect_utc_ms"] + data["ttl_ms"] > now}
        self.snapshots.clear()
        self.synchronized = True
        self.log("snapshot_synced", count=len(self.route), epoch=self.snapshot_epoch)

    def _ack(self, msg_id, mode, accepted=True, reject=0):
        fields = dict(ack_msg_id=msg_id, accepted=int(accepted), applied_mode=int(mode), reject_reason=reject)
        self.acks[msg_id] = fields
        self.link.send(self.link.make("MODE_ACK", **fields))
        self.log("ack_sent", msg_id=msg_id, applied_mode=mode.name, accepted=accepted)

    def _heartbeat(self, path, packet, now):
        data = packet.data
        if data["session_id"] != self.peer_session:
            self.peer_session = data["session_id"]
            self.heartbeats.clear()
            self.snapshots.clear()
            self.dismissed.clear()
            self.acks.clear()
            self.snapshot_epoch = 0
            self.synchronized = False
        self.heartbeats[path] = now
        if packet.duplicate:
            return
        self.remote_health = bool(data["health"] & HEALTHY)
        if data["snapshot_epoch"]:
            self.snapshot_epoch, self.snapshot_count = data["snapshot_epoch"], data["snapshot_count"]
            self._sync()

    def receive(self, path, packet, now):
        kind, data = packet.kind, packet.data
        if kind == "GUIDE_HEARTBEAT":
            self._heartbeat(path, packet, now)
            return
        log_id = data["msg_id"]
        if packet.duplicate:
            if log_id in self.acks:
                self.link.send(self.link.make("MODE_ACK", **self.acks[log_id]))
            return
        self.log("received", kind=kind, msg_id=log_id)
        if kind == "DRONE_STATE":
            self.drone = data
        elif kind == "HAZARD_CLEAR":
            ident = data["obstacle_id"]
            self.route.pop(ident, None)
            self.dismissed.pop(ident, None)
            for snapshot in self.snapshots.values():
                snapshot.pop(ident, None)
        elif kind == "OBSTACLE_ALERT":
            ident = data["obstacle_id"]
            if data["state"] == CLEARED:
                self.route.pop(ident, None)
                self.dismissed.pop(ident, None)
            elif data["t_detect_utc_ms"] + data["ttl_ms"] > utc_ms():
                if ident in self.dismissed:
                    self.dismissed[ident] = data["t_detect_utc_ms"] + data["ttl_ms"]
                self.route[ident] = data
                epoch = data["snapshot_epoch"]
                if epoch and not self.synchronized:
                    self.snapshots.setdefault(epoch, {})[ident] = data
                    self._sync()
            result = self._decision(now)
            if data["severity"] >= WARNING:
                mode = result.mode if result else Mode.LINK_DEGRADED
                self._ack(log_id, mode, accepted=result is not None, reject=int(result is None))
            if result and not data["snapshot_epoch"]:
                self.log("hazard_decision", msg_id=log_id, mode=result.mode.name,
                         detect_to_decision_ms=utc_ms() - data["t_detect_utc_ms"],
                         initial=data["state"] == NEW,
                         reason=result.reason, margin_short=result.margin_short)
        elif kind == "DRIVE_ADVICE":
            # 차량 판단에서는 독립적인 위치·코스·제동 조건으로 경보를 다시 평가한다.
            result = self._decision(now)
            mode = result.mode if result else Mode.LINK_DEGRADED
            self._ack(log_id, mode, accepted=result is not None, reject=int(result is None))

    def poll(self):
        now = time.monotonic()
        if self.synchronized and self.link_age(now) >= self.c["link_degraded_s"]:
            self.synchronized = False
            self.snapshot_epoch = 0
            self.snapshots.clear()
            self.log("link_degraded")
        for raw in packets(self.gps):
            try:
                self.update_car(raw)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                self.log("gps_rejected", reason=str(exc))
        for path, packet in self.link.receive():
            self.receive(path, packet, now)

        car = self.live_car()
        if now >= self.next_heartbeat:
            self.next_heartbeat = now + self.c["heartbeat_s"]
            health = (HEALTHY if car else 0) | (0 if self.synchronized else SYNC_REQUEST)
            quality = sum(now - stamp < self.c["link_degraded_s"] for stamp in self.heartbeats.values()) / 2
            mode = self.decision.mode if self.decision else Mode.LINK_DEGRADED
            self.link.send(self.link.make("GUIDE_HEARTBEAT", node_id=LINKER, t_utc_ms=utc_ms(),
                                          mode=int(mode), link_quality=quality, health=health,
                                          snapshot_epoch=0, snapshot_count=0))
        if car and now >= self.next_car:
            self.next_car = now + self.c["car_state_s"]
            mode = self.decision.mode if self.decision else Mode.LINK_DEGRADED
            self.link.send(self.link.make("CAR_STATE", lat=car["lat"], lon=car["lon"],
                speed_mps=car["speed_mps"], heading=car["heading"],
                track_s_m=-1 if car["track_s_m"] is None else car["track_s_m"],
                lateral_m=car["lateral_m"], drive_mode=int(mode)))
        if now >= self.next_output:
            self.next_output = now + self.c["car_state_s"]
            result = self._decision(now)
            if result:
                if self.interface:
                    self.interface.send(result, self.c)
                hazard = result.hazard
                self.log("decision", mode=result.mode.name, speed_mps=car["speed_mps"],
                         target_speed_mps=result.target_speed_mps, lateral_offset_m=result.lateral_offset_m,
                         remaining_m=hazard.remaining_m if hazard else None,
                         tta_s=hazard.tta_s if hazard else None, stopping_m=result.stopping_m,
                         reason=result.reason, margin_short=result.margin_short)
            for ident, expiry in tuple(self.dismissed.items()):
                if expiry <= utc_ms():
                    self.dismissed.pop(ident)
            while len(self.acks) > 4096:
                self.acks.pop(next(iter(self.acks)))

    def close(self):
        for resource in (self.interface, self.gps, self.link, self.log):
            if resource is not None:
                resource.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--dashboard", action="store_true", help="serve the drive dashboard on loopback")
    args = parser.parse_args(argv)
    session = dashboard = None
    try:
        session = VehicleSession(args.config)
        print("Linker guide receiver started · simulation output", flush=True)
        if args.dashboard:
            from drive_dashboard import Dashboard, bind_from_profile
            dashboard = Dashboard(bind_from_profile(args.config))
            dashboard.attach(session)
            print(f"Drive dashboard · {dashboard.url}", flush=True)
        while True:
            session.poll()
            if dashboard:
                dashboard.update()
            time.sleep(.005)
    except KeyboardInterrupt:
        return 0
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(f"Linker guide: {exc}", file=sys.stderr)
        return 1
    finally:
        if dashboard:
            dashboard.close()
        if session:
            session.close()


if __name__ == "__main__":
    sys.exit(main())
