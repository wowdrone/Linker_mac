import time
import unittest

from PyQt5.QtCore import QCoreApplication, QObject, pyqtSignal
from flight_actions import TakeoffSequence, MissionSequence


class FakeVehicle(QObject):
    commandFeedback = pyqtSignal(str, bool, str)

    def __init__(self):
        super().__init__()
        self.connected = True
        self.last_armed = False
        self.last_mode = 'STABILIZE'
        self.last_heartbeat = 0
        self.calls = []

    def set_mode_guided(self): self.calls.append('guided')
    def set_mode_auto(self): self.calls.append('auto')
    def arm(self): self.calls.append('arm')
    def takeoff(self, altitude): self.calls.append('takeoff')
    def upload_mission(self, waypoints): self.calls.append('upload')
    def start_mission(self): self.calls.append('start')


class FlightActionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_arm_failure_never_takes_off(self):
        vehicle = FakeVehicle()
        action = TakeoffSequence(vehicle, 10)
        self.app.processEvents()
        vehicle.last_mode = 'GUIDED'
        vehicle.last_heartbeat = time.time() + 1
        action._poll()
        self.assertIn('arm', vehicle.calls)
        vehicle.commandFeedback.emit('MAV_CMD_COMPONENT_ARM_DISARM', False, 'denied')
        self.assertNotIn('takeoff', vehicle.calls)
        self.assertEqual(action.stage, 'done')

    def test_mode_timeout_never_arms(self):
        vehicle = FakeVehicle()
        action = TakeoffSequence(vehicle, 10)
        action.deadline = 0
        action._poll()
        self.assertNotIn('arm', vehicle.calls)
        self.assertNotIn('takeoff', vehicle.calls)

    def test_upload_failure_and_timeout_never_start(self):
        for timeout in (False, True):
            vehicle = FakeVehicle()
            action = MissionSequence(vehicle, [{}])
            self.app.processEvents()
            if timeout:
                action.deadline = 0
                action._poll()
            else:
                vehicle.commandFeedback.emit('UPLOAD_MISSION', False, 'rejected')
            self.assertNotIn('start', vehicle.calls)
            self.assertEqual(action.stage, 'done')


if __name__ == '__main__':
    unittest.main()
