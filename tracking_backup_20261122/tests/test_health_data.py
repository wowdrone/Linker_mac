"""Health charts: decoded values, missing data, stream requests and real UDP."""
import os, sys, time, inspect, socket, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QSettings
from pymavlink.dialects.v20 import ardupilotmega as mav
from Linker import Linker
from fleet import FleetManager, ActiveVehicleProxy
from tab_health import HealthTab

def message(kind, **values):
    cls = getattr(mav, 'MAVLink_' + kind.lower() + '_message')
    return cls(**{name: values.get(name, param.default if param.default is not inspect.Parameter.empty else 0) for name, param in inspect.signature(cls).parameters.items()})

class HealthTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.settings = QSettings(str(Path(self.folder.name) / 'health.ini'), QSettings.IniFormat)
        self.tab = HealthTab(settings=self.settings)
        self.tab.set_connected(True)
        self.linker = Linker()
        self.linker.telemetryUpdated.connect(self.tab.update_from_telemetry)

    def tearDown(self):
        self.linker._rc_override_timer.stop()
        self.tab.timer.stop()
        self.tab.close()
        self.folder.cleanup()

    def until(self, predicate, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('Health UDP timeout')

    def test_no_fabricated_zeros_or_duplicate_samples(self):
        for _ in range(10):
            self.linker._update_telemetry()
        self.tab.refresh()
        self.assertFalse(self.tab.latest)
        self.assertEqual(self.tab.plots[0].lines['Velocity'].count(), 0)
        self.linker.handle_message(message('EKF_STATUS_REPORT', velocity_variance=0.25, pos_horiz_variance=0.4, pos_vert_variance=0, flags=19))
        for _ in range(10):
            self.linker._update_telemetry()
        self.tab.refresh()
        self.assertEqual(len(self.tab.history['ekf']), 1)
        self.assertAlmostEqual(self.tab.plots[0].lines['Velocity'].at(0).y(), 0.25)
        self.assertEqual(self.tab.plots[0].lines['Pos V'].at(0).y(), 0)

    def test_rc_sentinels_channel_selection_and_vehicle_reset(self):
        self.linker.handle_message(message('RC_CHANNELS', chancount=18, chan1_raw=1100, chan2_raw=65535, chan3_raw=0, chan18_raw=1850, rssi=255))
        self.linker.handle_message(message('SERVO_OUTPUT_RAW', servo1_raw=1600, servo9_raw=1725))
        self.tab.refresh()
        values = self.tab.latest['rc_in']['values']
        self.assertIsNone(values['CH2'])
        self.assertIsNone(values['CH3'])
        self.assertEqual(values['CH18'], 1850)
        plot = self.tab.plots[1]
        plot.choose_field('CH18', True)
        self.assertEqual(plot.lines['CH18'].at(0).y(), 1850)
        self.assertEqual(self.tab.plots[2].lines['CH1'].at(0).y(), 1600)
        self.tab.reset_vehicle(SimpleNamespace(name='SYS 2', connected=True))
        self.assertFalse(self.tab.latest)
        self.assertEqual(plot.lines['CH18'].count(), 0)

    def test_staleness_and_correct_sensor_units(self):
        self.linker.handle_message(message('VIBRATION', vibration_x=140, vibration_y=2, vibration_z=0))
        self.linker.handle_message(message('GPS_RAW_INT', satellites_visible=255, eph=65535, fix_type=1))
        self.linker.handle_message(message('RADIO_STATUS', rssi=200, remrssi=255))
        self.assertEqual(self.tab.latest['vibration']['values']['X'], 140)
        self.assertIsNone(self.tab.latest['gps']['values']['Sats'])
        self.assertIsNone(self.tab.latest['radio']['values']['Remote'])
        plot = self.tab.plots[0]
        plot.selector.setCurrentIndex(plot.selector.findData('vibration'))
        with patch('tab_health.time.monotonic', return_value=time.monotonic() + 4):
            plot.render()
            self.assertIn('수신 중단', plot.value.text())
        self.assertEqual(len(self.tab.history['vibration']), 1)

    def test_request_button_and_servo_zero_are_real_values(self):
        from PyQt5.QtWidgets import QPushButton
        requested = []
        self.tab.streamsRequested.connect(lambda: requested.append(True))
        self.tab.findChild(QPushButton).click()
        self.assertEqual(requested, [True])
        self.linker.handle_message(message('SERVO_OUTPUT_RAW', servo1_raw=0))
        self.tab.refresh()
        self.assertEqual(self.tab.plots[2].lines['CH1'].at(0).y(), 0)

    def test_stream_intervals_target_selected_fc(self):
        self.linker.master = SimpleNamespace(mav=Mock())
        self.linker.target_system = 7
        self.linker.target_component = 1
        self.linker.request_health_streams()
        calls = self.linker.master.mav.command_long_send.call_args_list
        self.assertEqual({call.args[4] for call in calls}, {241, 24, 109, 193, 230, 65, 36})
        self.assertTrue(all((call.args[:2] == (7, 1) and call.args[5] > 0 for call in calls)))

    def test_real_mavlink_udp_to_active_vehicle_and_charts(self):
        fleet = FleetManager()
        proxy = ActiveVehicleProxy()
        proxy.telemetryUpdated.connect(self.tab.update_from_telemetry)
        fc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fc.bind(('127.0.0.1', 0))
        fc.setblocking(False)
        reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reservation.bind(('127.0.0.1', 0))
        address = reservation.getsockname()
        reservation.close()
        encoder = mav.MAVLink(None, srcSystem=7, srcComponent=1)

        def send(kind, **fields):
            fc.sendto(message(kind, **fields).pack(encoder), address)
        try:
            fleet.add_link('udp', f'{address[0]}:{address[1]}')
            self.until(lambda: next(iter(fleet.links.values())).master is not None)
            send('HEARTBEAT', type=2, autopilot=3, base_mode=0, custom_mode=0, system_status=3, mavlink_version=3)
            self.until(lambda: bool(fleet.vehicles))
            vehicle = fleet.vehicle_list()[0]
            self.tab.reset_vehicle(vehicle)
            proxy.set_vehicle(vehicle)
            send('EKF_STATUS_REPORT', flags=19, velocity_variance=0.35, pos_horiz_variance=0.6, pos_vert_variance=0.2)
            send('RC_CHANNELS', chancount=8, chan1_raw=1450, chan2_raw=1600, chan3_raw=1700, chan4_raw=1200)
            send('SERVO_OUTPUT_RAW', servo1_raw=1550, servo2_raw=1650)
            send('VIBRATION', vibration_x=4, vibration_y=5, vibration_z=6)
            self.until(lambda: all((key in self.tab.latest for key in ('ekf', 'rc_in', 'rc_out', 'vibration'))))
            self.tab.refresh()
            self.assertEqual(self.tab.plots[1].lines['CH1'].at(0).y(), 1450)
            self.assertEqual(self.tab.plots[2].lines['CH1'].at(0).y(), 1550)
            self.assertAlmostEqual(self.tab.plots[0].lines['Velocity'].at(0).y(), 0.35, places=5)
            parser = mav.MAVLink(None)
            requested = set()
            for _ in range(30):
                try:
                    raw = fc.recv(8192)
                except BlockingIOError:
                    break
                for decoded in parser.parse_buffer(raw) or []:
                    if decoded.get_type() == 'COMMAND_LONG' and decoded.command == 511:
                        requested.add(int(decoded.param1))
            self.assertTrue({193, 65, 36}.issubset(requested), requested)
        finally:
            proxy.set_vehicle(None)
            fleet.remove_all()
            fc.close()
            fleet._watchdog.stop()
            for vehicle in fleet.vehicle_list():
                vehicle._rc_override_timer.stop()
if __name__ == '__main__':
    unittest.main()
