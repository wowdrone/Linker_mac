# tab_planning.py
"""
Planning 탭 전용 위젯

- WP 테이블
- RTH / Plan 옵션
- 지도(QWebEngineView + Leaflet, map_widget.create_map_widget 사용)
- 배경색 / 폰트색을 색상표(QColorDialog)로 쉽게 변경
"""

from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QGridLayout,
    QTableWidget,
    QHeaderView,
    QPushButton,
    QLabel,
    QDoubleSpinBox,
    QColorDialog,
    QTableWidgetItem,
)
from PyQt5.QtCore import pyqtSlot, Qt
from PyQt5.QtGui import QColor, QFont

import json

# 🔻 map_widget.py 에서 만든 공통 함수/상수 사용 (이름이 map_common이면 여기만 바꾸면 됨)
from map_widget import create_map_widget, DEFAULT_LAT, DEFAULT_LON


# 기본 테마 색 (원하는 색으로 기본값만 바꿔도 전체 톤 변경 가능)
DEFAULT_BG_COLOR = "#050D06"      # 진한 딥 그린/네이비
DEFAULT_TEXT_COLOR = "#E4FFE0"    # 밝은 미스트 그린


class PlanningTab(QWidget):
    """
    기존 MainWindow 안의 'Planning 페이지' 부분을
    분리한 전용 위젯 클래스.

    - self.wp_table : WP 목록 테이블
    - self.map_view_plan : Leaflet 지도(QWebEngineView)
    - self.spn_wp_alt / self.spn_wp_speed 등 Plan 옵션
    """

    def __init__(self, linker, parent=None):
        super().__init__(parent)
        self.setObjectName("page_planning")

        self.linker = linker

        # 테마 색 상태
        self._bg_color = QColor(DEFAULT_BG_COLOR)
        self._text_color = QColor(DEFAULT_TEXT_COLOR)

        # 1) 지도 생성 (Fly 탭과 동일한 HTML/JS 구조 사용)
        self.map_view_plan, self.map_bridge_plan = create_map_widget(
            default_lat=DEFAULT_LAT,
            default_lon=DEFAULT_LON,
        )

        # 2) UI 구성
        self._build_ui()

        # 3) 시그널 연결
        self._connect_signals()

        # 4) 초기 테마 적용 & 지도와 테이블 동기화
        self._apply_theme()
        self._sync_planning_map_waypoints()

    # ------------------------------------------------------------------ #
    # UI 구성                                                             #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 좌/우 분할: 왼쪽 = WP 테이블+옵션, 오른쪽 = 지도
        from PyQt5.QtWidgets import QSplitter

        splitter = QSplitter()
        splitter.setOrientation(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        # ---------- 왼쪽 패널 ----------
        self.left_panel = QWidget()
        self.left_panel.setObjectName("planning_left_panel")
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        # WP 테이블
        self.wp_table = QTableWidget(0, 6)
        self.wp_table.setHorizontalHeaderLabels(
            ["#", "Lat", "Lon", "Alt (m)", "Speed (m/s)", "Heading (deg)"]
        )
        header = self.wp_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.wp_table.verticalHeader().setVisible(False)

        # 테이블 기본 스타일 (색상은 _apply_theme 에서 다시 덮어씀)
        self.wp_table.setFont(QFont("Apple SD Gothic Neo", 9))

        left_layout.addWidget(self.wp_table, 1)

        # Flight Plan / RTH 옵션
        plan_group = QGroupBox("Flight Plan & RTH Options")
        self.plan_group = plan_group
        plan_grid = QGridLayout(plan_group)
        plan_grid.setHorizontalSpacing(10)
        plan_grid.setVerticalSpacing(6)

        # 라벨들
        self.lbl_wp_alt = QLabel("Default WP Alt (m)")
        self.lbl_wp_speed = QLabel("Cruise Speed (m/s)")
        self.lbl_rtl_alt = QLabel("RTH Alt (m)")
        self.lbl_rtl_action = QLabel("RTH Action")
        self.lbl_rtl_loiter = QLabel("Loiter Time (s)")

        for lab in (
            self.lbl_wp_alt,
            self.lbl_wp_speed,
            self.lbl_rtl_alt,
            self.lbl_rtl_action,
            self.lbl_rtl_loiter,
        ):
            lab.setFont(QFont("Apple SD Gothic Neo", 9, QFont.Bold))

        # 스핀박스 / 콤보박스
        self.spn_wp_alt = QDoubleSpinBox()
        self.spn_wp_alt.setRange(1.0, 1000.0)
        self.spn_wp_alt.setValue(50.0)

        self.spn_wp_speed = QDoubleSpinBox()
        self.spn_wp_speed.setRange(1.0, 100.0)
        self.spn_wp_speed.setValue(10.0)

        self.spn_rtl_alt = QDoubleSpinBox()
        self.spn_rtl_alt.setRange(1.0, 1000.0)
        self.spn_rtl_alt.setValue(60.0)

        from PyQt5.QtWidgets import QComboBox

        self.cmb_rtl_action = QComboBox()
        self.cmb_rtl_action.addItems(
            ["Loiter then Land", "Loiter only", "Land immediately"]
        )

        self.spn_loiter_time = QDoubleSpinBox()
        self.spn_loiter_time.setRange(0.0, 3600.0)
        self.spn_loiter_time.setValue(60.0)

        self.btn_apply_plan = QPushButton("Apply Plan")

        plan_grid.addWidget(self.lbl_wp_alt, 0, 0)
        plan_grid.addWidget(self.spn_wp_alt, 0, 1)
        plan_grid.addWidget(self.lbl_wp_speed, 0, 2)
        plan_grid.addWidget(self.spn_wp_speed, 0, 3)

        plan_grid.addWidget(self.lbl_rtl_alt, 1, 0)
        plan_grid.addWidget(self.spn_rtl_alt, 1, 1)
        plan_grid.addWidget(self.lbl_rtl_action, 1, 2)
        plan_grid.addWidget(self.cmb_rtl_action, 1, 3)

        plan_grid.addWidget(self.lbl_rtl_loiter, 2, 0)
        plan_grid.addWidget(self.spn_loiter_time, 2, 1)
        plan_grid.addWidget(self.btn_apply_plan, 2, 3)

        left_layout.addWidget(plan_group)

        # WP 버튼 + 테마 버튼
        btn_row = QHBoxLayout()
        self.btn_add_wp = QPushButton("Add WP")
        self.btn_del_wp = QPushButton("Delete WP")
        self.btn_clear_wp = QPushButton("Clear All")

        self.btn_theme_bg = QPushButton("배경색")
        self.btn_theme_font = QPushButton("폰트색")

        for b in (
            self.btn_add_wp,
            self.btn_del_wp,
            self.btn_clear_wp,
            self.btn_theme_bg,
            self.btn_theme_font,
        ):
            b.setMinimumHeight(26)

        btn_row.addWidget(self.btn_add_wp)
        btn_row.addWidget(self.btn_del_wp)
        btn_row.addWidget(self.btn_clear_wp)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_theme_bg)
        btn_row.addWidget(self.btn_theme_font)

        left_layout.addLayout(btn_row)

        splitter.addWidget(self.left_panel)

        # ---------- 오른쪽: 지도 ----------
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        right_layout.addWidget(self.map_view_plan)

        splitter.addWidget(right_panel)
        splitter.setSizes([500, 900])

    # ------------------------------------------------------------------ #
    # 시그널 연결                                                        #
    # ------------------------------------------------------------------ #
    def _connect_signals(self):
        # WP 버튼
        self.btn_add_wp.clicked.connect(self.on_add_wp_clicked)
        self.btn_del_wp.clicked.connect(self.on_del_wp_clicked)
        self.btn_clear_wp.clicked.connect(self.on_clear_wp_clicked)
        self.btn_apply_plan.clicked.connect(self.on_apply_plan_clicked)

        # 테마 버튼
        self.btn_theme_bg.clicked.connect(self.choose_bg_color)
        self.btn_theme_font.clicked.connect(self.choose_font_color)

        # 지도 브리지 (map_widget.create_map_widget 에서 반환된 bridge)
        if self.map_bridge_plan is not None:
            # 지도에서 WP 클릭 시 테이블에 추가
            self.map_bridge_plan.waypointClicked.connect(self.on_map_wp_clicked)

    # ------------------------------------------------------------------ #
    # 테마 적용 / 색상 선택                                              #
    # ------------------------------------------------------------------ #
    def _apply_theme(self):
        """
        현재 _bg_color / _text_color 기준으로
        Planning 탭 전체에 스타일 적용
        """
        bg = self._bg_color.name()
        fg = self._text_color.name()

        # 전체 배경
        self.setStyleSheet(
            f"""
            QWidget#page_planning {{
                background-color: {bg};
            }}
        """
        )

        # 왼쪽 패널 + 그룹박스 + 라벨
        self.left_panel.setStyleSheet(
            f"""
            QWidget#planning_left_panel {{
                background-color: {bg};
            }}
            QGroupBox {{
                background-color: #0a1a0c;
                border: 1px solid #294029;
                border-radius: 4px;
                color: {fg};
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
            QLabel {{
                color: {fg};
            }}
        """
        )

        # 테이블
        self.wp_table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: #050D06;
                color: {fg};
                gridline-color: #1f3a1f;
                font-family: 'Apple SD Gothic Neo';
                font-size: 9pt;
            }}
            QHeaderView::section {{
                background-color: #1f3a1f;
                color: {fg};
                font-weight: bold;
                border: 1px solid #2c472c;
            }}
            QTableWidget::item:selected {{
                background-color: #295229;
            }}
        """
        )

        # 버튼들(약간 환경모니터 느낌으로)
        for btn in (
            self.btn_add_wp,
            self.btn_del_wp,
            self.btn_clear_wp,
            self.btn_theme_bg,
            self.btn_theme_font,
            self.btn_apply_plan,
        ):
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color:#2e6f40;
                    color:#ecffe4;
                    border-radius:4px;
                    padding:4px 10px;
                    font-weight:bold;
                }
                QPushButton:hover {
                    background-color:#3f8f55;
                }
                QPushButton:pressed {
                    background-color:#285833;
                }
            """
            )

    def choose_bg_color(self):
        col = QColorDialog.getColor(self._bg_color, self, "Planning 배경색 선택")
        if col.isValid():
            self._bg_color = col
            self._apply_theme()

    def choose_font_color(self):
        col = QColorDialog.getColor(self._text_color, self, "Planning 폰트색 선택")
        if col.isValid():
            self._text_color = col
            self._apply_theme()

    # ------------------------------------------------------------------ #
    # 지도 <-> WP 테이블 동기화                                          #
    # ------------------------------------------------------------------ #
    def _sync_planning_map_waypoints(self):
        """
        WP 테이블 내용을 기반으로 Planning 지도에
        WP 마커/라인을 재구성 (Leaflet 쪽 pythonSetWaypoints 호출)
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

    # 지도에서 WP 찍었을 때
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

        self._sync_planning_map_waypoints()

    # ------------------------------------------------------------------ #
    # WP 버튼 동작                                                       #
    # ------------------------------------------------------------------ #
    def on_add_wp_clicked(self):
        """
        테이블에 새 WP 한 줄 추가 (기본 좌표는 DEFAULT_LAT/LON)
        """
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

        self._sync_planning_map_waypoints()

    def on_del_wp_clicked(self):
        """
        현재 선택된 WP 삭제 + 번호 재정렬 + 지도 재동기화
        """
        row = self.wp_table.currentRow()
        if row < 0:
            return
        self.wp_table.removeRow(row)

        # 번호 재정렬
        for r in range(self.wp_table.rowCount()):
            item = self.wp_table.item(r, 0)
            if not item:
                item = QTableWidgetItem()
                self.wp_table.setItem(r, 0, item)
            item.setText(str(r + 1))

        self._sync_planning_map_waypoints()

    def on_clear_wp_clicked(self):
        """
        모든 WP 삭제 + 지도 초기화
        """
        self.wp_table.setRowCount(0)
        self._sync_planning_map_waypoints()

    def on_apply_plan_clicked(self):
        """
        Plan 옵션 변경 (현재는 로그/디버깅용)
        나중에 linker.upload_mission 등에 활용 가능
        """
        alt = self.spn_wp_alt.value()
        speed = self.spn_wp_speed.value()
        rtl_alt = self.spn_rtl_alt.value()
        rtl_action = self.cmb_rtl_action.currentText()
        loiter = self.spn_loiter_time.value()

        print(
            f"[PlanningTab] Plan updated: "
            f"WP Alt={alt:.1f}m, Speed={speed:.1f}m/s, "
            f"RTL Alt={rtl_alt:.1f}m, RTL Action={rtl_action}, "
            f"Loiter={loiter:.0f}s"
        )

    # ------------------------------------------------------------------ #
    # 미션 데이터 가져오기 (메인에서 사용하고 싶을 때)                    #
    # ------------------------------------------------------------------ #
    def get_waypoints(self):
        """
        WP 테이블 내용을 리스트(dict)로 반환.
        main_window.py 에서 미션 업로드 시 사용할 수 있음.

        return [
            {
                "seq": 0,
                "lat": ...,
                "lon": ...,
                "alt": ...,
                "speed": ...,
                "heading": ...
            },
            ...
        ]
        """
        waypoints = []
        for row in range(self.wp_table.rowCount()):
            try:
                lat = float(self.wp_table.item(row, 1).text())
                lon = float(self.wp_table.item(row, 2).text())
                alt = float(self.wp_table.item(row, 3).text())
                speed = float(self.wp_table.item(row, 4).text())
                heading = float(self.wp_table.item(row, 5).text())
            except Exception:
                continue

            waypoints.append(
                {
                    "seq": row,
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "speed": speed,
                    "heading": heading,
                }
            )
        return waypoints
