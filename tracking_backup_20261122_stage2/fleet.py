# fleet.py
# 멀티 기체 동시 연결 관리 모듈
#
#  - MavLinkWorker : 링크(Serial / UDP / TCP) 1개당 수신 스레드 1개
#                    하나의 링크에 여러 SYSID 가 섞여 들어와도 SYSID 별로 분배
#  - Vehicle       : 기존 Linker 를 상속한 "기체 1대" 객체
#                    (arm / takeoff / mission / param 등 기존 API 그대로 사용)
#  - FleetManager  : 링크 / 기체 목록 관리, HEARTBEAT 타임아웃 감시
#  - ActiveVehicleProxy : MainWindow 가 쓰던 self.linker 자리에 들어가는
#                    "현재 선택된 기체" 프록시 (시그널/메서드 자동 전달)

import threading
import time

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as _mavlink_ardupilotmega  # noqa: F401

from Linker import Linker
from platform_mac import SERIAL_LINK_KINDS, serial_callout_port
from tuner_parameters import TunerEndpoint

mavutil.set_dialect("ardupilotmega")

# 기체별 표시 색상 (지도 마커 / 표 / 영상 채널 테두리 공통)
VEHICLE_COLORS = [
    "#00ffea", "#ff9f1a", "#ff4fd8", "#7dff4f",
    "#4f8bff", "#ffe14f", "#ff5c5c", "#b38cff",
]

HEARTBEAT_TIMEOUT_S = 5.0

GCS_SYSID = 255
GCS_COMPID = mavutil.mavlink.MAV_COMP_ID_MISSIONPLANNER  # 190


def build_link_url(kind: str, address: str, baud: int = 115200) -> str:
    """UI 입력값 → pymavlink 연결 문자열"""
    kind = (kind or "").lower()
    address = (address or "").strip()
    if kind in SERIAL_LINK_KINDS:
        return address
    if kind == "udp":            # 수신 대기 (SITL / 텔레메트리 브리지 → GCS)
        return f"udpin:{address or '127.0.0.1:14550'}"
    if kind == "udpout":         # GCS 가 먼저 송신
        return f"udpout:{address}"
    if kind == "tcp":
        return f"tcp:{address or '127.0.0.1:5760'}"
    return address


# ---------------------------------------------------------------------- #
# 링크 수신 스레드                                                       #
# ---------------------------------------------------------------------- #
class MavLinkWorker(QThread):
    """
    링크 1개를 열고 메시지를 수신하여 SYSID 별 Vehicle 로 분배한다.
    연결 자체도 이 스레드에서 열기 때문에 GUI 가 멈추지 않는다.
    """

    stateChanged = pyqtSignal(int, str, str)            # link_id, state, detail
    systemDiscovered = pyqtSignal(int, int, int, int, int)  # link_id, sysid, compid, type, autopilot

    def __init__(self, link_id, kind, address, baud, fleet):
        super().__init__()
        self.link_id = link_id
        self.kind = kind
        self.address = address
        self.baud = baud
        self.fleet = fleet

        self.master = None
        self.write_lock = threading.Lock()
        self._running = True
        self._pending = set()      # 발견 신호를 보냈지만 아직 Vehicle 이 안 만들어진 sysid
        self.rx_count = 0

    @property
    def human(self) -> str:
        if self.kind in SERIAL_LINK_KINDS:
            return f"{self.kind.capitalize()} {self.address}@{self.baud}"
        return f"{self.kind.upper()} {self.address}"

    def run(self):
        url = build_link_url(self.kind, self.address, self.baud)
        self.stateChanged.emit(self.link_id, "connecting", url)
        try:
            self.master = mavutil.mavlink_connection(
                url,
                baud=self.baud,
                dialect="ardupilotmega",
                source_system=GCS_SYSID,
                source_component=GCS_COMPID,
                autoreconnect=True,
            )
        except Exception as e:
            self.master = None
            self.stateChanged.emit(self.link_id, "error", str(e))
            return

        self.stateChanged.emit(self.link_id, "open", url)

        last_gcs_hb = 0.0
        while self._running:
            now = time.time()
            if now - last_gcs_hb >= 1.0:
                last_gcs_hb = now
                self._send_gcs_heartbeat()

            try:
                msg = self.master.recv_match(blocking=True, timeout=0.2)
            except Exception as e:
                self.stateChanged.emit(self.link_id, "error", f"recv: {e}")
                time.sleep(0.5)
                continue

            if msg is None or msg.get_type() == "BAD_DATA":
                continue

            self.rx_count += 1
            self._dispatch(msg)

        try:
            self.master.close()
        except Exception:
            pass
        self.master = None
        self.stateChanged.emit(self.link_id, "closed", "")

    def _send_gcs_heartbeat(self):
        if self.master is None:
            return
        try:
            with self.write_lock:
                self.master.mav.heartbeat_send(
                    mavutil.mavlink.MAV_TYPE_GCS,
                    mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                    0, 0, 0,
                )
        except Exception:
            pass

    def _dispatch(self, msg):
        sysid = msg.get_srcSystem()
        compid = msg.get_srcComponent()
        if sysid in (0, GCS_SYSID):
            return

        vehicle = self.fleet.vehicle_for(self.link_id, sysid)

        if msg.get_type() == "HEARTBEAT":
            # GCS / 짐벌 / 카메라 등 비행제어기가 아닌 컴포넌트는 기체로 보지 않음
            if msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                return
            if msg.type == mavutil.mavlink.MAV_TYPE_GCS:
                return
            if vehicle is None:
                if sysid not in self._pending:
                    self._pending.add(sysid)
                    self.systemDiscovered.emit(
                        self.link_id, sysid, compid, msg.type, msg.autopilot
                    )
                return

        if vehicle is None:
            return

        try:
            vehicle.handle_message(msg)
        except Exception as e:
            vehicle.statusTextReceived.emit(f"Message error: {e}")

    def write_to(self, buf: bytes):
        """
        링크로 패킷 송신. UDP 서버(udpin)는 pymavlink 가 접속한 모든 클라이언트로 보내고,
        실제 수신 기체는 패킷의 target_system 으로 구분된다.
        """
        master = self.master
        if master is None:
            return
        with self.write_lock:
            master.write(buf)

    def stop(self):
        self._running = False
        self.wait(2000)


# ---------------------------------------------------------------------- #
# 기체별 송신 래퍼                                                       #
# ---------------------------------------------------------------------- #
class _VehicleWriter:
    """MAVLink 인코더의 file 역할: 기체가 붙어있는 링크로 송신"""

    def __init__(self, vehicle):
        self.vehicle = vehicle

    def write(self, buf):
        link = self.vehicle.link
        if link is not None:
            link.write_to(buf)


class _VehicleMaster:
    """
    Linker 내부 코드가 self.master.xxx 로 쓰는 부분을 기체 1대 기준으로 바꿔주는 래퍼.
    (pymavlink master 를 링크 단위로 공유하므로 target_system 을 기체별로 고정해야 함)
    """

    def __init__(self, vehicle):
        self.vehicle = vehicle
        self.mav = mavutil.mavlink.MAVLink(
            _VehicleWriter(vehicle), srcSystem=GCS_SYSID, srcComponent=GCS_COMPID
        )
        self.mav.robust_parsing = True

    @property
    def target_system(self):
        return self.vehicle.sysid

    @property
    def target_component(self):
        return self.vehicle.compid

    def mode_mapping(self):
        return mavutil.mode_mapping_byname(self.vehicle.mav_type) or {}

    def set_mode(self, mode):
        mapping = self.mode_mapping()
        mode_id = mapping.get(str(mode).upper()) if not isinstance(mode, int) else mode
        if mode_id is None:
            raise ValueError(f"Unknown mode {mode}")
        self.mav.set_mode_send(
            self.target_system,
            mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
            mode_id,
        )

    # Linker.arm/disarm 이 COMMAND_LONG 을 따로 보내므로 편의함수는 생략
    def arducopter_arm(self):
        pass

    def arducopter_disarm(self):
        pass

    def recv_match(self, *args, **kwargs):
        # 수신은 링크 스레드가 전담 (동시 읽기 방지)
        return None

    def close(self):
        pass


# ---------------------------------------------------------------------- #
# 기체 1대                                                               #
# ---------------------------------------------------------------------- #
class Vehicle(Linker):
    """기존 Linker API 를 그대로 유지하는 기체 객체"""

    def __init__(self, link, sysid, compid, mav_type, autopilot, color, parent=None):
        super().__init__(parent)
        self.link = link
        self.sysid = sysid
        self.compid = compid
        self.mav_type = mav_type
        self.autopilot = autopilot
        self.color = color
        self.name = f"SYS {sysid}"

        self.master = _VehicleMaster(self)
        self.target_system = sysid
        self.target_component = compid

        self.telemetry_received = {}
        self.tracking_telemetry = {}
        self.last_heartbeat = time.time()
        self.connected = True

        # 최신 상태 캐시 (Fleet 표 / 지도용)
        self.last_mode = "-"
        self.last_armed = False
        self.roll = self.pitch = 0.0

        self._param_waiters = {}
        self._param_lock = threading.Lock()
        self.tuner_endpoint = TunerEndpoint(self)

    @property
    def key(self):
        return (self.link.link_id, self.sysid)

    @property
    def key_str(self):
        """콤보박스 / JS 에서 쓰는 문자열 key ("링크id_sysid")"""
        return f"{self.link.link_id}_{self.sysid}"

    @property
    def link_name(self):
        return self.link.human if self.link else "-"

    def start(self):
        self._start_default_streams(rate_hz=10)
        self.connectionStateChanged.emit(True)
        self.statusTextReceived.emit(f"[{self.name}] connected via {self.link_name}")

    # 개별 연결/해제는 FleetManager 가 담당
    def connect_serial(self, *a, **k):
        self.statusTextReceived.emit("Use FleetManager.add_link() for multi-vehicle.")

    connect_udp = connect_serial

    def disconnect(self):
        self.mark_lost()

    def mark_lost(self):
        if self.connected:
            self.connected = False
            self.connectionStateChanged.emit(False)
            self.statusTextReceived.emit(f"[{self.name}] link lost")

    def handle_message(self, msg):
        self.tuner_endpoint.observe(msg)
        mtype = msg.get_type()
        if msg.get_srcComponent() == self.compid:
            self.telemetry_received[mtype] = time.monotonic()
            fields = {
                'HEARTBEAT': ('autopilot', 'base_mode', 'custom_mode'),
                'ATTITUDE': ('roll', 'pitch', 'yaw'),
                'GPS_RAW_INT': ('fix_type', 'satellites_visible'),
                'GLOBAL_POSITION_INT': ('alt', 'relative_alt'),
                'VFR_HUD': ('alt',),
                'SYS_STATUS': ('voltage_battery', 'battery_remaining'),
                'AUTOPILOT_VERSION': ('flight_sw_version',),
            }.get(mtype, ())
            self.tracking_telemetry[mtype] = {k: getattr(msg, k, None) for k in fields}

        if mtype == "HEARTBEAT":
            if msg.get_srcComponent() != self.compid:
                return  # 동일 SYSID 의 다른 컴포넌트 HEARTBEAT 무시
            self.last_heartbeat = time.time()
            self.last_mode = mavutil.mode_string_v10(msg)
            self.last_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            if not self.connected:
                self.connected = True
                self.connectionStateChanged.emit(True)
                self.request_health_streams()

        elif mtype == "ATTITUDE":
            import math
            self.roll = math.degrees(msg.roll)
            self.pitch = math.degrees(msg.pitch)

        elif mtype == "SYS_STATUS":
            if msg.voltage_battery not in (0, 0xFFFF):
                self.battery_v = msg.voltage_battery / 1000.0
            if msg.battery_remaining >= 0:
                self.battery_percent = msg.battery_remaining

        elif mtype == "GPS_RAW_INT":
            self.gps_fix_type = msg.fix_type
            self.gps_sats = msg.satellites_visible
            self.gps_hdop = msg.eph / 100.0 if msg.eph != 0xFFFF else None
            fix_names = {0: "NoGPS", 1: "NoFix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK-F", 6: "RTK"}
            self.gps_status = f"{fix_names.get(msg.fix_type, msg.fix_type)} ({msg.satellites_visible})"

        elif mtype == "VIBRATION":
            self.vibe_x, self.vibe_y, self.vibe_z = msg.vibration_x, msg.vibration_y, msg.vibration_z
            self.vibe_clip0, self.vibe_clip1, self.vibe_clip2 = msg.clipping_0, msg.clipping_1, msg.clipping_2

        elif mtype == "RADIO_STATUS":
            self.radio_rssi, self.radio_remrssi = msg.rssi, msg.remrssi

        elif mtype == "PARAM_VALUE":
            name = msg.param_id
            if isinstance(name, bytes):
                name = name.decode("ascii", "ignore")
            name = name.strip("\x00")
            with self._param_lock:
                waiter = self._param_waiters.get(name)
            if waiter is not None:
                waiter[1] = msg.param_value
                waiter[0].set()

        super().handle_message(msg)

        # HEARTBEAT 처리 후 Linker 가 target_component 를 바꾸지 않도록 고정
        self.target_system = self.sysid
        self.target_component = self.compid

    def get_param(self, name: str):
        """수신 스레드가 받은 PARAM_VALUE 를 이벤트로 기다림 (recv_match 경합 방지)"""
        if not self.connected:
            self.commandFeedback.emit("GET_PARAM", False, "Not connected")
            return None
        ev = threading.Event()
        waiter = [ev, None]
        with self._param_lock:
            self._param_waiters[name] = waiter
        try:
            self.master.mav.param_request_read_send(
                self.sysid, self.compid, name.encode("utf-8"), -1
            )
            if ev.wait(1.5):
                self.commandFeedback.emit("GET_PARAM", True, f"{name}={waiter[1]}")
                return int(waiter[1])
            self.commandFeedback.emit("GET_PARAM", False, f"{name} read timeout")
            return None
        finally:
            with self._param_lock:
                self._param_waiters.pop(name, None)

    def summary(self) -> dict:
        return dict(
            sysid=self.sysid,
            link=self.link_name,
            connected=self.connected,
            mode=self.last_mode,
            armed=self.last_armed,
            alt=self.altitude,
            gs=self.groundspeed,
            heading=self.yaw_deg,
            lat=self.last_lat,
            lon=self.last_lon,
            batt_v=self.battery_v,
            batt_p=self.battery_percent,
            gps=self.gps_status,
            hb_age=time.time() - self.last_heartbeat,
            color=self.color,
        )


# ---------------------------------------------------------------------- #
# 전체 기체 관리                                                         #
# ---------------------------------------------------------------------- #
class FleetManager(QObject):
    linkStateChanged = pyqtSignal(int, str, str)   # link_id, state, detail
    linksChanged = pyqtSignal()
    vehicleAdded = pyqtSignal(object)
    vehicleRemoved = pyqtSignal(object)
    vehicleTelemetry = pyqtSignal(object)          # Vehicle (지도 / 표 갱신용)
    log = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.links = {}        # link_id -> MavLinkWorker
        self.link_states = {}  # link_id -> state
        self.vehicles = {}     # (link_id, sysid) -> Vehicle
        self._next_link_id = 1
        self._color_idx = 0

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_timeouts)
        self._watchdog.start()

    # ---- 링크 ----
    def add_link(self, kind: str, address: str, baud: int = 115200) -> int:
        kind = kind.lower()
        serial_link = kind in SERIAL_LINK_KINDS
        if serial_link:
            address = serial_callout_port(address)
        for w in self.links.values():
            same_transport = w.kind == kind or (serial_link and w.kind in SERIAL_LINK_KINDS)
            if not same_transport or w.address != address:
                continue
            if serial_link and self.link_states.get(w.link_id) in {"error", "closed"} and not w.isRunning():
                self.remove_link(w.link_id)
                break
            self.log.emit(f"Link already open: {w.human}")
            return w.link_id

        link_id = self._next_link_id
        self._next_link_id += 1
        worker = MavLinkWorker(link_id, kind, address, baud, self)
        worker.stateChanged.connect(self._on_link_state)
        worker.systemDiscovered.connect(self._on_system_discovered)
        self.links[link_id] = worker
        self.link_states[link_id] = "connecting"
        worker.start()
        self.log.emit(f"Link #{link_id} opening: {worker.human}")
        self.linksChanged.emit()
        return link_id

    def remove_link(self, link_id: int):
        worker = self.links.pop(link_id, None)
        if worker is None:
            return
        worker.stop()
        self.link_states.pop(link_id, None)
        for key in [k for k in self.vehicles if k[0] == link_id]:
            v = self.vehicles.pop(key)
            v.mark_lost()
            self.vehicleRemoved.emit(v)
        self.log.emit(f"Link #{link_id} closed: {worker.human}")
        self.linksChanged.emit()

    def remove_all(self):
        for link_id in list(self.links):
            self.remove_link(link_id)

    def _on_link_state(self, link_id, state, detail):
        if link_id not in self.links:
            return
        self.link_states[link_id] = state
        self.log.emit(f"Link #{link_id} {state} {detail}".rstrip())
        self.linkStateChanged.emit(link_id, state, detail)
        self.linksChanged.emit()

    # ---- 기체 ----
    def vehicle_for(self, link_id, sysid):
        return self.vehicles.get((link_id, sysid))

    def vehicle_by_str(self, key_str):
        if not key_str:
            return None
        for v in self.vehicles.values():
            if v.key_str == key_str:
                return v
        return None

    def _on_system_discovered(self, link_id, sysid, compid, mav_type, autopilot):
        worker = self.links.get(link_id)
        if worker is None or (link_id, sysid) in self.vehicles:
            return

        # 같은 SYSID 가 다른 링크로 이미 들어와 있으면 (이중 링크) 경고만
        for (lid, sid) in self.vehicles:
            if sid == sysid:
                self.log.emit(
                    f"⚠ SYSID {sysid} seen on link #{lid} and #{link_id}. "
                    "기체마다 SYSID_THISMAV 를 다르게 설정하세요."
                )

        color = VEHICLE_COLORS[self._color_idx % len(VEHICLE_COLORS)]
        self._color_idx += 1
        v = Vehicle(worker, sysid, compid, mav_type, autopilot, color, parent=self)
        v.telemetryUpdated.connect(lambda _d, veh=v: self.vehicleTelemetry.emit(veh))
        v.heartbeatUpdated.connect(lambda _d, veh=v: self.vehicleTelemetry.emit(veh))
        self.vehicles[(link_id, sysid)] = v
        worker._pending.discard(sysid)
        v.start()
        self.log.emit(f"Vehicle discovered: SYSID {sysid} (comp {compid}) on {worker.human}")
        self.vehicleAdded.emit(v)

    def vehicle_list(self):
        return sorted(self.vehicles.values(), key=lambda v: (v.sysid, v.link.link_id))

    def _check_timeouts(self):
        now = time.time()
        for v in list(self.vehicles.values()):
            if v.connected and now - v.last_heartbeat > HEARTBEAT_TIMEOUT_S:
                v.mark_lost()
                self.vehicleTelemetry.emit(v)

    # ---- 그룹 명령 ----
    def for_all(self, fn_name: str, *args):
        for v in self.vehicle_list():
            if v.connected:
                try:
                    getattr(v, fn_name)(*args)
                except Exception as e:
                    self.log.emit(f"[{v.name}] {fn_name} error: {e}")


# ---------------------------------------------------------------------- #
# MainWindow 용 "선택 기체" 프록시                                       #
# ---------------------------------------------------------------------- #
class ActiveVehicleProxy(QObject):
    """
    기존 MainWindow / 탭 코드는 self.linker.<signal> / self.linker.<method>() 를 사용한다.
    이 프록시가 현재 선택된 Vehicle 로 그대로 전달해 주므로 기존 코드를 거의 수정하지 않아도 된다.
    """

    connectionStateChanged = pyqtSignal(bool)
    heartbeatUpdated = pyqtSignal(dict)
    attitudeUpdated = pyqtSignal(float, float, float)
    telemetryUpdated = pyqtSignal(dict)
    statusTextReceived = pyqtSignal(str)
    commandFeedback = pyqtSignal(str, bool, str)
    modeChanged = pyqtSignal(str)
    armedChanged = pyqtSignal(bool)
    activeChanged = pyqtSignal(object)   # 새 Vehicle 또는 None

    _SIGNALS = (
        "connectionStateChanged", "heartbeatUpdated", "attitudeUpdated",
        "telemetryUpdated", "statusTextReceived", "commandFeedback",
        "modeChanged", "armedChanged",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vehicle = None

    @property
    def vehicle(self):
        return self._vehicle

    @property
    def connected(self):
        return bool(self._vehicle is not None and self._vehicle.connected)

    def set_vehicle(self, vehicle):
        if vehicle is self._vehicle:
            return
        old = self._vehicle
        if old is not None:
            for name in self._SIGNALS:
                try:
                    getattr(old, name).disconnect(getattr(self, name))
                except TypeError:
                    pass
            # 다른 기체로 넘어가면 이전 기체의 가상 RC override 해제
            if old._rc_override_active:
                old.clear_virtual_rc()

        self._vehicle = vehicle
        if vehicle is not None:
            for name in self._SIGNALS:
                getattr(vehicle, name).connect(getattr(self, name))

        self.activeChanged.emit(vehicle)

        # 새 기체의 현재 상태를 즉시 UI 에 반영
        self.connectionStateChanged.emit(self.connected)
        if vehicle is not None:
            self.armedChanged.emit(vehicle.last_armed)
            self.modeChanged.emit(vehicle.last_mode)
            vehicle._update_telemetry()

    def __getattr__(self, name):
        # QObject 자체 속성이 아니면 선택 기체로 전달
        vehicle = self.__dict__.get("_vehicle")
        if vehicle is not None:
            return getattr(vehicle, name)
        if name.startswith("__"):
            raise AttributeError(name)

        def _noop(*_a, **_k):
            self.statusTextReceived.emit(f"No vehicle selected ({name}).")
            return None
        return _noop
