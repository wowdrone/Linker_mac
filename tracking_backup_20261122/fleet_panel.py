# fleet_panel.py
# 멀티 기체 관리 화면
#  - 링크 목록 (추가 / 해제)
#  - 기체 표 (SYSID, 링크, 모드, ARM, 고도, 속도, 배터리, GPS, HB)
#  - 선택/전체 기체 그룹 명령 (ARM, DISARM, TAKEOFF, LAND, RTL, 모드 변경)

import html
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
import serial.tools.list_ports
from platform_mac import SERIAL_LINK_KINDS, refresh_port_selector
from gui_theme import DEFAULT_THEME, THEMES
from flight_actions import confirm_flight, TakeoffSequence

FLIGHT_MODES = [
    "GUIDED", "LOITER", "AUTO", "RTL", "LAND", "BRAKE",
    "POSHOLD", "ALT_HOLD", "STABILIZE", "SMART_RTL",
]

class VehicleTable(QTableWidget):
    """기체 목록 표. 행 클릭 → 선택 기체 변경"""

    vehicleActivated = pyqtSignal(object)   # Vehicle

    COLUMNS = ["", "SYSID", "Link", "Mode", "Armed", "Alt(m)", "GS(m/s)", "Batt", "GPS", "HB"]
    COMPACT_COLUMNS = ["", "SYSID", "Mode", "Armed", "Alt(m)", "Batt", "HB"]

    def __init__(self, fleet, compact=False, parent=None):
        self.compact = compact
        cols = self.COMPACT_COLUMNS if compact else self.COLUMNS
        super().__init__(0, len(cols), parent)
        self.fleet = fleet
        self.theme = THEMES[DEFAULT_THEME]
        self.cols = cols
        self.active_key = None
        self.setHorizontalHeaderLabels(cols)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        self.setColumnWidth(0, 8)
        self.cellDoubleClicked.connect(self._on_double_clicked)
        self.cellClicked.connect(self._on_double_clicked)

    def selected_vehicles(self):
        rows = sorted({i.row() for i in self.selectedIndexes()})
        out = []
        for r in rows:
            item = self.item(r, 1)
            if item is not None:
                v = self.fleet.vehicle_by_str(item.data(Qt.UserRole))
                if v is not None:
                    out.append(v)
        return out

    def _on_double_clicked(self, row, _col):
        item = self.item(row, 1)
        if item is None:
            return
        v = self.fleet.vehicle_by_str(item.data(Qt.UserRole))
        if v is not None:
            self.vehicleActivated.emit(v)

    def refresh(self):
        vehicles = self.fleet.vehicle_list()
        if self.rowCount() != len(vehicles):
            self.setRowCount(len(vehicles))
        for r, v in enumerate(vehicles):
            s = v.summary()
            batt = "-"
            if s["batt_v"] is not None:
                batt = f"{s['batt_v']:.1f}V"
            if s["batt_p"] is not None:
                batt += f" {s['batt_p']:.0f}%"
            hb = "LOST" if not s["connected"] else f"{s['hb_age']:.1f}s"
            active = (v.key_str == self.active_key)
            values = {
                "": "",
                "SYSID": f"{'▶ ' if active else ''}{v.sysid}",
                "Link": s["link"],
                "Mode": s["mode"],
                "Armed": "ARMED" if s["armed"] else "-",
                "Alt(m)": f"{s['alt']:.1f}",
                "GS(m/s)": f"{s['gs']:.1f}",
                "Batt": batt,
                "GPS": s["gps"],
                "HB": hb,
            }
            for c, name in enumerate(self.cols):
                item = self.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(r, c, item)
                item.setText(values[name])
                if name == "":
                    item.setBackground(QBrush(QColor(v.color)))
                elif name == "SYSID":
                    item.setData(Qt.UserRole, v.key_str)
                    item.setForeground(QBrush(self.theme.series_color(v.color)))
                elif name == "Armed":
                    item.setForeground(QBrush(QColor(self.theme.danger if s["armed"] else self.theme.muted)))
                elif name == "HB":
                    item.setForeground(QBrush(QColor(self.theme.success if s["connected"] else self.theme.danger)))


class FleetPanel(QWidget):
    """Fleet 페이지"""

    vehicleActivated = pyqtSignal(object)

    def __init__(self, fleet, parent=None):
        super().__init__(parent)
        self.setObjectName("page_fleet")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.fleet = fleet
        self.theme = THEMES[DEFAULT_THEME]

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # ---------------- 링크 ----------------
        link_box = QGroupBox("Links  (기체마다 링크 1개씩, 또는 UDP 1포트로 여러 SYSID 수신)")
        lb = QVBoxLayout(link_box)

        row = QHBoxLayout()
        self.cmb_kind = QComboBox()
        self.cmb_kind.addItems(["Serial", "Bluetooth", "UDP", "UDPOUT", "TCP"])
        self.cmb_port = QComboBox()
        self.cmb_port.setEditable(True)
        self.cmb_port.setMinimumWidth(90)
        self.cmb_port.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_port.setMinimumContentsLength(11)
        self.cmb_baud = QComboBox()
        self.cmb_baud.addItems(["57600", "115200", "230400", "460800", "921600"])
        self.cmb_baud.setCurrentText("115200")
        self.edit_addr = QLineEdit("127.0.0.1:14550")
        self.edit_addr.setMaximumWidth(190)
        self.edit_addr.setToolTip("UDP: 수신 대기 ip:port / UDPOUT·TCP: 대상 ip:port")
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(28)
        self.btn_add = QPushButton("Add Link")
        self.btn_add_range = QPushButton("UDP ×4 (14550~14580)")
        self.btn_add_range.setToolTip("SITL 다중 기체 기본 포트 4개를 한번에 엽니다")
        for w in (QLabel("Type"), self.cmb_kind, QLabel("Port"), self.cmb_port, self.btn_refresh,
                  QLabel("Baud"), self.cmb_baud, QLabel("Addr"), self.edit_addr):
            row.addWidget(w)
        row.addStretch()
        lb.addLayout(row)

        self.tbl_links = QTableWidget(0, 5)
        self.tbl_links.setHorizontalHeaderLabels(["#", "Link", "State", "RX msgs", "Vehicles"])
        self.tbl_links.verticalHeader().setVisible(False)
        self.tbl_links.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_links.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_links.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_links.setMinimumHeight(60)
        self.tbl_links.setMaximumHeight(140)
        lb.addWidget(self.tbl_links)

        row2 = QHBoxLayout()
        self.btn_remove = QPushButton("Remove Selected Link")
        self.btn_remove_all = QPushButton("Disconnect All")
        for b in (self.btn_add, self.btn_add_range, self.btn_remove, self.btn_remove_all):
            row2.addWidget(b)
        row2.addStretch()
        lb.addLayout(row2)
        splitter.addWidget(link_box)

        # ---------------- 기체 ----------------
        veh_box = QGroupBox("Vehicles  (클릭 = 조종 대상 선택, ⌘/Shift = 그룹 명령 대상 다중 선택)")
        vb = QVBoxLayout(veh_box)
        self.table = VehicleTable(fleet)
        self.table.vehicleActivated.connect(self.vehicleActivated)
        vb.addWidget(self.table, 1)

        cmd = QHBoxLayout()
        self.cmb_target = QComboBox()
        self.cmb_target.addItems(["Selected", "All"])
        cmd.addWidget(QLabel("Target"))
        cmd.addWidget(self.cmb_target)
        self.btn_arm = QPushButton("ARM")
        self.btn_disarm = QPushButton("DISARM")
        self.spn_alt = QDoubleSpinBox()
        self.spn_alt.setRange(1.0, 120.0)
        self.spn_alt.setValue(10.0)
        self.spn_alt.setSuffix(" m")
        self.btn_takeoff = QPushButton("TAKEOFF")
        self.btn_cancel_takeoff = QPushButton("취소")
        self.btn_cancel_takeoff.setEnabled(False)
        self._takeoffs = []
        self.btn_land = QPushButton("LAND")
        self.btn_rtl = QPushButton("RTL")
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(FLIGHT_MODES)
        self.btn_mode = QPushButton("Set Mode")
        for b in (self.btn_land, self.btn_rtl):
            b.setProperty("role", "danger")
        cmd.addWidget(self.cmb_mode)
        cmd.addWidget(self.btn_mode)
        cmd.addStretch()
        vb.addLayout(cmd)

        flight_row = QHBoxLayout()
        for control in (self.btn_arm, self.btn_disarm, self.spn_alt, self.btn_takeoff, self.btn_cancel_takeoff,
                        self.btn_land, self.btn_rtl):
            flight_row.addWidget(control)
        flight_row.addStretch()
        vb.addLayout(flight_row)
        splitter.addWidget(veh_box)

        # ---------------- 전체 기체 메시지 ----------------
        log_box = QGroupBox("Fleet Messages (모든 기체 STATUSTEXT)")
        lg = QVBoxLayout(log_box)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(60)
        self.txt_log.setStyleSheet("QTextEdit { font-family:Menlo; font-size:9pt; }")
        lg.addWidget(self.txt_log)
        splitter.addWidget(log_box)
        splitter.setSizes([200, 400, 200])

        # ---------------- 시그널 ----------------
        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.cmb_kind.currentTextChanged.connect(self._on_kind_changed)
        self.btn_add.clicked.connect(self._on_add_link)
        self.btn_add_range.clicked.connect(self._on_add_range)
        self.btn_remove.clicked.connect(self._on_remove_link)
        self.btn_remove_all.clicked.connect(fleet.remove_all)

        self.btn_arm.clicked.connect(lambda: self._group("arm", confirm="ARM"))
        self.btn_disarm.clicked.connect(lambda: self._group("disarm"))
        self.btn_takeoff.clicked.connect(self._group_takeoff)
        self.btn_cancel_takeoff.clicked.connect(self._cancel_takeoffs)
        self.btn_land.clicked.connect(lambda: self._group("land"))
        self.btn_rtl.clicked.connect(lambda: self._group("rtl"))
        self.btn_mode.clicked.connect(lambda: self._group("_set_mode", self.cmb_mode.currentText(), confirm=f"모드 {self.cmb_mode.currentText()}" if self.cmb_mode.currentText() not in ('LAND', 'RTL') else None))

        fleet.linksChanged.connect(self.refresh_links)
        fleet.log.connect(self.append_log)
        fleet.vehicleAdded.connect(self._on_vehicle_added)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._on_kind_changed(self.cmb_kind.currentText())

    # ---- 링크 ----
    def refresh_ports(self):
        refresh_port_selector(self.cmb_port, self.cmb_kind.currentText().lower())

    def _on_kind_changed(self, kind):
        serial_mode = kind.lower() in SERIAL_LINK_KINDS
        self.cmb_port.setEditable(kind == "Serial")
        self.cmb_port.setEnabled(serial_mode)
        self.cmb_baud.setEnabled(serial_mode)
        self.btn_refresh.setEnabled(serial_mode)
        self.edit_addr.setEnabled(not serial_mode)
        if serial_mode:
            self.refresh_ports()
        defaults = {"UDP": "127.0.0.1:14550", "UDPOUT": "127.0.0.1:14550", "TCP": "127.0.0.1:5760"}
        if kind in defaults:
            self.edit_addr.setText(defaults[kind])

    def _on_add_link(self):
        kind = self.cmb_kind.currentText().lower()
        if kind in SERIAL_LINK_KINDS:
            port = self.cmb_port.currentData() if kind == "bluetooth" else self.cmb_port.currentText().strip()
            if kind == "bluetooth" and not port:
                self.append_log("macOS에서 페어링한 Bluetooth SPP 포트를 선택하세요.")
                return
            self.fleet.add_link(kind, port, int(self.cmb_baud.currentText()))
        else:
            self.fleet.add_link(kind, self.edit_addr.text().strip())

    def _on_add_range(self):
        for i in range(4):
            self.fleet.add_link("udp", f"127.0.0.1:{14550 + i * 10}")

    def _on_remove_link(self):
        rows = sorted({i.row() for i in self.tbl_links.selectedIndexes()})
        for r in rows:
            item = self.tbl_links.item(r, 0)
            if item:
                self.fleet.remove_link(int(item.text()))

    def refresh_links(self):
        links = list(self.fleet.links.values())
        self.tbl_links.setRowCount(len(links))
        for r, w in enumerate(links):
            n_veh = sum(1 for k in self.fleet.vehicles if k[0] == w.link_id)
            state = self.fleet.link_states.get(w.link_id, "?")
            vals = [str(w.link_id), w.human, state, str(w.rx_count), str(n_veh)]
            for c, text in enumerate(vals):
                item = self.tbl_links.item(r, c) or QTableWidgetItem()
                item.setText(text)
                if c == 2:
                    color = {"open": self.theme.success, "connecting": self.theme.warning}.get(state, self.theme.danger)
                    item.setForeground(QBrush(QColor(color)))
                self.tbl_links.setItem(r, c, item)

    # ---- 기체 ----
    def _on_vehicle_added(self, v):
        v.statusTextReceived.connect(lambda t, veh=v: self.append_log(f"[{veh.name}] {t}", veh.color))
        v.commandFeedback.connect(
            lambda _c, ok, m, veh=v: self.append_log(f"[{veh.name}] {'OK' if ok else 'FAIL'} {m}", veh.color)
        )

    def apply_theme(self, theme):
        self.theme = self.table.theme = theme
        self.table.refresh()
        self.refresh_links()

    def set_active_key(self, key):
        self.table.active_key = key
        self.table.refresh()

    def _targets(self):
        if self.cmb_target.currentText() == "All":
            return [v for v in self.fleet.vehicle_list() if v.connected]
        return [v for v in self.table.selected_vehicles() if v.connected]

    def _confirm(self, action, vehicles):
        return confirm_flight(self, action, vehicles)

    def _group(self, fn, *args, confirm=None):
        vehicles = self._targets()
        if not vehicles:
            self.append_log("대상 기체가 없습니다. (표에서 선택하거나 Target=All)")
            return
        if fn == 'disarm':
            confirm = 'DISARM'
        if confirm and not self._confirm(confirm, vehicles):
            return
        for v in vehicles:
            try:
                getattr(v, fn)(*args)
            except Exception as e:
                self.append_log(f"[{v.name}] {fn} error: {e}")

    def _group_takeoff(self):
        vehicles = self._targets()
        if not vehicles:
            self.append_log("대상 기체가 없습니다.")
            return
        alt = self.spn_alt.value()
        if not self._confirm(f"GUIDED → ARM → TAKEOFF {alt:.0f}m", vehicles):
            return
        self.btn_takeoff.setEnabled(False)
        self.btn_cancel_takeoff.setEnabled(True)
        for v in vehicles:
            sequence = TakeoffSequence(v, alt, self)
            self._takeoffs.append(sequence)
            sequence.finished.connect(lambda veh, ok, msg, seq=sequence: self._takeoff_finished(seq, veh, msg))

    def _takeoff_finished(self, sequence, vehicle, message):
        self.append_log(f'[{vehicle.name}] {message}', vehicle.color)
        self._takeoffs.remove(sequence)
        if not self._takeoffs:
            self.btn_takeoff.setEnabled(True)
            self.btn_cancel_takeoff.setEnabled(False)

    def _cancel_takeoffs(self):
        for sequence in list(self._takeoffs):
            sequence.cancel()

    def append_log(self, text, color=None):
        ts = time.strftime("%H:%M:%S")
        text = html.escape(text)
        if color:
            self.txt_log.append(f"<span style='color:{color}'>●</span> {ts} {text}")
        else:
            self.txt_log.append(f"{ts} {text}")

    def _tick(self):
        if self.isVisible():
            self.table.refresh()
            self.refresh_links()
