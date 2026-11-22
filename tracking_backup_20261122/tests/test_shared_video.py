"""Fly/four-tile exclusive source sharing and recording regression."""
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from PyQt5.QtCore import QCoreApplication, Qt
from video_grid import VideoWorker, _capture_pool


def eventually(predicate, seconds=4):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for video')


class ExclusiveCamera:
    def __init__(self, owner, source):
        self.owner, self.source = owner, source
        self.frame, self.closed = 0, False
    def isOpened(self): return True
    def get(self, prop): return 30
    def read(self):
        time.sleep(.015)
        self.frame += 1
        return True, np.full((48, 64, 3), self.frame % 255, np.uint8)
    def release(self):
        if not self.closed:
            self.closed = True
            with self.owner.lock:
                self.owner.active.remove(self.source)


class SharedVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])
    def setUp(self):
        self.workers, self.frames = [], []
        self.active, self.opens = set(), []
        self.lock = threading.Lock()
        def open_camera(worker):
            with self.lock:
                if worker.source in self.active:
                    raise RuntimeError('Camera refuses second connection')
                self.active.add(worker.source)
                self.opens.append(worker.source)
            return ExclusiveCamera(self, worker.source)
        self.patch = patch.object(VideoWorker, '_open', open_camera)
        self.patch.start()
    def tearDown(self):
        for worker in self.workers:
            worker.stop()
            self.assertFalse(worker.isRunning())
        eventually(lambda: not _capture_pool)
        self.patch.stop()
    def start_view(self, source='rtsp://camera/live', transport='tcp'):
        worker = VideoWorker(source, transport)
        frames = []
        worker.frameReady.connect(lambda image: frames.append(image.pixelColor(0, 0).red()), Qt.DirectConnection)
        self.workers.append(worker)
        self.frames.append(frames)
        worker.start()
        return worker, frames
    def test_fly_and_four_tiles_share_connection_and_survive_stops(self):
        for i in range(5):
            self.start_view(transport='udp' if i else 'tcp')
        eventually(lambda: all(len(frames) >= 4 for frames in self.frames))
        self.assertEqual(self.opens, ['rtsp://camera/live'])
        before = len(self.frames[0])
        for worker in self.workers[1:]: worker.stop()
        eventually(lambda: len(self.frames[0]) > before + 3)
        _, frames = self.start_view()
        eventually(lambda: len(frames) > 3)
        self.assertEqual(len(self.opens), 1)
    def test_stopping_main_leaves_tiles_and_recording_independent(self):
        main, main_frames = self.start_view()
        tile, tile_frames = self.start_view()
        _, other_frames = self.start_view('rtsp://other/live')
        eventually(lambda: min(len(main_frames), len(tile_frames), len(other_frames)) > 3)
        self.assertEqual(len(self.opens), 2)
        with TemporaryDirectory() as folder:
            movie, snapshot = str(Path(folder)/'tile.mp4'), str(Path(folder)/'tile.png')
            tile.set_recording(True, movie)
            tile.request_snapshot(snapshot)
            main.stop()
            before = len(tile_frames)
            eventually(lambda: len(tile_frames) > before + 8 and Path(snapshot).exists())
            tile.stop()
            capture = cv2.VideoCapture(movie)
            try:
                ok, frame = capture.read()
                self.assertTrue(ok)
                self.assertEqual(frame.shape[:2], (48,64))
            finally: capture.release()
            self.assertEqual(cv2.imread(snapshot).shape[:2], (48,64))
    def test_restart_after_last_view_releases_camera(self):
        worker, frames = self.start_view()
        eventually(lambda: len(frames) > 2)
        worker.stop()
        eventually(lambda: not _capture_pool)
        _, frames = self.start_view()
        eventually(lambda: len(frames) > 2)
        self.assertEqual(len(self.opens), 2)

if __name__ == '__main__': unittest.main()
