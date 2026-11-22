# tab_support.py
"""
Support 탭 - 환경 모니터 스타일 센서 대시보드
 - CO / SO2 / NOx / VOCs / PM2.5 / PM10 / NH3 / OUs 등을
   실시간(임의 값) 그래프로 표시
 - 카드 배경색 / 글자색은 센서별로 변경 가능
"""

import random

PAYLOAD_LINE_COLORS = {
    "co":   "#ff5555",   # CO
    "so2":  "#ffaa00",   # SO2
    "nox":  "#ffff00",   # NOx
    "vocs": "#00ccff",   # VOCs
    "pm25": "#00ff00",   # PM2.5
    "pm10": "#0088ff",   # PM10
    "nh3":  "#ff00ff",   # NH3
    "ous":  "#ffffff",   # 냄새(취기) 등
}

from PyQt5.QtCore import Qt, QTimer, QMargins
from PyQt5.QtGui import QFont, QColor, QPainter
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QFrame,
    QSizePolicy,
    QPushButton,
    QColorDialog,
)
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QValueAxis
import pyqtgraph as pg
from gui_theme import DEFAULT_THEME, THEMES
# from .colors import PAYLOAD_LINE_COLORS   # 같은 파일이면 import 필요 없음

class SensorChart(QWidget):
    """
    개별 센서 카드 + 실시간 라인 차트
    """

    def __init__(self, key, title, unit, vmin, vmax, color="#2ecc71", parent=None):
        super().__init__(parent)
        self.key = key
        self.title = title
        self.unit = unit
        self.vmin = vmin
        self.vmax = vmax
        self.max_points = 60  # 최근 N개만 유지

        # 색상 상태
        self.theme = THEMES[DEFAULT_THEME]
        self.custom_background = None
        self.custom_text = None
        self.frame_bg_color = QColor(self.theme.panel)
        self.text_color = QColor(self.theme.text)
        self.line_color = QColor(color)           # 센서별 라인 색

        # ---- 레이아웃 구성 ----
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 카드 프레임
        self.card = QFrame()
        self.card.setObjectName("sensorCard")
        self.card.setFrameShape(QFrame.StyledPanel)
        self.card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.card)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(4)

        # 상단 헤더 (타이틀 + 현재 값)
        header = QHBoxLayout()
        self.lbl_title = QLabel(f"{title} ({unit})")
        self.lbl_title.setFont(QFont("Apple SD Gothic Neo", 10, QFont.Bold))

        header.addWidget(self.lbl_title)

        header.addStretch()

        self.lbl_value = QLabel("-")
        self.lbl_value.setFont(QFont("Menlo", 11, QFont.Bold))
        self.lbl_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.lbl_value)

        card_layout.addLayout(header)

        # ---- QtChart 설정 ----
        self.series = QLineSeries()
        self.series.setColor(self.line_color)

        self.chart = QChart()
        self.chart.addSeries(self.series)
        self.chart.legend().hide()
        self.chart.setBackgroundVisible(False)
        self.chart.setMargins(QMargins(4, 4, 4, 4))

        self.axis_x = QValueAxis()
        self.axis_x.setRange(0, self.max_points)
        self.axis_x.setVisible(False)

        self.axis_y = QValueAxis()
        self.axis_y.setRange(self.vmin, self.vmax)
        self.axis_y.setLabelFormat("%.1f")
        self.axis_y.setTitleText(unit)

        self.chart.addAxis(self.axis_x, Qt.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignLeft)
        self.series.attachAxis(self.axis_x)
        self.series.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)

        card_layout.addWidget(self.chart_view, 1)

        # 하단: 색상 변경 버튼
        btn_row = QHBoxLayout()
        self.btn_frame_color = QPushButton("배경색")
        self.btn_text_color = QPushButton("글자색")

        for b in (self.btn_frame_color, self.btn_text_color):
            b.setMinimumHeight(24)

        btn_row.addWidget(self.btn_frame_color)
        btn_row.addWidget(self.btn_text_color)
        btn_row.addStretch()

        card_layout.addLayout(btn_row)

        # 데이터 히스토리
        self._history = []

        # 시그널 연결
        self.btn_frame_color.clicked.connect(self.choose_frame_color)
        self.btn_text_color.clicked.connect(self.choose_text_color)

        # 초기 스타일 적용
        self.apply_colors()

    # ------------------------------------------------------------------ #
    # 값 업데이트                                                        #
    # ------------------------------------------------------------------ #
    def set_value(self, value: float):
        """센서 값 갱신 + 그래프 업데이트"""
        # 현재 값 표시
        self.lbl_value.setText(f"{value:5.2f} {self.unit}")

        # 히스토리 갱신
        self._history.append(value)
        if len(self._history) > self.max_points:
            self._history.pop(0)

        # 시리즈 재구성
        self.series.clear()
        for i, v in enumerate(self._history):
            self.series.append(i, v)

    # ------------------------------------------------------------------ #
    # 색상 관련                                                          #
    # ------------------------------------------------------------------ #
    def apply_colors(self):
        """현재 frame_bg_color / text_color / line_color를 UI에 반영"""
        bg = self.frame_bg_color.name()
        txt = self.text_color.name()

        # 카드 + 라벨 스타일
        self.card.setStyleSheet(
            f"""
            QFrame#sensorCard {{
                background-color: {bg};
                border: 1px solid {self.theme.border};
                border-radius: {self.theme.radius}px;
            }}
            QLabel {{
                color: {txt};
            }}
        """
        )

        # 차트 축 / 그리드 / 라인 색
        for axis in self.chart.axes():
            axis.setLabelsBrush(QColor(txt))
            axis.setTitleBrush(QColor(txt))
            axis.setGridLineColor(QColor(self.theme.border))
            axis.setLinePenColor(QColor(self.theme.border))

        self.series.setColor(self.theme.series_color(self.line_color) if self.custom_background is None else self.line_color)

    def apply_theme(self, theme):
        self.theme = theme
        self.frame_bg_color = self.custom_background or QColor(theme.panel)
        self.text_color = self.custom_text or QColor(theme.text)
        self.apply_colors()

    def choose_frame_color(self):
        col = QColorDialog.getColor(self.frame_bg_color, self, "카드 배경색 선택")
        if col.isValid():
            self.custom_background = col
            self.frame_bg_color = col
            self.apply_colors()

    def choose_text_color(self):
        col = QColorDialog.getColor(self.text_color, self, "글자색 선택")
        if col.isValid():
            self.custom_text = col
            self.text_color = col
            self.apply_colors()


class SupportTab(QWidget):
    """
    Support 탭 전체 위젯
     - 여러 SensorChart를 격자 배치
     - 타이머로 임의 값 갱신
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 예시: CO
        self.plot_co = pg.PlotWidget()
        co_color = PAYLOAD_LINE_COLORS.get("co", "#ff5555")
        self.curve_co = self.plot_co.plot(
            pen=pg.mkPen(co_color, width=2)
        )

        # 예시: PM2.5
        self.plot_pm25 = pg.PlotWidget()
        pm25_color = PAYLOAD_LINE_COLORS.get("pm25", "#68ca68")
        self.curve_pm25 = self.plot_pm25.plot(
            pen=pg.mkPen(pm25_color, width=2)
        )
        
        self.setObjectName("page_support")

        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(8)

        title = QLabel("Air Quality & Odor Monitoring (Demo)")
        title.setFont(QFont("Apple SD Gothic Neo", 11, QFont.Bold))
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        main.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(8)
        main.addLayout(grid, 1)

        self.sensors = {}

        def add_sensor(row, col, key, name, unit, vmin, vmax, color):
            w = SensorChart(key, name, unit, vmin, vmax, color, parent=self)
            grid.addWidget(w, row, col)
            self.sensors[key] = w

        # 2 x 4 정도로 센서 배치 (원하시면 더 추가 가능)
        add_sensor(0, 0, "co", "CO", "ppm", 0.0, 10.0, "#ff7675")
        add_sensor(0, 1, "so2", "SO₂", "ppm", 0.0, 5.0, "#ffeaa7")
        add_sensor(0, 2, "nox", "NOx", "ppm", 0.0, 5.0, "#74b9ff")
        add_sensor(0, 3, "vocs", "VOCs", "ppm", 0.0, 20.0, "#a29bfe")

        add_sensor(1, 0, "pm25", "PM2.5", "µg/m³", 0.0, 150.0, "#55efc4")
        add_sensor(1, 1, "pm10", "PM10", "µg/m³", 0.0, 200.0, "#81ecec")
        add_sensor(1, 2, "nh3", "NH₃", "ppm", 0.0, 20.0, "#fd79a8")
        add_sensor(1, 3, "ous", "OUs", "ou/m³", 0.0, 10.0, "#00cec9")

        # 임의 데이터 타이머
        self._timer = QTimer(self)
        self._timer.setInterval(500)  # 0.5초마다 값 갱신
        self._timer.timeout.connect(self._update_fake_data)
        self._timer.start()

    def apply_theme(self, theme):
        for sensor in self.sensors.values():
            sensor.apply_theme(theme)

    # ------------------------------------------------------------------ #
    # Demo 데이터 갱신                                                    #
    # ------------------------------------------------------------------ #
    def _update_fake_data(self):
        """
        실제 RF 데이터 대신, 센서별 vmin~vmax 사이에서 랜덤 값 생성
        """
        for key, sensor in self.sensors.items():
            # 센서별로 약간 다른 패턴을 줄 수도 있음
            base_min = sensor.vmin
            base_max = sensor.vmax

            # 중간값 근처를 중심으로 약간 랜덤하게 움직이게
            mid = (base_min + base_max) / 2.0
            span = (base_max - base_min) / 2.0

            value = random.uniform(mid - span * 0.6, mid + span * 0.6)
            value = max(base_min, min(base_max, value))

            sensor.set_value(value)

    # ------------------------------------------------------------------ #
    # (추후 실제 RF 데이터 연동용)                                        #
    # ------------------------------------------------------------------ #
    def update_sensor_value(self, key: str, value: float):
        """
        나중에 실제 RF 통신 데이터를 받을 때 사용할 수 있는 메서드 예시.
        main_window.py 에서 SupportTab 인스턴스를 가지고 있다면:

            self.page_support.update_sensor_value("co", co_value)

        같은 식으로 호출 가능.
        """
        sensor = self.sensors.get(key)
        if sensor is not None:
            sensor.set_value(value)
