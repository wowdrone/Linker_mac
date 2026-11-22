"""Encoded synthetic video replay, not aircraft SITL or HM30 evidence."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import cv2
import numpy as np
from video_grid import _SharedCapture
from tracking_session import TrackingSession

class ReplayTest(unittest.TestCase):
    def test_encoded_video_tracking_and_loop_session(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.mp4'
            writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),30,(320,240))
            self.assertTrue(writer.isOpened())
            texture=np.random.default_rng(5).integers(0,256,(70,70,3),np.uint8)
            for i in range(24):
                frame=np.zeros((240,320,3),np.uint8);frame[80:150,80+i:150+i]=texture;writer.write(frame)
            writer.release()
            capture=_SharedCapture.acquire(str(path),'tcp');session=TrackingSession(.5)
            try:
                deadline=time.monotonic()+3;last=None;first=None;tracked=[]
                while time.monotonic()<deadline:
                    frame=capture.latest()
                    if frame and (frame.session_id,frame.frame_seq)!=last:
                        last=(frame.session_id,frame.frame_seq)
                        session.offer(frame)
                        if first is None:
                            first=frame.session_id
                            session.select((80,80,70,70))
                        elif frame.session_id!=first:
                            self.assertEqual(session.snapshot().state,'LOST');break
                    result=session.snapshot()
                    if result.bbox:tracked.append(result)
                    time.sleep(.005)
                self.assertGreater(len(tracked),10)
                result=tracked[-1]
                self.assertAlmostEqual(result.bbox[0],80+result.frame_seq-1,delta=2)
                self.assertNotEqual(capture.latest().session_id,first)
            finally:
                capture.release();session.close();session.worker.join(2);capture.join(2)
                self.assertFalse(capture.is_alive())
