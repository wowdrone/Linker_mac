import unittest
from unittest.mock import patch
import cv2
import numpy as np
from tracking_core import RoiTracker


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((120, 180), dtype=np.uint8)
        self.frame[35:75, 45:85] = np.random.default_rng(42).integers(0, 256, (40, 40), dtype=np.uint8)
        self.tracker = RoiTracker()
        self.assertEqual(self.tracker.select(self.frame, (45, 35, 40, 40), 1, 1.0, 1.0).state, "TRACKING")

    def test_known_translation(self):
        moved = cv2.warpAffine(self.frame, np.float32([[1, 0, 4], [0, 1, 3]]), (180, 120))
        result = self.tracker.update(moved, 2, 1.05, 1.06)
        self.assertEqual(result.state, "TRACKING")
        self.assertAlmostEqual(result.bbox[0], 49, delta=.5)
        self.assertAlmostEqual(result.bbox[1], 38, delta=.5)

    def test_timeout_without_new_frame_and_no_auto_resume(self):
        self.assertEqual(self.tracker.tick(1.31).reason, "frame_timeout")
        self.assertIsNone(self.tracker.snapshot().bbox)
        self.assertEqual(self.tracker.update(self.frame, 2, 1.32, 1.32).state, "LOST")
        self.assertEqual(self.tracker.select(self.frame, (45, 35, 40, 40), 3, 1.4, 1.4).state, "TRACKING")

    def test_invalid_roi(self):
        for roi in ((-1, 0, 40, 40), (1, 1, 0, 8), (170, 100, 20, 20), (float('nan'), 1, 40, 40)):
            with self.subTest(roi=roi):
                self.assertEqual(self.tracker.select(self.frame, roi, 2, 1.1, 1.1).state, "LOST")

    def test_no_texture(self):
        result = self.tracker.select(np.zeros_like(self.frame), (45, 35, 40, 40), 2, 1.1, 1.1)
        self.assertEqual(result.reason, "insufficient_texture")

    def test_occlusion(self):
        self.assertEqual(self.tracker.update(np.zeros_like(self.frame), 2, 1.05, 1.05).state, "LOST")

    def test_resolution_changed(self):
        self.assertEqual(self.tracker.update(self.frame[:100], 2, 1.05, 1.05).reason, "resolution_changed")

    def test_invalid_time(self):
        for stamp in (float('nan'), float('inf'), 2.0, .5):
            with self.subTest(stamp=stamp):
                self.setUp()
                self.assertEqual(self.tracker.update(self.frame, 2, stamp, 1.1).state, "LOST")

    def test_duplicate_or_reordered(self):
        self.assertEqual(self.tracker.update(self.frame, 1, 1.05, 1.05).reason, "out_of_order_frame")

    def test_clock_regression(self):
        self.assertEqual(self.tracker.tick(.9).reason, "invalid_clock")

    def test_processing_failure(self):
        with patch('tracking_core.cv2.calcOpticalFlowPyrLK', side_effect=cv2.error('failed')):
            self.assertEqual(self.tracker.update(self.frame, 2, 1.05, 1.05).reason, "processing_error")

    def test_operator_stop(self):
        self.tracker.stop()
        result = self.tracker.update(self.frame, 2, 1.05, 1.05)
        self.assertEqual(result.state, "IDLE")
        self.assertIsNone(result.bbox)

    def test_no_transport_dependencies(self):
        import ast
        import tracking_core
        from pathlib import Path
        tree = ast.parse(Path(tracking_core.__file__).read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertEqual(set(imports), {'dataclasses', 'math', 'cv2', 'numpy'})


if __name__ == '__main__':
    unittest.main()

class ValidationTests(unittest.TestCase):
    def test_invalid_arrays_and_roi(self):
        tracker=RoiTracker()
        for frame in (np.zeros((20,20),np.float32),np.zeros((20,20,4),np.uint8),np.zeros((0,0),np.uint8),None):
            self.assertEqual(tracker.select(frame,(1,1,10,10),1,1.,1.).state,'LOST')
        for roi in ((1,1,float('inf'),10),(1,1,2,2),(1,2,3),None):
            self.assertEqual(tracker.select(np.zeros((20,20),np.uint8),roi,1,1.,1.).state,'LOST')

    def test_scale_rotation(self):
        frame=np.zeros((240,320),np.uint8)
        frame[80:160,120:200]=np.random.default_rng(9).integers(0,256,(80,80),np.uint8)
        tracker=RoiTracker();tracker.select(frame,(120,80,80,80),1,1,1)
        matrix=cv2.getRotationMatrix2D((160,120),2,1.02)
        moved=cv2.warpAffine(frame,matrix,(320,240))
        result=tracker.update(moved,2,1.03,1.03)
        self.assertEqual(result.state,'TRACKING')
        self.assertAlmostEqual(result.bbox[2],81.6,delta=1)
