"""Flight command confirmations and non-blocking takeoff sequence."""
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QMessageBox


def confirm_flight(parent, action, vehicles):
    lines = [f"{v.name} (SYS ID {v.sysid}) · 모드 {v.last_mode} · "
             f"{'시동 켜짐' if v.last_armed else '시동 꺼짐'} · 고도 {v.altitude:.1f} m"
             for v in vehicles]
    box = QMessageBox(QMessageBox.Question, '비행 명령 확인',
                      f"명령: {action}\n\n" + '\n'.join(lines),
                      QMessageBox.Yes | QMessageBox.No, parent)
    box.setDefaultButton(QMessageBox.No)
    box.setStyleSheet('QLabel, QPushButton { font-size: 16px; }')
    return box.exec_() == QMessageBox.Yes


class TakeoffSequence(QObject):
    finished = pyqtSignal(object, bool, str)

    def __init__(self, vehicle, altitude, parent=None):
        super().__init__(parent)
        self.vehicle, self.altitude = vehicle, altitude
        self.stage = 'mode'
        self.arm_accepted = False
        self.started = time.time()
        self.deadline = time.monotonic() + 3
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        vehicle.commandFeedback.connect(self._ack)
        self.timer.start()
        QTimer.singleShot(0, self._start)

    def _start(self):
        if self.stage == 'done':
            return
        try:
            self.vehicle.set_mode_guided()
        except Exception as exc:
            self._finish(False, f'GUIDED 전환 실패: {exc}')

    def cancel(self):
        self._finish(False, '이륙 절차 취소')

    def _finish(self, ok, message):
        if self.stage == 'done':
            return
        self.stage = 'done'
        self.timer.stop()
        try:
            self.vehicle.commandFeedback.disconnect(self._ack)
        except (TypeError, RuntimeError):
            pass
        self.finished.emit(self.vehicle, ok, message)

    def _poll(self):
        v = self.vehicle
        if not v.connected:
            self._finish(False, '기체 연결 끊김')
        elif time.monotonic() >= self.deadline:
            self._finish(False, f'{self.stage} 단계 시간 초과')
        elif self.stage == 'mode' and v.last_heartbeat > self.started and v.last_mode.upper() == 'GUIDED':
            if v.last_armed:
                self._send_takeoff()
            else:
                self.stage = 'arm'
                self.started = time.time()
                self.deadline = time.monotonic() + 5
                v.arm()
        elif self.stage == 'arm' and self.arm_accepted and v.last_heartbeat > self.started and v.last_armed:
            self._send_takeoff()

    def _send_takeoff(self):
        self.stage = 'takeoff'
        self.deadline = time.monotonic() + 5
        self.vehicle.takeoff(self.altitude)

    def _ack(self, command, success, message):
        if self.stage == 'arm' and command == 'MAV_CMD_COMPONENT_ARM_DISARM':
            if not success:
                self._finish(False, f'ARM 실패: {message}')
            else:
                self.arm_accepted = True
        elif self.stage == 'takeoff' and command == 'MAV_CMD_NAV_TAKEOFF':
            self._finish(success, message)


class MissionSequence(QObject):
    uploaded = pyqtSignal(object)
    finished = pyqtSignal(object, bool, str)

    def __init__(self, vehicle, waypoints, parent=None):
        super().__init__(parent)
        self.vehicle, self.waypoints = vehicle, waypoints
        self.stage = 'upload'
        self.deadline = time.monotonic() + 10
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        vehicle.commandFeedback.connect(self._ack)
        self.timer.start()
        QTimer.singleShot(0, self._start)

    def _start(self):
        if self.stage == 'upload':
            self.vehicle.upload_mission(self.waypoints)

    def start_confirmed(self):
        if self.stage != 'confirm':
            return
        self.stage = 'mode'
        self.started = time.time()
        self.deadline = time.monotonic() + 3
        self.vehicle.set_mode_auto()

    def cancel(self):
        self._finish(False, '미션 절차 취소')

    def _poll(self):
        if not self.vehicle.connected:
            self._finish(False, '기체 연결 끊김')
        elif self.stage != 'confirm' and time.monotonic() >= self.deadline:
            self._finish(False, f'{self.stage} 단계 시간 초과')
        elif (self.stage == 'mode' and self.vehicle.last_heartbeat > self.started
              and self.vehicle.last_mode.upper() == 'AUTO'):
            self.stage = 'start'
            self.deadline = time.monotonic() + 5
            self.vehicle.start_mission()

    def _ack(self, command, success, message):
        if self.stage == 'upload' and command == 'UPLOAD_MISSION':
            if success:
                self.stage = 'confirm'
                self.uploaded.emit(self.vehicle)
            else:
                self._finish(False, message)
        elif self.stage == 'start' and command == 'MAV_CMD_MISSION_START':
            self._finish(success, message)

    def _finish(self, ok, message):
        if self.stage == 'done':
            return
        self.stage = 'done'
        self.timer.stop()
        try:
            self.vehicle.commandFeedback.disconnect(self._ack)
        except (TypeError, RuntimeError):
            pass
        self.finished.emit(self.vehicle, ok, message)
