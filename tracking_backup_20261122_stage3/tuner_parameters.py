"""Target-scoped MAVLink parameter reads and explicitly authorized transactions."""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
from uuid import uuid4

from pymavlink.dialects.v20 import ardupilotmega as mav

PID = frozenset(
    [f"ATC_RAT_{axis}_{term}" for axis in ("RLL", "PIT", "YAW") for term in "PID"]
    + ["ATC_ANG_RLL_P", "ATC_ANG_PIT_P"]
)
FILTER = frozenset(f"INS_HNTCH_{k}" for k in ("ENABLE", "MODE", "FREQ", "BW", "ATT", "REF", "HMNCS", "OPTS"))
SUPPORTED = PID | FILTER
COPTER_TYPES = frozenset(getattr(mav, f"MAV_TYPE_{kind}") for kind in (
    "QUADROTOR", "COAXIAL", "HEXAROTOR", "OCTOROTOR", "TRICOPTER", "DODECAROTOR", "DECAROTOR"
))


class TunerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    uid: str
    version: int
    build: str
    board: int
    autopilot: int
    vehicle_type: int

    @property
    def firmware(self) -> str:
        return ".".join(str((self.version >> n) & 255) for n in (24, 16, 8))

    @property
    def copter(self) -> bool:
        return self.autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA and self.vehicle_type in COPTER_TYPES


@dataclass(frozen=True)
class Change:
    name: str
    old: float
    new: float


class TunerEndpoint:
    """Mailboxes fed by Fleet's sole reader. Never consumes the serial/socket stream."""

    def __init__(self, vehicle):
        self.vehicle = vehicle
        self.condition = threading.Condition(threading.RLock())
        self.operation = threading.Lock()
        self.heartbeat = None
        self.last_armed = False
        self.arm_epoch = 0
        self.replies = deque(maxlen=128)
        self.parameter_capture = None
        self.telemetry = deque(maxlen=36000)
        self.last_times = {}

    def observe(self, msg):
        v = self.vehicle
        if (msg.get_srcSystem(), msg.get_srcComponent()) != (v.sysid, v.compid):
            return
        kind = msg.get_type()
        if kind not in ("HEARTBEAT", "PARAM_VALUE", "AUTOPILOT_VERSION", "EXTENDED_SYS_STATE",
                        "ATTITUDE", "ATTITUDE_TARGET"):
            return
        now = time.monotonic()
        with self.condition:
            if kind == "HEARTBEAT":
                if msg.autopilot != v.autopilot or msg.type != v.mav_type:
                    self.heartbeat = None
                    self.arm_epoch += 1
                    return
                armed = bool(msg.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)
                if armed != self.last_armed:
                    self.arm_epoch += 1
                    self.telemetry.append(("gap", 0., now, ()))
                self.last_armed = armed
                self.heartbeat = now
            elif kind in ("PARAM_VALUE", "AUTOPILOT_VERSION", "EXTENDED_SYS_STATE"):
                self.replies.append((now, msg))
                if kind == "PARAM_VALUE" and self.parameter_capture is not None:
                    self.parameter_capture.append(msg)
                self.condition.notify_all()
            elif self.last_armed and self.heartbeat and now-self.heartbeat < 2.5:
                stamp = int(msg.time_boot_ms)*.001
                last = self.last_times.get(kind)
                if last is not None and stamp < last:
                    self.telemetry.clear()
                    self.last_times.clear()
                    self.arm_epoch += 1
                self.last_times[kind] = stamp
                if kind == "ATTITUDE_TARGET":
                    rates = (msg.body_roll_rate, msg.body_pitch_rate, msg.body_yaw_rate)
                    values = tuple(None if msg.type_mask & (1 << i) else rate
                                   for i, rate in enumerate(rates))
                    self.telemetry.append(("target", stamp, now, values))
                else:
                    self.telemetry.append(("actual", stamp, now,
                                           (msg.rollspeed, msg.pitchspeed, msg.yawspeed)))

    def snapshot(self) -> list:
        with self.condition:
            return list(self.telemetry)


def _name(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii")
    return value.rstrip("\0")


def _float32(value) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise TunerError("유한한 값만 허용됩니다.")
    return struct.unpack("<f", struct.pack("<f", value))[0]


def save_record(folder: Path, kind: str, data: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.json"
    _write_record(target, data)
    return target


def _write_record(target: Path, data: dict) -> None:
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


class ParameterSession:
    """Pin the original Vehicle and link. Calls run outside the Qt UI thread."""

    def __init__(self, endpoint: TunerEndpoint, selected, timeout=1.5):
        self.endpoint = endpoint
        self.vehicle = endpoint.vehicle
        self.selected = selected
        self.master = self.vehicle.link.master
        self.ids = self.vehicle.sysid, self.vehicle.compid
        self.timeout = timeout

    def _guard(self, epoch=None):
        ep, v = self.endpoint, self.vehicle
        if not self.selected() or not v.connected or not v.link._running or self.master is None or (
                v.link.master is not self.master or ep.heartbeat is None or time.monotonic()-ep.heartbeat >= 2.5):
            raise TunerError("선택 기체, 연결 또는 heartbeat가 유효하지 않습니다.")
        if epoch is not None and (ep.last_armed or ep.arm_epoch != epoch):
            raise TunerError("Armed 상태이거나 작업 중 상태가 변경되었습니다.")

    def _exchange(self, send, matches, epoch=None, attempts=2):
        ep = self.endpoint
        with ep.condition:
            for _ in range(attempts):
                self._guard(epoch)
                sent = time.monotonic()
                send()
                deadline = sent + self.timeout
                while True:
                    self._guard(epoch)
                    for stamp, msg in reversed(ep.replies):
                        if stamp >= sent and matches(msg):
                            return msg
                    wait = deadline-time.monotonic()
                    if wait <= 0:
                        break
                    ep.condition.wait(min(.1, wait))
        raise TunerError("지정된 기체에서 응답을 받지 못했습니다.")

    def _request(self, msg_id, kind, epoch=None):
        v = self.vehicle
        return self._exchange(lambda: v.master.mav.command_long_send(
            *self.ids, mav.MAV_CMD_REQUEST_MESSAGE, 0, msg_id, 0, 0, 0, 0, 0, 0),
            lambda m: m.get_type() == kind, epoch)

    def identify(self, epoch=None) -> Identity:
        v = self.vehicle
        version = self._request(mav.MAVLINK_MSG_ID_AUTOPILOT_VERSION, "AUTOPILOT_VERSION", epoch)
        uid2 = bytes(getattr(version, "uid2", ()))
        uid = "uid2-"+uid2.hex() if any(uid2) else f"uid-{version.uid:016x}" if version.uid else ""
        return Identity(uid, version.flight_sw_version, bytes(version.flight_custom_version).hex(),
                        version.board_version, v.autopilot, v.mav_type)

    def _parameter(self, name, value=None, epoch=None):
        v = self.vehicle
        encoded = name.encode("ascii")
        if value is None:
            def send():
                v.master.mav.param_request_read_send(*self.ids, encoded, -1)
        else:
            def send():
                v.master.mav.param_set_send(*self.ids, encoded, value, mav.MAV_PARAM_TYPE_REAL32)
        msg = self._exchange(send, lambda m: m.get_type() == "PARAM_VALUE" and _name(m.param_id) == name,
                             epoch, attempts=2 if value is None else 1)
        if msg.param_type != mav.MAV_PARAM_TYPE_REAL32:
            is_ardu_integer = (self.vehicle.autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA
                               and msg.param_type in (mav.MAV_PARAM_TYPE_INT8,
                                                      mav.MAV_PARAM_TYPE_INT16,
                                                      mav.MAV_PARAM_TYPE_INT32)
                               and math.isfinite(msg.param_value) and float(msg.param_value).is_integer())
            if name not in FILTER or not is_ardu_integer:
                raise TunerError(f"{name}: 지원하지 않는 파라미터 형식입니다.")
        return _float32(msg.param_value)

    def inspect(self, names: tuple[str, ...]) -> tuple[Identity, dict]:
        with self.endpoint.operation:
            identity = self.identify()
            if names and not identity.copter:
                raise TunerError("이 기체의 펌웨어에 대한 PID/필터 파라미터 스키마를 지원하지 않습니다.")
            values = {}
            for name in names:
                if name not in SUPPORTED:
                    raise TunerError(f"지원 범위 밖 파라미터: {name}")
                try:
                    values[name] = self._parameter(name)
                except TunerError as exc:
                    with self.endpoint.condition:
                        self._guard()
                    values[name] = str(exc)
            return identity, values

    def transact(self, identity: Identity, changes: tuple[Change, ...], metadata: dict,
                 root: Path, *, approved: bool = False) -> dict:
        if not approved or not changes:
            raise TunerError("변경 목록에 대한 명시적인 승인이 필요합니다.")
        if self.vehicle.link.kind not in ("serial", "bluetooth", "tcp"):
            raise TunerError("쓰기에는 단일 대상 Serial, Bluetooth 또는 TCP 링크가 필요합니다.")
        if not identity.uid or not identity.version or not any(bytes.fromhex(identity.build)) or not identity.copter:
            raise TunerError("고유 ID와 빌드가 확인된 ArduCopter만 지원합니다.")
        firmware_keys = ("version", "build", "board", "autopilot", "vehicle_type")
        if metadata.get("firmware") != {k: getattr(identity, k) for k in firmware_keys}:
            raise TunerError("연결 기체의 정확한 빌드와 일치하는 메타데이터가 필요합니다.")
        if len({c.name for c in changes}) != len(changes):
            raise TunerError("중복 파라미터를 적용할 수 없습니다.")
        for change in changes:
            spec = metadata.get("parameters", {}).get(change.name, {})
            # Writes are limited to continuous rate/angle gains. Notch modes need
            # cross-parameter validation and are read-only in this implementation.
            if change.name not in PID or spec.get("type") != "REAL32" or not spec.get("writable") is True:
                raise TunerError(f"{change.name}: 검증된 쓰기 범위 밖입니다.")
            lo, hi = _float32(spec.get("min", math.nan)), _float32(spec.get("max", math.nan))
            if not lo <= _float32(change.new) <= hi or not lo <= _float32(change.old) <= hi:
                raise TunerError(f"{change.name}: 메타데이터 범위를 벗어납니다.")
        with self.endpoint.operation:
            epoch = self.endpoint.arm_epoch
            if self.identify(epoch) != identity:
                raise TunerError("기체 ID 또는 펌웨어가 변경되었습니다.")
            current = {c.name: self._parameter(c.name, epoch=epoch) for c in changes}
            if any(current[c.name] != _float32(c.old) for c in changes):
                raise TunerError("현재 값이 미리보기와 달라졌습니다. 다시 읽으세요.")
            folder = root / identity.uid
            backup = save_record(folder, "backup", dict(identity=asdict(identity), values=current))
            journal = dict(identity=asdict(identity), backup=backup.name,
                           requested=[asdict(c) for c in changes], verified=[], error=None,
                           state="pending", attempted=None)
            journal_path = save_record(folder, "transaction", journal)
            for change in changes:
                try:
                    if self.identify(epoch) != identity:
                        raise TunerError("적용 중 기체 ID 또는 펌웨어가 변경되었습니다.")
                    state = self._request(mav.MAVLINK_MSG_ID_EXTENDED_SYS_STATE, "EXTENDED_SYS_STATE", epoch)
                    if state.landed_state != mav.MAV_LANDED_STATE_ON_GROUND:
                        raise TunerError("지상 상태를 확인하지 못했습니다.")
                    value = _float32(change.new)
                    journal["attempted"] = change.name
                    _write_record(journal_path, journal)
                    if self._parameter(change.name, value, epoch) != value:
                        raise TunerError("PARAM_SET 응답 값 불일치")
                    if self._parameter(change.name, epoch=epoch) != value:
                        raise TunerError("독립 PARAM_REQUEST_READ 검증 값 불일치")
                    journal["verified"].append(change.name)
                except (OSError, TunerError, ValueError, OverflowError) as exc:
                    journal["error"] = f"{change.name}: {exc}. 실제 값은 다시 읽어 확인하세요."
                    break
            journal["state"] = "partial" if journal["error"] else "verified"
            _write_record(journal_path, journal)
            return journal
