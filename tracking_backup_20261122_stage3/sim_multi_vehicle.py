# sim_multi_vehicle.py
# 드론 없이 멀티 기체 GCS 를 시험하기 위한 간이 MAVLink 시뮬레이터
#
#   python sim_multi_vehicle.py                 # SYSID 1~3, 모두 UDP 14550 으로 송신 (1포트 다중 SYSID)
#   python sim_multi_vehicle.py -n 4 --ports    # SYSID 1~4, 14550/14560/14570/14580 각각 송신
#
# GCS 에서 UDP 0.0.0.0:14550 (또는 "UDP ×4") 링크를 추가하면 기체가 나타난다.
# ARM / DISARM / 모드 변경 / TAKEOFF / LAND / RTL 에 반응한다.

import argparse
import math
import time

from pymavlink import mavutil


class SimVehicle:
    def __init__(self, sysid, url, base_lat, base_lon):
        self.sysid = sysid
        self.m = mavutil.mavlink_connection(url, source_system=sysid, source_component=1)
        self.lat0 = base_lat + (sysid - 1) * 0.0004
        self.lon0 = base_lon
        self.armed = False
        self.custom_mode = 0          # STABILIZE
        self.alt = 0.0
        self.target_alt = 0.0
        self.t0 = time.time()
        self.batt = 100.0

    def mode_name(self):
        return mavutil.mode_mapping_acm.get(self.custom_mode, str(self.custom_mode))

    def step(self, dt):
        t = time.time() - self.t0
        if self.armed and self.alt < self.target_alt:
            self.alt = min(self.target_alt, self.alt + 2.0 * dt)
        if self.mode_name() in ("LAND", "RTL") and self.alt > 0:
            self.alt = max(0.0, self.alt - 1.5 * dt)
            if self.alt == 0.0:
                self.armed = False
        if self.armed:
            self.batt = max(0.0, self.batt - 0.05 * dt)

        r = 30.0 if self.alt > 1 else 0.0
        ang = t * 0.2 + self.sysid
        lat = self.lat0 + (r * math.cos(ang)) / 111320.0
        lon = self.lon0 + (r * math.sin(ang)) / (111320.0 * math.cos(math.radians(self.lat0)))
        hdg = (math.degrees(ang) + 90) % 360

        base_mode = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if self.armed:
            base_mode |= mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        mav = self.m.mav
        mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                           mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                           base_mode, self.custom_mode, mavutil.mavlink.MAV_STATE_ACTIVE)
        mav.global_position_int_send(int(t * 1000), int(lat * 1e7), int(lon * 1e7),
                                     int((50 + self.alt) * 1000), int(self.alt * 1000),
                                     0, 0, 0, int(hdg * 100))
        mav.vfr_hud_send(0, 6.0 if r else 0.0, int(hdg), 50, 50 + self.alt, 0.0)
        mav.attitude_send(int(t * 1000), math.radians(5 * math.sin(t)), math.radians(3 * math.cos(t)),
                          math.radians(hdg if hdg <= 180 else hdg - 360), 0, 0, 0)
        mav.sys_status_send(0, 0, 0, 500, int(12600 * (0.85 + 0.15 * self.batt / 100)), -1,
                            int(self.batt), 0, 0, 0, 0, 0, 0)
        mav.gps_raw_int_send(int(t * 1e6), 3, int(lat * 1e7), int(lon * 1e7), 50000, 80, 100, 0, 0, 14)

    def poll(self):
        while True:
            try:
                msg = self.m.recv_match(blocking=False)
            except OSError:
                # Windows: 첫 송신 전 recv 또는 GCS 포트 닫힘(10054)
                return
            if msg is None:
                return
            if getattr(msg, "target_system", self.sysid) not in (0, self.sysid):
                continue
            mt = msg.get_type()
            if mt == "COMMAND_LONG":
                self.on_command(msg)
            elif mt == "SET_MODE":
                self.custom_mode = msg.custom_mode
                print(f"[SYS{self.sysid}] mode -> {self.mode_name()}")

    def ack(self, cmd, result=0):
        self.m.mav.command_ack_send(cmd, result)

    def on_command(self, msg):
        c = msg.command
        L = mavutil.mavlink
        if c == L.MAV_CMD_COMPONENT_ARM_DISARM:
            self.armed = msg.param1 > 0.5
            if not self.armed:
                self.alt = self.target_alt = 0.0
            print(f"[SYS{self.sysid}] {'ARMED' if self.armed else 'DISARMED'}")
            self.ack(c)
        elif c == L.MAV_CMD_NAV_TAKEOFF:
            if not self.armed:
                self.ack(c, L.MAV_RESULT_FAILED)
                return
            self.target_alt = msg.param7
            print(f"[SYS{self.sysid}] TAKEOFF {msg.param7}m")
            self.ack(c)
        elif c == L.MAV_CMD_NAV_LAND:
            self.custom_mode = 9
            self.ack(c)
        elif c == L.MAV_CMD_NAV_RETURN_TO_LAUNCH:
            self.custom_mode = 6
            self.ack(c)
        elif c == L.MAV_CMD_DO_SET_MODE:
            self.custom_mode = int(msg.param2)
            self.ack(c)
        else:
            self.ack(c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="기체 수")
    ap.add_argument("--ports", action="store_true", help="기체마다 다른 UDP 포트 (14550 + 10*i)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--lat", type=float, default=37.5196976)
    ap.add_argument("--lon", type=float, default=126.6109531)
    a = ap.parse_args()

    sims = []
    for i in range(a.n):
        port = 14550 + (10 * i if a.ports else 0)
        url = f"udpout:{a.host}:{port}"
        sims.append(SimVehicle(i + 1, url, a.lat, a.lon))
        print(f"SYSID {i + 1} -> {url}")

    dt = 0.1
    while True:
        for s in sims:
            try:
                s.step(dt)
            except OSError:
                pass
            s.poll()
        time.sleep(dt)


if __name__ == "__main__":
    main()
