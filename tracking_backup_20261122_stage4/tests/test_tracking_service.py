"""TrackingService on a real QApplication: Qt event safety, lifecycle, browser commands."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import ast
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication
from pymavlink import mavutil

from Linker import Linker
from tracking_service import TrackingService, mask_url, reason_text
from tracking_dashboard import validate

ROOT = Path(__file__).resolve().parents[1]
FLIGHT = ('send_virtual_rc', 'arm', 'disarm', 'takeoff', '_set_mode', '_send_rc_override_now', 'land', 'rtl',
          'start_mission', 'set_mode_guided', 'set_mode_auto', 'upload_mission')


class Mav:
    def __init__(self):
        self.sent = []

    def command_long_send(self, *args):
        self.sent.append(args)


class FakeVehicle(QObject):
    commandFeedback = pyqtSignal(str, bool, str)
    modeChanged = pyqtSignal(str)
    armedChanged = pyqtSignal(bool)
    connectionStateChanged = pyqtSignal(bool)

    def __init__(self, sysid=1, mode='STABILIZE', armed=False):
        super().__init__()
        self.sysid, self.compid, self.name = sysid, 1, f'SYS {sysid}'
        self.link = SimpleNamespace(link_id=1, human='udp:14550')
        self.key_str = f'1_{sysid}'
        self.autopilot = mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
        self.mav_type = mavutil.mavlink.MAV_TYPE_QUADROTOR
        self.connected, self.connection_epoch = True, 1
        self.master = SimpleNamespace(mav=Mav())
        self.tracking_telemetry = {'GPS_RAW_INT': {'fix_type': 3, 'satellites_visible': 12}}
        self.telemetry_received = {}
        self.beat(mode, armed)

    def beat(self, mode, armed=False):
        now = time.monotonic()
        self.last_mode, self.last_armed = mode, armed
        self.telemetry_received['HEARTBEAT'] = self.telemetry_received['GPS_RAW_INT'] = now
        self.heartbeat_state = (now, mode, armed, 0, 0)


def pump(app, seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(.005)
    return bool(until and until())


class StaticChecks(unittest.TestCase):
    def test_no_qobject_method_is_overridden(self):
        """Regression: TrackingService.event(kind, text) replaced QObject.event and crashed on any event."""
        reserved = {name for name in dir(QObject) if not name.startswith('__')}
        for path in ('tracking_service.py', 'tracking_panel.py', 'tracking_dashboard.py'):
            tree = ast.parse((ROOT / path).read_text())
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                bases = {getattr(b, 'id', getattr(b, 'attr', '')) for b in cls.bases}
                if not bases & {'QObject', 'QDialog', 'QWidget', 'VideoCanvas'}:
                    continue
                allowed = {'paintEvent', 'mousePressEvent', 'mouseMoveEvent', 'mouseReleaseEvent', 'closeEvent'}
                for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                    if fn.name in reserved and fn.name not in allowed:
                        self.fail(f'{path}:{cls.name}.{fn.name} overrides a QObject/QWidget method')

    def test_mask_url_and_reasons(self):
        self.assertEqual(mask_url('rtsp://admin:secret@192.168.144.25:8554/main'),
                         'rtsp://***@192.168.144.25:8554/main')
        self.assertNotIn('secret', mask_url('RTSP://u:secret@h/x'))
        self.assertIn('모서리', reason_text('insufficient_texture'))
        self.assertIn('최신 프레임', reason_text('catchup_failed_redraw_latest'))


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.spies = [self.stack.enter_context(patch.object(Linker, name)) for name in FLIGHT]
        self.service = TrackingService()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.service.shutdown()
        self.stack.close()

    def assert_no_flight_calls(self):
        for spy in self.spies:
            spy.assert_not_called()

    def test_qt_events_and_child_timer(self):
        child = QTimer(self.service)
        child.start(5)
        QCoreApplication.postEvent(self.service, QEvent(QEvent.User))
        QCoreApplication.sendEvent(self.service, QEvent(QEvent.User))
        pump(self.app, .1)
        self.assertTrue(self.service.event(QEvent(QEvent.User)) in (True, False))
        child.stop()

    def test_demo_simulation_intervention_and_double_shutdown(self):
        self.service.start_demo()
        self.assertTrue(pump(self.app, 2, lambda: self.service.session.snapshot().state == 'TRACKING'))
        self.assertEqual(self.service.monitor_label(), 'SIMULATION')
        self.assertIn('SIMULATION', self.service.snapshot()['banner']['text'])
        self.assertTrue(self.service.simulate())
        self.assertTrue(pump(self.app, 1, lambda: self.service.session.sink.output() is not None))
        self.service.execute(dict(action='intervention'))
        result = self.service.session.snapshot()
        self.assertEqual((result.state, result.reason), ('STOPPED', 'operator_intervention'))
        self.assertIsNone(self.service.session.sink.output())
        self.service.shutdown()
        self.service.shutdown()
        self.assert_no_flight_calls()

    def test_vehicle_change_cancels_session_and_ticket(self):
        a, b = FakeVehicle(1), FakeVehicle(2)
        self.service.set_vehicle(a)
        self.service.start_demo()
        self.assertTrue(pump(self.app, 2, lambda: self.service.session.snapshot().state == 'TRACKING'))
        self.service.prepare_mode('LOITER', 'app')
        self.service.set_vehicle(b)
        self.assertEqual(self.service.session.snapshot().reason, 'vehicle_changed')
        self.assertEqual(self.service.modes.state, 'CANCELLED')
        self.assertEqual(a.master.mav.sent + b.master.mav.sent, [])

    def test_tracking_start_and_loss_send_no_flight_command(self):
        v = FakeVehicle(1)
        self.service.set_vehicle(v)
        self.service.start_demo()
        self.assertTrue(pump(self.app, 2, lambda: self.service.session.snapshot().state == 'TRACKING'))
        self.service.demo = False                      # frames stop → watchdog LOST
        self.assertTrue(pump(self.app, 2, lambda: self.service.session.snapshot().state == 'LOST'))
        self.assertEqual(v.master.mav.sent, [])
        self.assertEqual(self.service.modes.state, 'IDLE')
        self.assert_no_flight_calls()

    def test_browser_mode_flow_and_forbidden_requests(self):
        v = FakeVehicle(1)
        self.service.set_vehicle(v)
        run = lambda **c: self.service.execute(validate(c))
        for mode in ('AUTO', 'TAKEOFF', 'MISSION', 'OFFBOARD', 'FLIP'):
            with self.assertRaises(Exception):
                run(action='mode_prepare', mode=mode, vehicle=v.key_str)
        with self.assertRaises(ValueError):
            run(action='mode_prepare', mode='LOITER', vehicle='1_9')     # not the selected vehicle
        self.assertEqual(v.master.mav.sent, [])
        run(action='mode_prepare', mode='LOITER', vehicle=v.key_str)
        ticket = self.service.modes.snapshot()['ticket']
        self.assertEqual(ticket['source'], 'web')
        with self.assertRaises(Exception):
            self.service.commit_mode(ticket['id'], 'app')                # another screen cannot confirm it
        run(action='mode_prepare', mode='LOITER', vehicle=v.key_str)     # re-prepare (supersedes)
        ticket = self.service.modes.snapshot()['ticket']
        run(action='mode_commit', ticket=ticket['id'], vehicle=v.key_str)
        self.assertEqual(len(v.master.mav.sent), 1)
        self.assertEqual(v.master.mav.sent[0][2], mavutil.mavlink.MAV_CMD_DO_SET_MODE)
        v.beat('LOITER')
        self.service.tick()
        self.assertEqual(self.service.modes.state, 'CONFIRMED')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log.json'
            self.service.save(path)
            audit = json.loads(path.read_text())['mode_audit']
        events = [(a['event'], a['source']) for a in audit]
        for expected in (('REJECTED_PREPARE', 'web'), ('REJECTED_WEB_TARGET', 'web'), ('PREPARE', 'web'),
                         ('REJECTED_COMMIT', 'app'), ('SUPERSEDED', 'web'), ('SENT', 'web'), ('CONFIRMED', 'web')):
            self.assertIn(expected, events)
        self.assert_no_flight_calls()

    def test_dashboard_open_close_and_flag(self):
        url = self.service.open_dashboard(('127.0.0.1', 0))
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        self.assertTrue(self.service.snapshot()['app']['web_mode_change'])
        self.service.close_dashboard()
        self.assertIsNone(self.service.dashboard)
        self.assertFalse(self.service.snapshot()['app']['web_mode_change'])

    def test_transport_rule(self):
        self.assertTrue(TrackingService.transport_applies('rtsp://192.168.144.25:8554/main'))
        for source in ('0', '/tmp/video.mp4', 'udp://0.0.0.0:5600', ''):
            self.assertFalse(TrackingService.transport_applies(source))


if __name__ == '__main__':
    unittest.main()


class VideoStaleTests(unittest.TestCase):
    """A frame older than the tracking timeout is STALE (not LIVE) and shows no receive FPS."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stalled_capture_is_stale(self):
        import numpy as np
        from tracking_models import FrameEnvelope
        service = TrackingService()
        self.addCleanup(service.shutdown)
        frame = FrameEnvelope.create('cam', 's', 1, np.zeros((48, 64, 3), np.uint8), time.monotonic())
        service.capture = SimpleNamespace(status=('Connected', True), latest=lambda: frame, release=lambda: None)
        service.tick()
        self.assertEqual(service.video_state()['state'], 'LIVE')
        time.sleep(service.session.timeout + .05)
        service.tick()
        video = service.video_state()
        self.assertEqual((video['state'], video['decode_fps']), ('STALE', None))
        self.assertEqual(service.snapshot()['banner']['level'], 'danger')
