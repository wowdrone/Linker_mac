"""User-requested flight mode change transaction (no Qt, no own MAVLink link).

Separation of concerns
- Tracking never calls this module. Only an explicit operator action (PyQt button or a
  validated browser request drained on the Qt thread) prepares and commits a request.
- A request never arms, takes off or starts a mission. Modes that would (AUTO / MISSION /
  TAKEOFF) and modes that need extra preparation (OFFBOARD: continuous setpoint stream)
  are listed as disabled with a reason. No dummy setpoints are ever sent.

Protocol (checked against ArduPilot dev docs "Get/Set Flight Mode", PX4 Offboard docs and
MAVLink common.xml; SET_MODE is superseded by MAV_CMD_DO_SET_MODE):
- ArduPilot: COMMAND_LONG MAV_CMD_DO_SET_MODE, param1=MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
  param2=custom mode number taken from pymavlink's per-vehicle-type ArduPilot mapping.
- PX4: COMMAND_LONG MAV_CMD_DO_SET_MODE, param1=base mode, param2=custom main mode,
  param3=custom sub mode taken from pymavlink's PX4 mapping (px4_map).
- Mode numbers are never shared between firmwares and never hard coded here.
- COMMAND_ACK, if it arrives, can reject the request. ACCEPTED alone is NOT success:
  only a HEARTBEAT from the pinned vehicle, received after the send, that reports the
  requested mode confirms the transition. Missing ACK is tolerated.

The packet is written once through the vehicle's existing per-vehicle encoder
(vehicle.master.mav → the Fleet link writer). No retries are performed automatically.
"""
from dataclasses import dataclass
from collections import deque
import threading
import time
import uuid

from pymavlink import mavutil

MAV = mavutil.mavlink
ARDUPILOT, PX4 = MAV.MAV_AUTOPILOT_ARDUPILOTMEGA, MAV.MAV_AUTOPILOT_PX4

HEARTBEAT_MAX_AGE_S = 3.0
CONFIRM_TIMEOUT_S = 5.0
TICKET_TTL_S = 30.0
MISMATCH_AFTER_ACK = 3          # heartbeats still showing the old mode after an ACCEPTED ACK
RATE_WINDOW_S, RATE_MAX = 60.0, 3  # explicit commits per vehicle per window

# Modes offered when (and only when) the firmware mapping of the vehicle contains them.
OFFERED = {
    ARDUPILOT: ("STABILIZE", "ALT_HOLD", "LOITER", "POSHOLD", "BRAKE", "GUIDED", "RTL",
                "SMART_RTL", "LAND", "MANUAL", "FBWA", "CRUISE", "HOLD",
                "QHOVER", "QLOITER", "QLAND", "QRTL"),
    PX4: ("MANUAL", "STABILIZED", "ALTCTL", "POSCTL", "LOITER", "RTL", "LAND"),
}
# Present in the mapping but deliberately disabled, with the reason shown to the operator.
DISABLED = {
    ARDUPILOT: {"AUTO": "미션 실행을 시작할 수 있어 이 화면에서 요청하지 않습니다.",
                "TAKEOFF": "이륙을 수행하므로 요청하지 않습니다."},
    PX4: {"MISSION": "미션 실행을 시작할 수 있어 이 화면에서 요청하지 않습니다.",
          "TAKEOFF": "이륙을 수행하므로 요청하지 않습니다.",
          "OFFBOARD": "진입 전 연속 setpoint 스트림(≥2 Hz)과 상실 failsafe 설계가 필요합니다. "
                      "해당 구현이 없으므로 비활성이며 더미 setpoint를 보내지 않습니다."},
}
POSITION_MODES = {"LOITER", "POSHOLD", "GUIDED", "RTL", "SMART_RTL", "BRAKE", "POSCTL",
                  "QLOITER", "QRTL", "CRUISE"}
EFFECT = {
    "STABILIZE": "수동 자세 모드 · 스로틀/고도는 조종자 입력",
    "STABILIZED": "수동 자세 모드 · 스로틀/고도는 조종자 입력",
    "MANUAL": "수동 모드 · 조종자 입력 필요",
    "ALT_HOLD": "고도 유지 · 수평 위치는 조종자 입력", "ALTCTL": "고도 유지 · 수평 위치는 조종자 입력",
    "LOITER": "현재 위치 유지(위치 추정 필요)", "POSHOLD": "현재 위치 유지(위치 추정 필요)",
    "POSCTL": "위치 제어 · 조종자 입력이 없으면 위치 유지(위치 추정 필요)",
    "BRAKE": "감속 후 위치 유지(위치 추정 필요)",
    "GUIDED": "GCS 위치·속도 명령 대기 모드 · Linker Tracking은 어떤 명령도 보내지 않습니다",
    "RTL": "홈 위치로 복귀 비행(복귀 고도까지 상승할 수 있음)",
    "SMART_RTL": "왔던 경로로 복귀 비행",
    "LAND": "현재 위치에서 착륙", "QLAND": "현재 위치에서 VTOL 착륙",
    "QRTL": "VTOL 복귀", "QLOITER": "VTOL 위치 유지", "QHOVER": "VTOL 고도 유지",
    "FBWA": "고정익 보조 수동", "CRUISE": "고정익 방위/고도 유지", "HOLD": "정지 유지",
}
TERMINAL = ("CONFIRMED", "REJECTED", "TIMEOUT", "FAILED", "CANCELLED")


class ModeRequestError(ValueError):
    """A request that must not be sent; the message is shown to the operator."""


@dataclass(frozen=True)
class ModeOption:
    name: str
    enabled: bool
    reason: str = ""


def family(vehicle):
    autopilot = getattr(vehicle, "autopilot", None)
    return autopilot if autopilot in (ARDUPILOT, PX4) else None


def firmware_name(vehicle):
    return {ARDUPILOT: "ArduPilot", PX4: "PX4"}.get(getattr(vehicle, "autopilot", None), "미지원/미확인")


def mode_mapping(vehicle):
    """Firmware specific mapping from pymavlink: ArduPilot by vehicle type, PX4 by px4_map."""
    fam = family(vehicle)
    if fam == ARDUPILOT:
        return dict(mavutil.mode_mapping_byname(getattr(vehicle, "mav_type", None)) or {})
    if fam == PX4:
        return dict(mavutil.px4_map)
    return {}


def mode_options(vehicle):
    fam = family(vehicle)
    if vehicle is None or fam is None:
        return []
    mapping = mode_mapping(vehicle)
    options = [ModeOption(name, True) for name in OFFERED[fam] if name in mapping]
    options += [ModeOption(name, False, reason) for name, reason in DISABLED[fam].items() if name in mapping]
    return options


def identity(vehicle):
    link = getattr(vehicle, "link", None)
    return (id(vehicle), getattr(link, "link_id", None), getattr(vehicle, "sysid", None),
            getattr(vehicle, "compid", None), getattr(vehicle, "connection_epoch", None))


class FlightModeController:
    """One transaction at a time. Call tick() periodically from the owner thread."""

    def __init__(self, selected=lambda: None, clock=time.monotonic,
                 heartbeat_max_age=HEARTBEAT_MAX_AGE_S, confirm_timeout=CONFIRM_TIMEOUT_S):
        self.selected, self.clock = selected, clock
        self.heartbeat_max_age, self.confirm_timeout = heartbeat_max_age, confirm_timeout
        self.lock = threading.RLock()
        self.state, self.detail = "IDLE", "요청 없음"
        self.ticket = None
        self.request = None
        self.history = {}
        self.log = []
        self.audit = deque(maxlen=500)   # every prepare/commit/cancel/reject/result, saved with the log
        self.sent_count = 0

    # ------------------------------------------------------------------ helpers
    def _heartbeat(self, vehicle):
        state = getattr(vehicle, "heartbeat_state", None)
        if not state:
            return None
        return state

    def _check(self, vehicle, mode):
        if vehicle is None:
            raise ModeRequestError("선택된 기체가 없습니다.")
        if vehicle is not self.selected():
            raise ModeRequestError("요청 대상이 현재 선택 기체가 아닙니다.")
        if not getattr(vehicle, "connected", False):
            raise ModeRequestError("기체 연결이 끊겼습니다.")
        if family(vehicle) is None:
            raise ModeRequestError("autopilot이 ArduPilot/PX4로 확인되지 않아 모드 변경을 지원하지 않습니다.")
        hb = self._heartbeat(vehicle)
        if hb is None:
            raise ModeRequestError("이 기체의 HEARTBEAT를 아직 받지 못했습니다.")
        age = self.clock() - hb[0]
        if not 0 <= age < self.heartbeat_max_age:
            raise ModeRequestError(f"HEARTBEAT가 오래되었습니다 ({age:.1f}s).")
        option = next((o for o in mode_options(vehicle) if o.name == mode), None)
        if option is None:
            raise ModeRequestError(f"{firmware_name(vehicle)} 매핑에서 확인되지 않은 모드입니다: {mode}")
        if not option.enabled:
            raise ModeRequestError(option.reason)
        if hb[1] == mode:
            raise ModeRequestError(f"이미 {mode} 모드입니다.")
        return hb

    def _impact(self, vehicle, hb, mode):
        lines = [EFFECT.get(mode, "펌웨어 정의 동작")]
        armed = hb[2]
        lines.append("ARMED · 비행 중일 수 있음: 승인 즉시 기체 거동이 바뀝니다." if armed
                     else "DISARMED · 모드만 바뀌며 시동·이륙은 하지 않습니다.")
        if mode in POSITION_MODES:
            fix = (getattr(vehicle, "tracking_telemetry", {}).get("GPS_RAW_INT") or {}).get("fix_type")
            if fix is None or fix < 3:
                lines.append("GPS 3D fix가 확인되지 않음 · 펌웨어가 요청을 거부할 수 있습니다.")
        lines.append("모드 목록에 있다는 사실이 전환 준비 완료를 뜻하지 않습니다. 결과는 기체 HEARTBEAT로 확인합니다.")
        lines.append("이 요청은 ARM·이륙·미션 시작을 수행하지 않으며 자동 재시도하지 않습니다.")
        return lines

    def _audit(self, event, detail, source=None, vehicle=None, mode=None, target=None):
        target = target or self.request or self.ticket or {}
        vehicle = vehicle if vehicle is not None else target.get("vehicle")
        hb = getattr(vehicle, "heartbeat_state", None) or (None, None, None)
        link = getattr(vehicle, "link", None)
        self.audit.append(dict(
            t_wall=time.time(), t_mono=self.clock(), event=event, detail=str(detail),
            source=source or target.get("source"),
            vehicle=None if vehicle is None else dict(
                name=getattr(vehicle, "name", None), link_id=getattr(link, "link_id", None),
                sysid=getattr(vehicle, "sysid", None), compid=getattr(vehicle, "compid", None),
                firmware=firmware_name(vehicle), epoch=getattr(vehicle, "connection_epoch", None)),
            from_mode=target.get("current_mode", hb[1]), to_mode=mode or target.get("mode"),
            armed=target.get("armed", hb[2]), ticket=target.get("id"), ack=target.get("ack")))

    def _finish(self, state, detail):
        self.state, self.detail = state, detail
        self.log.append(dict(t=self.clock(), state=state, detail=detail,
                             mode=(self.request or self.ticket or {}).get("mode")))
        del self.log[:-50]
        self._audit(state, detail)
        self.request = self.ticket = None   # every _finish state is terminal

    # ------------------------------------------------------------------ API
    def prepare(self, vehicle, mode, source="app"):
        """Validate and return a confirmation ticket. Nothing is sent.

        Only one ticket is valid at a time: a new prepare (from the PyQt window or any browser
        tab) supersedes and invalidates the previous unconfirmed ticket.
        """
        with self.lock:
            mode = str(mode or "").upper()
            if self.state == "REQUESTING":
                self._audit("REJECTED_PREPARE", "이전 요청 결과 확인 중", source, vehicle, mode, target={})
                raise ModeRequestError("이전 모드 변경 요청을 확인 중입니다.")
            try:
                hb = self._check(vehicle, mode)
            except ModeRequestError as exc:
                self._audit("REJECTED_PREPARE", exc, source, vehicle, mode, target={})
                raise
            if self.ticket is not None:
                self._audit("SUPERSEDED", f"새 요청({source})이 이전 확인 티켓을 무효화", target=self.ticket)
            ticket = dict(id=uuid.uuid4().hex, mode=mode, vehicle=vehicle, identity=identity(vehicle), source=source,
                          name=getattr(vehicle, "name", "?"), link_id=identity(vehicle)[1],
                          sysid=vehicle.sysid, compid=vehicle.compid, firmware=firmware_name(vehicle),
                          current_mode=hb[1], armed=hb[2], impact=self._impact(vehicle, hb, mode),
                          expires=self.clock() + TICKET_TTL_S)
            self.ticket = ticket
            self.state, self.detail = "CONFIRMING", f"{ticket['current_mode']} → {mode} 확인 대기 ({source})"
            self._audit("PREPARE", self.detail)
            return self.public_ticket(ticket)

    @staticmethod
    def public_ticket(ticket):
        return {k: v for k, v in ticket.items() if k not in ("vehicle", "identity")}

    def cancel(self, reason="사용자 취소", source=None):
        """Cancels an unconfirmed ticket. A request already sent cannot be recalled: it is
        recorded as CANCELLED here, but the vehicle may still apply it (shown to the operator)."""
        with self.lock:
            if self.state == "CONFIRMING":
                self._finish("CANCELLED", f"{reason}" + (f" ({source})" if source else ""))
            elif self.state == "REQUESTING":
                self._finish("CANCELLED", f"{reason} · 이미 송신된 요청은 기체가 적용할 수 있음")

    def commit(self, ticket_id, source="app"):
        """Operator confirmed. Re-validate against the latest state, then send exactly once."""
        with self.lock:
            if self.state == "REQUESTING":
                self._audit("REJECTED_COMMIT", "이미 송신 후 결과 확인 중", source, target={})
                raise ModeRequestError("이미 요청을 송신했고 결과를 확인 중입니다.")
            ticket = self.ticket
            if ticket is None or ticket_id != ticket["id"]:
                self._audit("REJECTED_COMMIT", "유효하지 않은(만료·무효화된) 티켓", source, target={})
                raise ModeRequestError("확인된 요청이 없거나 만료·무효화되었습니다. 다시 요청하세요.")
            if ticket.get("source") != source:
                self._audit("REJECTED_COMMIT", f"{ticket.get('source')}에서 만든 티켓을 {source}에서 확인할 수 없음",
                            source, target={})
                raise ModeRequestError("다른 화면에서 만든 확인 요청입니다. 이 화면에서 다시 요청하세요.")
            now = self.clock()
            vehicle = ticket["vehicle"]
            try:
                if now > ticket["expires"]:
                    raise ModeRequestError("확인 시간이 지났습니다. 다시 요청하세요.")
                if identity(vehicle) != ticket["identity"] or vehicle is not self.selected():
                    raise ModeRequestError("확인 중 대상 기체 또는 연결 세션이 바뀌어 요청을 취소했습니다.")
                hb = self._check(vehicle, ticket["mode"])
                if hb[1] != ticket["current_mode"] or hb[2] != ticket["armed"]:
                    raise ModeRequestError("확인 중 기체 모드/시동 상태가 바뀌어 요청을 취소했습니다.")
                key = ticket["identity"][1:4]
                recent = [t for t in self.history.get(key, []) if now - t < RATE_WINDOW_S]
                if len(recent) >= RATE_MAX:
                    raise ModeRequestError(f"{RATE_WINDOW_S:.0f}초에 {RATE_MAX}회 요청 한도를 넘었습니다.")
            except ModeRequestError as exc:
                self._finish("CANCELLED", str(exc))
                raise
            self.history[key] = recent + [now]
            self._send(vehicle, ticket["mode"])
            self.sent_count += 1
            self.request = dict(ticket, sent_at=self.clock(), ack=None, mismatches=0, committed_by=source)
            self.ticket = None
            self.state, self.detail = "REQUESTING", f"{ticket['mode']} 요청 송신 ({source}) · HEARTBEAT 확인 대기"
            self._audit("SENT", self.detail, source)
            return True

    def _send(self, vehicle, mode):
        mapping = mode_mapping(vehicle)
        if family(vehicle) == ARDUPILOT:
            params = (MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mapping[mode], 0)
        else:
            params = tuple(mapping[mode])          # (base mode, custom main mode, custom sub mode)
        vehicle.master.mav.command_long_send(vehicle.sysid, vehicle.compid, MAV.MAV_CMD_DO_SET_MODE, 0,
                                             float(params[0]), float(params[1]), float(params[2]), 0, 0, 0, 0)

    def on_command_feedback(self, vehicle, command, success, message):
        """Existing Linker COMMAND_ACK signal: (command name, accepted?, 'NAME: MAV_RESULT_x')."""
        with self.lock:
            request = self.request
            if (self.state != "REQUESTING" or request is None or vehicle is not request["vehicle"]
                    or command != "MAV_CMD_DO_SET_MODE"):
                return
            text = str(message)
            if success:
                request["ack"] = "ACCEPTED"
                self.detail = f"{request['mode']} ACK 수락 · 실제 모드는 HEARTBEAT로 확인 중"
                self._audit("ACK", text)
            elif "IN_PROGRESS" in text:
                request["ack"] = "IN_PROGRESS"
            else:
                self._finish("REJECTED", f"기체가 거부했습니다 · {text}")

    def tick(self):
        with self.lock:
            now = self.clock()
            if self.state == "CONFIRMING" and self.ticket:
                vehicle = self.ticket["vehicle"]
                if now > self.ticket["expires"]:
                    self._finish("CANCELLED", "확인 시간이 지났습니다.")
                elif identity(vehicle) != self.ticket["identity"] or vehicle is not self.selected():
                    self._finish("CANCELLED", "확인 중 선택 기체 또는 연결이 바뀌었습니다.")
                return
            if self.state != "REQUESTING":
                return
            request = self.request
            vehicle = request["vehicle"]
            if vehicle is not self.selected():
                return self._finish("FAILED", "결과 확인 중 선택 기체가 바뀌었습니다.")
            if identity(vehicle) != request["identity"] or not getattr(vehicle, "connected", False):
                return self._finish("FAILED", "결과 확인 중 기체 연결이 끊기거나 재연결되었습니다.")
            hb = self._heartbeat(vehicle)
            if hb is not None and hb[0] > request["sent_at"] and hb[0] != request.get("last_hb"):
                request["last_hb"] = hb[0]
                if hb[1] == request["mode"]:
                    ack = request["ack"] or "ACK 없음"
                    return self._finish("CONFIRMED", f"HEARTBEAT에서 {hb[1]} 확인 · ACK {ack}")
                if request["ack"] == "ACCEPTED":
                    request["mismatches"] += 1
                    if request["mismatches"] >= MISMATCH_AFTER_ACK:
                        return self._finish("FAILED", f"ACK는 수락됐지만 HEARTBEAT 모드가 {hb[1]}로 유지됩니다.")
            if now - request["sent_at"] >= self.confirm_timeout:
                ack = request["ack"] or "ACK 없음"
                current = hb[1] if hb else "—"
                self._finish("TIMEOUT", f"{self.confirm_timeout:.0f}초 안에 {request['mode']} 미확인 · "
                                        f"현재 {current} · {ack}")

    def snapshot(self):
        with self.lock:
            target = self.request or self.ticket
            return dict(state=self.state, detail=self.detail,
                        ticket=self.public_ticket(self.ticket) if self.ticket else None,
                        mode=target["mode"] if target else None, source=target.get("source") if target else None,
                        ack=self.request["ack"] if self.request else None,
                        log=[dict(entry) for entry in self.log[-8:]])
