# main_window.py
# LINKER GCS – Python 3.11 / PyQt5

from serial.tools import list_ports   # ← 이 줄 유지

from PyQt5 import uic
from pathlib import Path
from tab_ai_tuner import AITunerTab

BASE_DIR = Path(__file__).resolve().parent

from platform_mac import SERIAL_LINK_KINDS, prepare_env, refresh_port_selector, setup_fonts

def safe_relative(path: Path, start: Path) -> Path:
    path = path.resolve()
    start = start.resolve()
    try:
        return path.relative_to(start)
    except ValueError:
        # 다른 드라이브(C: vs E:)면 그냥 절대 경로 그대로 사용
        return path

import sys
import json
import threading
import math
import time  # ★ TAKEOFF 자동 모드 전환/ARM을 위해 추가

from PyQt5.QtCore import (
    QPointF,
    Qt,
    QRect,
    QRectF,
    pyqtSlot,
    QObject,
    pyqtSignal,
    QTimer,
    QSettings,
)
from PyQt5.QtGui import (
    QConicalGradient,
    QPainter,
    QPen,
    QBrush,
    QColor,
    QPixmap,
    QPainterPath,
    QRadialGradient,
    QFont,
    QIcon,
)
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QBoxLayout,
    QGridLayout,
    QPushButton,
    QLabel,
    QComboBox,
    QTabWidget,
    QFrame,
    QMenu,
    QActionGroup,
    QTextEdit,
    QSplitter,
    QStatusBar,
    QDoubleSpinBox,
    QGroupBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QSplashScreen,   # 사용은 linker_intro 로 옮겼지만, 남겨둬도 무방
    QSizePolicy,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QProgressBar,
    QScrollArea,
    QStyle,
)
from PyQt5.QtWebChannel import QWebChannel

import serial.tools.list_ports
import requests

from fleet import FleetManager, ActiveVehicleProxy
from fleet_panel import FLIGHT_MODES, FleetPanel
from flight_actions import confirm_flight, TakeoffSequence, MissionSequence
from video_grid import VideoCanvas, VideoGridPage, VideoWorker
from linker_intro import create_splash   # ★ 스플래시 전용 모듈에서 가져옴
from linker_splash import LinkerSplashScreen
from map_common import MapBridge, DEFAULT_LAT, DEFAULT_LON
from map_widget import create_map_widget, set_map_theme
from tab_planning import PlanningTab
from tab_support import SupportTab
from tab_health import HealthTab
from tab_safety import SafetyTab
from tab_fly import FlyTab
from guide_vehicle_panel import GuideVehiclePanel
from tracking_panel import TrackingPanel
from tracking_service import TrackingService

from gui_theme import DEFAULT_THEME, THEME_SETTING, THEMES, set_status_tone


# 여러 이름 중에서 먼저 나오는 유효한 값을 반환하는 헬퍼
def pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


class AHRSWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.has_attitude = False
        self.layered = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    @pyqtSlot(float, float, float)
    def update_attitude(self, roll, pitch, yaw):
        self.roll = roll
        self.pitch = pitch
        self.yaw = yaw
        self.has_attitude = True
        self.update()

    def clear_attitude(self):
        self.roll = self.pitch = self.yaw = 0.0
        self.has_attitude = False
        self.update()

    def update_hud(self, hud: dict):
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if not self.layered:
            painter.fillRect(self.rect(), self.palette().base().color())

        margin = 20
        r = self.rect().adjusted(margin, margin, -margin, -margin - 18)
        size = min(r.width(), r.height())
        cx = r.center().x()
        cy = r.center().y()
        radius = size / 2.0

        circle_rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        if self.layered:
            self._paint_dial_frame(painter, circle_rect)
            if not self.has_attitude:
                self._paint_readout(painter)
                return

        # 원형 클리핑
        painter.save()
        clip_path = QPainterPath()
        clip_path.addEllipse(circle_rect)
        painter.setClipPath(clip_path)

        painter.translate(cx, cy)
        painter.rotate(-self.roll)

        pitch_offset = self.pitch / 45.0 * radius

        sky_color = QColor(40, 120, 210)
        ground_color = QColor(150, 90, 40)

        sky_rect = QRectF(-2 * radius, -2 * radius - pitch_offset, 4 * radius, 2 * radius)
        ground_rect = QRectF(-2 * radius, -pitch_offset, 4 * radius, 2 * radius)

        painter.setBrush(QBrush(sky_color))
        painter.setPen(Qt.NoPen)
        painter.drawRect(sky_rect)

        painter.setBrush(QBrush(ground_color))
        painter.drawRect(ground_rect)

        painter.setPen(QPen(Qt.white, 2))
        painter.drawLine(
            int(-radius * 0.9),
            int(-pitch_offset),
            int(radius * 0.9),
            int(-pitch_offset),
        )

        painter.restore()

        # 원형 테두리
        painter.setPen(QPen(self.palette().link().color(), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(circle_rect)

        # 방위 표시 (N/E/S/W 글자 강조)
        painter.save()
        painter.translate(cx, cy)
        heading_font = painter.font()
        heading_font.setBold(True)
        heading_font.setPointSizeF(heading_font.pointSizeF() + 2)
        painter.setFont(heading_font)

        def heading_label(deg):
            d = deg % 360
            if d == 0:
                return "N"
            elif d == 90:
                return "E"
            elif d == 180:
                return "S"
            elif d == 270:
                return "W"
            return str(d)

        for deg in range(0, 360, 30):
            ang = math.radians(deg - 90)
            outer = radius
            inner = radius * (0.92 if deg % 90 == 0 else 0.96)

            painter.setPen(QPen(QColor("#FAF4F4"), 1))
            x1 = inner * math.cos(ang)
            y1 = inner * math.sin(ang)
            x2 = outer * math.cos(ang)
            y2 = outer * math.sin(ang)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

            if deg % 90 == 0:
                txt = heading_label(deg)
                text_r = radius * 1.05
                tx = text_r * math.cos(ang)
                ty = text_r * math.sin(ang)
                rect = QRect(int(tx - 14), int(ty - 10), 28, 20)

                painter.setPen(QPen(self.palette().link().color(), 2))
                painter.drawText(rect, Qt.AlignCenter, txt)

        painter.restore()

        # 중앙 비행기 십자
        painter.save()
        painter.translate(cx, cy)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawLine(0, int(radius * 0.2), 0, int(radius * -0.2))
        painter.drawLine(int(-radius * 0.25), 0, int(radius * 0.25), 0)
        painter.drawLine(0, int(radius * 0.15), int(-radius * 0.07), int(radius * 0.28))
        painter.drawLine(0, int(radius * 0.15), int(radius * 0.07), int(radius * 0.28))
        painter.restore()

        self._paint_readout(painter)

    def _paint_dial_frame(self, painter, face):
        painter.save()
        center = face.center()
        rim = face.adjusted(-9, -9, 9, 9)
        bezel = rim.adjusted(-5, -5, 5, 5)
        shine = QRadialGradient(center, bezel.width() / 2)
        shine.setColorAt(0, QColor("#3c4556"))
        shine.setColorAt(0.72, QColor("#282e3b"))
        shine.setColorAt(0.84, QColor("#11151c"))
        shine.setColorAt(1, QColor("#252c38"))
        painter.setPen(QPen(QColor("#51596a"), 1))
        painter.setBrush(QBrush(shine))
        painter.drawEllipse(bezel)

        track = QConicalGradient(center, 0)
        track.setColorAt(0.0, QColor("#ff79aa"))
        track.setColorAt(0.26, QColor("#bf9cff"))
        track.setColorAt(0.50, QColor("#53b8ff"))
        track.setColorAt(0.69, QColor("#54f1d4"))
        track.setColorAt(0.93, QColor("#ff79aa"))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#0c1017"), 7, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rim, -28 * 16, 236 * 16)
        painter.setPen(QPen(QBrush(track), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rim, -28 * 16, 236 * 16)
        painter.setPen(QPen(QColor("#5d6779"), 1))
        for angle in range(-30, 211, 10):
            radians = math.radians(angle)
            outward = QPointF(math.cos(radians), -math.sin(radians))
            outside = bezel.width() / 2 + 5
            inside = outside - (4 if angle % 30 == 0 else 2)
            painter.drawLine(center + outward * inside, center + outward * outside)

        if not self.has_attitude:
            font = painter.font()
            font.setPointSize(26)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(self.palette().text().color())
            painter.drawText(face, Qt.AlignCenter, "—")
        painter.restore()

    def _paint_readout(self, painter):
        text_rect = QRect(0, self.height() - 20, self.width(), 20)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(self.palette().text().color())
        painter.drawText(
            text_rect,
            Qt.AlignCenter,
            f"R:{self.roll:.1f}°  P:{self.pitch:.1f}°  Y:{self.yaw:.1f}°" if self.has_attitude else "자세 데이터 대기",
        )


class PreArmDialog(QDialog):
    """
    ARMING_CHECK 비트마스크 편집 다이얼로그
    """

    ARMING_BITS = [
        ("ALL", 1),
        ("Barometer", 2),
        ("Compass", 4),
        ("GPS lock", 8),
        ("INS", 16),
        ("Parameters", 32),
        ("RC Channels", 64),
        ("Board voltage", 128),
        ("Battery level", 256),
        ("Logging", 512),
        ("HW safety switch", 1024),
        ("GPS configuration", 2048),
        ("System", 4096),
        ("Mission", 8192),
        ("Rangefinder", 16384),
    ]

    def __init__(self, current_mask: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PreArm Checks (ARMING_CHECK)")
        self.resize(500, 320)

        self._mask = int(current_mask)

        main = QVBoxLayout(self)

        desc = QLabel(
            "ARMING_CHECK 비트마스크를 편집합니다.\n"
            "체크된 항목만 사용되며 실제 의미는 ArduPilot 문서를 참고하세요."
        )
        desc.setWordWrap(True)
        main.addWidget(desc)

        grid = QGridLayout()
        main.addLayout(grid)

        self._checkboxes = []
        row = 0
        col = 0
        for name, bit in self.ARMING_BITS:
            cb = QCheckBox(name)
            if current_mask & bit:
                cb.setChecked(True)
            grid.addWidget(cb, row, col)
            self._checkboxes.append((cb, bit))
            col += 1
            if col >= 2:
                col = 0
                row += 1

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        main.addWidget(buttons)

    def _on_accept(self):
        mask = 0
        for cb, bit in self._checkboxes:
            if cb.isChecked():
                mask |= bit
        self._mask = mask
        self.accept()

    def selected_mask(self) -> int:
        return self._mask


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # 멀티 기체: FleetManager 가 링크/기체 전체를 관리하고,
        # self.linker 는 "현재 선택된 기체" 프록시 (기존 Linker API 와 동일)
        self.fleet = FleetManager(self)
        self.linker = ActiveVehicleProxy(self)
        self._map_last_push = {}   # 기체 key -> 마지막 지도 갱신 시각 (스로틀)
        self._armed = False
        self.theme = THEMES[DEFAULT_THEME]
        self._home_from_gps = False

        # 오른쪽 영역에 비디오가 올라가 있는지 여부
        self.video_on_main = False

        # -------- 공용 Video 뷰 (한 번만 생성) --------
        self.video_view = VideoCanvas(0, self)
        self._fly_video_worker = None
        self._retired_video_workers = set()
        self.video_view.setContextMenuPolicy(Qt.NoContextMenu)

        # -------- 지도 위젯 두 개 (Fly / Planning용) 선 생성 --------
        self.map_view_fly, self.map_bridge_fly = create_map_widget(DEFAULT_LAT, DEFAULT_LON)
        self.map_view_plan, self.map_bridge_plan = create_map_widget(DEFAULT_LAT, DEFAULT_LON)

        # -------- Health 탭용 위젯 포인터 초기화 --------
        self.bar_vibe_x = QProgressBar()
        self.bar_vibe_y = QProgressBar()
        self.bar_vibe_z = QProgressBar()

        # vibe bar 기본 스타일
        for bar in (self.bar_vibe_x, self.bar_vibe_y, self.bar_vibe_z):
            bar.setRange(0, 100)
            bar.setOrientation(Qt.Vertical)
            bar.setTextVisible(False)
            bar.setMinimumSize(60, 260)
            bar.setMaximumWidth(80)

        self.lbl_vibe_clip = None

        self.lbl_gps_sats = None
        self.lbl_gps_hdop = None
        self.lbl_gps_fix_type = None

        self.bar_radio_rssi = None
        self.bar_radio_remrssi = None

        # -------- UI 구성 & 시그널 연결 --------
        self._setup_ui()
        self._connect_signals()
        self._apply_theme(self.appearance_settings.value(THEME_SETTING, DEFAULT_THEME, str))
        self._fit_to_screen()

        # 처음에는 액션 버튼 비활성화
        self._set_action_buttons_enabled(False)

        # 시리얼 포트 자동 새로고침 타이머 (2초마다)
        self._port_timer = QTimer(self)
        self._port_timer.setInterval(2000)
        self._port_timer.timeout.connect(self._refresh_ports)
        self._port_timer.start()

    # ------------------------------------------------------------------ #
    # UI 구성                                                             #
    # ------------------------------------------------------------------ #
    def _setup_ui(self):
        self.setWindowTitle("Linker")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # One Tracking service shared by the PyQt Tracking window and the browser dashboard.
        self.tracking_service = TrackingService(self, legacy_status=self._sentinel_legacy_status)
        self.tracking_panel = TrackingPanel(self, self.tracking_service)
        self.guide_vehicle = GuideVehiclePanel(BASE_DIR / "config" / "stage1.json", self)

        # Keep the navigation and connection controls above the current page.
        header = QWidget()
        self.header = header
        header.setObjectName("linkerHeader")
        header.setAttribute(Qt.WA_StyledBackground)
        header.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 6)
        header_layout.setSpacing(4)
        self.header_row = QBoxLayout(QBoxLayout.TopToBottom)
        self.header_row.setSpacing(6)
        header_layout.addLayout(self.header_row)

        self.navigation = QWidget()
        navigation_layout = QHBoxLayout(self.navigation)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(4)
        self.header_row.addWidget(self.navigation, 1)

        self.vehicle_status = QWidget()
        vehicle_layout = QHBoxLayout(self.vehicle_status)
        vehicle_layout.setContentsMargins(0, 0, 0, 0)
        vehicle_layout.setSpacing(4)
        self.header_row.addWidget(self.vehicle_status)

        link_controls = QWidget()
        link_layout = QHBoxLayout(link_controls)
        link_layout.setContentsMargins(0, 0, 0, 0)
        link_layout.setSpacing(4)
        header_layout.addWidget(link_controls)

        brand_mark = QLabel()
        brand_mark.setPixmap(QPixmap(str(BASE_DIR / "quad.png")).scaled(
            28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        navigation_layout.addWidget(brand_mark)
        brand = QLabel("Linker")
        brand.setObjectName("linkerBrand")
        navigation_layout.addWidget(brand)
        navigation_layout.addSpacing(6)

        def top_btn(text):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setProperty("role", "navigation")
            return btn

        self.btn_flying = top_btn("Fly")
        self.btn_planning = top_btn("Plan")
        self.btn_health = top_btn("Health")
        self.btn_safety = top_btn("Safety")
        self.btn_support = top_btn("Payload Data")
        self.btn_ai_tuner = top_btn("AI Tuner")   # ★ 추가
        self.btn_fleet = top_btn("Fleet")
        self.btn_video = top_btn("Video 4CH")
        self.btn_flying.setChecked(True)

        # 조종 대상(선택 기체) 콤보
        self.cmb_active_vehicle = QComboBox()
        self.cmb_active_vehicle.setMinimumWidth(110)
        self.cmb_active_vehicle.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_active_vehicle.setMinimumContentsLength(8)
        self.cmb_active_vehicle.setToolTip("Fly / Plan / Health / AI Tuner가 제어할 기체")
        self.cmb_active_vehicle.addItem("No vehicle", None)

        self.lbl_armed_state = QLabel("Disarmed")
        self.lbl_link_state = QLabel("링크 없음")
        for badge in (self.lbl_armed_state, self.lbl_link_state):
            badge.setProperty("statusBadge", True)
            badge.setProperty("statusTone", "neutral")
        self.btn_link_controls = QPushButton("Links")
        self.btn_link_controls.setCheckable(True)
        self.btn_link_controls.setToolTip("연결 설정 열기")
        self.btn_link_controls.toggled.connect(link_controls.setVisible)

        self.cmb_link_type = QComboBox()
        self.cmb_link_type.addItems(["Serial", "Bluetooth", "UDP", "TCP"])

        self.cmb_port = QComboBox()
        self.cmb_port.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_port.setMinimumContentsLength(11)
        self._refresh_ports()

        # UDP(수신 대기) / TCP 주소 입력 (기본 14550)
        self.edit_udp_addr = QLineEdit("127.0.0.1:14550")
        self.edit_udp_addr.setMaximumWidth(190)

        # 포트 새로고침 버튼
        self.btn_port_refresh = QPushButton("↻")
        self.btn_port_refresh.setFixedWidth(28)
        self.btn_port_refresh.setToolTip("시리얼 포트 새로고침")

        # 링크는 여러 개 동시에 열 수 있다 (기체마다 1개 또는 UDP 1포트에 여러 SYSID)
        self.btn_connect = QPushButton("Add Link")
        self.btn_disconnect = QPushButton("Disconnect All")
        self.btn_disconnect.setEnabled(False)

        # Baud rate 콤보
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(
            [
                "57600",
                "115200",
                "230400",
                "460800",
                "921600",
            ]
        )
        self.baud_combo.setCurrentText("115200")  # 기본값

        # ---- 레이아웃에 배치 ----
        for button in (self.btn_flying, self.btn_planning, self.btn_health, self.btn_safety,
                       self.btn_support, self.btn_ai_tuner, self.btn_fleet, self.btn_video):
            navigation_layout.addWidget(button)
        navigation_layout.addStretch()

        vehicle_layout.addWidget(self.cmb_active_vehicle)
        vehicle_layout.addWidget(self.lbl_armed_state)
        vehicle_layout.addWidget(self.lbl_link_state)
        vehicle_layout.addWidget(self.btn_link_controls)
        tracking = QPushButton("Tracking")
        tracking.clicked.connect(self.tracking_panel.show)
        vehicle_layout.addWidget(tracking)
        guide = QPushButton("Sentinel Legacy")
        guide.setToolTip("차량 경보 및 STAGE1 시뮬레이션")
        guide.clicked.connect(self.guide_vehicle.show)
        vehicle_layout.addWidget(guide)
        self.appearance_settings = QSettings("WowDrone", "LinkerGCS")
        self.theme_button = QPushButton("테마")
        self.theme_button.setObjectName("appearanceButton")
        self.theme_button.setAccessibleName("화면 테마 설정")
        theme_menu = QMenu(self.theme_button)
        self.theme_button.setMenu(theme_menu)
        theme_group = QActionGroup(theme_menu)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for key, theme in THEMES.items():
            action = theme_menu.addAction(theme.name)
            action.setCheckable(True)
            theme_group.addAction(action)
            action.triggered.connect(lambda _checked, name=key: self._apply_theme(name, save=True))
            self.theme_actions[key] = action
        vehicle_layout.addWidget(self.theme_button)
        vehicle_layout.addStretch()

        link_layout.addWidget(QLabel("Link:"))
        link_layout.addWidget(self.cmb_link_type)
        link_layout.addWidget(self.cmb_port)

        # UDP / TCP 주소
        self.lbl_addr = QLabel("Addr:")
        link_layout.addWidget(self.lbl_addr)
        link_layout.addWidget(self.edit_udp_addr)

        # Baud 콤보
        self.lbl_baud = QLabel("Baud:")
        link_layout.addWidget(self.lbl_baud)
        link_layout.addWidget(self.baud_combo)
        link_layout.addWidget(self.btn_port_refresh)
        link_layout.addWidget(self.btn_connect)
        link_layout.addWidget(self.btn_disconnect)
        link_layout.addStretch()

        root.addWidget(header)
        root.addWidget(self.guide_vehicle.banner)

        # 링크 종류에 맞는 입력칸만 표시 (상단 바 공간 절약)
        self.cmb_link_type.currentTextChanged.connect(self._update_link_inputs)
        self._update_link_inputs(self.cmb_link_type.currentText())
        # 메인 스택 (Fly / Planning / Health / Support)
        self.main_stack = QStackedWidget()
        self.page_viewports = {}
        root.addWidget(self.main_stack, 1)

        # ---------------- Fly 페이지 ----------------
        # AHRS 위젯 먼저 생성해서 FlyTab에 넘긴다.
        self.ahrs = AHRSWidget(self)

        self.fly_tab = FlyTab(
            map_view=self.map_view_fly,
            video_view=self.video_view,
            ahrs_widget=self.ahrs,
            parent=self,
            settings=self.appearance_settings,
        )
        self.page_flying = self.fly_tab
        self.page_flying.setObjectName("page_flying")
        self._add_page(self.page_flying)

        # FlyTab 내부 주요 위젯을 MainWindow 에서도 바로 접근할 수 있게 포인터 연결
        self.lbl_connection = self.fly_tab.lbl_connection
        self.lbl_heartbeat = self.fly_tab.lbl_heartbeat
        self.lbl_mode = self.fly_tab.lbl_mode
        self.lbl_armed = self.fly_tab.lbl_armed

        self.lbl_hud_gps = self.fly_tab.lbl_hud_gps
        self.lbl_hud_temp = self.fly_tab.lbl_hud_temp
        self.lbl_hud_batt = self.fly_tab.lbl_hud_batt
        self.lbl_hud_calib = self.fly_tab.lbl_hud_calib

        self.alt_value = self.fly_tab.alt_value
        self.gs_value = self.fly_tab.gs_value
        self.yaw_value = self.fly_tab.yaw_value
        self.vs_value = self.fly_tab.vs_value
        self.dhome_value = self.fly_tab.dhome_value
        self.dmav_value = self.fly_tab.dmav_value

        self.btn_prearm = self.fly_tab.btn_prearm
        self.btn_flight_mode = self.fly_tab.btn_flight_mode
        self.flight_mode_menu = QMenu(self.btn_flight_mode)
        self.flight_mode_menu.aboutToShow.connect(self._populate_flight_mode_menu)
        self.btn_flight_mode.setMenu(self.flight_mode_menu)
        self.btn_arm_cmd = self.fly_tab.btn_arm_cmd
        self.btn_takeoff = self.fly_tab.btn_takeoff
        self.btn_land = self.fly_tab.btn_land
        self.btn_rtl = self.fly_tab.btn_rtl
        self.btn_mission_start = self.fly_tab.btn_mission_start

        self.txt_msgs = self.fly_tab.txt_msgs

        self.edit_stream_url = self.fly_tab.edit_stream_url
        video_settings = QSettings("WowDrone", "LinkerGCS")
        self.fly_tab.set_stream_input(video_settings.value("video/fly/source", "auto", str),
                                      video_settings.value("video/fly/url", "", str))
        self.btn_start_stream = self.fly_tab.btn_start_stream
        self.btn_swap_views = self.fly_tab.btn_swap_views

        # ---------------- Planning 페이지 ----------------
        self.page_planning = QWidget()
        self.page_planning.setObjectName("page_planning")
        self._add_page(self.page_planning)

        p_layout = QVBoxLayout(self.page_planning)
        p_layout.setContentsMargins(0, 0, 0, 0)

        plan_splitter = QSplitter()
        plan_splitter.setChildrenCollapsible(False)
        p_layout.addWidget(plan_splitter, 1)

        plan_left = QWidget()
        plan_left_layout = QVBoxLayout(plan_left)
        plan_left_layout.setContentsMargins(8, 8, 8, 8)

        self.wp_table = QTableWidget(0, 6)
        self.wp_table.setHorizontalHeaderLabels(
            ["#", "Lat", "Lon", "Alt (m)", "Spd (m/s)", "Hdg (°)"]
        )
        header = self.wp_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.wp_table.verticalHeader().setVisible(False)

        plan_left_layout.addWidget(self.wp_table, 1)

        plan_group = QGroupBox("Flight Plan & RTH Options")
        plan_grid = QGridLayout(plan_group)
        plan_grid.setHorizontalSpacing(10)
        plan_grid.setVerticalSpacing(6)

        lbl_wp_alt = QLabel("Default WP Alt (m)")
        lbl_wp_speed = QLabel("Cruise Speed (m/s)")
        lbl_rtl_alt = QLabel("RTH Alt (m)")
        lbl_rtl_action = QLabel("RTH Action")
        lbl_rtl_loiter = QLabel("Loiter Time (s)")

        self.spn_wp_alt = QDoubleSpinBox()
        self.spn_wp_alt.setRange(1.0, 1000.0)
        self.spn_wp_alt.setValue(50.0)

        self.spn_wp_speed = QDoubleSpinBox()
        self.spn_wp_speed.setRange(1.0, 100.0)
        self.spn_wp_speed.setValue(10.0)

        self.spn_rtl_alt = QDoubleSpinBox()
        self.spn_rtl_alt.setRange(1.0, 1000.0)
        self.spn_rtl_alt.setValue(60.0)

        self.cmb_rtl_action = QComboBox()
        self.cmb_rtl_action.addItems(
            ["Loiter then Land", "Loiter only", "Land immediately"]
        )

        self.spn_loiter_time = QDoubleSpinBox()
        self.spn_loiter_time.setRange(0.0, 3600.0)
        self.spn_loiter_time.setValue(60.0)

        self.btn_apply_plan = QPushButton("Apply Plan")
        self.btn_apply_plan.setProperty("role", "primary")
        plan_grid.addWidget(lbl_wp_alt, 0, 0)
        plan_grid.addWidget(self.spn_wp_alt, 0, 1)
        plan_grid.addWidget(lbl_wp_speed, 1, 0)
        plan_grid.addWidget(self.spn_wp_speed, 1, 1)
        plan_grid.addWidget(lbl_rtl_alt, 2, 0)
        plan_grid.addWidget(self.spn_rtl_alt, 2, 1)
        plan_grid.addWidget(lbl_rtl_action, 3, 0)
        plan_grid.addWidget(self.cmb_rtl_action, 3, 1)
        plan_grid.addWidget(lbl_rtl_loiter, 4, 0)
        plan_grid.addWidget(self.spn_loiter_time, 4, 1)
        plan_grid.addWidget(self.btn_apply_plan, 5, 0, 1, 2)
        plan_grid.setColumnStretch(1, 1)

        plan_left_layout.addWidget(plan_group)

        btn_row = QHBoxLayout()
        self.btn_add_wp = QPushButton("Add WP")
        self.btn_del_wp = QPushButton("Delete WP")
        self.btn_clear_wp = QPushButton("Clear All")
        btn_row.addWidget(self.btn_add_wp)
        btn_row.addWidget(self.btn_del_wp)
        btn_row.addWidget(self.btn_clear_wp)
        btn_row.addStretch()
        plan_left_layout.addLayout(btn_row)

        plan_splitter.addWidget(plan_left)

        # Planning 오른쪽: 지도
        plan_right = QWidget()
        plan_right_layout = QVBoxLayout(plan_right)
        plan_right_layout.setContentsMargins(0, 0, 0, 0)
        plan_right_layout.addWidget(self.map_view_plan)
        plan_splitter.addWidget(plan_right)
        plan_splitter.setStretchFactor(1, 1)
        plan_splitter.setSizes([390, 850])

        # ---------------- Health 페이지 (모듈화) ----------------
        self.page_health = HealthTab(parent=self)
        self._add_page(self.page_health)
        self.page_safety = SafetyTab(self.linker, parent=self)
        self._add_page(self.page_safety)

        # ---------------- Support 페이지 ----------------
        self.page_support = SupportTab(parent=self)
        self._add_page(self.page_support)

        # StatusBar
        sb = QStatusBar()
        sb.setObjectName("linkerStatus")
        self.setStatusBar(sb)
        self.status_label = QLabel("Ready")
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sb.addPermanentWidget(self.status_label, 1)

        link_controls.hide()
        self._set_action_buttons_enabled(False)

        # 초기에는 Fly 모드이므로 Planning 전용 컨트롤 갱신
        self._update_planning_only_controls()
        
        # Fly / Planning / Health / Support 생성 뒤에
        self.page_ai_tuner = AITunerTab(linker=self.linker, parent=self)
        self._add_page(self.page_ai_tuner)

        # ---------------- Fleet 페이지 (멀티 기체) ----------------
        self.page_fleet = FleetPanel(self.fleet, parent=self)
        self._add_page(self.page_fleet)

        # ---------------- Video 4CH 페이지 ----------------
        self.page_video = VideoGridPage(self.fleet, parent=self)
        self._add_page(self.page_video)

        # 표 갱신 타이머 (Fly 탭 기체 표)
        self._fleet_ui_timer = QTimer(self)
        self._fleet_ui_timer.setInterval(500)
        self._fleet_ui_timer.timeout.connect(self._refresh_fleet_ui)
        self._fleet_ui_timer.start()

    def _apply_theme(self, key, *, save=False):
        key = key if key in THEMES else DEFAULT_THEME
        theme = THEMES[key]
        app = QApplication.instance()
        app.setPalette(theme.palette())
        app.setStyleSheet(theme.stylesheet())
        self.theme_actions[key].setChecked(True)
        self.theme_button.setToolTip(f"화면 테마 · {theme.name}")
        if save:
            self.appearance_settings.setValue(THEME_SETTING, key)
            self.appearance_settings.sync()
        for view in (self.map_view_fly, self.map_view_plan):
            set_map_theme(view, theme)
        self.page_health.apply_theme(theme)
        self.page_support.apply_theme(theme)
        self.page_fleet.apply_theme(theme)
        self.page_ai_tuner.apply_theme(theme)
        self.theme = theme
        for index in range(self.cmb_active_vehicle.count()):
            vehicle = self.fleet.vehicle_by_str(self.cmb_active_vehicle.itemData(index))
            if vehicle is not None:
                self.cmb_active_vehicle.setItemData(index, theme.series_color(vehicle.color), Qt.ForegroundRole)
        self.fly_tab.set_dashboard_theme(key, layered=theme.layered)
        self.ahrs.layered = theme.layered
        self.ahrs.update()
        self.fly_tab.altitude_history.update()
        self._reflow_header()

    def _add_page(self, page):
        viewport = QScrollArea()
        viewport.setFrameShape(QFrame.NoFrame)
        viewport.setWidgetResizable(True)
        viewport.setWidget(page)
        self.page_viewports[page] = viewport
        self.main_stack.addWidget(viewport)

    def _fit_to_screen(self):
        available = self.screen().availableGeometry().adjusted(10, 10, -10, -10)
        title_height = self.style().pixelMetric(QStyle.PM_TitleBarHeight, None, self)
        width = min(1500, available.width())
        height = min(900, available.height() - title_height)
        self.resize(width, height)
        self.move(
            available.x() + (available.width() - width) // 2,
            available.y() + (available.height() - height - title_height) // 2,
        )

    def _reflow_header(self):
        if not self.header.minimumWidth():
            # Keep the stacked minimum after the header moves to a single row.
            self.header.setMinimumWidth(self.header.minimumSizeHint().width())
        needed = self.navigation.minimumSizeHint().width() + self.vehicle_status.minimumSizeHint().width()
        if self.width() >= needed + 32:
            direction = QBoxLayout.LeftToRight
        else:
            direction = QBoxLayout.TopToBottom
        self.header_row.setDirection(direction)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_header()

    # ------------------------------------------------------------------ #
    # 시그널 연결                                                         #
    # ------------------------------------------------------------------ #
    def _connect_signals(self):
        # 상단 탭
        self.btn_flying.clicked.connect(lambda: self.on_mode_button_clicked("fly"))
        self.btn_planning.clicked.connect(lambda: self.on_mode_button_clicked("planning"))
        self.btn_health.clicked.connect(lambda: self.on_mode_button_clicked("health"))
        self.btn_safety.clicked.connect(lambda: self.on_mode_button_clicked("safety"))
        self.btn_support.clicked.connect(lambda: self.on_mode_button_clicked("support"))
        self.btn_ai_tuner.clicked.connect(lambda: self.on_mode_button_clicked("ai_tuner"))  # ★
        self.btn_fleet.clicked.connect(lambda: self.on_mode_button_clicked("fleet"))
        self.btn_video.clicked.connect(lambda: self.on_mode_button_clicked("video"))

        # 멀티 기체
        self.fleet.vehicleAdded.connect(self.on_vehicle_added)
        self.fleet.vehicleRemoved.connect(self.on_vehicle_removed)
        self.fleet.vehicleTelemetry.connect(self.on_vehicle_telemetry)
        self.fleet.linksChanged.connect(self._update_link_summary)
        self.fleet.log.connect(self._log_msg)
        self.linker.activeChanged.connect(self.on_active_vehicle_changed)
        self.cmb_active_vehicle.currentIndexChanged.connect(self.on_active_combo_changed)
        self.page_fleet.vehicleActivated.connect(self.set_active_vehicle)
        self.fly_tab.vehicleSelected.connect(self.on_map_vehicle_clicked)
        self.fly_tab.btn_manage_fleet.clicked.connect(lambda: self.on_mode_button_clicked("fleet"))
        self.map_bridge_fly.vehicleClicked.connect(self.on_map_vehicle_clicked)
        self.map_bridge_plan.vehicleClicked.connect(self.on_map_vehicle_clicked)

        # 링크 버튼
        self.btn_connect.clicked.connect(self.on_connect_clicked)
        self.btn_disconnect.clicked.connect(self.on_disconnect_clicked)
        self.btn_port_refresh.clicked.connect(self._refresh_ports)

        # Actions 탭
        self.btn_prearm.clicked.connect(self.on_prearm_check_clicked)
        self.btn_arm_cmd.clicked.connect(self.on_arm_clicked)
        self.btn_takeoff.clicked.connect(self.on_takeoff_clicked)
        self.fly_tab.btn_takeoff_cancel.clicked.connect(self._cancel_takeoff)
        self.btn_land.clicked.connect(self.on_land_clicked)
        self.btn_rtl.clicked.connect(self.on_rtl_clicked)
        self.btn_mission_start.clicked.connect(self.on_mission_start_clicked)
        self.btn_apply_plan.clicked.connect(self.on_apply_plan_clicked)
        self.btn_add_wp.clicked.connect(self.on_add_wp_clicked)
        self.btn_del_wp.clicked.connect(self.on_del_wp_clicked)
        self.btn_clear_wp.clicked.connect(self.on_clear_wp_clicked)

        # Linker 시그널
        self.linker.connectionStateChanged.connect(self.on_connection_state_changed)
        self.linker.heartbeatUpdated.connect(self.on_heartbeat_updated)
        self.linker.attitudeUpdated.connect(self.ahrs.update_attitude)
        self.linker.telemetryUpdated.connect(self.on_telemetry_updated)
        self.page_health.streamsRequested.connect(lambda: self.linker.request_health_streams())
        self.linker.statusTextReceived.connect(self.on_status_text)
        self.linker.commandFeedback.connect(self.on_command_feedback)
        self.linker.modeChanged.connect(self.on_mode_changed)
        self.linker.armedChanged.connect(self.on_armed_changed)

        # 지도 브리지 (Fly)
        self.map_bridge_fly.searchResult.connect(self.on_search_result_fly)
        self.map_bridge_fly.searchError.connect(self.on_search_error_fly)
        self.map_bridge_fly.mapModeChanged.connect(self.on_map_mode_changed)
        # Fly 지도에서는 WP 클릭을 미션에 사용하지 않음

        # 지도 브리지 (Planning)
        self.map_bridge_plan.searchResult.connect(self.on_search_result_plan)
        self.map_bridge_plan.searchError.connect(self.on_search_error_plan)
        self.map_bridge_plan.mapModeChanged.connect(self.on_map_mode_changed)
        self.map_bridge_plan.waypointClicked.connect(self.on_map_wp_clicked)

        # Video / Swap
        self.video_view.doubleClicked.connect(self.swap_map_and_video)
        self.btn_start_stream.clicked.connect(self.on_start_stream_clicked)
        self.edit_stream_url.returnPressed.connect(self.on_start_stream_clicked)
        self.btn_swap_views.clicked.connect(self.swap_map_and_video)

    # ------------------------------------------------------------------ #
    # 상단 모드 버튼 + Planning 전용 컨트롤 갱신                         #
    # ------------------------------------------------------------------ #
    def on_mode_button_clicked(self, mode):
        for name, button, page in (
            ("fly", self.btn_flying, self.page_flying),
            ("planning", self.btn_planning, self.page_planning),
            ("health", self.btn_health, self.page_health),
            ("safety", self.btn_safety, self.page_safety),
            ("support", self.btn_support, self.page_support),
            ("ai_tuner", self.btn_ai_tuner, self.page_ai_tuner),
            ("fleet", self.btn_fleet, self.page_fleet),
            ("video", self.btn_video, self.page_video),
        ):
            button.setChecked(name == mode)
            if name == mode:
                self.main_stack.setCurrentWidget(self.page_viewports[page])

        # 탭 변경 시 Planning 전용 컨트롤 상태 재계산
        self._update_planning_only_controls()

    def _update_planning_only_controls(self):
        """
        Mission Start / Circle 옵션 등 'Planning 전용' 컨트롤을
        현재 탭/연결 상태에 맞춰 Enable/Disable.
        """

        # 현재 탭이 Planning 인지 여부
        is_planning = self.main_stack.currentWidget() is self.page_viewports[self.page_planning]

        # ── ① Mission Start 버튼 활성화 조건 ──
        #    → 연결만 되어 있으면 어떤 탭이든 사용 가능하게
        #    (Planning 탭에서만 쓰고 싶으면 self.linker.connected 대신
        #     is_planning and self.linker.connected 로 바꾸면 됨)
        self.btn_mission_start.setEnabled(self.linker.connected)

        # ── ② Circle 옵션은 여전히 Planning 전용으로 유지 ──
        if hasattr(self.fly_tab, "set_circle_controls_enabled"):
            self.fly_tab.set_circle_controls_enabled(is_planning)

    # ------------------------------------------------------------------ #
    # 시리얼 포트                                                        #
    # ------------------------------------------------------------------ #
    def _refresh_ports(self):
        kind = self.cmb_link_type.currentText().lower()
        if kind in SERIAL_LINK_KINDS:
            refresh_port_selector(self.cmb_port, kind)

    def _update_link_inputs(self, link_type: str):
        is_serial = link_type.lower() in SERIAL_LINK_KINDS
        for w in (self.cmb_port, self.lbl_baud, self.baud_combo, self.btn_port_refresh):
            w.setVisible(is_serial)
        for w in (self.lbl_addr, self.edit_udp_addr):
            w.setVisible(not is_serial)
        self._refresh_ports()
        if link_type == "UDP":
            self.edit_udp_addr.setText("127.0.0.1:14550")
        elif link_type == "TCP":
            self.edit_udp_addr.setText("127.0.0.1:5760")

    def _set_action_buttons_enabled(self, enabled: bool):
        for b in (
            self.btn_prearm,
            self.btn_flight_mode,
            self.btn_arm_cmd,
            self.btn_mission_start,
        ):
            b.setEnabled(enabled)
        self.btn_takeoff.setEnabled(enabled and not getattr(self, '_takeoff_sequence', None))
        for b in (self.btn_land, self.btn_rtl):
            b.setEnabled(enabled and self._armed)

    # ------------------------------------------------------------------ #
    # Link / ARM / Actions                                               #
    # ------------------------------------------------------------------ #
    def on_connect_clicked(self):
        """
        Add Link 버튼 눌렀을 때 호출.
        이미 열린 링크는 유지한 채 새 링크를 추가한다 (멀티 기체).
        - Serial / Bluetooth : 포트/baud
        - UDP    : 수신 대기 ip:port (한 포트에 여러 SYSID 가능)
        - TCP    : 대상 ip:port
        """
        link_type = self.cmb_link_type.currentText()
        kind = link_type.lower()

        if kind in SERIAL_LINK_KINDS:
            self._refresh_ports()
            port = self.cmb_port.currentData()
            if not port:
                message = "macOS에서 페어링한 Bluetooth SPP 포트를 선택하세요."
                self._log_msg(message)
                self.show_status(message)
                return

            # Baudrate 읽기
            try:
                baud = int(self.baud_combo.currentText())
            except Exception:
                baud = 115200

            self._log_msg(f"Connecting {link_type} {port} @ {baud}...")
            self.fleet.add_link(kind, port, baud)

        else:
            address = self.edit_udp_addr.text().strip()
            if not address:
                address = "127.0.0.1:14550" if kind == "udp" else "127.0.0.1:5760"

            self._log_msg(f"Connecting {link_type} {address}...")
            self.fleet.add_link(kind, address)

    def on_disconnect_clicked(self):
        """상단 Disconnect All 버튼 핸들러"""
        self._log_msg("Disconnect requested.")
        self.show_status("Disconnect requested.")
        try:
            self.fleet.remove_all()
        except Exception as e:
            self._log_msg(f"Disconnect error: {e}")
            self.show_status(f"Disconnect error: {e}")

    @pyqtSlot(bool)
    def on_connection_state_changed(self, connected):
        self.page_health.set_connected(connected)
        # 선택 기체 기준 연결 상태 (링크 전체 상태는 _update_link_summary)
        self.lbl_connection.setText("연결됨" if connected else "연결 끊김" if self.linker.vehicle else "—")
        if not connected:
            self.lbl_heartbeat.setText("—")
        self._set_action_buttons_enabled(connected)

        self._update_link_summary()
        v = self.linker.vehicle
        name = v.name if v is not None else "No vehicle"
        self.show_status(f"{name}: {'Connected' if connected else 'Disconnected'}")
        # 연결 상태가 바뀌면 Planning 전용 컨트롤도 다시 계산
        self._update_planning_only_controls()

    @pyqtSlot(dict)
    def on_heartbeat_updated(self, hb):
        self.lbl_heartbeat.setText("정상")
        self.on_mode_changed(hb.get("mode") or "—")
        self.lbl_armed.setText("ARMED" if hb.get("armed") else "DISARMED")

    @pyqtSlot(dict)
    @pyqtSlot(object)
    def on_telemetry_updated(self, data):
        # --- 기본 수치 표시 (HUD) ---
        alt = pick(data, "altitude", "alt", "rel_alt")
        for label, value in (
            (self.alt_value, alt),
            (self.gs_value, pick(data, "groundspeed", "ground_speed", "speed")),
            (self.yaw_value, pick(data, "yaw_deg", "yaw")),
            (self.vs_value, pick(data, "vert_speed", "climb", "vz")),
            (self.dhome_value, pick(data, "dist_to_home", "dist_home")),
            (self.dmav_value, pick(data, "dist_to_mav", "dist_mav")),
        ):
            text = f"{float(value):.1f}" if value is not None else "—"
            label.setText(text)
            label.setToolTip(text)
        if alt is not None and self.linker.connected:
            self.fly_tab.altitude_history.add(float(alt))

        gps = data.get("gps_status") or data.get("gps") or "—"
        batt_v = pick(data, "battery_v", "voltage_battery")
        batt_p = pick(data, "battery_percent", "battery_remaining")
        if batt_p is not None:
            batt_text = f"{float(batt_p):.0f}%"
            if batt_v is not None:
                batt_text += f" · {float(batt_v):.1f} V"
        elif batt_v is not None:
            batt_text = f"{float(batt_v):.1f} V"
        else:
            batt_text = "—"

        fc_temp = pick(data, "fc_temp", "temperature", "temp")
        rssi = data.get("radio_rssi")
        for label, text in (
            (self.lbl_hud_gps, str(gps)),
            (self.lbl_hud_temp, "—" if fc_temp is None else f"{float(fc_temp):.1f}"),
            (self.lbl_hud_batt, batt_text),
            (self.lbl_hud_calib, str(data.get("calib_status") or data.get("calib") or "—")),
            (self.fly_tab.radio_signal, "—" if rssi is None else str(rssi)),
        ):
            label.setText(text)
            label.setToolTip(text)

        # --- Health 탭으로 센서 데이터 전달 ---
        try:
            if hasattr(self, "page_health") and hasattr(self.page_health, "update_from_telemetry"):
                self.page_health.update_from_telemetry(data)
        except Exception as e:
            self._log_msg(f"Health update error: {e}")

        # --- 지도용 위도/경도/헤딩 처리 ---
        lat = pick(data, "lat", "latitude")
        lon = pick(data, "lon", "lng", "longitude")

        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            self.fly_tab.set_position(lat, lon)

            # 드론 마커는 on_vehicle_telemetry 에서 기체별로 그린다 (멀티 기체)

            # Home 은 선택 기체 기준 최초 1회만 GPS 에서 가져와 설정
            if not self._home_from_gps:
                js_home = (
                    f"pythonUpdateHome({float(lat):.8f}, "
                    f"{float(lon):.8f});"
                )
                for view in (self.map_view_fly, self.map_view_plan):
                    if view is not None:
                        view.page().runJavaScript(js_home)
                self._home_from_gps = True
                self._log_msg(f"Home updated from GPS: {float(lat):.6f}, {float(lon):.6f}")
                self.show_status("Home updated from GPS")

    @pyqtSlot(str)
    def on_status_text(self, text):
        self._log_msg(f"[STATUSTEXT] {text}")
        self.show_status(text)

    @pyqtSlot(str, bool, str)
    def on_command_feedback(self, cmd_name, success, message):
        prefix = "OK" if success else "FAIL"
        msg = f"[{prefix}] {message}"
        self._log_msg(msg)
        self.show_status(msg)

    @pyqtSlot(str)
    def on_mode_changed(self, mode):
        self.lbl_mode.setText(mode)
        self.lbl_mode.setToolTip(mode)
        self.btn_flight_mode.setToolTip(f"비행 모드 선택 · 현재 {mode}")

    def _populate_flight_mode_menu(self):
        self.flight_mode_menu.clear()
        vehicle = self.linker.vehicle
        if vehicle is None or not vehicle.connected:
            return
        supported = vehicle.master.mode_mapping()
        for mode in FLIGHT_MODES:
            if mode not in supported:
                continue
            action = self.flight_mode_menu.addAction(mode)
            action.setCheckable(True)
            action.setChecked(mode == self.lbl_mode.text())
            action.triggered.connect(
                lambda _checked, selected=mode: self._request_flight_mode(vehicle, selected)
            )
        if self.flight_mode_menu.isEmpty():
            self.flight_mode_menu.addAction("선택 가능한 모드 없음").setEnabled(False)

    def _request_flight_mode(self, vehicle, mode):
        if vehicle is not self.linker.vehicle or not vehicle.connected:
            self.show_status("선택 기체의 연결 상태를 확인하세요.")
            return
        if mode not in ('LAND', 'RTL') and not confirm_flight(self, f'모드 {mode}', [vehicle]):
            return
        self._log_msg(f"[{vehicle.name}] 비행 모드 변경 요청: {mode}")
        vehicle._set_mode(mode)

    @pyqtSlot(bool)
    def on_armed_changed(self, armed):
        self._armed = armed
        self.lbl_armed.setText(("ARMED" if armed else "DISARMED") if self.linker.vehicle else "—")
        self.lbl_armed_state.setText("Armed" if armed else "Disarmed")
        set_status_tone(self.lbl_armed_state, "success" if armed else "neutral")
        self.btn_arm_cmd.setText("Disarm" if armed else "Arm")
        self._set_action_buttons_enabled(self.linker.connected)

    # ------------------------------------------------------------------ #
    # PreArm Check                                                       #
    # ------------------------------------------------------------------ #
    def on_prearm_check_clicked(self):
        if not getattr(self.linker, "connected", False):
            self.show_status("Not connected.")
            return

        current_mask = 0
        try:
            if hasattr(self.linker, "get_param"):
                val = self.linker.get_param("ARMING_CHECK")
                if val is not None:
                    current_mask = int(val)
        except Exception as e:
            self._log_msg(f"ARMING_CHECK read failed: {e}")

        dlg = PreArmDialog(current_mask=current_mask, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            self.show_status("PreArm check cancelled.")
            return

        new_mask = dlg.selected_mask()
        if new_mask == current_mask:
            self.show_status(f"ARMING_CHECK unchanged ({new_mask}).")
            return

        try:
            if hasattr(self.linker, "set_param"):
                self.linker.set_param("ARMING_CHECK", int(new_mask))
                self._log_msg(f"PreArm check: ARMING_CHECK={new_mask}")
                self.show_status(f"ARMING_CHECK set to {new_mask}")
            else:
                msg = "Linker.set_param 이 구현되어 있지 않습니다."
                self._log_msg(msg)
                self.show_status(msg)
        except Exception as e:
            self._log_msg(f"Failed to set ARMING_CHECK: {e}")
            self.show_status(f"Failed to set ARMING_CHECK: {e}")

    # ------------------------------------------------------------------ #
    # 지도 브리지 핸들러                                                 #
    # ------------------------------------------------------------------ #
    @pyqtSlot(float, float, str)
    def on_search_result_fly(self, lat, lon, name):
        payload = json.dumps({"lat": lat, "lon": lon, "name": name})
        js = f"pythonSearchResult({payload});"
        self.map_view_fly.page().runJavaScript(js)
        self.show_status(f"검색 완료(Fly): {name}")

    @pyqtSlot(str)
    def on_search_error_fly(self, msg):
        safe = msg.replace("\\", "\\\\").replace("'", "\\'")
        js = f"pythonSearchError('{safe}');"
        self.map_view_fly.page().runJavaScript(js)
        self.show_status(msg)

    @pyqtSlot(float, float, str)
    def on_search_result_plan(self, lat, lon, name):
        payload = json.dumps({"lat": lat, "lon": lon, "name": name})
        js = f"pythonSearchResult({payload});"
        self.map_view_plan.page().runJavaScript(js)
        self.show_status(f"검색 완료(Plan): {name}")

    @pyqtSlot(str)
    def on_search_error_plan(self, msg):
        safe = msg.replace("\\", "\\\\").replace("'", "\\'")
        js = f"pythonSearchError('{safe}');"
        self.map_view_plan.page().runJavaScript(js)
        self.show_status(msg)

    @pyqtSlot(str)
    def on_map_mode_changed(self, mode):
        """
        map.html 에서 지도 모드가 바뀔 때 JS → Python 으로 들어오는 신호.
        여기서 다시 JS 함수 pythonUpdateCircleOptions(...) 를 호출해서
        Circle 옵션 패널/폴리라인을 초기화해 준다.
        """
        self._log_msg(f"[MAP MODE] {mode}")
        self.show_status(f"Map mode: {mode}")

        # 필요하면 Fly 지도에도 Circle 옵션 초기값 내려보내기
        try:
            m = (mode or "").upper()
            if m == "CIRCLE":
                center_lat = DEFAULT_LAT
                center_lon = DEFAULT_LON
                radius_m = 100.0

                payload = {
                    "lat": center_lat,
                    "lon": center_lon,
                    "radius": radius_m,
                    "direction": "CW",
                    "alt": 60.0,
                    "speed": 8.0,
                }

                js = f"pythonUpdateCircleOptions({json.dumps(payload)})"
                self.map_view_fly.page().runJavaScript(js)
        except Exception as e:
            self._log_msg(f"Circle options JS error: {e}")

    @pyqtSlot(float, float)
    def on_map_wp_clicked(self, lat, lon):
        row = self.wp_table.rowCount()
        self.wp_table.insertRow(row)

        def set_item(r, c, text):
            item = QTableWidgetItem(text)
            self.wp_table.setItem(r, c, item)

        set_item(row, 0, str(row + 1))
        set_item(row, 1, f"{lat:.7f}")
        set_item(row, 2, f"{lon:.7f}")
        set_item(row, 3, f"{self.spn_wp_alt.value():.1f}")
        set_item(row, 4, f"{self.spn_wp_speed.value():.1f}")
        set_item(row, 5, "0.0")

        self.show_status(f"WP added from map: #{row+1}")

        # 테이블이 바뀌었으니 지도도 재동기화
        self._sync_planning_map_waypoints()

    def _sync_planning_map_waypoints(self):
        """
        Planning 탭 WP 테이블 내용을 기준으로
        Planning 지도(WP 마커/라인)를 재구성한다.
        """
        points = []
        for row in range(self.wp_table.rowCount()):
            try:
                lat_item = self.wp_table.item(row, 1)
                lon_item = self.wp_table.item(row, 2)
                if not lat_item or not lon_item:
                    continue

                lat = float(lat_item.text())
                lon = float(lon_item.text())
                points.append([lat, lon])
            except Exception:
                continue

        payload = json.dumps(points)
        js = f"pythonSetWaypoints({payload});"
        self.map_view_plan.page().runJavaScript(js)

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #
    def on_arm_clicked(self):
        if not self.linker.connected:
            self.show_status("Not connected.")
            return
        v = self.linker.vehicle
        if not confirm_flight(self, 'DISARM' if self._armed else 'ARM', [v]):
            return
        if self._armed:
            self._log_msg("DISARM requested.")
            self.linker.disarm()
        else:
            self._log_msg("ARM requested.")
            self.linker.arm()

    def on_takeoff_clicked(self):
        v = self.linker.vehicle
        if not v or not v.connected or getattr(self, '_takeoff_sequence', None):
            return
        alt = self.fly_tab.spn_takeoff_alt.value()
        if not confirm_flight(self, f'TAKEOFF {alt:.1f} m', [v]):
            return
        sequence = TakeoffSequence(v, alt, self)
        self._takeoff_sequence = sequence
        sequence.finished.connect(self._takeoff_finished)
        self.btn_takeoff.setEnabled(False)
        self.fly_tab.btn_takeoff_cancel.setEnabled(True)

    def _cancel_takeoff(self):
        sequence = getattr(self, '_takeoff_sequence', None)
        if sequence:
            sequence.cancel()

    def _takeoff_finished(self, vehicle, ok, message):
        self._takeoff_sequence = None
        self.fly_tab.btn_takeoff_cancel.setEnabled(False)
        self._set_action_buttons_enabled(self.linker.connected)
        self.show_status(f'[{vehicle.name}] {message}')

    def on_land_clicked(self):
        if not self.linker.connected:
            self.show_status("Not connected.")
            return
        self._log_msg("LAND requested.")
        self.linker.land()

    def on_rtl_clicked(self):
        if not self.linker.connected:
            self.show_status("Not connected.")
            return
        self._log_msg("RTL requested.")
        self.linker.rtl()

    def _collect_waypoints_from_table(self):
        waypoints = []
        for row in range(self.wp_table.rowCount()):
            values = []
            for col in range(1, 6):
                item = self.wp_table.item(row, col)
                try:
                    value = float(item.text().strip()) if item else float('nan')
                except ValueError:
                    value = float('nan')
                if not math.isfinite(value):
                    raise ValueError(f"{row + 1}행 {col + 1}열: 숫자를 입력하세요")
                values.append(value)
            lat, lon, alt, speed, heading = values
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < alt <= self.spn_wp_alt.maximum()):
                raise ValueError(f"{row + 1}행: 위도, 경도 또는 고도가 범위를 벗어났습니다")
            waypoints.append(dict(seq=row, lat=lat, lon=lon, alt=alt, speed=speed, heading=heading))
        return waypoints

    def on_mission_start_clicked(self):
        v = self.linker.vehicle
        if not v or not v.connected or getattr(self, '_mission_sequence', None):
            return
        try:
            waypoints = self._collect_waypoints_from_table()
        except ValueError as exc:
            self.show_status(str(exc))
            return
        if not waypoints:
            self.show_status('미션에 웨이포인트가 없습니다.')
            return
        sequence = MissionSequence(v, waypoints, self)
        self._mission_sequence = sequence
        sequence.uploaded.connect(self._mission_uploaded)
        sequence.finished.connect(self._mission_finished)
        self.btn_mission_start.setEnabled(False)

    def _mission_uploaded(self, vehicle):
        sequence = self._mission_sequence
        if confirm_flight(self, '미션 시작', [vehicle]):
            sequence.start_confirmed()
        else:
            sequence.cancel()

    def _mission_finished(self, vehicle, ok, message):
        self._mission_sequence = None
        self.btn_mission_start.setEnabled(self.linker.connected)
        self.show_status(f'[{vehicle.name}] {message}')

    def on_apply_plan_clicked(self):
        alt = self.spn_wp_alt.value()
        speed = self.spn_wp_speed.value()
        rtl_alt = self.spn_rtl_alt.value()
        rtl_action = self.cmb_rtl_action.currentText()
        loiter = self.spn_loiter_time.value()

        msg = (
            f"Plan updated: WP Alt={alt:.1f}m, Speed={speed:.1f}m/s, "
            f"RTL Alt={rtl_alt:.1f}m, RTL Action={rtl_action}, Loiter={loiter:.0f}s"
        )
        self._log_msg(msg)
        self.show_status(msg)

    def on_add_wp_clicked(self):
        row = self.wp_table.rowCount()
        self.wp_table.insertRow(row)

        def set_item(r, c, text):
            item = QTableWidgetItem(text)
            self.wp_table.setItem(r, c, item)

        set_item(row, 0, str(row + 1))
        set_item(row, 1, f"{DEFAULT_LAT:.7f}")
        set_item(row, 2, f"{DEFAULT_LON:.7f}")
        set_item(row, 3, f"{self.spn_wp_alt.value():.1f}")
        set_item(row, 4, f"{self.spn_wp_speed.value():.1f}")
        set_item(row, 5, "0.0")

        # 지도 재동기화
        self._sync_planning_map_waypoints()

    def on_del_wp_clicked(self):
        row = self.wp_table.currentRow()
        if row < 0:
            return
        self.wp_table.removeRow(row)
        for r in range(self.wp_table.rowCount()):
            item = self.wp_table.item(r, 0)
            if not item:
                item = QTableWidgetItem()
                self.wp_table.setItem(r, 0, item)
            item.setText(str(r + 1))

        # 지도 재동기화
        self._sync_planning_map_waypoints()

    def on_clear_wp_clicked(self):
        self.wp_table.setRowCount(0)
        # 테이블이 비었으니 지도도 초기화
        self._sync_planning_map_waypoints()

    def on_start_stream_clicked(self):
        url = self.edit_stream_url.text().strip()
        if not url:
            self._stop_fly_video()
            self.fly_tab.show_video_empty_state()
            return
        source = self.fly_tab.cmb_stream_source.currentData()
        if source == "gstreamer":
            url_to_open = f"gst-pipeline://{url}"
        elif source in ("udp", "rtp-h264", "rtp-h265") and url.isdecimal():
            host = "@:" if source == "udp" else ""
            url_to_open = f"{source}://{host}{url}"
        else:
            url_to_open = url
        self._stop_fly_video()
        settings = QSettings("WowDrone", "LinkerGCS")
        settings.setValue("video/fly/source", source)
        settings.setValue("video/fly/url", url)
        settings.sync()
        self.video_view.clear("영상 연결 중...")
        worker = VideoWorker(url_to_open, "udp" if source == "rtsp-udp" else "tcp")
        self._fly_video_worker = worker
        worker.attach_canvas(self.video_view)
        worker.statusChanged.connect(self._fly_video_status)
        worker.start()

    def _fly_video_status(self, text, ok):
        self.show_status(f"영상: {text}")
        if not ok and self.video_view.image is None:
            self.video_view.clear(text)

    def _stop_fly_video(self):
        worker, self._fly_video_worker = self._fly_video_worker, None
        if worker is None:
            return
        worker.detach_canvas()
        worker.statusChanged.disconnect(self._fly_video_status)
        self._retired_video_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._retired_video_workers.discard(w))
        worker.stop()
        if not worker.isRunning():
            self._retired_video_workers.discard(worker)

    # ------------------------------------------------------------------ #
    # Video <-> Map 스와핑                                               #
    # ------------------------------------------------------------------ #
    def swap_map_and_video(self):
        if self.main_stack.currentWidget() is not self.page_viewports[self.page_flying]:
            self.show_status("영상/지도 스왑은 Fly 모드에서만 가능합니다.")
            return

        self.video_on_main = not self.video_on_main
        self.fly_tab.set_video_on_main(self.video_on_main)
        self.show_status("중앙 영상 · 오른쪽 지도" if self.video_on_main else "중앙 지도 · 오른쪽 영상")

    # ------------------------------------------------------------------ #
    # 멀티 기체                                                          #
    # ------------------------------------------------------------------ #
    def on_vehicle_added(self, v):
        self.cmb_active_vehicle.blockSignals(True)
        if self.cmb_active_vehicle.findData(None) >= 0 and self.linker.vehicle is None:
            self.cmb_active_vehicle.removeItem(self.cmb_active_vehicle.findData(None))
        self.cmb_active_vehicle.addItem(f"● {v.name}", v.key_str)
        idx = self.cmb_active_vehicle.count() - 1
        self.cmb_active_vehicle.setItemData(idx, self.theme.series_color(v.color), Qt.ForegroundRole)
        self.cmb_active_vehicle.blockSignals(False)

        # 처음 발견된 기체는 자동으로 선택
        if self.linker.vehicle is None:
            self.set_active_vehicle(v)
        self._refresh_fleet_ui()

    def on_vehicle_removed(self, v):
        idx = self.cmb_active_vehicle.findData(v.key_str)
        if idx >= 0:
            self.cmb_active_vehicle.removeItem(idx)
        js = f"pythonRemoveVehicle('{v.key_str}');"
        for view in (self.map_view_fly, self.map_view_plan):
            view.page().runJavaScript(js)
        self._map_last_push.pop(v.key_str, None)

        if self.linker.vehicle is v:
            remaining = self.fleet.vehicle_list()
            self.set_active_vehicle(remaining[0] if remaining else None)
        if self.cmb_active_vehicle.count() == 0:
            self.cmb_active_vehicle.addItem("No vehicle", None)
        self._refresh_fleet_ui()

    def set_active_vehicle(self, v):
        self.linker.set_vehicle(v)

    def on_active_combo_changed(self, _idx):
        key = self.cmb_active_vehicle.currentData()
        v = self.fleet.vehicle_by_str(key)
        if v is not None and v is not self.linker.vehicle:
            self.set_active_vehicle(v)

    def _sentinel_legacy_status(self):
        """Read-only Sentinel Legacy link text for the Tracking views (None when unused)."""
        session = getattr(getattr(self, "guide_vehicle", None), "session", None)
        if session is None:
            return None
        return "동기화" if session.synchronized else "동기화 대기"

    def on_map_vehicle_clicked(self, key_str):
        v = self.fleet.vehicle_by_str(key_str)
        if v is not None:
            self.set_active_vehicle(v)

    def on_active_vehicle_changed(self, v):
        self.tracking_panel.set_vehicle(v)
        self.page_health.reset_vehicle(v)
        key = v.key_str if v is not None else None
        idx = self.cmb_active_vehicle.findData(key)
        if idx >= 0:
            self.cmb_active_vehicle.blockSignals(True)
            self.cmb_active_vehicle.setCurrentIndex(idx)
            self.cmb_active_vehicle.blockSignals(False)
        self.page_fleet.set_active_key(key)
        self.fly_tab.reset_telemetry(v.name if v is not None else "기체 미선택")
        self.btn_flight_mode.setToolTip("비행 모드 선택")
        if v is None:
            self.on_armed_changed(False)
        self.fly_tab.refresh_vehicles(self.fleet.vehicle_list(), key)

        # 선택 기체의 Home 으로 다시 맞추고, 마커 하이라이트 갱신
        self._home_from_gps = False
        for veh in self.fleet.vehicle_list():
            self._map_last_push.pop(veh.key_str, None)
            self.on_vehicle_telemetry(veh)
        if v is not None:
            self._log_msg(f"Active vehicle → {v.name} ({v.link_name})")
            js = f"pythonFocusVehicle('{v.key_str}');"
            self.map_view_fly.page().runJavaScript(js)

    def on_vehicle_telemetry(self, v):
        """모든 기체의 위치를 지도에 표시 (기체당 최대 5Hz)"""
        if v.last_lat is None or v.last_lon is None:
            return
        if v.last_lat == 0 and v.last_lon == 0:
            return
        now = time.time()
        if now - self._map_last_push.get(v.key_str, 0) < 0.2:
            return
        self._map_last_push[v.key_str] = now

        active = "true" if v is self.linker.vehicle else "false"
        lost = "false" if v.connected else "true"
        label = json.dumps(f"{v.name} {v.altitude:.0f}m")
        js = (
            f"pythonUpdateVehicle('{v.key_str}', {v.last_lat:.8f}, {v.last_lon:.8f}, "
            f"{float(v.yaw_deg):.1f}, '{v.color}', {label}, {active}, {lost});"
        )
        for view in (self.map_view_fly, self.map_view_plan):
            view.page().runJavaScript(js)

    def _update_link_summary(self):
        n_links = len(self.fleet.links)
        n_open = sum(1 for s in self.fleet.link_states.values() if s == "open")
        n_veh = len(self.fleet.vehicles)
        n_ok = sum(1 for v in self.fleet.vehicles.values() if v.connected)
        self.lbl_link_state.setText(f"링크 {n_open}/{n_links} · 기체 {n_ok}/{n_veh}" if n_links else "링크 없음")
        tone = "success" if n_ok else "warning" if n_links else "neutral"
        set_status_tone(self.lbl_link_state, tone)
        self._reflow_header()
        self.btn_disconnect.setEnabled(n_links > 0)

    def _refresh_fleet_ui(self):
        v = self.linker.vehicle
        self.fly_tab.refresh_vehicles(self.fleet.vehicle_list(), v.key_str if v is not None else None)
        self._update_link_summary()

    def closeEvent(self, event):
        try:
            self.appearance_settings.sync()
            self.page_ai_tuner.shutdown()
            self.tracking_panel.shutdown()
            self.guide_vehicle.shutdown()
            self.guide_vehicle.close_dashboard()
            self.fly_tab.close_fullscreen()
            self._stop_fly_video()
            for worker in list(self._retired_video_workers):
                worker.wait()
            self.page_video.shutdown()
            self.fleet.remove_all()
        finally:
            super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # 로그 / 상태                                                         #
    # ------------------------------------------------------------------ #
    def _log_msg(self, text: str):
        self.txt_msgs.append(text)
        self.txt_msgs.moveCursor(self.txt_msgs.textCursor().End)

    def show_status(self, text: str):
        self.status_label.setText(text)
        self.status_label.setToolTip(text)


# ---------------------------------------------------------------------- #
# main                                                                   #
# ---------------------------------------------------------------------- #

def main():
    if sys.argv[1:2] == ["--guide-vehicle"]:
        from guide_vehicle import main as guide_main
        sys.exit(guide_main(sys.argv[2:]))
    prepare_env()
    app = QApplication(sys.argv)
    app.setApplicationName("LINKER")
    app.setApplicationDisplayName("LINKER")
    app.setWindowIcon(QIcon(str(BASE_DIR / "LINKER.icns")))
    setup_fonts()

    # 로고 경로 (BASE_DIR 는 파일 상단에서 정의돼 있음)
    logo_path = str(BASE_DIR / "logo1.png")

    # 스플래시 생성
    splash = LinkerSplashScreen(
        version="v0.1.0",
        logo_path=logo_path,
    )
    splash.show()
    app.processEvents()

    # 메인 윈도우 생성
    win = MainWindow()

    # 스플래시 로딩 끝난 뒤 실행할 콜백
    def show_main():
        win.show()
        splash.finish(win)

    # ProgressBar 애니메이션 시작 (약 3초 정도 연출)
    splash.start(on_finished=show_main, interval_ms=30)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
