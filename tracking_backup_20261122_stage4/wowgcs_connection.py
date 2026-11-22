# wowgcs_connection.py
from dataclasses import dataclass
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QComboBox,
    QFormLayout, QVBoxLayout, QWidget, QApplication
)
from PyQt5.QtCore import Qt

@dataclass
class LinkSpec:
    human: str   # UI 표현용
    address: str # MAVSDK system_address (udp://..., serial:///, tcp://...)

class ConnectionDialog(QDialog):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setWindowTitle("Select Link")
        self.setModal(True)

        # 모드
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["udp", "serial", "tcp"])
        self.mode_combo.setCurrentText("udp")
        self.mode_combo.currentTextChanged.connect(self._update_form_visibility)

        # UDP
        self.udp_bind_ip = QLineEdit("0.0.0.0")
        self.udp_port = QLineEdit("14540")

        # Serial
        self.serial_port = QLineEdit("/dev/cu.usbmodem1")
        self.serial_baud = QComboBox()
        self.serial_baud.addItems(["57600", "115200", "230400"])
        self.serial_baud.setCurrentText("115200")

        # TCP
        self.tcp_host = QLineEdit("127.0.0.1")
        self.tcp_port = QLineEdit("5760")

        # 섹션 레이아웃
        udp_form = QFormLayout();   udp_form.addRow("Bind IP", self.udp_bind_ip); udp_form.addRow("Port", self.udp_port)
        serial_form = QFormLayout();serial_form.addRow("Port", self.serial_port); serial_form.addRow("Baud", self.serial_baud)
        tcp_form = QFormLayout();   tcp_form.addRow("Host", self.tcp_host);       tcp_form.addRow("Port", self.tcp_port)

        udp_box = QWidget();    udp_box.setLayout(udp_form)
        serial_box = QWidget(); serial_box.setLayout(serial_form)
        tcp_box = QWidget();    tcp_box.setLayout(tcp_form)

        self.stack = {"udp": udp_box, "serial": serial_box, "tcp": tcp_box}

        # 버튼
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        # 메인 레이아웃
        main = QVBoxLayout(self)
        main.addWidget(QLabel("Link Type"))
        main.addWidget(self.mode_combo)
        for w in self.stack.values():
            main.addWidget(w)
        main.addStretch(1)
        main.addWidget(self.buttons)

        self._update_form_visibility()

    def set_mode(self, mode: str):
        m = (mode or "").lower()
        if m in self.stack:
            self.mode_combo.setCurrentText(m)
        self._update_form_visibility()

    def _update_form_visibility(self):
        cur = self.mode_combo.currentText().lower()
        for name, widget in self.stack.items():
            widget.setVisible(name == cur)

    def get_spec(self) -> "LinkSpec | None":
        if self.exec_() != QDialog.Accepted:
            return None
        mode = self.mode_combo.currentText().lower()

        if mode == "udp":
            ip = self.udp_bind_ip.text().strip() or "0.0.0.0"
            port = self.udp_port.text().strip() or "14540"
            return LinkSpec(f"UDP {ip}:{port}", f"udp://{ip}:{port}")

        if mode == "serial":
            port = self.serial_port.text().strip() or "/dev/cu.usbmodem1"
            baud = self.serial_baud.currentText().strip() or "115200"
            return LinkSpec(f"Serial {port} @ {baud}", f"serial:///{port}:{baud}")

        host = self.tcp_host.text().strip() or "127.0.0.1"
        port = self.tcp_port.text().strip() or "5760"
        return LinkSpec(f"TCP {host}:{port}", f"tcp://{host}:{port}")

# 단독 테스트용
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    dlg = ConnectionDialog()
    spec = dlg.get_spec()
    print("Selected:" if spec else "Canceled", spec)
