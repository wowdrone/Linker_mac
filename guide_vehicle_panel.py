"""Driver advisory display for the Linker desktop app."""
import time

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QVBoxLayout,
)
from drive_dashboard import Dashboard, bind_from_profile
from dashboard_bridge import DashboardBridge
from guide_protocol import CRITICAL, KINDS, WARNING, Mode, utc_ms
from guide_vehicle import VehicleSession
from gui_help import help_button, set_guide

COLORS = (("#45515e", "#fff"), ("#ffe066", "#201b0e"), ("#ffae58", "#211407"), ("#b41229", "#fff"))


class GuideVehiclePanel(QDialog):
    def __init__(self, default_profile, parent):
        super().__init__(parent)
        self.setWindowTitle("Linker Drive · Sentinel Legacy")
        self.setObjectName("driveGuide")
        self.setStyleSheet("""
            QDialog#driveGuide { background:#10141c; color:#f3f6fa; }
            QDialog#driveGuide QLabel { color:#b8c3d4; }
            QDialog#driveGuide QLabel#driveTitle { color:#ffce35; font-size:22px; font-weight:800; }
            QDialog#driveGuide QLineEdit, QDialog#driveGuide QListWidget {
                background:#181e29; color:#f3f6fa; border:1px solid #394456;
                border-radius:8px; padding:8px;
            }
            QDialog#driveGuide QPushButton {
                background:#252e3d; color:#f3f6fa; border:1px solid #465267;
                border-radius:6px; padding:7px 12px;
            }
            QDialog#driveGuide QPushButton:hover { background:#344158; }
            QDialog#driveGuide QPushButton#openDriveDashboard { background:#ffce35; color:#191909; }
            QDialog#driveGuide QPushButton:disabled { color:#7c8799; }
        """)
        self.session = self.dashboard = None
        self.last_sound, self.last_sound_id = 0.0, None
        self.last_view = None
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setTextFormat(Qt.PlainText)
        self.banner.setWordWrap(True)
        self.banner.hide()

        layout = QVBoxLayout(self)
        title = QLabel("SENTINEL LEGACY / DRIVE")
        title.setObjectName("driveTitle")
        guide = ["차량 경보 수신 · 코스 모니터링",
                 "프로필(JSON)을 고르고 「수신 시작」으로 Sentinel 차량 경보 수신을 시작합니다. 「수신 정지」로 끕니다.",
                 "대시보드: 주행 대시보드를 브라우저에서 엽니다 (수신 전에는 시뮬레이션).",
                 "브라우저 영상은 Linker가 만든 JPEG 스냅샷이라 앱 창보다 늦습니다.",
                 "Tracking(영상 추적)과는 별도 기능이며 서로의 수신을 시작·정지하지 않습니다."]
        head = QHBoxLayout()
        head.addWidget(title)
        head.addWidget(help_button(self, "Sentinel Legacy", guide))
        head.addStretch(1)
        layout.addLayout(head)
        set_guide(self, "Sentinel Legacy", guide)
        row = QHBoxLayout()
        self.path = QLineEdit(str(default_profile))
        row.addWidget(self.path)
        choose = QPushButton("프로필")
        choose.clicked.connect(self.choose)
        row.addWidget(choose)
        self.start_button = QPushButton("수신 시작")
        self.start_button.clicked.connect(self.start)
        row.addWidget(self.start_button)
        stop = QPushButton("수신 정지")
        stop.clicked.connect(self.shutdown)
        row.addWidget(stop)
        self.dashboard_button = QPushButton("대시보드")
        self.dashboard_button.setObjectName("openDriveDashboard")
        self.dashboard_button.setToolTip("주행 대시보드 열기")
        self.dashboard_button.clicked.connect(self.open_dashboard)
        row.addWidget(self.dashboard_button)
        layout.addLayout(row)
        self.status = QLabel("차량 경보 대기")
        layout.addWidget(self.status)
        self.list = QListWidget()
        layout.addWidget(self.list)
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.desktop_timer = QTimer(self)
        self.desktop_timer.setInterval(150)
        self.desktop_timer.timeout.connect(self._update_desktop)
        self.resize(680, 340)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "차량 프로필", self.path.text(), "JSON (*.json)")
        if path:
            self.path.setText(path)

    def start(self):
        if self.session:
            return
        try:
            self.session = VehicleSession(self.path.text())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(f"수신 시작 실패 · {exc}")
            return
        self.start_button.setEnabled(False)
        if self._ensure_dashboard():
            self.dashboard.attach(self.session)
        self.timer.start()

    def _ensure_dashboard(self):
        if self.dashboard:
            return True
        try:
            self.dashboard = Dashboard(bind_from_profile(self.path.text()))
            window = self.parent()
            if window is not None and hasattr(window, "fleet"):
                self.dashboard.bridge = DashboardBridge(window)
                self.desktop_timer.start()
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(f"대시보드 시작 실패 · {exc}")
            return False

    def open_dashboard(self):
        if self._ensure_dashboard():
            QDesktopServices.openUrl(QUrl(self.dashboard.url))

    def _update_desktop(self):
        if self.dashboard:
            self.dashboard.update_desktop()

    def close_dashboard(self):
        self.desktop_timer.stop()
        if self.dashboard:
            self.dashboard.close()
            self.dashboard = None

    def tick(self):
        try:
            self.session.poll()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.shutdown()
            self.status.setText(f"차량 경보 정지 · {exc}")
            return
        if self.dashboard:
            try:
                self.dashboard.update()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                # The dashboard must never stop the advisory receiver.
                self.dashboard.close()
                self.dashboard = None
                self.session.log("dashboard_error", reason=str(exc))
        result = self.session.decision
        if result is None:
            self.status.setText("차량 GPS 대기")
            current = [data for data in self.session.route.values()
                       if data["t_detect_utc_ms"] + data["ttl_ms"] > utc_ms()]
            if current:
                hazard = max(current, key=lambda data: data["severity"])
                self._show_banner(hazard["severity"], f'{KINDS[hazard["type"]]} · 거리/TTA 미확인 · 차량 GPS 대기')
                self._sound(hazard["severity"], hazard["obstacle_id"], None)
            else:
                self.banner.hide()
                self.last_sound_id = None
            return
        drive = "운전자 표시" if self.session.interface is None else "시뮬레이션"
        sync = "동기화 완료" if self.session.synchronized else "경보 동기화 대기"
        self.status.setText(f"{drive} · {result.mode.name} · {sync}")
        hazard = result.hazard
        hazards = self.session.hazards(self.session.live_car() or self.session.car)
        if hazard is None and hazards:
            hazard = hazards[0]
        views = tuple((KINDS[v.data["type"]], f"{v.remaining_m:.1f} m") for v in hazards)
        if views != self.last_view:
            self.last_view = views
            self.list.clear()
            self.list.addItems([f"{kind} · {dist}" for kind, dist in views])
        if hazard is None and result.mode not in (Mode.STOP, Mode.CAUTION, Mode.LINK_DEGRADED):
            self.banner.hide()
            self.last_sound_id = None
            return
        level = hazard.data["severity"] if hazard else WARNING
        direction = {
            Mode.STOP: "■ 정지", Mode.CAUTION: "↓ 감속", Mode.LINK_DEGRADED: "↓ 링크 확인",
            Mode.AVOID: "← 좌측 회피" if result.requested_offset_m < 0 else "→ 우측 회피",
        }.get(result.mode, "장애물 확인")
        warning = " · 정지 거리 여유 부족" if result.margin_short else ""
        distance = f"{hazard.remaining_m:.1f} m" if hazard and hazard.remaining_m is not None else "거리 —"
        eta = f"TTA {hazard.tta_s:.1f} 초" if hazard and hazard.tta_s is not None else "TTA —"
        self._show_banner(level, f"{distance} / {eta} · {result.mode.name} · {direction}{warning}")
        self._sound(level, hazard.data["obstacle_id"] if hazard else result.mode,
                    hazard.tta_s if hazard else None)

    def _show_banner(self, level, text):
        bg, fg = COLORS[level]
        css = f"QLabel{{background:{bg};color:{fg};font-size:24px;font-weight:800;padding:9px;}}"
        if self.banner.styleSheet() != css:
            self.banner.setStyleSheet(css)
        self.banner.setText(text)
        self.banner.show()

    def _sound(self, level, ident, seconds):
        if self.session.c["sound"] and level >= WARNING:
            now = time.monotonic()
            delay = max(.25, min(1.2, seconds / 4)) if seconds is not None else .8
            if ident != self.last_sound_id or (level >= CRITICAL and now - self.last_sound >= delay):
                QApplication.beep()
                self.last_sound, self.last_sound_id = now, ident

    def shutdown(self):
        self.timer.stop()
        if self.dashboard:
            self.dashboard.detach()
        if self.session:
            self.session.close()
            self.session = None
        self.banner.hide()
        self.list.clear()
        self.last_view = None
        self.status.setText("차량 경보 대기")
        self.start_button.setEnabled(True)
