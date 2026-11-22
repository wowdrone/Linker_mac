"""Flight dashboard and selected-vehicle telemetry."""
from collections import deque
import math
from pathlib import Path
import time

from PyQt5.QtCore import QPointF, QRectF, QSettings, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QShortcut, QSizePolicy,
    QSplitter, QSplitterHandle, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from gui_theme import DEFAULT_THEME

FLY_VIDEO_SOURCES = (
    ("auto", "자동 / 직접 입력", "스트림 주소, 카메라 번호 또는 파일 경로", ""),
    ("rtsp-tcp", "RTSP (TCP)", "rtsp://카메라주소:8554/live", ""),
    ("rtsp-udp", "RTSP (UDP)", "rtsp://카메라주소:8554/live", ""),
    ("udp", "UDP (MPEG-TS)", "udp://@:5600", "udp://@:5600"),
    ("rtp-h264", "RTP (H.264)", "rtp-h264://5600", "rtp-h264://5600"),
    ("rtp-h265", "RTP (H.265)", "rtp-h265://5600", "rtp-h265://5600"),
    ("gstreamer", "GStreamer", "videotestsrc is-live=true ! video/x-raw", ""),
    ("http", "HTTP (MJPEG / HLS)", "http://카메라주소/stream", ""),
    ("camera", "카메라 장치", "카메라 번호 (0, 1, …)", "0"),
    ("file", "영상 파일", "/Users/이름/Movies/video.mp4", ""),
)


def label(text, role=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if role:
        widget.setProperty("role", role)
    return widget


def card(title=None):
    frame = QFrame()
    frame.setProperty("dashboardCard", True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(9, 7, 9, 8)
    layout.setSpacing(6)
    if title:
        layout.addWidget(label(title, "title"))
    return frame, layout


class DashboardHandle(QSplitterHandle):
    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self.setToolTip("드래그하여 카드 크기 조절")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        color = self.palette().link().color() if self.underMouse() else self.palette().placeholderText().color()
        if not self.underMouse():
            color.setAlpha(170)
        painter.setBrush(color)
        center = QRectF(self.rect()).center()
        vertical_handle = self.orientation() == Qt.Horizontal
        for offset in (-5, 0, 5):
            point = center + (QPointF(0, offset) if vertical_handle else QPointF(offset, 0))
            painter.drawEllipse(point, 1.3, 1.3)


class DashboardSplitter(QSplitter):
    def __init__(self, orientation):
        super().__init__(orientation)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(7)

    def createHandle(self):
        return DashboardHandle(self.orientation(), self)


DEFAULT_SPLITTER_SIZES = {
    False: {
        "columns": [213, 758, 312], "vehicles": [240, 495],
        "cockpit": [550, 155], "monitoring": [460, 145, 166], "metrics": [1, 1, 1],
    },
    True: {
        "columns": [306, 620, 322], "vehicles": [310, 165, 300],
        "cockpit": [590, 170], "monitoring": [590, 170], "metrics": [1, 1, 1],
    },
}


class AltitudeHistory(QWidget):
    """Draw the last minute of altitude samples for the selected vehicle."""

    WINDOW_SECONDS = 60

    def __init__(self):
        super().__init__()
        self.samples = deque(maxlen=self.WINDOW_SECONDS + 1)
        self.setMinimumHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def sizeHint(self):
        return QSize(230, 132)

    def clear(self):
        self.samples.clear()
        self.update()

    def add(self, altitude):
        now = time.monotonic()
        if not math.isfinite(altitude) or (self.samples and now - self.samples[-1][0] < 1):
            return
        self.samples.append((now, altitude))
        while now - self.samples[0][0] > self.WINDOW_SECONDS:
            self.samples.popleft()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        plot = QRectF(self.rect()).adjusted(37, 8, -10, -21)
        values = [value for _, value in self.samples]
        low = min(0, min(values, default=0))
        high = max(30, max(values, default=0))
        step = max(5, math.ceil((high - low) / 15) * 5)
        low = math.floor(low / step) * step
        high = math.ceil(high / step) * step
        for index in range(4):
            y = plot.top() + index * plot.height() / 3
            painter.setPen(self.palette().mid().color())
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(self.palette().placeholderText().color())
            value = high - index * (high - low) / 3
            painter.drawText(QRectF(0, y - 8, 32, 16), Qt.AlignRight | Qt.AlignVCenter, f"{value:.0f}")
        painter.drawText(QRectF(plot.left(), plot.bottom() + 4, plot.width(), 16), Qt.AlignLeft, "−60 s")
        painter.drawText(QRectF(plot.left(), plot.bottom() + 4, plot.width(), 16), Qt.AlignRight, "현재")
        if not self.samples:
            painter.drawText(plot, Qt.AlignCenter, "고도 데이터 대기 중")
            return

        latest = self.samples[-1][0]
        points = [QPointF(
            plot.right() - (latest - timestamp) * plot.width() / self.WINDOW_SECONDS,
            plot.bottom() - (value - low) * plot.height() / (high - low),
        ) for timestamp, value in self.samples]
        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        area = QPainterPath(line)
        area.lineTo(points[-1].x(), plot.bottom())
        area.lineTo(points[0].x(), plot.bottom())
        area.closeSubpath()
        fill = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        shade = self.palette().link().color()
        shade.setAlpha(95)
        fill.setColorAt(0, shade)
        shade.setAlpha(8)
        fill.setColorAt(1, shade)
        painter.fillPath(area, fill)
        painter.setPen(QPen(self.palette().link().color(), 2))
        painter.drawPath(line)
        painter.setBrush(self.palette().link().color())
        painter.drawEllipse(points[-1], 2.5, 2.5)


class FlyTab(QWidget):
    vehicleSelected = pyqtSignal(str)

    def __init__(self, ahrs_widget, map_view, video_view, parent=None, *, settings=None):
        super().__init__(parent)
        self.setObjectName("page_flying")
        self.setAttribute(Qt.WA_StyledBackground)
        self.ahrs = ahrs_widget
        self.map_view = map_view
        self.video_view = video_view
        self._vehicle_rows = ()
        self._vehicle_key = None
        self._fullscreen_origin = None
        self._layered = False
        self._dashboard_theme = None
        self._dashboard_settings = settings if settings is not None else QSettings("WowDrone", "LinkerGCS")
        self._dashboard_splitters = {}
        self._build_ui()
        self._build_fullscreen()
        self.show_video_empty_state()

    def _build_ui(self):
        self.dashboard = QVBoxLayout(self)
        self.dashboard.setContentsMargins(7, 7, 7, 7)
        self.splitter = self._make_splitter("columns", Qt.Horizontal)
        self.dashboard.addWidget(self.splitter)
        self.sidebar = self._build_sidebar()
        self.cockpit = self._build_cockpit()
        self.monitoring = self._build_monitoring()
        for column in (self.sidebar, self.cockpit, self.monitoring):
            self.splitter.addWidget(column)
        self.splitter.setStretchFactor(1, 1)
        self.set_dashboard_theme(DEFAULT_THEME, layered=False)
        self._build_details()

    def _make_splitter(self, name, orientation):
        splitter = DashboardSplitter(orientation)
        splitter.splitterMoved.connect(lambda _pos, _index: self._save_splitter_sizes(name, splitter))
        self._dashboard_splitters[name] = splitter
        return splitter

    def _splitter_key(self, name):
        return f"appearance/fly/{self._dashboard_theme}/{name}"

    def _save_splitter_sizes(self, name, splitter):
        if (self.altitude_card.parentWidget() is splitter and
                not self.btn_altitude_toggle.isChecked()):
            return
        sizes = splitter.sizes()
        if all(sizes):
            self._dashboard_settings.setValue(self._splitter_key(name), sizes)

    def set_dashboard_theme(self, key, *, layered):
        if self._dashboard_theme == key and self._layered == layered:
            return
        self._set_layered_layout(layered)
        self._dashboard_theme = key
        self.altitude_card.setMaximumHeight(16777215)
        self.altitude_history.show()
        defaults = DEFAULT_SPLITTER_SIZES[layered]
        for name, splitter in self._dashboard_splitters.items():
            stored = self._dashboard_settings.value(self._splitter_key(name), defaults[name])
            try:
                sizes = [int(size) for size in stored] if isinstance(stored, (tuple, list)) else []
            except (TypeError, ValueError, OverflowError):
                sizes = []
            if len(sizes) != splitter.count() or not all(0 < size < 16777215 for size in sizes):
                sizes = defaults[name]
            splitter.setSizes(sizes)
        expanded = self._dashboard_settings.value(self._splitter_key("altitudeExpanded"), True, bool)
        self.btn_altitude_toggle.blockSignals(True)
        self.btn_altitude_toggle.setChecked(expanded)
        self.btn_altitude_toggle.blockSignals(False)
        self._set_altitude_expanded(expanded, save=False)

    def _set_altitude_expanded(self, expanded, *, save=True):
        splitter = self.altitude_card.parentWidget()
        name = "cockpit" if self._layered else "monitoring"
        if save and not expanded:
            sizes = splitter.sizes()
            if all(sizes):
                self._dashboard_settings.setValue(self._splitter_key(name), sizes)
        self.altitude_history.setVisible(expanded)
        self.btn_altitude_toggle.setText("접기 ▴" if expanded else "펼치기 ▾")
        self.btn_altitude_toggle.setAccessibleName("고도 프로파일 접기" if expanded else "고도 프로파일 펼치기")
        if expanded:
            self.altitude_card.setMaximumHeight(16777215)
            sizes = self._dashboard_settings.value(self._splitter_key(name),
                                                   DEFAULT_SPLITTER_SIZES[self._layered][name])
            try:
                sizes = [int(size) for size in sizes]
                if len(sizes) == splitter.count() and all(size > 0 for size in sizes):
                    splitter.setSizes(sizes)
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            self.altitude_card.setMaximumHeight(self.altitude_card.minimumSizeHint().height())
        if save:
            self._dashboard_settings.setValue(self._splitter_key("altitudeExpanded"), expanded)

    def _set_layered_layout(self, enabled):
        if self._layered == enabled:
            return
        self._layered = enabled
        if enabled:
            self.sidebar.insertWidget(0, self.instruments)
            self.cockpit.addWidget(self.altitude_card)
        else:
            self.cockpit.addWidget(self.instruments)
            self.monitoring.addWidget(self.altitude_card)
        self.instrument_layout.setDirection(QVBoxLayout.TopToBottom if enabled else QVBoxLayout.LeftToRight)
        self.instrument_layout.setStretch(0, 1 if enabled else 2)
        self.instrument_layout.setStretch(1, 0 if enabled else 5)
        self.ahrs.setMinimumHeight(194 if enabled else 138)
        self.ahrs.setMaximumWidth(16777215 if enabled else 185)
        self.vehicle_silhouette.setVisible(not enabled)
        self.sidebar.setMinimumWidth(247 if enabled else 155)
        self.dashboard.setContentsMargins(*(12, 10, 12, 10) if enabled else (7, 7, 7, 7))
        self.splitter.setHandleWidth(9 if enabled else 7)

    def _build_sidebar(self):
        sidebar = self._make_splitter("vehicles", Qt.Vertical)
        sidebar.setMinimumWidth(155)

        fleet_card, fleet_layout = card()
        heading = QHBoxLayout()
        self.fleet_title = label("기체 목록 (0)", "title")
        heading.addWidget(self.fleet_title, 1)
        self.btn_manage_fleet = QPushButton("+")
        self.btn_manage_fleet.setToolTip("Fleet에서 링크와 기체 관리")
        self.btn_manage_fleet.setAccessibleName("기체 및 링크 관리")
        heading.addWidget(self.btn_manage_fleet)
        fleet_layout.addLayout(heading)
        self.vehicle_list = QListWidget()
        self.vehicle_list.setMinimumHeight(85)
        self.vehicle_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.vehicle_list.setTextElideMode(Qt.ElideRight)
        self.vehicle_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.vehicle_list.currentItemChanged.connect(self._select_list_item)
        fleet_layout.addWidget(self.vehicle_list, 1)
        self.empty_fleet = label("연결된 기체 없음", "caption")
        fleet_layout.addWidget(self.empty_fleet)
        sidebar.addWidget(fleet_card)

        status_card, status_layout = card("선택 기체")
        self.vehicle_name = label("기체 미선택", "vehicle")
        self.vehicle_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status_layout.addWidget(self.vehicle_name)
        self.vehicle_silhouette = QLabel()
        self.vehicle_silhouette.setAlignment(Qt.AlignCenter)
        self.vehicle_silhouette.setPixmap(QPixmap(str(Path(__file__).with_name("quad.png"))).scaled(
            70, 70, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        status_layout.addWidget(self.vehicle_silhouette)

        self.lbl_mode = label("—")
        self.lbl_armed = label("—")
        self.position = label("—")
        self.lbl_hud_batt = label("—")
        self.lbl_hud_gps = label("—")
        self.lbl_connection = label("—")
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for row, (title, value) in enumerate((
            ("모드", self.lbl_mode), ("시동", self.lbl_armed), ("위치", self.position),
            ("배터리", self.lbl_hud_batt), ("GPS", self.lbl_hud_gps), ("링크", self.lbl_connection),
        )):
            grid.addWidget(label(title, "caption"), row, 0, Qt.AlignTop)
            value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        status_layout.addLayout(grid)
        status_layout.addStretch(1)
        sidebar.addWidget(status_card)
        return sidebar

    def _build_cockpit(self):
        cockpit = self._make_splitter("cockpit", Qt.Vertical)

        map_card, map_layout = card()
        self.map_title = label("운항 지도", "title")
        heading = QHBoxLayout()
        heading.addWidget(self.map_title, 1)
        self.btn_map_fullscreen = self._fullscreen_button()
        heading.addWidget(self.btn_map_fullscreen)
        map_layout.addLayout(heading)
        self.map_body = QVBoxLayout()
        self.map_body.setContentsMargins(0, 0, 0, 0)
        self.map_view.setMinimumSize(215, 188)
        self.map_body.addWidget(self.map_view)
        map_layout.addLayout(self.map_body, 1)
        self.btn_map_fullscreen.clicked.connect(lambda: self.show_panel_fullscreen(self.map_body))
        cockpit.addWidget(map_card)

        self.instruments, instrument_layout = card()
        self.instrument_layout = instrument_layout
        instrument_layout.setDirection(QVBoxLayout.LeftToRight)
        instrument_layout.setContentsMargins(7, 7, 7, 7)
        self.ahrs.setMinimumSize(120, 138)
        self.ahrs.setMaximumWidth(185)
        instrument_layout.addWidget(self.ahrs, 2)

        panel = QVBoxLayout()
        panel.setSpacing(7)
        stats = self._make_splitter("metrics", Qt.Horizontal)
        stats.setHandleWidth(6)
        for attr, caption, metric in (
            ("gs_value", "속도 m/s", "speed"), ("alt_value", "고도 m", "altitude"),
            ("dhome_value", "홈 거리 m", "distance"),
        ):
            box, box_layout = card()
            box.setProperty("flightMetric", metric)
            box_layout.setContentsMargins(5, 5, 5, 5)
            box_layout.setSpacing(1)
            value = label("—", "metric")
            value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            box_layout.addWidget(label(caption, "caption"))
            box_layout.addWidget(value)
            setattr(self, attr, value)
            stats.addWidget(box)
        panel.addWidget(stats, 1)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.btn_flight_mode = QPushButton("모드")
        self.btn_flight_mode.setAccessibleName("비행 모드 선택")
        self.btn_flight_mode.setToolTip("비행 모드 선택")
        self.btn_arm_cmd = QPushButton("Arm")
        self.btn_takeoff = QPushButton("이륙")
        from PyQt5.QtWidgets import QDoubleSpinBox
        self.spn_takeoff_alt = QDoubleSpinBox()
        self.spn_takeoff_alt.setRange(1, 120)
        self.spn_takeoff_alt.setValue(10)
        self.spn_takeoff_alt.setSuffix(" m")
        self.btn_takeoff_cancel = QPushButton("취소")
        self.btn_takeoff_cancel.setEnabled(False)
        self.btn_takeoff.setProperty("role", "primary")
        self.btn_land = QPushButton("착륙")
        self.btn_rtl = QPushButton("RTL")
        self.btn_rtl.setProperty("role", "return")
        self.btn_rtl.setToolTip("복귀 (Return to Launch)")
        actions.addWidget(self.spn_takeoff_alt)
        for button in (self.btn_flight_mode, self.btn_arm_cmd, self.btn_takeoff, self.btn_takeoff_cancel, self.btn_land, self.btn_rtl):
            button.setProperty("flightAction", True)
            actions.addWidget(button, 1)
        panel.addLayout(actions)
        instrument_layout.addLayout(panel, 5)
        cockpit.addWidget(self.instruments)
        return cockpit

    def _build_monitoring(self):
        monitoring = self._make_splitter("monitoring", Qt.Vertical)
        monitoring.setMinimumWidth(208)

        video, video_layout = card()
        heading = QHBoxLayout()
        self.video_title = label("실시간 영상", "title")
        heading.addWidget(self.video_title, 1)
        self.btn_swap_views = QPushButton("↔")
        self.btn_swap_views.setToolTip("지도와 영상 패널 바꾸기")
        self.btn_swap_views.setAccessibleName("지도와 영상 패널 바꾸기")
        heading.addWidget(self.btn_swap_views)
        self.btn_video_fullscreen = self._fullscreen_button()
        heading.addWidget(self.btn_video_fullscreen)
        video_layout.addLayout(heading)
        self.video_body = QVBoxLayout()
        self.video_body.setContentsMargins(0, 0, 0, 0)
        self.video_view.setMinimumHeight(112)
        self.video_body.addWidget(self.video_view)
        video_layout.addLayout(self.video_body, 1)
        self.btn_video_fullscreen.clicked.connect(lambda: self.show_panel_fullscreen(self.video_body))
        self.cmb_stream_source = QComboBox()
        self.cmb_stream_source.setObjectName("flyVideoSource")
        self.cmb_stream_source.setAccessibleName("영상 입력 소스")
        for source, name, _, _ in FLY_VIDEO_SOURCES:
            self.cmb_stream_source.addItem(name, source)
        video_layout.addWidget(self.cmb_stream_source)

        stream = QHBoxLayout()
        self.edit_stream_url = QLineEdit()
        self.edit_stream_url.setAccessibleName("영상 입력 주소 또는 파이프라인")
        stream.addWidget(self.edit_stream_url, 1)
        self.btn_start_stream = QPushButton("열기")
        self.btn_start_stream.setObjectName("flyOpenStream")
        stream.addWidget(self.btn_start_stream)
        video_layout.addLayout(stream)
        self._stream_source = None
        self._stream_inputs = {}
        self.cmb_stream_source.currentIndexChanged.connect(self._select_stream_source)
        self._select_stream_source(0)
        monitoring.addWidget(video)

        health, health_layout = card("텔레메트리")
        self.lbl_heartbeat = label("—")
        self.lbl_hud_temp = label("—")
        self.lbl_hud_calib = label("—")
        self.radio_signal = label("—")
        self.yaw_value = label("—")
        self.vs_value = label("—")
        self.dmav_value = label("—")
        grid = QGridLayout()
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(4)
        for index, (caption, value) in enumerate((
            ("Heartbeat", self.lbl_heartbeat), ("FC °C", self.lbl_hud_temp),
            ("보정", self.lbl_hud_calib), ("RSSI", self.radio_signal),
            ("방위 °", self.yaw_value), ("수직 m/s", self.vs_value),
            ("기체 거리 m", self.dmav_value),
        )):
            row, column = divmod(index, 2)
            grid.addWidget(label(caption, "caption"), row, column * 2)
            value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(value, row, column * 2 + 1)
            grid.setColumnStretch(column * 2 + 1, 1)
        health_layout.addLayout(grid)
        self.btn_details = QPushButton("비행 점검 및 로그")
        self.btn_details.clicked.connect(self._show_details)
        health_layout.addWidget(self.btn_details)
        monitoring.addWidget(health)

        self.altitude_card, altitude_layout = card()
        heading = QHBoxLayout()
        heading.addWidget(label("고도 프로파일 · m", "title"), 1)
        self.btn_altitude_toggle = QPushButton("접기 ▴")
        self.btn_altitude_toggle.setCheckable(True)
        self.btn_altitude_toggle.setChecked(True)
        self.btn_altitude_toggle.toggled.connect(self._set_altitude_expanded)
        heading.addWidget(self.btn_altitude_toggle)
        altitude_layout.addLayout(heading)
        self.altitude_history = AltitudeHistory()
        altitude_layout.addWidget(self.altitude_history, 1)
        monitoring.addWidget(self.altitude_card)
        return monitoring

    def _build_details(self):
        self.details = QDialog(self)
        self.details.setObjectName("flightDetails")
        self.details.setWindowTitle("비행 점검 및 로그")
        self.details.resize(540, 340)
        layout = QVBoxLayout(self.details)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        checks = QWidget()
        row = QHBoxLayout(checks)
        self.btn_prearm = QPushButton("PreArm Check")
        self.btn_mission_start = QPushButton("Mission Start")
        for button in (self.btn_prearm, self.btn_mission_start):
            row.addWidget(button, 0, Qt.AlignTop)
        tabs.addTab(checks, "점검")
        self.txt_msgs = QTextEdit()
        self.txt_msgs.setReadOnly(True)
        tabs.addTab(self.txt_msgs, "로그")

    def _show_details(self):
        self.details.show()
        self.details.raise_()
        self.details.activateWindow()

    def _select_list_item(self, item, _previous):
        if item is not None:
            self.vehicleSelected.emit(item.data(Qt.UserRole))

    def refresh_vehicles(self, vehicles, active_key):
        rows = tuple((v.key_str, v.name, v.last_mode, v.connected) for v in vehicles)
        if (rows, active_key) == (self._vehicle_rows, self._vehicle_key):
            return
        self._vehicle_rows, self._vehicle_key = rows, active_key
        self.fleet_title.setText(f"기체 목록 ({len(rows)})")
        self.empty_fleet.setVisible(not rows)
        self.vehicle_list.blockSignals(True)
        try:
            self.vehicle_list.clear()
            for key, name, mode, connected in rows:
                state = mode if connected else "연결 끊김"
                item = QListWidgetItem(f"{'●' if connected else '○'}  {name}   ·   {state}")
                item.setToolTip(f"{name} · {state}")
                item.setData(Qt.UserRole, key)
                self.vehicle_list.addItem(item)
                if key == active_key:
                    self.vehicle_list.setCurrentItem(item)
        finally:
            self.vehicle_list.blockSignals(False)

    def reset_telemetry(self, name):
        self.vehicle_name.setText(name)
        self.vehicle_name.setToolTip(name)
        for value in (
            self.lbl_mode, self.lbl_armed, self.position, self.lbl_hud_batt, self.lbl_hud_gps,
            self.lbl_connection, self.lbl_heartbeat, self.lbl_hud_temp, self.lbl_hud_calib,
            self.radio_signal, self.gs_value, self.alt_value, self.dhome_value,
            self.yaw_value, self.vs_value, self.dmav_value,
        ):
            value.setText("—")
            value.setToolTip("")
        self.altitude_history.clear()
        self.ahrs.clear_attitude()

    def set_position(self, lat, lon):
        self.position.setText(f"{lat:.6f}\n{lon:.6f}")
        self.position.setToolTip(f"{lat:.6f}, {lon:.6f}")

    def show_video_empty_state(self):
        self.video_view.clear("영상 입력 대기\n소스를 선택하고 연결하세요.")

    def set_stream_input(self, source, value):
        index = self.cmb_stream_source.findData(source)
        self.cmb_stream_source.setCurrentIndex(max(index, 0))
        self.edit_stream_url.setText(value)
        self.edit_stream_url.setCursorPosition(0)

    def _select_stream_source(self, index):
        current_value = self.edit_stream_url.text()
        if self._stream_source is not None:
            self._stream_inputs[self._stream_source] = current_value
        source, _, placeholder, default = FLY_VIDEO_SOURCES[index]
        if source.startswith("rtsp-") and current_value.lower().startswith("rtsp://"):
            default = current_value
        self._stream_source = source
        self.edit_stream_url.setPlaceholderText(placeholder)
        self.edit_stream_url.setText(self._stream_inputs.get(source, default))
        self.edit_stream_url.setCursorPosition(0)

    @staticmethod
    def _fullscreen_button():
        button = QPushButton("⛶")
        button.setAccessibleName("패널 전체화면")
        button.setToolTip("패널 전체화면")
        return button

    def _build_fullscreen(self):
        self.fullscreen = QDialog(self, Qt.Window)
        self.fullscreen.setObjectName("flightFullscreen")
        self.fullscreen.setWindowModality(Qt.ApplicationModal)
        root = QVBoxLayout(self.fullscreen)
        root.setContentsMargins(8, 6, 8, 8)
        heading = QHBoxLayout()
        self.fullscreen_title = label("", "title")
        heading.addWidget(self.fullscreen_title, 1)
        exit_button = QPushButton("전체화면 닫기 · Esc")
        exit_button.clicked.connect(self.close_fullscreen)
        heading.addWidget(exit_button)
        root.addLayout(heading)
        self.fullscreen_body = QVBoxLayout()
        root.addLayout(self.fullscreen_body, 1)
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self.fullscreen)
        escape.activated.connect(self.close_fullscreen)
        self.fullscreen.finished.connect(self._restore_fullscreen_panel)

    def show_panel_fullscreen(self, origin):
        if self._fullscreen_origin is not None:
            return
        title = self.map_title if origin is self.map_body else self.video_title
        self.fullscreen_title.setText(title.text())
        self.fullscreen.setWindowTitle(title.text())
        self._fullscreen_origin = origin
        view = origin.takeAt(0).widget()
        self.fullscreen_body.addWidget(view)
        self.fullscreen.showFullScreen()
        view.show()

    def close_fullscreen(self):
        if self._fullscreen_origin is not None:
            self.fullscreen.reject()

    def _restore_fullscreen_panel(self):
        origin = self._fullscreen_origin
        if origin is None:
            return
        self._fullscreen_origin = None
        view = self.fullscreen_body.takeAt(0).widget()
        origin.addWidget(view)
        view.show()

    def set_video_on_main(self, enabled):
        self.close_fullscreen()
        wide_view, side_view = (self.video_view, self.map_view) if enabled else (self.map_view, self.video_view)
        self.map_body.addWidget(wide_view)
        self.video_body.addWidget(side_view)
        wide_view.show()
        side_view.show()
        self.map_title.setText("실시간 영상" if enabled else "운항 지도")
        self.video_title.setText("운항 지도" if enabled else "실시간 영상")
