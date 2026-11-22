"""Vehicle-side decision only. The current output adapter is simulated."""
from dataclasses import dataclass
import math

from guide_protocol import BLOCKED, BOTH, CAUTION, CRITICAL, INFO, LEFT, RIGHT, Mode


@dataclass(frozen=True)
class Hazard:
    data: dict
    remaining_m: float | None
    tta_s: float | None
    route_known: bool = True


@dataclass(frozen=True)
class Decision:
    mode: Mode
    target_speed_mps: float
    lateral_offset_m: float
    stopping_m: float
    margin_short: bool
    reason: str
    hazard: Hazard | None
    requested_offset_m: float = 0.0


def stopping_distance(speed, config):
    return speed**2 / (2 * config["a_max"]) + speed * config["t_latency"] + config["margin"]


def avoidance_distance(speed, offset, config):
    return speed * math.sqrt(4 * abs(offset) / config["a_lat_max"])


def approach(previous, target, rise, fall):
    return min(previous + rise, max(previous - fall, target))


class DriveModes:
    def __init__(self, config):
        self.c = config
        self.output = None
        self.last_time = None
        self.release_time = None

    def _hazard_choice(self, hazard, car, stopping_m):
        cfg = self.c
        data, remaining = hazard.data, hazard.remaining_m
        if data["severity"] == INFO:
            return None
        if remaining is None:
            return Mode.CAUTION, 0.0
        if data["severity"] == CAUTION:
            if remaining < cfg["caution_distance_multiplier"] * stopping_m:
                return Mode.CAUTION, 0.0
            return None
        offset = cfg["avoid_offset_m"]
        corridor = cfg["course_width_m"] / 2 - cfg["vehicle_half_width_m"]
        obstacle_offset = data["lateral_m"]
        uncertainty = data["pos_err_m"] + max(0, data["size_m"]) / 2 + cfg["vehicle_half_width_m"]
        side = data["free_side"]
        for target, legal in ((-offset, side in (LEFT, BOTH)), (offset, side in (RIGHT, BOTH))):
            # 회피에는 실제 코스 위치와 검증된 여유 공간이 모두 필요하다.
            if legal and hazard.route_known and abs(target) <= corridor and abs(target - obstacle_offset) >= uncertainty:
                shift = target - car["lateral_m"]
                needed = max(avoidance_distance(car["speed_mps"], shift, cfg),
                             car["speed_mps"] * abs(shift) / cfg["offset_rate_mps"])
                if abs(shift) <= 0.1 or remaining >= needed + cfg["margin"]:
                    return Mode.AVOID, target
        if data["severity"] >= CRITICAL or data["free_side"] == BLOCKED or hazard.route_known:
            return Mode.STOP, 0.0
        return Mode.CAUTION, 0.0

    def update(self, now, car, hazards, link_age, synchronized, drone_healthy):
        cfg = self.c
        speed = car["speed_mps"]
        stop = stopping_distance(speed, cfg)
        dt = 0 if self.last_time is None else min(max(now - self.last_time, 0), .2)
        self.last_time = now
        target = min(cfg["car_speed_nominal_mps"], cfg["vehicle_speed_limit_mps"])
        offset, selected, short = 0.0, None, False
        mode, reason = Mode.NORMAL, "clear"
        if car["manual"]:
            self.release_time = None
            self.output = Decision(Mode.MANUAL, speed, car["lateral_m"], stop, False, "operator override", None)
            return self.output

        choices = []
        for hazard in hazards:
            choice = self._hazard_choice(hazard, car, stop)
            if choice:
                choices.append((choice[0], -(hazard.remaining_m if hazard.remaining_m is not None else math.inf),
                                choice[1], hazard))
        if choices:
            mode, _, offset, selected = max(choices, key=lambda choice: choice[:2])
            reason = "hazard"
            if mode == Mode.STOP:
                target = 0.0
                short = selected.remaining_m is not None and selected.remaining_m < stop
            elif mode == Mode.CAUTION:
                target *= cfg["caution_ratio"]
        if not drone_healthy and mode < Mode.CAUTION:
            mode, target, reason = Mode.CAUTION, min(target, cfg["safe_speed_mps"]), "drone health"
        if link_age >= cfg["link_stop_s"]:
            mode, target, reason = Mode.STOP, 0.0, "guide link lost"
        elif mode != Mode.STOP and (link_age >= cfg["link_degraded_s"] or not synchronized):
            mode, reason = Mode.LINK_DEGRADED, "guide link or snapshot pending"
            hold_speed = self.output.target_speed_mps if self.output and self.output.mode != Mode.MANUAL else speed
            limit = cfg["safe_speed_mps"] if link_age >= cfg["link_slow_s"] else hold_speed
            target = min(target, hold_speed, limit)
            if self.output and self.output.mode != Mode.MANUAL:
                offset = self.output.lateral_offset_m

        if mode == Mode.NORMAL and self.output and self.output.mode not in (Mode.NORMAL, Mode.MANUAL):
            if self.release_time is None:
                self.release_time = now + cfg["normal_hold_s"]
            if now < self.release_time:
                mode, reason = self.output.mode, "clear hold"
                target = min(target, self.output.target_speed_mps)
                offset = self.output.lateral_offset_m
        else:
            self.release_time = None

        previous_speed = speed if not self.output or self.output.mode == Mode.MANUAL else self.output.target_speed_mps
        previous_offset = car["lateral_m"] if not self.output or self.output.mode == Mode.MANUAL else self.output.lateral_offset_m
        corridor = cfg["course_width_m"] / 2 - cfg["vehicle_half_width_m"]
        desired_offset = max(-corridor, min(corridor, offset))
        # 속도나 오프셋 목표가 한 번에 뛰지 않도록 단계 프로필의 한계를 적용한다.
        target = max(0.0, min(cfg["vehicle_speed_limit_mps"], approach(
            previous_speed, target, cfg["a_accel"] * dt, cfg["a_max"] * dt)))
        offset = max(-corridor, min(corridor, approach(
            previous_offset, desired_offset, cfg["offset_rate_mps"] * dt, cfg["offset_rate_mps"] * dt)))
        self.output = Decision(mode, target, offset, stop, short, reason, selected, desired_offset)
        return self.output
