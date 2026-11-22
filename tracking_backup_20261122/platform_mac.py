# platform_mac.py
# macOS 전용 보정 (폰트 / 시리얼 포트 / Qt 환경변수)

import os
import re
import subprocess
import sys

UI_FONT = "Apple SD Gothic Neo"
MONO_FONT = "Menlo"
DEFAULT_SERIAL = "/dev/cu.usbmodem1"
SERIAL_LINK_KINDS = {"serial", "bluetooth"}

# 시리얼 목록에서 숨길 macOS 기본 포트
_IGNORED_PORTS = ("Bluetooth-Incoming-Port", "debug-console", "wlan-debug")
_BLUETOOTH_DRIVERS = ("IOUserBluetoothSerialDriver", "IOBluetoothSerialClient")
_CALLOUT_DEVICE = re.compile(r'"IOCalloutDevice" = "(/dev/cu\.[^"\n]+)"')
_SELECT_BLUETOOTH = "Bluetooth 장치 선택"
_NO_BLUETOOTH = "페어링된 SPP 포트 없음"


def serial_callout_port(port):
    port = port.strip()
    if port.startswith("/dev/tty."):
        return "/dev/cu." + port[len("/dev/tty."):]
    return port


def prepare_env():
    """QApplication 생성 전에 호출."""
    # Big Sur 이후 Qt 5.15 창이 비어 보이는 문제 방지
    os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    # Finder 에서 .app 실행 시 작업 폴더가 / 이므로 리소스 폴더로 이동
    if getattr(sys, "frozen", False):
        os.chdir(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))


def setup_fonts():
    """윈도우용 폰트 이름이 남아 있어도 mac 폰트로 보이도록 치환."""
    from PyQt5.QtGui import QFont
    for name in ("Malgun Gothic", "맑은 고딕", "Segoe UI"):
        QFont.insertSubstitution(name, UI_FONT)
    QFont.insertSubstitution("Consolas", MONO_FONT)


def list_serial_ports():
    """mac 시리얼 포트 목록. 송신용 /dev/cu.* 만 사용 (/dev/tty.* 는 DCD 대기로 멈출 수 있음)."""
    import serial.tools.list_ports
    ports = []
    for p in serial.tools.list_ports.comports():
        dev = serial_callout_port(p.device)
        if any(k in dev for k in _IGNORED_PORTS):
            continue
        if dev not in ports:
            ports.append(dev)
    # 텔레메트리 모뎀/FC(usbmodem, usbserial, SLAB, wchusbserial)를 앞으로
    ports.sort(key=lambda d: (not any(k in d for k in ("usbmodem", "usbserial", "SLAB", "wchusb")), d))
    return ports


def list_bluetooth_ports():
    """Get outbound serial ports published by macOS Bluetooth SPP drivers."""
    for driver in _BLUETOOTH_DRIVERS:
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-r", "-n", driver, "-l", "-w", "0"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=1,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        ports = set(_CALLOUT_DEVICE.findall(result.stdout))
        ports.discard("/dev/cu.Bluetooth-Incoming-Port")
        if ports:
            return sorted(ports, key=str.casefold)
    return []


def refresh_port_selector(combo, kind):
    if combo.view().isVisible():
        return
    current = combo.currentText()
    bluetooth = kind == "bluetooth"
    ports = list_bluetooth_ports() if bluetooth else list_serial_ports() or [DEFAULT_SERIAL]
    choices = [(port, port) for port in ports]
    if bluetooth:
        choices.insert(0, (_SELECT_BLUETOOTH if ports else _NO_BLUETOOTH, None))
    if [combo.itemText(i) for i in range(combo.count())] == [text for text, _ in choices]:
        return
    signals_blocked = combo.blockSignals(True)
    try:
        combo.clear()
        for text, port in choices:
            combo.addItem(text, port)
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif combo.isEditable() and current and current not in {_SELECT_BLUETOOTH, _NO_BLUETOOTH}:
            combo.setEditText(current)
    finally:
        combo.blockSignals(signals_blocked)
