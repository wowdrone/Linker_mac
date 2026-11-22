"""Static checks of tracking_dashboard.html against the real TrackingService snapshot."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import re
import time
import unittest

from PyQt5.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'tracking_dashboard.html').read_text(encoding='utf-8')
TOP = {'app', 'banner', 'video', 'tracking', 'simulation', 'vehicle', 'modes', 'sentinel', 'events'}


def keys(var):
    return set(re.findall(r'(?<![\w.])%s\.([a-z_]+)\b(?!\()' % re.escape(var), HTML))


class StaticHtml(unittest.TestCase):
    def test_self_contained(self):
        self.assertIsNone(re.search(r'https?://', HTML), 'external URL in page')
        self.assertNotRegex(HTML, r'(?i)<link\b|@import|@font-face|<script\s[^>]*src=|<img\s[^>]*src=|url\(')
        self.assertNotIn('fonts.googleapis', HTML)

    def test_token_placeholder_once(self):
        self.assertEqual(HTML.count('__DASHBOARD_TOKEN__'), 1)

    def test_only_allowed_commands_posted(self):
        actions = set(re.findall(r"action:'([a-z_]+)'", HTML)) | set(re.findall(r'data-cmd="([a-z_]+)"', HTML))
        from tracking_dashboard import SIMPLE
        self.assertTrue(actions <= SIMPLE | {'select_roi', 'mode_prepare', 'mode_commit'}, actions)
        self.assertFalse(actions & {'arm', 'disarm', 'takeoff', 'request', 'rc', 'mission'})


class SnapshotKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_page_keys_exist_in_snapshot(self):
        from tracking_service import TrackingService
        from test_tracking_service import FakeVehicle
        service = TrackingService()
        self.addCleanup(service.shutdown)
        v = FakeVehicle(1)
        now = time.monotonic()
        v.tracking_telemetry.update({
            'ATTITUDE': dict(roll=.1, pitch=-.1, yaw=1.0), 'VFR_HUD': dict(alt=120.0, groundspeed=3.2, heading=90),
            'GLOBAL_POSITION_INT': dict(lat=375000000, lon=1270000000, alt=120000, relative_alt=15000),
            'SYS_STATUS': dict(voltage_battery=15800, battery_remaining=76),
            'AUTOPILOT_VERSION': dict(flight_sw_version=67372800)})
        v.telemetry_received.update({k: now for k in v.tracking_telemetry})
        service.set_vehicle(v)
        service.start_demo()
        end = time.monotonic() + 2
        while service.session.snapshot().state != 'TRACKING' and time.monotonic() < end:
            self.app.processEvents(); time.sleep(.005)
        service.prepare_mode('LOITER', 'web')
        snap = service.snapshot()
        self.assertEqual(set(snap), TOP)
        self.assertEqual(keys('s'), TOP)
        self.assertEqual(keys('state') - {'standby'}, keys('state') & TOP)
        for var, section in (('t', snap['tracking']), ('v', snap['video']), ('veh', snap['vehicle']),
                             ('ctl', snap['modes']['controller']), ('tk', snap['modes']['controller']['ticket']),
                             ('a', snap['vehicle']['attitude']), ('alt', snap['vehicle']['altitude']),
                             ('gs', snap['vehicle']['groundspeed'])):
            with self.subTest(var=var):
                self.assertTrue(keys(var) <= set(section), keys(var) - set(section))
        service.begin_selection()
        sel = service.snapshot()['tracking']['selection']
        self.assertTrue(keys('sel') <= set(sel))


if __name__ == '__main__':
    unittest.main()
