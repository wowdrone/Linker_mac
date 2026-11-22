"""Selectable health charts driven only by timestamped MAVLink samples."""
import math
import time
from collections import defaultdict, deque
from PyQt5.QtCore import Qt, QTimer, QPointF, QMargins, QSettings, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox, QToolButton, QMenu, QPushButton, QSizePolicy
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QValueAxis
from gui_theme import DEFAULT_THEME, THEMES, set_status_tone
from gui_help import help_button, set_guide

GROUPS = {'ekf': ('EKF', ['Velocity', 'Pos H', 'Pos V', 'Compass', 'Terrain'], 'Variance / ratio'), 'rc_in': ('RC CH IN', [f'CH{i}' for i in range(1, 19)], 'PWM (µs)'), 'rc_out': ('RC CH OUT', [f'CH{i}' for i in range(1, 33)], 'PWM (µs)'), 'vibration': ('Vibration X/Y/Z', list('XYZ'), 'm/s²'), 'gps': ('GPS', ['Sats', 'HDOP'], 'Sats / HDOP'), 'radio': ('DataLink (Radio)', ['Local', 'Remote'], 'RSSI (raw 0–254)'), 'attitude': ('Attitude', ['Roll', 'Pitch', 'Yaw'], 'Degrees')}
COLORS = ['#55efc4', '#ffeaa7', '#74b9ff', '#fd79a8', '#ff7675', '#a29bfe', '#00cec9', '#fab1a0']
WINDOW_S, STALE_S = (60, 3)

class HealthPlot(QWidget):

    def __init__(self, owner, index, default):
        super().__init__(owner)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.owner, self.index = (owner, index)
        self.lines, self.actions = ({}, {})
        self.selections = {}
        self.setProperty("chartCard", True)
        self.theme = THEMES[DEFAULT_THEME]
        root = QVBoxLayout(self)
        root.setContentsMargins(9, 8, 9, 8)
        row = QHBoxLayout()
        self.selector = QComboBox()
        for key, (name, _, _) in GROUPS.items():
            self.selector.addItem(name, key)
        self.channel_button = QToolButton()
        self.channel_button.setText('표시 항목 ▾')
        self.channel_button.setPopupMode(QToolButton.InstantPopup)
        self.value = QLabel('수신 대기')
        self.value.setMinimumWidth(0)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value.setProperty("role", "title")
        row.addWidget(self.selector)
        row.addWidget(self.channel_button)
        row.addWidget(self.value, 1)
        root.addLayout(row)
        self.chart = QChart()
        self.chart.setPlotAreaBackgroundVisible(True)
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.chart.legend().setAlignment(Qt.AlignTop)
        self.axis_x = QValueAxis()
        self.axis_y = QValueAxis()
        self.axis_x.setTitleText('Time (s)')
        self.axis_x.setLabelFormat('%.0f')
        self.axis_x.setRange(0, 60)
        self.axis_y.setLabelFormat('%.1f')
        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        view = QChartView(self.chart)
        view.setRenderHint(QPainter.Antialiasing)
        view.setMinimumHeight(140)
        root.addWidget(view, 1)
        self.status = QLabel('수신 대기')
        self.status.setWordWrap(True)
        self.status.setProperty("statusTone", "neutral")
        root.addWidget(self.status)
        key = owner.settings.value(f'health/plot{index}/group', default, str)
        self.selector.setCurrentIndex(max(0, self.selector.findData(key)))
        self.selector.currentIndexChanged.connect(self.change_group)
        self.change_group()
        self.apply_theme(self.theme)

    def apply_theme(self, theme):
        self.theme = theme
        self.chart.setBackgroundBrush(QBrush(QColor(theme.panel)))
        self.chart.setPlotAreaBackgroundBrush(QColor(theme.field))
        self.chart.legend().setLabelColor(QColor(theme.text))
        for axis in (self.axis_x, self.axis_y):
            axis.setLabelsColor(QColor(theme.muted))
            axis.setTitleBrush(QBrush(QColor(theme.muted)))
            axis.setGridLineColor(QColor(theme.border))
            axis.setLinePenColor(QColor(theme.border))
        self._style_lines()

    def _style_lines(self):
        for i, field in enumerate(GROUPS[self.group][1]):
            if field in self.lines:
                self.lines[field].setPen(QPen(self.theme.series_color(COLORS[i % len(COLORS)]), 2))

    @property
    def group(self):
        return self.selector.currentData()

    def change_group(self, *_):
        key = self.group
        fields = GROUPS[key][1]
        defaults = fields[:4] if key.startswith('rc_') else fields[:3] if key == 'ekf' else fields[:1] if key == 'gps' else fields
        if key not in self.selections:
            saved = self.owner.settings.value(f'health/plot{self.index}/{key}/fields', defaults)
            if isinstance(saved, str):
                saved = [saved]
            self.selections[key] = set(saved or []).intersection(fields)
        self.owner.settings.setValue(f'health/plot{self.index}/group', key)
        menu = QMenu(self.channel_button)
        self.actions = {}
        for i, field in enumerate(fields):
            if key.startswith('rc_') and i % 8 == 0:
                submenu = menu.addMenu(f'CH{i + 1}–CH{min(i + 8, len(fields))}')
            target = submenu if key.startswith('rc_') else menu
            action = target.addAction(field)
            action.setCheckable(True)
            action.setChecked(field in self.selections[key])
            self.actions[field] = action
            action.toggled.connect(lambda checked, field=field: self.choose_field(field, checked))
        old = self.channel_button.menu()
        self.channel_button.setMenu(menu)
        if old:
            old.deleteLater()
        self.rebuild()

    def choose_field(self, field, checked):
        chosen = self.selections[self.group]
        chosen.add(field) if checked else chosen.discard(field)
        self.owner.settings.setValue(f'health/plot{self.index}/{self.group}/fields', sorted(chosen))
        self.rebuild()

    def rebuild(self):
        self.chart.removeAllSeries()
        self.lines = {}
        for field in GROUPS[self.group][1]:
            if field not in self.selections[self.group]:
                continue
            line = QLineSeries()
            line.setName(field)
            self.chart.addSeries(line)
            line.attachAxis(self.axis_x)
            line.attachAxis(self.axis_y)
            self.lines[field] = line
        self._style_lines()
        self.axis_y.setTitleText(GROUPS[self.group][2])
        self.render()

    def render(self):
        key = self.group
        history = self.owner.history[key]
        sample = self.owner.latest.get(key)
        now = time.monotonic()
        elapsed = max(0, now - self.owner.origin)
        self.axis_x.setRange(max(0, elapsed - WINDOW_S), max(WINDOW_S, elapsed))
        plotted = []
        for field, line in self.lines.items():
            points = []
            previous = None
            for item in history:
                at = item['at']
                value = item['values'].get(field)
                if at - item.get('field_at', {}).get(field, at) > STALE_S:
                    value = None
                if at < now - WINDOW_S:
                    continue
                if value is None or not math.isfinite(value):
                    points = []
                    previous = None
                    continue
                if previous is not None and at - previous > STALE_S:
                    points = []
                points.append(QPointF(at - self.owner.origin, value))
                previous = at
            line.replace(points)
            plotted.extend((point.y() for point in points))
        if key.startswith('rc_'):
            low, high = (800, 2200)
        elif key == 'radio':
            low, high = (0, 254)
        else:
            low, high = (0, 1)
        if plotted:
            low = min(low, min(plotted))
            high = max(high, max(plotted))
            pad = max((high - low) * 0.08, 0.05)
            low, high = (low - pad, high + pad)
        self.axis_y.setRange(low, high)
        if sample is None:
            set_status_tone(self.status, "neutral")
            self.value.setText('수신 대기')
            self.status.setText('아직 수신한 데이터가 없습니다. FC 지원 및 스트림 설정을 확인하세요.')
            return
        age = now - sample['at']
        stale = age > STALE_S or not self.owner.connected
        values = []
        for field in self.lines:
            value = sample['values'].get(field)
            fresh = now - sample.get('field_at', {}).get(field, sample['at']) <= STALE_S
            values.append(f'{field}: {value:.2f}' if value is not None and fresh else f'{field}: —')
        self.value.setText(' · '.join(values[:4]) if not stale else '연결 끊김' if not self.owner.connected else '수신 중단')
        self.value.setToolTip(' · '.join(values))
        prefix = '수신 중단' if stale else '수신 중'
        self.status.setText(f"{prefix} · {age:.1f}s 전 · {sample['source']} · {sample['detail']}")
        set_status_tone(self.status, "warning" if stale else "neutral")

class HealthTab(QWidget):
    streamsRequested = pyqtSignal()

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setObjectName('page_health')
        self.settings = settings if settings is not None else QSettings('WowDrone', 'LinkerGCS')
        self.history = defaultdict(lambda: deque(maxlen=1200))
        self.latest = {}
        self.origin = time.monotonic()
        self.connected = False
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        self.vehicle_label = QLabel('기체 미선택')
        toolbar.addWidget(self.vehicle_label)
        toolbar.addWidget(QLabel('최근 60초'))
        toolbar.addStretch()
        request = QPushButton('스트림 다시 요청')
        request.clicked.connect(lambda _checked=False: self.streamsRequested.emit())
        toolbar.addWidget(request)
        guide = ['각 그래프의 종류와 표시 항목을 선택하세요 · 최근 60초',
                 '「스트림 다시 요청」은 선택 기체에 Health 텔레메트리 스트림을 다시 요청합니다.',
                 '아직 수신한 데이터가 없으면 FC 지원 및 스트림 설정을 확인하세요.']
        toolbar.addWidget(help_button(self, 'Health', guide))
        set_guide(self, 'Health', guide)
        root.addLayout(toolbar)
        grid = QGridLayout()
        root.addLayout(grid, 1)
        self.plots = [HealthPlot(self, i, key) for i, key in enumerate(('ekf', 'rc_in', 'rc_out'))]
        grid.addWidget(self.plots[0], 0, 0, 1, 2)
        grid.addWidget(self.plots[1], 1, 0)
        grid.addWidget(self.plots[2], 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def apply_theme(self, theme):
        for plot in self.plots:
            plot.apply_theme(theme)

    def reset_vehicle(self, vehicle=None):
        self.history.clear()
        self.latest.clear()
        self.origin = time.monotonic()
        self.connected = bool(vehicle and vehicle.connected)
        self.vehicle_label.setText(vehicle.name if vehicle else '기체 미선택')
        self.refresh()

    def set_connected(self, connected):
        self.connected = connected
        self.refresh()

    def update_from_telemetry(self, data):
        for key, sample in data.get('health_samples', {}).items():
            if key not in GROUPS:
                continue
            previous = self.latest.get(key)
            if previous is not None and sample['seq'] <= previous['seq']:
                continue
            self.latest[key] = sample
            self.history[key].append(sample)

    def refresh(self):
        for plot in self.plots:
            plot.render()
