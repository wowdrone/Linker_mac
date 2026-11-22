"""STAGE1 local simulated vehicle. Receives only the Linker simulation output."""
import argparse
import json
import math
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drive_mode_manager import approach
from guide_protocol import Mode, endpoint, load_settings, packets, udp, utc_ms
from obstacle_geo import Course, from_xy, local_xy


class SimCar:
    def __init__(self, config, course, start, speed):
        self.c, self.course = config, course
        self.station, self.speed = start, speed
        self.lateral = self.lateral_speed = 0.0
        self.mode = Mode.NORMAL.name
        self.manual = False
        self.target = None
        self.target_at = None
        self.accel = 0.0

    def accept(self, raw, now):
        data = json.loads(raw)
        Mode[data["mode"]]
        if not 0 <= utc_ms() - int(data["t_utc_ms"]) <= self.c["gps_max_age_ms"]:
            return
        if not 0 <= data["target_speed_mps"] <= self.c["vehicle_speed_limit_mps"]:
            return
        if abs(data["lateral_offset_m"]) > self.c["course_width_m"] / 2:
            return
        self.target, self.target_at = data, now

    def advance(self, now, dt):
        cfg = self.c
        if self.manual:
            self.mode = Mode.MANUAL.name
        elif self.target and self.target["automatic"]:
            stale = now - self.target_at >= cfg["link_slow_s"]
            speed = min(self.speed, cfg["safe_speed_mps"]) if stale else self.target["target_speed_mps"]
            previous = self.speed
            self.speed = approach(self.speed, speed, cfg["a_accel"] * dt, cfg["a_max"] * dt)
            self.accel = (self.speed - previous) / dt if dt > 0 else 0.0
            delta = self.target["lateral_offset_m"] - self.lateral
            desired = math.copysign(min(cfg["offset_rate_mps"], math.sqrt(2 * cfg["a_lat_max"] * abs(delta))), delta)
            self.lateral_speed = approach(self.lateral_speed, desired, cfg["a_lat_max"] * dt, cfg["a_lat_max"] * dt)
            self.lateral += self.lateral_speed * dt
            self.mode = Mode.LINK_DEGRADED.name if stale else self.target["mode"]
        station = self.station + self.speed * dt
        self.station = station % self.course.length if self.course.closed else min(station, self.course.length)

    def gps(self):
        course = self.course
        origin = course.point_at(self.station)
        ahead_s = self.station + .2
        if not course.closed and ahead_s > course.length:
            east, north = local_xy(*course.point_at(self.station - .2), origin)
            east, north = -east, -north
        else:
            east, north = local_xy(*course.point_at(ahead_s), origin)
        heading = math.atan2(east, north)
        lat, lon = from_xy(math.cos(heading) * self.lateral, -math.sin(heading) * self.lateral, origin)
        return dict(lat=lat, lon=lon, speed_mps=self.speed, heading=math.degrees(heading) % 360,
                    track_s_m=self.station, manual=self.manual, t_utc_ms=utc_ms(), **self.powertrain())

    def powertrain(self):
        # Toy 8-speed model scaled to the profile speed limit; for dashboard display only.
        top = self.c["vehicle_speed_limit_mps"]
        fraction = min(1.0, self.speed / top) if top > 0 else 0.0
        gear = min(8, int(fraction * 8) + 1) if self.speed > 0.3 else 0
        in_gear = fraction * 8 - (gear - 1) if gear else 0.0
        rpm = 4000 + in_gear * 9500 if gear else 4000
        throttle = min(100.0, max(0.0, 25 + self.accel / self.c["a_accel"] * 75)) if self.accel >= 0 else 0.0
        brake = min(100.0, -self.accel / self.c["a_max"] * 100) if self.accel < 0 else 0.0
        return dict(rpm=round(rpm), gear=gear, throttle_pct=round(throttle), brake_pct=round(brake))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/stage1.json")
    parser.add_argument("--start", type=float, default=25.0)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--manual-after", type=float)
    args = parser.parse_args()
    base, config = load_settings(args.config)
    cfg = config["linker"]
    if any(cfg[name][0] != "127.0.0.1" for name in ("sim_output", "gps_bind")):
        parser.error("Simulation only accepts loopback endpoints")
    sim = SimCar(config, Course.load(base / config["course"]), args.start, config["car_speed_nominal_mps"])
    start = previous = next_gps = time.monotonic()
    with udp(cfg["sim_output"]) as control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as gps:
        while (now := time.monotonic()) - start < args.seconds:
            for data in packets(control):
                sim.accept(data, now)
            if args.manual_after is not None and now - start >= args.manual_after:
                sim.manual = True
            sim.advance(now, min(.1, now - previous))
            previous = now
            if now >= next_gps:
                gps.sendto(json.dumps(sim.gps()).encode(), endpoint(cfg["gps_bind"]))
                next_gps = now + config["car_state_s"]
            time.sleep(.01)


if __name__ == "__main__":
    main()
