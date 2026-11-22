"""Single-stream video structure: pool keys, relay route, link warnings, decode health,
relay dropout/recovery from a recorded file, and one JPEG encode per dashboard.

No mediamtx, camera or HM30 is used: the relay is simulated at the capture boundary
(VideoWorker._open) with frames from an encoded synthetic MP4.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import http.client
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PyQt5.QtWidgets import QApplication

import video_grid
from video_grid import RELAY_URL, _SharedCapture, _capture_pool, _source_key, direct_rtsp_connections, source_route
from video_health import DecodeHealth, WARNING
from tracking_session import TrackingSession


def eventually(predicate, timeout=4.0, app=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if app:
            app.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


def write_clip(path, frames=90, size=(320, 240)):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 30, size)
    texture = cv2.GaussianBlur(np.random.default_rng(11).integers(0, 256, (70, 70, 3), np.uint8), (3, 3), 1)
    for i in range(frames):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        frame[80:150, 60 + i % 60:130 + i % 60] = texture
        writer.write(frame)
    writer.release()


class KeyTests(unittest.TestCase):
    def test_same_stream_spellings_share_one_key(self):
        same = ['rtsp://Cam.Local/main', 'RTSP://cam.local:554/main', 'rtsp://cam.local/main/',
                'rtsp://CAM.LOCAL:554/main/']
        self.assertEqual(len({_source_key(u) for u in same}), 1)
        self.assertNotEqual(_source_key('rtsp://cam.local/main'), _source_key('rtsp://cam.local/Main'))   # path case kept
        self.assertNotEqual(_source_key('rtsp://cam.local:8554/main'), _source_key('rtsp://cam.local/main'))
        self.assertEqual(_source_key('http://h:80/x/'), _source_key('HTTP://h/x'))

    def test_credentials_rule(self):
        plain = _source_key('rtsp://cam.local/main')
        a = _source_key('rtsp://admin:secret@cam.local/main')
        b = _source_key('rtsp://admin:other@cam.local:554/main/')
        self.assertNotEqual(plain, a)          # with vs without credentials: separate decoders
        self.assertNotEqual(a, b)              # different credentials: separate decoders
        self.assertEqual(a, _source_key('RTSP://admin:secret@CAM.local:554/main/'))
        for key in (a, b):
            self.assertNotIn('secret', key); self.assertNotIn('other', key); self.assertNotIn('admin', key)

    def test_route(self):
        self.assertEqual(source_route(RELAY_URL), '로컬 중계')
        self.assertEqual(source_route('rtsp://192.168.144.25:8554/main.264'), '직접 연결')
        self.assertEqual(source_route('0'), '장치')
        self.assertEqual(source_route(__file__), '파일')
        self.assertIn(('로컬 중계(권장)', RELAY_URL), video_grid.VIDEO_SOURCES)

    def test_direct_connection_count_ignores_loopback_relay(self):
        fake = SimpleNamespace(users=1)
        keys = ['rtsp://192.168.144.25:8554/main.264', 'rtsp://192.168.144.25:8554/video1',
                RELAY_URL, 'rtsp://10.0.0.9/x']
        try:
            for key in keys:
                _capture_pool[key] = fake
            self.assertEqual(direct_rtsp_connections(), {'192.168.144.25': 2, '10.0.0.9': 1})
        finally:
            for key in keys:
                _capture_pool.pop(key, None)


class DecodeHealthTests(unittest.TestCase):
    def feed(self, health, fps, seconds, start=0.0, src=30.0):
        t = start
        while t < start + seconds:
            health.observe(t, src)
            t += 1 / fps
        return t

    def test_steady_rate_has_no_warning(self):
        h = DecodeHealth()
        end = self.feed(h, 30, 8)
        self.assertIsNone(h.snapshot(end)['warning'])

    def test_sustained_low_rate_warns_only_after_window(self):
        h = DecodeHealth()
        end = self.feed(h, 20, 3)                 # 67 % of 30 fps, but only 3 s of data
        self.assertFalse(h.snapshot(end)['lagging'])
        end = self.feed(h, 20, 4, start=end)
        snap = h.snapshot(end)
        self.assertTrue(snap['lagging']); self.assertEqual(snap['warning'], WARNING)
        end = self.feed(h, 30, 6, start=end)      # recovered
        self.assertFalse(h.snapshot(end)['lagging'])

    def test_burst_detection_and_hold(self):
        h = DecodeHealth(hold_s=2.0)
        end = self.feed(h, 30, 2)
        for i in range(10):                       # 10 frames 2 ms apart (decoder catching up)
            h.observe(end + i * .002, 30)
        snap = h.snapshot(end + .03)
        self.assertTrue(snap['bursting']); self.assertEqual(snap['warning'], WARNING)
        self.assertFalse(h.snapshot(end + 3)['bursting'])

    def test_threshold_validation_and_settings(self):
        for bad in (dict(ratio=0), dict(ratio=float('nan')), dict(window_s=0.5), dict(burst_run=1.5),
                    dict(unknown=1)):
            with self.assertRaises(ValueError):
                DecodeHealth(**bad)
        settings = SimpleNamespace(value=lambda key, default=None: {'video/decode_health/ratio': '0.5',
                                                                    'video/decode_health/burst_run': 'x'}.get(key))
        h = DecodeHealth.from_settings(settings)
        self.assertEqual((h.ratio, h.burst_run), (.5, 8))


class FakeRelayCap:
    """Reads a recorded file at 30 fps; after `drop_after` frames the relay loses the camera."""
    def __init__(self, path, drop_after=None):
        self.cap = cv2.VideoCapture(str(path))
        self.drop_after, self.count = drop_after, 0

    def isOpened(self):
        return True

    def read(self):
        time.sleep(1 / 30)
        if self.drop_after is not None and self.count >= self.drop_after:
            return False, None
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        self.count += 1
        return ok, frame

    def get(self, prop):
        return 30.0

    def set(self, *args):
        return True

    def release(self):
        self.cap.release()


class RelayRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_relay_dropout_changes_session_and_discards_tracking(self):
        with TemporaryDirectory() as directory:
            clip = Path(directory) / 'recorded.mp4'
            write_clip(clip)
            caps = [FakeRelayCap(clip, drop_after=40), FakeRelayCap(clip)]
            with patch.object(video_grid.VideoWorker, '_open', lambda self: caps.pop(0) if caps else None):
                capture = _SharedCapture.acquire(RELAY_URL, 'tcp')
                session = TrackingSession(.5)
                try:
                    first, last, tracked = None, None, False
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        frame = capture.latest()
                        if frame and (frame.session_id, frame.frame_seq) != last:
                            last = frame.session_id, frame.frame_seq
                            if first is None:
                                first = frame.session_id
                            if frame.session_id == first and frame.frame_seq == 3:
                                session.select_roi((60 + 2, 80, 66, 70))
                            session.offer(frame)
                            if frame.session_id != first:
                                break
                        tracked = tracked or session.snapshot().state == 'TRACKING'
                        time.sleep(.005)
                    self.assertTrue(tracked, 'tracking never started on the relayed recording')
                    self.assertIsNotNone(last); self.assertNotEqual(last[0], first)   # reconnect → new session id
                    self.assertEqual(capture.reconnects, 1)
                    result = session.snapshot()
                    self.assertEqual(result.state, 'LOST'); self.assertIsNone(result.bbox)
                    self.assertEqual(result.reason, 'source_session_or_resolution_changed')
                    time.sleep(.3)
                    for _ in range(5):                    # frames keep coming: no automatic resume
                        frame = capture.latest()
                        session.offer(frame); time.sleep(.04)
                    self.assertNotEqual(session.snapshot().state, 'TRACKING')
                finally:
                    capture.release(); session.close(); capture.join(3)
                    self.assertFalse(capture.is_alive())

    def test_relay_down_message(self):
        from tracking_service import TrackingService
        with patch.object(video_grid.VideoWorker, '_open', lambda self: None):
            service = TrackingService()
            self.addCleanup(service.shutdown)
            service.connect_source(RELAY_URL, 'tcp')
            self.assertTrue(eventually(lambda: service.video_state()['status'].startswith('로컬 중계 서버 응답 없음'),
                                       app=self.app))
            video = service.video_state()
            self.assertEqual((video['state'], video['route']), ('CONNECTING', '로컬 중계'))

    def test_same_camera_warning_in_service(self):
        from tracking_service import TrackingService
        service = TrackingService()
        self.addCleanup(service.shutdown)
        fake = SimpleNamespace(users=1)
        keys = ['rtsp://192.168.144.25:8554/main.264', 'rtsp://192.168.144.25:8554/video1']
        try:
            for key in keys:
                _capture_pool[key] = fake
            service.tick()
            text = '같은 카메라에 직접 연결 2개 · 무선 링크 부하 증가 · 로컬 중계 사용 권장'
            self.assertIn(text, service.snapshot()['video']['warnings'])
            self.assertTrue(any(e['text'] == text for e in service.events))
        finally:
            for key in keys:
                _capture_pool.pop(key, None)
        service.tick()
        self.assertEqual(service.snapshot()['video']['warnings'], [])


class EncodeOnceTests(unittest.TestCase):
    """JPEG work does not grow with the number of browser tabs and stops without readers."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def readers(self, port, token, count, seconds):
        stop = time.monotonic() + seconds

        def loop():
            while time.monotonic() < stop:
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                conn.request('GET', '/frame.jpg', headers={'Host': f'127.0.0.1:{port}', 'X-Dashboard-Token': token})
                response = conn.getresponse(); response.read(); conn.close()
                time.sleep(.02)
        threads = [threading.Thread(target=loop) for _ in range(count)]
        for thread in threads:
            thread.start()
        return threads

    def pump(self, seconds, step=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            if step:
                step()
            time.sleep(.01)

    def test_tracking_dashboard(self):
        from tracking_service import TrackingService
        service = TrackingService()
        self.addCleanup(service.shutdown)
        service.open_dashboard(('127.0.0.1', 0))
        service.start_demo()
        server = service.dashboard
        counts = {}
        for tabs in (1, 4):
            before = server.frames_published
            threads = self.readers(server.port, server.token, tabs, 1.5)
            self.pump(1.5)
            for thread in threads:
                thread.join()
            counts[tabs] = server.frames_published - before
        self.assertGreater(counts[1], 5)
        self.assertLessEqual(counts[4], 1.5 / .1 + 3)              # ≤ 10 Hz regardless of tabs
        self.assertLessEqual(counts[4], counts[1] + 3)
        self.pump(3.3)                                             # FRAME_INTEREST_S passes
        idle = server.frames_published
        self.pump(1.0)
        self.assertEqual(server.frames_published, idle)            # nobody reads → no encoding

    def test_legacy_drive_dashboard(self):
        from drive_dashboard import Dashboard
        calls = []
        bridge = SimpleNamespace(snapshot=lambda: {}, frame=lambda: calls.append(1) or b'\xff\xd8x', frame_at=None,
                                 execute=lambda c: None)
        dashboard = Dashboard(('127.0.0.1', 0))
        self.addCleanup(dashboard.close)
        dashboard.bridge = bridge
        tick = lambda: dashboard.update_desktop()
        self.pump(.5, tick)
        self.assertEqual(calls, [])                                # no reader → no JPEG
        counts = {}
        for tabs in (1, 4):
            before = len(calls)
            last = [0.]

            def step():
                if time.monotonic() - last[0] >= .15:              # the panel's 150 ms timer
                    last[0] = time.monotonic(); tick()
            threads = self.readers(dashboard.server.port, dashboard.server.token, tabs, 1.2)
            self.pump(1.2, step)
            for thread in threads:
                thread.join()
            counts[tabs] = len(calls) - before
        self.assertLessEqual(counts[4], counts[1] + 2)


class LinkMetricsToolTest(unittest.TestCase):
    def test_file_source_csv(self):
        import csv, json, sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        import video_link_metrics as tool
        with TemporaryDirectory() as directory:
            clip = Path(directory) / 'clip.mp4'
            write_clip(clip, frames=60)
            out = Path(directory) / 'link.csv'
            rows, summary = tool.run(str(clip), seconds=2.5, out=out, label='file')
            with out.open() as stream:
                header = next(csv.reader(stream))
            self.assertEqual(header, tool.FIELDS)
            self.assertGreaterEqual(len(rows), 2)
            self.assertGreater(summary['frames'], 30)
            self.assertIn('glass-to-glass', json.loads(out.with_suffix('.summary.json').read_text())['note'])


if __name__ == '__main__':
    unittest.main()
