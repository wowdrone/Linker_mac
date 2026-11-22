### Linker.py
# MAVLink 연결 / 텔레메트리 / 명령 전송 담당 모듈
# PyQt5 + pymavlink 기반

import math
import time

from PyQt5.QtCore import QObject, pyqtSignal, QThread
from pymavlink import mavutil
from health_telemetry import HealthTelemetry
# ★ PyInstaller가 dialect 파일을 같이 싸서 넣도록 강제 import
from pymavlink.dialects.v20 import ardupilotmega as _mavlink_ardupilotmega  # noqa: F401


def _haversine(lat1, lon1, lat2, lon2):
    """두 위경도 사이 거리(m) 계산"""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# ---------------------------------------------------------------------- #
# 수신 스레드                                                            #
# ---------------------------------------------------------------------- #
class MavReader(QThread):
    """수신 전용 스레드: 받은 msg 를 Linker.handle_message 로 넘겨줌"""

    def __init__(self, master, linker):
        super().__init__(linker)
        self.master = master
        self.linker = linker
        self._running = True

    def run(self):
        while self._running and self.master is not None:
            try:
                msg = self.master.recv_match(blocking=True, timeout=1)
            except Exception:
                continue

            if msg is None:
                continue

            try:
                self.linker.handle_message(msg)
            except Exception as e:
                self.linker.statusTextReceived.emit(f"Message error: {e}")

    def stop(self):
        self._running = False
        try:
            self.wait(1000)
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# 메인 MAVLink 핸들러                                                    #
# ---------------------------------------------------------------------- #
class Linker(QObject):
    """
    메인 MAVLink 핸들러 클래스
    main_window.MainWindow 에서 한 번 생성해서 사용.
    """

    connectionStateChanged = pyqtSignal(bool)
    heartbeatUpdated = pyqtSignal(dict)
    attitudeUpdated = pyqtSignal(float, float, float)
    telemetryUpdated = pyqtSignal(dict)
    statusTextReceived = pyqtSignal(str)
    commandFeedback = pyqtSignal(str, bool, str)
    modeChanged = pyqtSignal(str)
    armedChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        # MAVLink 연결 상태
        self.master = None
        self.reader = None
        self.connected = False

        # 타겟 시스템/컴포넌트 (HEARTBEAT 수신 시 갱신)
        self.target_system = 1
        self.target_component = 1

        # ARM / 모드 상태
        self._armed = False
        self._mode = "UNKNOWN"

        # 모드 매핑 (GUIDED / AUTO 등)
        self.mode_mapping = {}

        # 텔레메트리 기본값
        self.altitude = 0.0
        self.groundspeed = 0.0
        self.yaw_deg = 0.0
        self.vert_speed = 0.0
        self.dist_to_home = 0.0
        self.dist_to_mav = 0.0

        self.home_lat = None
        self.home_lon = None
        self.last_lat = None
        self.last_lon = None

        # 추가 정보 (GPS, 배터리, FC 온도 등 - 향후 확장용)
        self.gps_status = "GPS?"
        self.battery_v = None
        self.battery_percent = None
        self.fc_temp = None
        self.calib_status = "N/A"

        # Health 탭용 확장 필드
        self.vibe_x = None
        self.vibe_y = None
        self.vibe_z = None
        self.vibe_clip0 = None
        self.vibe_clip1 = None
        self.vibe_clip2 = None
        self.gps_sats = None
        self.gps_hdop = None
        self.gps_fix_type = None
        self.radio_rssi = None
        self.radio_remrssi = None
        self.health = HealthTelemetry()

        # ★ 해발 기준 홈 고도 (VFR_HUD에서 첫 alt)
        self.home_alt = None

        # ★ 미션 아이템 캐시 (MISSION_REQUEST 응답용)
        self._mission_items = []
        self._mission_requested = set()
        self.landed_state = None
        
        # ---------- Virtual RC (AI Tuner용) ----------
        # 8채널 PWM 값 저장 (0 이면 override 안 보냄)
        self._rc_override = [0] * 8
        self._rc_override_active = False
        
        from PyQt5.QtCore import QTimer
        self._rc_override_timer = QTimer()
        self._rc_override_timer.setInterval(100)   # 10 Hz
        self._rc_override_timer.timeout.connect(self._on_rc_override_timer)
        self._rc_override_timer.start()

    # ------------------------------------------------------------------ #
    # 파라미터 읽기 / 쓰기                                               #
    # ------------------------------------------------------------------ #
    def get_param(self, name: str):
        """PARAM_REQUEST_READ + PARAM_VALUE 응답을 이용하여 파라미터를 읽는다."""
        if not self.connected or self.master is None:
            self.commandFeedback.emit("GET_PARAM", False, "Not connected")
            return None

        try:
            self.master.mav.param_request_read_send(
                self.target_system,
                self.target_component,
                name.encode("utf-8"),
                -1,
            )
            # 응답 기다리기 (타임아웃 1초 예시)
            t0 = mavutil.time.time()
            while mavutil.time.time() - t0 < 1.0:
                msg = self.master.recv_match(type="PARAM_VALUE", blocking=False)
                if msg is None:
                    continue
                if msg.param_id.decode("ascii").strip("\x00") == name:
                    value = msg.param_value
                    self.commandFeedback.emit(
                        "GET_PARAM", True, f"{name}={value}"
                    )
                    return int(value)
            self.commandFeedback.emit(
                "GET_PARAM", False, f"{name} read timeout"
            )
            return None
        except Exception as e:
            self.commandFeedback.emit(
                "GET_PARAM", False, f"Failed to read {name}: {e}"
            )
            return None

    def set_param(self, name: str, value, param_type=None):
        """
        MAVLink PARAM_SET 사용해서 파라미터 설정
        (PreArm ARMING_CHECK 포함, 공통으로 사용)
        """
        if not getattr(self, "connected", False):
            self.commandFeedback.emit("SET_PARAM", False, "Not connected")
            return

        if self.master is None:
            self.commandFeedback.emit("SET_PARAM", False, "master is None")
            return

        if param_type is None:
            # 기본값: int32
            param_type = mavutil.mavlink.MAV_PARAM_TYPE_INT32

        try:
            self.master.mav.param_set_send(
                self.target_system,
                self.target_component,
                name.encode("utf-8"),
                float(value),
                param_type,
            )
            self.commandFeedback.emit("SET_PARAM", True, f"{name} set to {value}")
        except Exception as e:
            self.commandFeedback.emit(
                "SET_PARAM", False, f"Failed to set {name}: {e}"
            )

    # ------------------------------------------------------------------ #
    # 내부 공통: 모드 변경 헬퍼                                          #
    # ------------------------------------------------------------------ #
    def _ensure_mode_mapping(self):
        """
        self.mode_mapping 이 비어있으면 master.mode_mapping() 으로 채움.
        실패하면 빈 dict 유지.
        """
        if self.master is None:
            return

        if self.mode_mapping:
            return

        try:
            self.mode_mapping = self.master.mode_mapping()
            if not isinstance(self.mode_mapping, dict):
                self.mode_mapping = {}
        except Exception as e:
            self.statusTextReceived.emit(f"Mode mapping failed: {e}")
            self.mode_mapping = {}

    def _set_mode(self, mode_name: str):
        """
        주어진 모드명(GUIDED, AUTO 등)으로 모드 변경 시도.
        - 우선 master.set_mode() 사용
        - 실패 시 MAV_CMD_DO_SET_MODE + mode_mapping 사용
        """
        if not self.master:
            self.statusTextReceived.emit("No connection for set_mode.")
            return

        mode_name = (mode_name or "").upper()
        if not mode_name:
            self.statusTextReceived.emit("Empty mode name for set_mode.")
            return

        # 편의 함수 우선 시도
        try:
            self.statusTextReceived.emit(f"Mode change (set_mode) to {mode_name}...")
            self.master.set_mode(mode_name)
            return
        except Exception:
            pass

        # mode_mapping 기반 Fallback
        self._ensure_mode_mapping()
        if not self.mode_mapping:
            self.statusTextReceived.emit("Mode mapping is empty; cannot set_mode.")
            return

        mode_id = self.mode_mapping.get(mode_name)
        if mode_id is None:
            self.statusTextReceived.emit(f"Mode {mode_name} not in mode_mapping.")
            return

        try:
            self.statusTextReceived.emit(
                f"Mode change (MAV_CMD_DO_SET_MODE) to {mode_name} (id={mode_id})..."
            )
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                mode_id,
                0, 0, 0, 0, 0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"Mode change failed: {e}")

    # 외부에서 호출하는 모드 변경 API
    def set_mode_guided(self):
        """GUIDED 모드로 전환"""
        self._set_mode("GUIDED")

    def set_mode_auto(self):
        """AUTO 모드로 전환 (미션 모드)"""
        self._set_mode("AUTO")

    # ------------------------------------------------------------------ #
    # 연결                                                               #
    # ------------------------------------------------------------------ #
    def _start_default_streams(self, rate_hz: int = 10):
        """
        FC 쪽 SRx_* 값이 0이어도 기본 HUD/ATTITUDE/GPS 데이터를 보내도록
        MAVLink DATA_STREAM 을 요청한다.
        """
        if self.master is None:
            return

        try:
            # 1) 전체 스트림 요청
            self.master.mav.request_data_stream_send(
                self.target_system,
                self.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL,
                rate_hz,
                1,  # start
            )

            # 2) 일부 펌웨어가 ALL 을 무시하는 경우를 대비해 주요 스트림 개별 요청
            for stream_id in (
                mavutil.mavlink.MAV_DATA_STREAM_EXTENDED_STATUS,  # VFR_HUD 등
                mavutil.mavlink.MAV_DATA_STREAM_POSITION,         # GLOBAL_POSITION_INT
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA1,           # ATTITUDE
                mavutil.mavlink.MAV_DATA_STREAM_RC_CHANNELS,
                mavutil.mavlink.MAV_DATA_STREAM_RAW_CONTROLLER,
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA3,
            ):
                self.master.mav.request_data_stream_send(
                    self.target_system,
                    self.target_component,
                    stream_id,
                    rate_hz,
                    1,
                )

            self.statusTextReceived.emit("Requested MAVLink data streams.")
        except Exception as e:
            self.statusTextReceived.emit(f"Stream request failed: {e}")
        self.request_health_streams()

    def request_health_streams(self):
        """Request individual status messages without changing FC parameters."""
        if self.master is None:
            return
        for message_id, hz in ((241, 5), (24, 2), (109, 2), (193, 2),
                               (230, 2), (65, 5), (36, 5)):
            try:
                self.master.mav.command_long_send(
                    self.target_system, self.target_component,
                    mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    message_id, int(1_000_000 / hz), 0, 0, 0, 0, 0,
                )
            except Exception as exc:
                self.statusTextReceived.emit(f"Message {message_id} request failed: {exc}")
        self.statusTextReceived.emit("Requested Health / EKF / RC input / output streams.")

    def connect_serial(self, port, baud=115200):
        """
        시리얼 포트로 MAVLink 연결
        :param port: '/dev/cu.usbmodem1' 같은 포트 이름
        :param baud: 57600, 115200 등
        """
        # 기존 연결 정리
        self.disconnect()

        try:
            self.statusTextReceived.emit(f"Connecting SERIAL {port} @ {baud}...")
            # pymavlink 가 내부에서 pyserial 로 포트 연다
            self.master = mavutil.mavlink_connection(
                port,
                baud=baud,
                dialect="ardupilotmega",
                autoreconnect=True,
            )

            # HEARTBEAT 기다리기
            hb = self.master.wait_heartbeat(timeout=10)
            sysid = hb.get_srcSystem()
            compid = hb.get_srcComponent()
            self.statusTextReceived.emit(
                f"Heartbeat from system {sysid}, component {compid}"
            )

        except Exception as e:
            self.master = None
            self.connected = False
            self.connectionStateChanged.emit(False)
            self.statusTextReceived.emit(f"Serial connect failed: {e}")
            return

        # 타겟 시스템/컴포넌트 ID 설정
        self.target_system = self.master.target_system or sysid or 1
        self.target_component = self.master.target_component or compid or 1

        # 모드 매핑 초기화
        self.mode_mapping = {}
        self._ensure_mode_mapping()

        # 기본 데이터 스트림 요청
        self._start_default_streams(rate_hz=10)

        # 수신 스레드 시작
        self._start_reader()
        self.connected = True
        self.connectionStateChanged.emit(True)
        self.statusTextReceived.emit("Serial connected.")

    def connect_udp(self, address: str = "127.0.0.1:14550"):
        """
        UDP 소켓으로 MAVLink 연결
        :param address: "ip:port" 형식
        """
        self.disconnect()

        try:
            self.statusTextReceived.emit(f"Connecting UDP {address}...")
            self.master = mavutil.mavlink_connection(f"udp:{address}")
            hb = self.master.wait_heartbeat(timeout=10)
            sysid = hb.get_srcSystem()
            compid = hb.get_srcComponent()
            self.statusTextReceived.emit(
                f"Heartbeat from system {sysid}, component {compid}"
            )
        except Exception as e:
            self.statusTextReceived.emit(f"UDP connect failed: {e}")
            self.master = None
            self.connected = False
            self.connectionStateChanged.emit(False)
            return

        # 타겟 시스템/컴포넌트 ID 설정
        if self.master.target_system != 0:
            self.target_system = self.master.target_system
        else:
            self.target_system = sysid or 1

        if self.master.target_component != 0:
            self.target_component = self.master.target_component
        else:
            self.target_component = compid or 1

        # 모드 매핑 초기화
        self.mode_mapping = {}
        self._ensure_mode_mapping()

        # 기본 데이터 스트림 요청
        self._start_default_streams(rate_hz=10)

        # 수신 스레드 시작
        self._start_reader()
        self.connected = True
        self.connectionStateChanged.emit(True)
        self.statusTextReceived.emit("UDP connected.")

    def _start_reader(self):
        if self.master is None:
            return
        self.reader = MavReader(self.master, self)
        self.reader.start()

    def disconnect(self):
        """연결 종료 및 리소스 정리"""
        # 리더 스레드 정리
        if self.reader is not None:
            try:
                self.reader.stop()
            except Exception:
                pass
            self.reader = None

        # MAVLink master 닫기
        if self.master is not None:
            try:
                self.master.close()
            except Exception:
                pass
            self.master = None

        self.connected = False
        self.connectionStateChanged.emit(False)
        self.statusTextReceived.emit("Disconnected.")

    # ------------------------------------------------------------------ #
    # 명령 (ARM, DISARM, TAKEOFF, LAND, RTL)                             #
    # ------------------------------------------------------------------ #
    def arm(self):
        if not self.master:
            self.statusTextReceived.emit("No connection for ARM.")
            return

        self.statusTextReceived.emit("ARM command sending...")
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                1, 0, 0, 0, 0, 0, 0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"ARM send error: {e}")

    def disarm(self):
        if not self.master:
            self.statusTextReceived.emit("No connection for DISARM.")
            return

        self.statusTextReceived.emit("DISARM command sending...")
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                0, 0, 0, 0, 0, 0, 0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"DISARM send error: {e}")

    def takeoff(self, altitude: float = 10.0):
        if not self.master:
            self.statusTextReceived.emit("No connection for TAKEOFF.")
            return

        self.statusTextReceived.emit(f"TAKEOFF to {altitude:.1f} m command sending...")
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                0,
                0, 0, 0, 0,
                0, 0,
                altitude,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"TAKEOFF send error: {e}")

    def land(self):
        if not self.master:
            self.statusTextReceived.emit("No connection for LAND.")
            return

        self.statusTextReceived.emit("LAND command sending...")
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_NAV_LAND,
                0,
                0, 0, 0, 0,
                0, 0,
                0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"LAND send error: {e}")

    def rtl(self):
        if not self.master:
            self.statusTextReceived.emit("No connection for RTL.")
            return

        self.statusTextReceived.emit("RTL command sending...")
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
                0,
                0, 0, 0, 0,
                0, 0,
                0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"RTL send error: {e}")

    # ------------------------------------------------------------------ #
    # 미션 업로드 / 시작                                                 #
    # ------------------------------------------------------------------ #
    def upload_mission(self, waypoints):
        """
        MainWindow 에서 넘어온 waypoints 리스트를 미션으로 업로드.
        각 항목 예:
        {
            "seq": 0,
            "lat": ...,
            "lon": ...,
            "alt": ...,
            "speed": ...,
            "heading": ...
        }
        """
        if not self.connected or self.master is None:
            self.commandFeedback.emit("UPLOAD_MISSION", False, "Not connected")
            return

        if not waypoints:
            self.commandFeedback.emit("UPLOAD_MISSION", False, "No waypoints")
            return

        self._mission_items = []
        self._mission_requested = set()
        
        # 0번: 홈/이륙 지점(dummy) 추가 (선택사항)
        # 홈 GPS 를 알고 있으면 그걸 쓰고, 없으면 첫 WP 좌표 사용
        if self.home_lat is not None and self.home_lon is not None:
            home_lat = self.home_lat
            home_lon = self.home_lon
        else:
            home_lat = float(waypoints[0].get("lat", 0.0))
            home_lon = float(waypoints[0].get("lon", 0.0))

        home_alt = float(waypoints[0].get("alt", 50.0))

        if getattr(self, 'autopilot', None) != mavutil.mavlink.MAV_AUTOPILOT_PX4:
            self._mission_items.append(
            dict(
                seq=0,
                frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                command=mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,   # 또는 MAV_CMD_NAV_TAKEOFF
                current=0,
                autocontinue=1,
                param1=0.0,     # hold time
                param2=5.0,     # acceptance radius
                param3=0.0,
                param4=0.0,     # heading
                lat=home_lat,
                lon=home_lon,
                alt=home_alt,
            )
            )

        # 1~N번: 테이블에 있는 WP 들

        for idx, wp in enumerate(waypoints):
            lat = float(wp.get("lat", 0.0))
            lon = float(wp.get("lon", 0.0))
            alt = float(wp.get("alt", 50.0))
            heading = float(wp.get("heading", 0.0))

            item = dict(
                seq=len(self._mission_items),
                frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                command=wp.get('command', mavutil.mavlink.MAV_CMD_NAV_WAYPOINT),
                current=0,          # ★ 모든 WP 를 0으로
                autocontinue=1,
                param1=0.0,    # hold time
                param2=2.0,    # acceptance radius
                param3=0.0,    # pass radius
                param4=heading,
                lat=lat,
                lon=lon,
                alt=alt,
            )
            self._mission_items.append(item)

        try:
            # MISSION_COUNT begins replacement; a separate clear ACK is ambiguous.
            self.master.mav.mission_count_send(
                self.target_system,
                self.target_component,
                len(self._mission_items),
            )
            self.statusTextReceived.emit(f"Mission upload started ({len(self._mission_items)} items)")
        except Exception as e:
            self.commandFeedback.emit(
                "UPLOAD_MISSION", False,
                f"Mission upload failed: {e}"
            )

    def start_mission(self, first_seq: int = 0):
        """업로드된 미션을 AUTO 모드에서 시작"""
        if not self.master:
            self.statusTextReceived.emit("No connection for mission start.")
            return

        self.statusTextReceived.emit(f"Mission start at seq={first_seq}")
        try:
            # MISSION_START 전송
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_MISSION_START,
                0,
                first_seq, 0, 0, 0, 0, 0, 0,
            )
        except Exception as e:
            self.statusTextReceived.emit(f"Mission start send error: {e}")

    # ------------------------------------------------------------------ #
    # 내부 상태 갱신                                                     #
    # ------------------------------------------------------------------ #
    def _update_telemetry(self):
        data = dict(
            altitude=self.altitude,
            groundspeed=self.groundspeed,
            
            # ★ AHRS에서 넘어온 yaw 값을 여러 이름으로 같이 넣어줌
            yaw_deg=self.yaw_deg,                   # 기존
            yaw=self.yaw_deg,              # 혹시 yaw 이름을 쓰는 코드용
            heading=self.yaw_deg,                   # 지도 헤딩 지시선용
            
            vert_speed=self.vert_speed,
            dist_to_home=self.dist_to_home,
            dist_to_mav=self.dist_to_mav,
            lat=self.last_lat,
            lon=self.last_lon,
            gps_status=self.gps_status,
            battery_v=self.battery_v,
            battery_percent=self.battery_percent,
            fc_temp=self.fc_temp,
            calib_status=self.calib_status,
            # Health 탭용
            vibe_x=self.vibe_x,
            vibe_y=self.vibe_y,
            vibe_z=self.vibe_z,
            vibe_clip0=self.vibe_clip0,
            vibe_clip1=self.vibe_clip1,
            vibe_clip2=self.vibe_clip2,
            gps_sats=self.gps_sats,
            gps_hdop=self.gps_hdop,
            gps_fix_type=self.gps_fix_type,
            radio_rssi=self.radio_rssi,
            radio_remrssi=self.radio_remrssi,
            health_samples=self.health.snapshot(),
        )
        self.telemetryUpdated.emit(data)

    def _update_armed_mode(self, armed: bool, mode: str):
        if armed != self._armed:
            self._armed = armed
            self.armedChanged.emit(armed)
        if mode != self._mode:
            self._mode = mode
            self.modeChanged.emit(mode)

    # ------------------------------------------------------------------ #
    # 메시지 처리                                                        #
    # ------------------------------------------------------------------ #
    def handle_message(self, msg):
        mtype = msg.get_type()
        if self.health.consume(msg):
            for name, value in self.health.legacy.items():
                setattr(self, name, value)
            self._update_telemetry()

        if mtype == "HEARTBEAT":
            # 시스템/컴포넌트 ID 갱신
            try:
                self.target_system = msg.get_srcSystem()
                self.target_component = msg.get_srcComponent()
            except Exception:
                pass

            mode = mavutil.mode_string_v10(msg)
            armed = bool(
                msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            self._update_armed_mode(armed, mode)

            hb = dict(
                type=msg.type,
                autopilot=msg.autopilot,
                base_mode=msg.base_mode,
                system_status=msg.system_status,
                armed=armed,
                mode=mode,
            )
            self.heartbeatUpdated.emit(hb)

        elif mtype == "VFR_HUD":
            raw_alt = float(msg.alt)   # FC가 주는 해발 고도(MSL)

            # 처음 들어온 값을 홈 고도로 저장
            if self.home_alt is None:
                self.home_alt = raw_alt

            # 표시용 고도는 "현재 해발 - 홈 해발" → 지상에서 0m
            rel_alt = raw_alt - self.home_alt

            self.altitude = rel_alt
            self.groundspeed = float(msg.groundspeed)
            self.vert_speed = float(msg.climb)
            self.yaw_deg = float(msg.heading)
            self._update_telemetry()

        elif mtype == "GLOBAL_POSITION_INT":
            lat = msg.lat / 1e7
            lon = msg.lon / 1e7
            self.last_lat = lat
            self.last_lon = lon

            # 홈 위치 초기화
            if self.home_lat is None and lat != 0 and lon != 0:
                self.home_lat = lat
                self.home_lon = lon

            # Dist to home 계산
            if self.home_lat is not None:
                self.dist_to_home = _haversine(
                    self.home_lat, self.home_lon, lat, lon
                )
            else:
                self.dist_to_home = 0.0

            # 여기서는 dist_to_mav 를 dist_to_home 과 동일하게 사용
            self.dist_to_mav = self.dist_to_home
            self._update_telemetry()

        elif mtype == "ATTITUDE":
            roll = math.degrees(msg.roll)
            pitch = math.degrees(msg.pitch)
            yaw = math.degrees(msg.yaw)
            self.attitudeUpdated.emit(roll, pitch, yaw)

        elif mtype == "EXTENDED_SYS_STATE":
            self.landed_state = msg.landed_state

        elif mtype == "COMMAND_ACK":
            cmd_id = msg.command
            result = msg.result

            cmd_name = f"CMD_{cmd_id}"
            enums = mavutil.mavlink.enums.get("MAV_CMD")
            if enums and cmd_id in enums:
                cmd_name = enums[cmd_id].name

            res_enum = mavutil.mavlink.enums.get("MAV_RESULT")
            result_text = f"result={result}"
            if res_enum and result in res_enum:
                result_text = res_enum[result].name

            success = result == mavutil.mavlink.MAV_RESULT_ACCEPTED
            message = f"{cmd_name}: {result_text}"
            self.commandFeedback.emit(cmd_name, success, message)

        elif mtype in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
            seq = msg.seq
            if not self._mission_items or seq < 0 or seq >= len(self._mission_items):
                self.statusTextReceived.emit(
                    f"Mission request for invalid seq {seq}"
                )
                return

            wp = self._mission_items[seq]
            frame = wp["frame"]
            command = wp["command"]
            current = wp["current"]
            autocontinue = wp["autocontinue"]
            p1 = wp["param1"]
            p2 = wp["param2"]
            p3 = wp["param3"]
            p4 = wp["param4"]
            lat = wp["lat"]
            lon = wp["lon"]
            alt = wp["alt"]

            try:
                if mtype == "MISSION_REQUEST_INT":
                    self.master.mav.mission_item_int_send(
                        self.target_system,
                        self.target_component,
                        seq,
                        frame,
                        command,
                        current,
                        autocontinue,
                        p1, p2, p3, p4,
                        int(lat * 1e7),
                        int(lon * 1e7),
                        alt,
                    )
                else:
                    self.master.mav.mission_item_send(
                        self.target_system,
                        self.target_component,
                        seq,
                        frame,
                        command,
                        current,
                        autocontinue,
                        p1, p2, p3, p4,
                        lat,
                        lon,
                        alt,
                    )
                self._mission_requested.add(seq)
                self.statusTextReceived.emit(f"Mission item {seq} sent.")
            except Exception as e:
                self.statusTextReceived.emit(f"Mission item {seq} send error: {e}")

        elif mtype == "MISSION_ACK":
            # 업로드가 끝나면 들어오는 확인 메시지
            try:
                result = msg.type  # ArduPilot 는 type 필드 사용
            except Exception:
                result = getattr(msg, "result", 0)

            enums = mavutil.mavlink.enums.get("MAV_MISSION_RESULT")
            if enums and result in enums:
                res_text = enums[result].name
            else:
                res_text = f"result={result}"

            requested_seq = set(range(len(self._mission_items)))
            success = (result == mavutil.mavlink.MAV_MISSION_ACCEPTED and
                       self._mission_requested == requested_seq and
                       bool(self._mission_items))
            if result == mavutil.mavlink.MAV_MISSION_ACCEPTED and not success:
                res_text += ' (미요청 미션 항목 있음)'
            self.commandFeedback.emit(
                "UPLOAD_MISSION", success, f"MISSION_ACK: {res_text}"
            )

        elif mtype == "STATUSTEXT":
            try:
                txt = msg.text
                if isinstance(txt, bytes):
                    txt = txt.decode("utf-8", "ignore")
            except Exception:
                txt = str(msg)
            self.statusTextReceived.emit(txt)
        # VIBRATION, GPS_RAW_INT, RADIO_STATUS 등은 나중에 확장 가능
        
    # -1.0 ~ +1.0 범위의 가상 스틱 값을 PWM 1000~2000 으로 변환
    # (0.0 → 1500, -1.0 → 1000, +1.0 → 2000)
    def _stick_to_pwm(self, v: float) -> int:
        try:
            v = float(v)
        except Exception:
            v = 0.0
        if v > 1.0:
            v = 1.0
        if v < -1.0:
            v = -1.0
        return int(1500 + v * 500)
    
    def _send_rc_override_now(self):
        """
        _rc_override 배열 내용을 즉시 RC_CHANNELS_OVERRIDE 로 전송.
        self.master 가 pymavlink 연결 객체라고 가정.
        """
        if not getattr(self, "connected", False):
            return
        master = getattr(self, "master", None)
        if master is None or master.mav is None:
            return

        ch = self._rc_override

        # pymavlink 는 target_system / target_component 를 속성으로 가지고 있음
        try:
            master.mav.rc_channels_override_send(
                master.target_system,
                master.target_component,
                ch[0], ch[1], ch[2], ch[3],
                ch[4], ch[5], ch[6], ch[7],
            )
        except Exception as e:
            # 필요하면 로그 신호로 넘겨도 됨
            print(f"RC override send error: {e}")
            
    def _on_rc_override_timer(self):
        """
        10 Hz 타이머. override 가 활성화되어 있으면 계속 재전송.
        """
        if self._rc_override_active:
            self._send_rc_override_now()
            
    # ---------- 외부에서 쓸 API ---------        
    def send_virtual_rc(self, roll=None, pitch=None, throttle=None, yaw=None):
        """
        가상 조이스틱에서 들어온 값을 -1.0~+1.0 범위로 받아
        ArduCopter RC 채널로 매핑해서 RC override 를 보낸다.

        채널 매핑(일반적인 Mode2 기준):
          ch1: roll
          ch2: pitch
          ch3: throttle
          ch4: yaw
        나머지 채널은 그대로 유지.
        """
        # 아직 연결 안되었으면 무시
        if not getattr(self, "connected", False):
            return

        # 기본: 이전 값 유지, None 이 아닌 것만 갱신
        # roll / pitch / throttle / yaw 모두 -1.0~+1.0 을 기대
        if roll is not None:
            self._rc_override[0] = self._stick_to_pwm(roll)
        if pitch is not None:
            self._rc_override[1] = self._stick_to_pwm(pitch)
        if throttle is not None:
            self._rc_override[2] = self._stick_to_pwm(throttle)
        if yaw is not None:
            self._rc_override[3] = self._stick_to_pwm(yaw)

        # 최소 한 번이라도 override를 보낸 상태 표시
        self._rc_override_active = True

        # 즉시 전송 (추후 타이머로 10Hz 재전송 추가해도 됨)
        self._send_rc_override_now()

    def clear_virtual_rc(self):
        """
        GCS가 걸어둔 RC override 를 모두 해제.
        0 을 보내면 해당 채널 override 없음(FC가 원래 RC 입력 사용).
        """
        self._rc_override = [0] * 8
        self._rc_override_active = False

        if not getattr(self, "connected", False):
            return

        master = getattr(self, "master", None)
        if master is None or master.mav is None:
            return

        try:
            master.mav.rc_channels_override_send(
                master.target_system,
                master.target_component,
                0, 0, 0, 0, 0, 0, 0, 0
            )
        except Exception as e:
            print(f"RC override clear error: {e}")

        
    


    
