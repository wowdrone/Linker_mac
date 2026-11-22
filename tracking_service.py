"""Tracking service: the single owner of Tracking state on the Qt thread.

- One TrackingSession, shared by the PyQt Tracking view and the browser dashboard.
- Video comes from the existing _SharedCapture pool: the same source decoded once for
  Fly / Video 4CH / Tracking. No separate RTSP connection is opened here.
- The selected Fleet Vehicle is read only. Flight mode changes go exclusively through the
  FlightModeController after explicit operator confirmation. Tracking never commands the
  vehicle: no RC, attitude, position or velocity output, no arming, no takeoff.
- Browser requests are validated by the HTTP layer, queued, and executed here in tick().
- Independent of the Sentinel Legacy (VehicleSession) lifecycle: nothing here starts it.
"""
from collections import deque
import math
import re
import time
import uuid

import cv2
import numpy as np
from PyQt5.QtCore import QObject, QSettings, QTimer, pyqtSignal

from flight_mode_controller import FlightModeController, ModeRequestError, firmware_name, mode_options
from tracking_models import FrameEnvelope
from tracking_session import TrackingSession, frame_ref
from video_grid import _SharedCapture, direct_rtsp_connections, source_route

STALE_S = 3.0             # telemetry sample age that is shown as STALE
TICK_MS = 33
WEB_FRAME_S = .1          # at most 10 JPEG encodes per second, only while a browser is reading
WEB_STATE_S = .2
FIX_TEXT = {0: "GPS 없음", 1: "No Fix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}
REASON_TEXT = {
    "awaiting_roi": "ROI 선택 대기", "operator_stop": "사용자 정지", "draw_roi": "ROI 그리기 대기",
    "no_frame": "영상 프레임 없음", "roi_pending": "ROI 초기화 중", "roi_selected": "ROI 초기화 완료",
    "flow_updated": "광류 추적 중", "frame_timeout": "새 프레임 없음(시간 초과)",
    "frame_or_worker_timeout": "영상 지연·단절 또는 처리 지연", "processing_timeout": "처리 지연",
    "low_quality": "특징점 품질 저하", "incoherent_motion": "특징점 이동 불일치", "incoherent_affine": "형상 변화 불일치",
    "scale_jump": "급격한 크기 변화", "out_of_frame": "영상 범위 이탈", "feature_attrition": "특징점 잔존 비율 저하",
    "insufficient_texture": "ROI 텍스처 부족", "invalid_roi_or_frame": "ROI/프레임 오류",
    "invalid_frame_time": "프레임 시각 오류", "out_of_order_frame": "프레임 순서 역전",
    "resolution_changed": "해상도 변경", "source_session_or_resolution_changed": "영상 소스·세션·해상도 변경",
    "source_disconnected": "영상 해제", "vehicle_changed": "선택 기체 변경", "operator_intervention": "조종자 개입",
    "app_shutdown": "앱 종료", "panel_closed": "Tracking 창 닫힘", "camera_mode_changed": "카메라 장착 방식 변경",
    "selection_frame_expired_reselect": "선택 시간 초과 · 최신 프레임 재고정",
    "selection_stale_redraw_latest": "따라잡기 시간 초과 · 최신 프레임 재고정",
    "catchup_failed_redraw_latest": "따라잡기 실패 · 최신 프레임 재고정",
    "invalid_roi": "ROI 크기·위치 오류",
    "operator_revoke": "모의 출력 해제", "pixhawk_stale": "기체 HEARTBEAT stale",
    "pixhawk_state_or_command_event": "기체 상태·명령 이벤트", "operator_rc_override": "RC override 활성",
    "vehicle_mode_or_armed_changed": "기체 모드·시동 변경", "closed": "종료됨",
    "roi_frame_mismatch_reselect": "다른 프레임의 ROI · 최신 프레임 재고정", "selection_not_active": "ROI 선택 상태 아님",
    "selection_cancelled": "ROI 선택 취소", "timeout_changed": "timeout 변경", "invalid_clock": "시계 오류",
    "flow_failed": "광류 계산 실패", "processing_error": "처리 오류", "source_changed": "영상 소스 변경",
}


def mask_url(text):
    """Hide credentials in URLs before showing or logging them."""
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]*@", r"\1***@", str(text or ""))


def reason_text(reason):
    reason = str(reason or "")
    if reason.startswith("worker_exception:"):
        return "워커 오류 · " + reason.split(":", 1)[1]
    return REASON_TEXT.get(reason, reason)


def _num(value, digits=2):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


class TrackingService(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None, legacy_status=None, timeout=.3):
        super().__init__(parent)
        self.session = TrackingSession(timeout)
        self.legacy_status = legacy_status or (lambda: None)
        self.capture = None
        self.source_text, self.transport = "", "tcp"
        settings = QSettings("WowDrone", "LinkerGCS")
        # Reuse the operator's existing, known-good Fly / CH0 settings. Never assume a preset is HM30.
        self.configured_source = (settings.value("video/fly/url", "", str)
                                  or settings.value("video/ch0/url", "", str) or "")
        self.configured_transport = settings.value("video/ch0/transport", "tcp", str) or "tcp"
        self.mount = "기체 고정 · 비짐벌"
        self.demo = False
        self.demo_session = None
        self.demo_seq = 0
        self.vehicle = None
        self._vehicle_mode = None
        self.modes = FlightModeController(selected=lambda: self.vehicle)
        self.recent = deque(maxlen=8)
        self.receipts = deque(maxlen=60)
        self.positions = deque(maxlen=300)
        self.events = deque(maxlen=200)
        self.last_key = None
        self._last_track = None
        self._last_mode_state = None
        self.web_result = "대기"
        self.link_warnings = []
        self._last_link_warnings = ()
        self._last_health_warning = None
        self.dashboard = None
        self._next_web_frame = self._next_web_state = 0.
        self._web_frame_key = None
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.log_event("앱", "Tracking 서비스 시작 · MONITOR ONLY · 영상·기체 자동 연결 없음")

    # ------------------------------------------------------------------ events
    def log_event(self, kind, text, level="info"):
        # Not named event(): that would override QObject.event(QEvent) and crash on any Qt event.
        self.events.appendleft(dict(t=time.strftime("%H:%M:%S"), kind=kind, text=text, level=level))

    # ------------------------------------------------------------------ video
    @property
    def source_label(self):
        if self.demo:
            return "합성 모의 영상"
        return mask_url(self.source_text) or "—"

    def connect_source(self, source=None, transport=None):
        source = (self.configured_source if source is None else str(source)).strip()
        transport = transport if transport in ("tcp", "udp") else self.configured_transport
        self.disconnect_source("source_changed")
        if not source:
            self.log_event("영상", "영상 주소 없음 · 연결하지 않음", "warn")
            return False
        self.source_text, self.transport = source, transport
        self.capture = _SharedCapture.acquire(source, transport)
        self.log_event("영상", f"영상 연결 요청 · {mask_url(source)} · {transport}")
        return True

    def disconnect_source(self, reason="source_disconnected"):
        had = self.capture is not None or self.demo
        self.session.clear_source(reason)
        self.demo = False
        if self.capture:
            self.capture.release()
            self.capture = None
        self.last_key = None
        self.recent.clear()
        self.receipts.clear()
        if had:
            self.log_event("영상", "영상 해제 · 추적 세션 무효화")

    def start_demo(self):
        self.disconnect_source("source_changed")
        self.demo = True
        self.demo_session = uuid.uuid4().hex
        self.demo_seq = 0
        self.texture = np.random.default_rng(42).integers(0, 256, (90, 100, 3), np.uint8)
        self.log_event("모의", "합성 모의 영상 시작 · SIMULATION · 실제 영상·기체와 무관")

    def _demo_frame(self):
        pixels = np.zeros((480, 640, 3), np.uint8)
        pixels[:, :, 1] = 24
        x = 120 + int(80 * np.sin(self.demo_seq / 60))
        pixels[180:270, x:x + 100] = self.texture
        self.demo_seq += 1
        return FrameEnvelope.create("synthetic", self.demo_session, self.demo_seq, pixels, time.monotonic())

    def change_timeout(self, value):
        old = self.session
        self.session = TrackingSession(float(value), old.selection_window)
        old.close()
        self.last_key = None
        self.log_event("추적", f"timeout {float(value):.2f}s 적용 · 이전 추적 세션 종료")

    def set_selection_window(self, seconds):
        self.session.set_selection_window(float(seconds))
        self.log_event("추적", f"ROI 선택 시간 {float(seconds):.0f}초")

    @staticmethod
    def transport_applies(source):
        """tcp/udp only selects the RTSP transport; device indexes and files ignore it."""
        return str(source or "").strip().lower().startswith("rtsp")

    # ------------------------------------------------------------------ tracking
    def begin_selection(self):
        ref = self.session.begin_selection()
        return ref

    def select_roi(self, roi, ref=None):
        ok, reason = self.session.select_roi(roi, ref)
        if not ok:
            self.log_event("추적", f"ROI 미적용 · {reason_text(reason)}", "warn")
        return ok, reason

    def stop(self, reason="operator_stop"):
        self.session.stop(reason)

    def simulate(self):
        """Simulated output only. Live video additionally needs a fresh Pixhawk heartbeat."""
        if not (self.demo or self.pixhawk_fresh()):
            self.log_event("모의", "모의 출력 거부 · 선택 기체 HEARTBEAT가 최신이 아님", "warn")
            return False
        ok = self.session.simulate()
        self.log_event("모의", "모의 출력 허용(메모리 기록만 · 비행 명령 아님)" if ok else "모의 출력 거부 · 추적 중 아님",
                   "info" if ok else "warn")
        return ok

    def monitor_label(self):
        """Single rule for every view: synthetic video or enabled simulated output = SIMULATION."""
        return "SIMULATION" if (self.demo or self.session.sink.enabled) else "MONITOR ONLY"

    def pixhawk_fresh(self):
        stamp = getattr(self.vehicle, "telemetry_received", {}).get("HEARTBEAT")
        return stamp is not None and 0 <= time.monotonic() - stamp < STALE_S

    # ------------------------------------------------------------------ vehicle
    def vehicle_event(self, *args):
        self.session.revoke_simulation("pixhawk_state_or_command_event")

    def _feedback(self, command, success, message):
        self.modes.on_command_feedback(self.vehicle, command, success, message)
        self.session.revoke_simulation("pixhawk_state_or_command_event")

    def set_vehicle(self, vehicle):
        if vehicle is self.vehicle:
            return
        self.session.stop("vehicle_changed")
        self.modes.cancel("선택 기체가 바뀌어 모드 요청을 취소했습니다.")
        old = self.vehicle
        for signal, slot in (("commandFeedback", self._feedback), ("modeChanged", self.vehicle_event),
                             ("armedChanged", self.vehicle_event), ("connectionStateChanged", self.vehicle_event)):
            if old is not None and hasattr(old, signal):
                try:
                    getattr(old, signal).disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            if vehicle is not None and hasattr(vehicle, signal):
                getattr(vehicle, signal).connect(slot)
        self.vehicle = vehicle
        self._vehicle_mode = None
        self.positions.clear()
        name = getattr(vehicle, "name", None)
        self.log_event("기체", f"선택 기체 {name} · 추적 세션·모드 요청 초기화" if vehicle else "선택 기체 없음")

    # ------------------------------------------------------------------ flight modes
    def prepare_mode(self, mode, source="app"):
        try:
            ticket = self.modes.prepare(self.vehicle, mode, source)
        except ModeRequestError as exc:
            self.log_event("모드", f"모드 요청 거부 · {exc}", "warn")
            raise
        self.log_event("모드", f"{ticket['name']} {ticket['current_mode']} → {ticket['mode']} 확인 대기 ({source})")
        return ticket

    def commit_mode(self, ticket_id, source="app"):
        try:
            self.modes.commit(ticket_id, source)
        except ModeRequestError as exc:
            self.log_event("모드", f"모드 요청 취소 · {exc}", "warn")
            raise
        self.log_event("모드", f"모드 변경 요청 1회 송신 ({source}) · HEARTBEAT 확인 대기")

    def cancel_mode(self, reason="사용자 취소", source="app"):
        self.modes.cancel(reason, source)

    # ------------------------------------------------------------------ browser commands
    def execute(self, command):
        """Runs on the Qt thread only (drained from the dashboard queue in tick())."""
        action = command["action"]
        key = getattr(self.vehicle, "key_str", None)
        if action == "begin_selection":
            ref = self.begin_selection()
            self.web_result = "프레임 고정 · ROI 그리기 대기" if ref else "영상 프레임이 없습니다"
        elif action == "select_roi":
            ok, reason = self.select_roi(command["roi"], command["ref"])
            self.web_result = "ROI 적용 · 초기화 중" if ok else f"ROI 미적용 · {reason_text(reason)}"
        elif action == "cancel_selection":
            self.stop("selection_cancelled")
            self.web_result = "ROI 선택 취소"
        elif action == "stop":
            self.stop("operator_stop")
            self.web_result = "추적 정지"
        elif action == "intervention":
            self.stop("operator_intervention")
            self.web_result = "조종자 개입 · 추적·모의 출력 정지"
        elif action == "simulate":
            self.web_result = "모의 출력 허용" if self.simulate() else "모의 출력 거부"
        elif action == "revoke_simulation":
            self.session.revoke_simulation("operator_revoke")
            self.web_result = "모의 출력 해제"
        elif action == "video_connect":
            self.web_result = "영상 연결 요청" if self.connect_source() else "설정된 영상 주소 없음"
        elif action == "video_disconnect":
            self.disconnect_source()
            self.web_result = "영상 해제"
        elif action == "demo":
            self.start_demo()
            self.web_result = "합성 모의 영상"
        elif action in ("mode_prepare", "mode_commit"):
            if command["vehicle"] != key:
                self.modes.cancel("브라우저 요청 대상이 현재 선택 기체와 다릅니다.", "web")
                self.modes._audit("REJECTED_WEB_TARGET", f"요청 기체 {command['vehicle']} ≠ 선택 {key}", "web",
                                  target={})
                raise ValueError("요청 대상이 현재 선택 기체와 다릅니다.")
            if action == "mode_prepare":
                self.prepare_mode(command["mode"], "web")
                self.web_result = "모드 변경 확인 대기"
            else:
                self.commit_mode(command["ticket"], "web")
                self.web_result = "모드 변경 요청 송신"
        elif action == "mode_cancel":
            self.cancel_mode(source="web")
            self.web_result = "모드 요청 취소"
        else:  # the HTTP validator already restricts actions
            raise ValueError("지원하지 않는 요청")

    def _drain_web(self):
        if self.dashboard is None:
            return
        for command in self.dashboard.drain():
            try:
                self.execute(command)
            except (ValueError, TypeError, KeyError, ModeRequestError) as exc:
                self.web_result = f"요청 실패 · {exc}"

    # ------------------------------------------------------------------ periodic
    def tick(self):
        if self.closed:
            return
        self._drain_web()
        frame = self.capture.latest() if self.capture else None
        if self.demo:
            frame = self._demo_frame()
        if frame is not None:
            key = frame.session_id, frame.frame_seq
            if key != self.last_key:
                if self.last_key and self.last_key[0] != frame.session_id:
                    self.log_event("영상", "영상 세션 변경(재연결·해상도 변경) · 이전 추적 무효화", "warn")
                self.last_key = key
                self.recent.append(frame)
                self.receipts.append(frame.decoder_received_monotonic)
                self.session.offer(frame)
                if self.demo and self.demo_seq == 1:
                    ref = self.session.begin_selection()
                    self.session.select_roi((120, 180, 100, 90), ref)   # where _demo_frame drew seq 1
        self._supervise_vehicle()
        self._supervise_video_link()
        self.modes.tick()
        self._log_transitions()
        if self.dashboard is not None:
            self._publish_web()
        self.changed.emit()

    def _supervise_vehicle(self):
        v = self.vehicle
        sink = self.session.sink
        if v is not None:
            if getattr(v, "_rc_override_active", False) and sink.enabled:
                self.session.revoke_simulation("operator_rc_override")
                self.log_event("모의", "RC override 활성 감지 · 모의 출력 해제", "warn")
            mode = (getattr(v, "last_mode", None), getattr(v, "last_armed", None))
            if self._vehicle_mode is not None and mode != self._vehicle_mode:
                self.session.revoke_simulation("vehicle_mode_or_armed_changed")
            self._vehicle_mode = mode
            pos = self.vehicle_value("GLOBAL_POSITION_INT")
            if pos and not pos["stale"] and pos["lat"] not in (None, 0) and pos["lon"] not in (None, 0):
                point = (pos["lat"] / 1e7, pos["lon"] / 1e7)
                if not self.positions or self.positions[-1] != point:
                    self.positions.append(point)
        if not self.demo and sink.enabled and not self.pixhawk_fresh():
            self.session.revoke_simulation("pixhawk_stale")
            self.log_event("모의", "선택 기체 HEARTBEAT stale · 모의 출력 해제", "warn")

    def _supervise_video_link(self):
        """Warnings Linker can know from its own decoder pool (other programs are not counted)."""
        counts = direct_rtsp_connections()
        self.link_warnings = [f"같은 카메라에 직접 연결 {n}개 · 무선 링크 부하 증가 · 로컬 중계 사용 권장"
                              for host, n in sorted(counts.items()) if n >= 2]
        key = tuple(self.link_warnings)
        if key != self._last_link_warnings:
            for text in self.link_warnings:
                self.log_event("영상", text, "warn")
            self._last_link_warnings = key
        health = getattr(self.capture, "health", None)
        warning = health.snapshot(time.monotonic())["warning"] if health else None
        if warning != self._last_health_warning:
            if warning:
                self.log_event("영상", warning, "warn")
            self._last_health_warning = warning

    def _log_transitions(self):
        r = self.session.snapshot()
        key = r.state, r.reason, r.session_id
        if self._last_track and key[0] != self._last_track[0]:
            level = "danger" if r.state in ("LOST", "ERROR") else "info"
            self.log_event("추적", f"{self._last_track[0]} → {r.state} · {reason_text(r.reason)}", level)
        self._last_track = key
        m = self.modes.snapshot()
        if m["state"] != self._last_mode_state and self._last_mode_state is not None:
            level = "info" if m["state"] in ("CONFIRMED", "CONFIRMING", "REQUESTING", "IDLE") else "danger"
            self.log_event("모드", f"{m['state']} · {m['detail']}", level)
        self._last_mode_state = m["state"]

    # ------------------------------------------------------------------ view model
    def view(self):
        """(frame, result) to draw. The overlay is only attached to the exact frame it belongs to."""
        result = self.session.snapshot()
        if result.state == "SELECTING":
            return self.session.display_frame(), None
        if result.state == "TRACKING" and result.bbox:
            for frame in reversed(self.recent):
                if (frame.session_id, frame.frame_seq) == (result.frame_session_id, result.frame_seq):
                    return frame, result
        return self.session.display_frame(), None

    def video_state(self):
        frame = self.session.frame
        status = getattr(self.capture, "status", None)
        status_text = status[0] if status else ("합성 영상" if self.demo else "연결 안 됨")
        age = time.monotonic() - frame.decoder_received_monotonic if frame else None
        if self.demo:
            state = "SIMULATION"
        elif self.capture is None:
            state = "DISCONNECTED"
        elif frame is None:
            state = "CONNECTING"
        elif age is not None and age >= self.session.timeout:
            state = "STALE"   # same limit the tracker uses: older frames cannot keep TRACKING
        else:
            state = "LIVE"
        fps = None
        if state in ("LIVE", "SIMULATION") and len(self.receipts) >= 2 and self.receipts[-1] > self.receipts[0]:
            fps = (len(self.receipts) - 1) / (self.receipts[-1] - self.receipts[0])
        route = "합성" if self.demo else (source_route(self.source_text) if self.capture else "—")
        if route == "로컬 중계" and state == "CONNECTING" and str(status_text).startswith(("Open failed", "Stream")):
            status_text = "로컬 중계 서버 응답 없음 · 127.0.0.1:8554"
        health = self.capture.health.snapshot(time.monotonic()) if getattr(self.capture, "health", None) else None
        warnings = list(self.link_warnings)
        if health and health["warning"] and state == "LIVE":
            warnings.append(health["warning"])
        return dict(state=state, source=self.source_label, transport=None if self.demo else self.transport,
                    status=status_text, configured=bool(self.configured_source), mount=self.mount,
                    route=route, warnings=warnings, reconnects=getattr(self.capture, "reconnects", 0),
                    source_fps=health and health["source_fps"], actual_fps=health and health["actual_fps"],
                    frame_age_ms=None if age is None else round(age * 1000), decode_fps=_num(fps, 1),
                    width=frame.width if frame else None, height=frame.height if frame else None,
                    source_id=frame.source_id if frame else None, session_id=frame.session_id if frame else None,
                    frame_seq=frame.frame_seq if frame else None)

    def vehicle_value(self, kind):
        v = self.vehicle
        stamp = getattr(v, "telemetry_received", {}).get(kind)
        if stamp is None:
            return None
        values = dict(getattr(v, "tracking_telemetry", {}).get(kind) or {})
        values["age_s"] = _num(time.monotonic() - stamp, 1)
        values["stale"] = time.monotonic() - stamp > STALE_S
        return values

    def vehicle_state(self):
        v = self.vehicle
        if v is None:
            return None
        hb_stamp = getattr(v, "telemetry_received", {}).get("HEARTBEAT")
        hb_age = None if hb_stamp is None else time.monotonic() - hb_stamp
        att, gpos, hud = (self.vehicle_value(k) for k in ("ATTITUDE", "GLOBAL_POSITION_INT", "VFR_HUD"))
        gps, sys_status = self.vehicle_value("GPS_RAW_INT"), self.vehicle_value("SYS_STATUS")
        version = self.vehicle_value("AUTOPILOT_VERSION")

        def deg(value):
            return None if value is None else _num(math.degrees(value), 1)
        volt = sys_status and sys_status.get("voltage_battery")
        pct = sys_status and sys_status.get("battery_remaining")
        fix = gps and gps.get("fix_type")
        sats = gps and gps.get("satellites_visible")
        position = None
        if gpos and gpos.get("lat") not in (None, 0) and gpos.get("lon") not in (None, 0):
            position = dict(lat=gpos["lat"] / 1e7, lon=gpos["lon"] / 1e7, stale=gpos["stale"], age_s=gpos["age_s"])
        link = getattr(v, "link", None)
        return dict(
            key=getattr(v, "key_str", None), name=getattr(v, "name", "—"),
            link_id=getattr(link, "link_id", None), link=getattr(link, "human", None),
            sysid=getattr(v, "sysid", None), compid=getattr(v, "compid", None),
            firmware=firmware_name(v), autopilot=getattr(v, "autopilot", None), mav_type=getattr(v, "mav_type", None),
            sw_version=version and version.get("flight_sw_version"),
            connected=bool(getattr(v, "connected", False)),
            heartbeat_age_s=_num(hb_age, 1), heartbeat_stale=hb_age is None or hb_age > STALE_S,
            mode=getattr(v, "last_mode", None) if hb_stamp else None,
            armed=getattr(v, "last_armed", None) if hb_stamp else None,
            rc_override=bool(getattr(v, "_rc_override_active", False)),
            attitude=None if not att else dict(roll=deg(att.get("roll")), pitch=deg(att.get("pitch")),
                                               yaw=deg(att.get("yaw")), stale=att["stale"]),
            altitude=self._altitude(gpos, hud),
            groundspeed=None if not hud else dict(mps=_num(hud.get("groundspeed"), 1), stale=hud["stale"]),
            gps=None if not gps else dict(fix=fix, fix_text=FIX_TEXT.get(fix, str(fix)),
                                          sats=None if sats in (None, 255) else sats, stale=gps["stale"]),
            battery=None if not sys_status else dict(
                volt=None if volt in (None, 0, 65535) else _num(volt / 1000, 2),
                pct=None if pct in (None, -1) else pct, stale=sys_status["stale"]),
            position=position, track=[list(p) for p in list(self.positions)[-200:]])

    @staticmethod
    def _altitude(gpos, hud):
        """GLOBAL_POSITION_INT relative_alt is home-relative (not AGL); alt / VFR_HUD alt are MSL."""
        if gpos:
            rel, msl = gpos.get("relative_alt"), gpos.get("alt")
            return dict(relative_m=None if rel is None else _num(rel / 1000, 1),
                        msl_m=None if msl is None else _num(msl / 1000, 1),
                        reference="홈 기준 상대고도(relative_alt) · AGL 아님", stale=gpos["stale"])
        if hud:
            return dict(relative_m=None, msl_m=_num(hud.get("alt"), 1),
                        reference="VFR_HUD alt (MSL) · AGL 아님", stale=hud["stale"])
        return None

    def banner(self, video, result):
        if video["state"] == "DISCONNECTED":
            return "warn", "영상 없음 · 추적 비활성"
        if video["state"] == "CONNECTING":
            return "warn", f"영상 연결 중 · {video['status']}"
        if video["state"] == "STALE":
            return "danger", f"영상 지연·단절 · 마지막 프레임 {video['frame_age_ms']} ms 전 · 추적 결과 무효"
        if result.state in ("LOST", "ERROR"):
            return "danger", (f"추적 {result.state} · {reason_text(result.reason)} · 재선택 필요 "
                              "· 기체 동작과 무관(정지·호버 아님)")
        if result.state == "SELECTING":
            info = self.session.selection_info()
            left = f" · 남은 시간 {info['remaining_s']:.0f}초" if info else ""
            return "warn", f"ROI 선택 중 · {reason_text(result.reason)}{left}"
        if result.state == "TRACKING":
            return "ok", f"추적 중 · {self.monitor_label()} · 기체 제어 출력 없음"
        if result.state == "STOPPED":
            return "info", f"추적 정지 · {reason_text(result.reason)}"
        return "info", "영상 수신 중 · 추적 대기"

    def snapshot(self):
        result = self.session.snapshot()
        video = self.video_state()
        level, text = self.banner(video, result)
        hold = None
        if result.state == "TRACKING" and result.tracking_since:
            hold = _num(time.monotonic() - result.tracking_since, 1)
        pinned = frame_ref(self.session.display_frame()) if result.state == "SELECTING" else None
        legacy = self.legacy_status()
        vehicle = self.vehicle_state()
        output = self.session.sink.output()
        return dict(
            app=dict(connected=True, monitor=self.monitor_label(), web_result=self.web_result,
                     web_mode_change=self.dashboard is not None),
            banner=dict(level=level, text=text), video=video,
            tracking=dict(state=result.state, reason=result.reason, reason_text=reason_text(result.reason),
                          session_id=result.session_id, frame_session_id=result.frame_session_id or None,
                          frame_seq=result.frame_seq, bbox=[_num(v, 1) for v in result.bbox] if result.bbox else None,
                          center=[_num(v, 1) for v in result.center] if result.center else None,
                          image_error=[_num(v, 3) for v in result.normalized_image_error]
                          if result.normalized_image_error else None,
                          quality=_num(result.quality, 2) if result.state == "TRACKING" else None,
                          hold_s=hold, processing_fps=_num(result.processing_fps, 1) if result.state == "TRACKING" else None,
                          object_class=result.object_class, timeout_s=self.session.timeout,
                          selection=self.session.selection_info(),
                          pinned=None if pinned is None else dict(zip(
                              ("source_id", "session_id", "frame_seq", "width", "height"), pinned))),
            simulation=dict(enabled=self.session.sink.enabled,
                            output=None if output is None else dict(frame_seq=output["frame_seq"],
                                image_error=[_num(v, 3) for v in output["image_error"]])),
            vehicle=vehicle,
            modes=dict(options=[dict(name=o.name, enabled=o.enabled, reason=o.reason) for o in mode_options(self.vehicle)],
                       controller=self.modes.snapshot()),
            sentinel=dict(state="미사용" if legacy is None else legacy),
            events=list(self.events)[:40])

    def save(self, path):
        """Tracking events, simulated output and the flight mode audit trail as one JSON file."""
        import json
        with self.session.lock:
            data = dict(events=list(self.session.events), simulation=list(self.session.sink.rows))
        with self.modes.lock:
            data["mode_audit"] = list(self.modes.audit)
        data["operator_log"] = list(self.events)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False, default=str)

    # ------------------------------------------------------------------ browser
    def open_dashboard(self, bind=("127.0.0.1", 8766)):
        from tracking_dashboard import TrackingDashboard
        if self.dashboard is None:
            try:
                self.dashboard = TrackingDashboard(bind)
            except OSError:
                self.dashboard = TrackingDashboard(("127.0.0.1", 0))   # port busy: ephemeral loopback port
            self.log_event("웹", f"Tracking 웹 대시보드 시작 · {self.dashboard.url} · 브라우저에서 비행 모드 변경 가능",
                           "warn")
        return self.dashboard.url

    def close_dashboard(self):
        if self.dashboard is not None:
            self.dashboard.close()
            self.dashboard = None
            if (self.modes.snapshot().get("ticket") or {}).get("source") == "web":
                self.modes.cancel("브라우저 대시보드 종료", "web")
            self.log_event("웹", "Tracking 웹 대시보드 종료")

    def _publish_web(self):
        now = time.monotonic()
        if now >= self._next_web_state:
            self._next_web_state = now + WEB_STATE_S
            self.dashboard.publish_state(self.snapshot())
        if now < self._next_web_frame or not self.dashboard.frame_wanted(now):
            return
        self._next_web_frame = now + WEB_FRAME_S
        frame, result = self.view()
        if frame is None:
            self.dashboard.publish_frame(None, None)
            return
        key = frame.session_id, frame.frame_seq, result.session_id if result else None, self.session.result.state
        if key == self._web_frame_key:
            return
        self._web_frame_key = key
        pixels = frame.pixels
        if frame.width > 1280:
            pixels = cv2.resize(pixels, (1280, round(frame.height * 1280 / frame.width)), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        overlay = None
        if result is not None:
            overlay = dict(session=result.session_id, bbox=[_num(v, 1) for v in result.bbox],
                           center=[_num(v, 1) for v in result.center],
                           trail=[[_num(a, 1), _num(b, 1)] for a, b in result.trail[-60:]])
        meta = dict(source_id=frame.source_id, session_id=frame.session_id, frame_seq=frame.frame_seq,
                    width=frame.width, height=frame.height, received=frame.decoder_received_monotonic,
                    selecting=self.session.result.state == "SELECTING", overlay=overlay)
        self.dashboard.publish_frame(jpeg.tobytes(), meta)

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        if self.closed:
            return
        self.timer.stop()
        self.modes.cancel("앱 종료")
        self.disconnect_source("app_shutdown")
        self.set_vehicle(None)
        self.session.close()
        self.close_dashboard()
        self.closed = True
