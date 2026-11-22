"""Tracking browser dashboard over a real loopback socket (ephemeral port)."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import http.client
import json
import unittest

from PyQt5.QtWidgets import QApplication

from tracking_dashboard import TrackingDashboard, validate

REF = dict(source_id='ab12', session_id='cd34', frame_seq=5, width=640, height=480)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = TrackingDashboard(('127.0.0.1', 0))
        self.addCleanup(self.server.close)
        self.host = f'127.0.0.1:{self.server.port}'

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=3)
        self.addCleanup(conn.close)
        base = {'Host': self.host}
        if method == 'POST':
            base.update({'Origin': f'http://{self.host}', 'X-Dashboard-Token': self.server.token,
                         'Content-Type': 'application/json'})
        base.update(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        conn.request(method, path, body=data, headers=base)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def post(self, body=None, **kw):
        return self.call('POST', '/command', body, **kw)[0]

    def test_page_token_substituted_once(self):
        status, headers, page = self.call('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'__DASHBOARD_TOKEN__', page)
        self.assertEqual(page.count(self.server.token.encode()), 1)
        self.assertIn('Linker Tracking / Telemetry', page.decode())

    def test_get_guards(self):
        self.assertEqual(self.call('GET', '/', headers={'Host': 'evil.example:80'})[0], 403)
        self.assertEqual(self.call('GET', '/state')[0], 403)
        self.assertEqual(self.call('GET', '/frame.jpg')[0], 403)
        self.assertEqual(self.call('GET', '/state', headers={'X-Dashboard-Token': 'wrong'})[0], 403)
        self.assertEqual(self.call('GET', '/nope')[0], 404)

    def test_state_and_frame(self):
        token = {'X-Dashboard-Token': self.server.token}
        status, _, body = self.call('GET', '/state', headers=token)
        self.assertEqual((status, json.loads(body)), (200, {'standby': True}))
        self.server.publish_state({'app': {'monitor': 'MONITOR ONLY'}})
        self.assertEqual(json.loads(self.call('GET', '/state', headers=token)[2])['app']['monitor'], 'MONITOR ONLY')
        self.assertEqual(self.call('GET', '/frame.jpg', headers=token)[0], 204)
        meta = dict(REF, received=1.0, selecting=False,
                    overlay=dict(session='e' * 32, bbox=[1, 2, 30, 40], center=[16, 22], trail=[[16, 22]]))
        self.server.publish_frame(b'\xff\xd8jpeg', meta)
        status, headers, body = self.call('GET', '/frame.jpg', headers=token)
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/jpeg', b'\xff\xd8jpeg'))
        parsed = json.loads(headers['X-Frame-Meta'])
        for key in ('source_id', 'session_id', 'frame_seq', 'width', 'height', 'selecting', 'overlay'):
            self.assertIn(key, parsed)
        self.assertEqual(parsed['overlay']['bbox'], [1, 2, 30, 40])
        import time
        self.assertTrue(self.server.frame_wanted(time.monotonic()))

    def test_post_guards(self):
        cmd = {'action': 'stop'}
        self.assertEqual(self.post(cmd, headers={'Host': 'evil.example:80'}), 403)
        self.assertEqual(self.post(cmd, headers={'X-Dashboard-Token': ''}), 403)
        self.assertEqual(self.post(cmd, headers={'Origin': 'http://evil.example'}), 403)
        self.assertEqual(self.post(cmd, headers={'Origin': ''}), 403)
        self.assertEqual(self.call('POST', '/other', cmd)[0], 404)
        self.assertEqual(self.post(raw=b'{' + b' ' * 5000 + b'}'), 400)
        self.assertEqual(self.post(raw=b'not json'), 400)
        self.assertEqual(self.post(raw=b'[1,2]'), 400)
        self.assertEqual(self.server.drain(), [])

    def test_validation_rejects_bad_payloads(self):
        bad = [
            {'action': 'stop', 'extra': 1},
            {'action': 'launch'},
            {'action': 'arm'}, {'action': 'takeoff'}, {'action': 'rc_override'},
            {'action': 'request', 'message_id': 30},
            {'action': 'select_roi', 'ref': REF, 'roi': [float('nan'), 1, 20, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [float('inf'), 1, 20, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [-1, 1, 20, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [1, 1, 0, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [630, 1, 20, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [1, 1, 20]},
            {'action': 'select_roi', 'ref': REF, 'roi': [True, 1, 20, 20]},
            {'action': 'select_roi', 'ref': dict(REF, source_id='XYZ!'), 'roi': [1, 1, 20, 20]},
            {'action': 'select_roi', 'ref': dict(REF, frame_seq=True), 'roi': [1, 1, 20, 20]},
            {'action': 'select_roi', 'ref': dict(REF, extra=1), 'roi': [1, 1, 20, 20]},
            {'action': 'mode_prepare', 'mode': 'loiter', 'vehicle': '1_1'},
            {'action': 'mode_prepare', 'mode': 'LOITER', 'vehicle': 'abc'},
            {'action': 'mode_prepare', 'mode': 'LOITER', 'vehicle': '1_1', 'param2': 4},
            {'action': 'mode_commit', 'ticket': 'zz', 'vehicle': '1_1'},
            {'action': 'mode_commit', 'ticket': 'a' * 31, 'vehicle': '1_1'},
        ]
        for body in bad:
            with self.subTest(body=body):
                raw = json.dumps(body, allow_nan=True).encode()
                self.assertEqual(self.post(raw=raw), 400)
                with self.assertRaises(ValueError):
                    validate(json.loads(raw))
        self.assertEqual(self.server.drain(), [])

    def test_valid_commands_and_queue_limit(self):
        good = [{'action': 'stop'}, {'action': 'select_roi', 'ref': REF, 'roi': [10.5, 20, 30, 40]},
                {'action': 'mode_prepare', 'mode': 'LOITER', 'vehicle': '1_1'},
                {'action': 'mode_commit', 'ticket': 'a' * 32, 'vehicle': '1_1'}, {'action': 'mode_cancel'}]
        for body in good:
            self.assertEqual(self.post(body), 202)
        drained = self.server.drain()
        self.assertEqual(drained[1]['ref'], ('ab12', 'cd34', 5, 640, 480))
        self.assertEqual(drained[1]['roi'], (10.5, 20.0, 30.0, 40.0))
        for _ in range(TrackingDashboard.QUEUE):
            self.assertEqual(self.post({'action': 'stop'}), 202)
        self.assertEqual(self.post({'action': 'stop'}), 429)


class EndToEndTests(unittest.TestCase):
    """HTTP → queue → Qt-thread execution against a fake vehicle (no real link)."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tracking_service import TrackingService
        from test_tracking_service import FakeVehicle
        self.service = TrackingService()
        self.addCleanup(self.service.shutdown)
        self.vehicle = FakeVehicle(1)
        self.service.set_vehicle(self.vehicle)
        self.service.open_dashboard(('127.0.0.1', 0))
        self.server = self.service.dashboard
        self.host = f'127.0.0.1:{self.server.port}'

    def post(self, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', '/command', body=json.dumps(body).encode(),
                     headers={'Host': self.host, 'Origin': f'http://{self.host}',
                              'X-Dashboard-Token': self.server.token})
        status = conn.getresponse().status
        self.service.tick()            # drain on the Qt thread
        return status

    def test_forbidden_modes_wrong_vehicle_and_expired_ticket(self):
        for mode in ('AUTO', 'TAKEOFF', 'MISSION'):
            self.assertEqual(self.post({'action': 'mode_prepare', 'mode': mode, 'vehicle': '1_1'}), 202)
            self.assertIn('요청 실패', self.service.web_result)
        self.assertEqual(self.post({'action': 'mode_prepare', 'mode': 'LOITER', 'vehicle': '1_9'}), 202)
        self.assertIn('선택 기체와 다릅니다', self.service.web_result)
        self.assertEqual(self.post({'action': 'mode_commit', 'ticket': 'b' * 32, 'vehicle': '1_1'}), 202)
        self.assertIn('요청 실패', self.service.web_result)
        self.assertEqual(self.vehicle.master.mav.sent, [])

    def test_prepare_commit_sends_once(self):
        self.assertEqual(self.post({'action': 'mode_prepare', 'mode': 'LOITER', 'vehicle': '1_1'}), 202)
        ticket = self.service.modes.snapshot()['ticket']['id']
        for _ in range(3):           # repeated clicks / tabs
            self.post({'action': 'mode_commit', 'ticket': ticket, 'vehicle': '1_1'})
        self.assertEqual(len(self.vehicle.master.mav.sent), 1)


if __name__ == '__main__':
    unittest.main()
