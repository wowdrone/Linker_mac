import time
import threading
import unittest
from unittest.mock import patch, MagicMock
import numpy as np
from tracking_models import FrameEnvelope
from tracking_session import TrackingSession, SimulationSink
from tracking_core import RoiTracker

def wait_for(predicate, timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('timeout')

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.s=TrackingSession(.2)
        self.addCleanup(self.cleanup)
        self.pixels=np.random.default_rng(42).integers(0,256,(120,180,3),np.uint8)
    def cleanup(self):
        self.s.close(); self.s.worker.join(2); self.s.watchdog.join(2)
        self.assertFalse(self.s.worker.is_alive()); self.assertFalse(self.s.watchdog.is_alive())
    def frame(self, seq=1, session='camera1', source='test', pixels=None):
        return FrameEnvelope.create(source,session,seq,self.pixels if pixels is None else pixels,time.monotonic())
    def start(self):
        self.s.offer(self.frame()); self.s.select((45,35,60,60))
        wait_for(lambda:self.s.snapshot().state=='TRACKING')
    def test_immutable_owned_buffer(self):
        f=self.frame(); self.pixels[:]=0
        self.assertGreater(f.pixels.sum(),0)
        with self.assertRaises(ValueError): f.pixels.setflags(write=True)
    def test_timeout_without_frames_latches_and_revokes(self):
        self.start(); self.assertTrue(self.s.simulate())
        self.s.offer(self.frame(2));wait_for(lambda:self.s.sink.current is not None)
        wait_for(lambda:self.s.snapshot().state=='LOST')
        self.assertIsNone(self.s.sink.current);self.assertFalse(self.s.sink.enabled)
        self.s.offer(self.frame(3));time.sleep(.03)
        self.assertEqual(self.s.snapshot().state,'LOST');self.assertIsNone(self.s.snapshot().bbox)
    def test_changes_invalidate(self):
        for kw in ({'session':'reconnect'},{'source':'other'},{'pixels':self.pixels[:80]}):
            self.start();self.s.offer(self.frame(2,**kw))
            self.assertEqual(self.s.snapshot().state,'LOST');self.assertIsNone(self.s.snapshot().bbox)
    def test_late_result_discarded_after_stop(self):
        entered=threading.Event(); release=threading.Event(); original=RoiTracker.select
        def delayed(core,*args):
            entered.set();release.wait(1);return original(core,*args)
        with patch.object(RoiTracker,'select',delayed):
            self.s.offer(self.frame());self.s.select((45,35,60,60));self.assertTrue(entered.wait(1))
            self.s.stop();generation=self.s.snapshot().session_id;release.set();time.sleep(.04)
            self.assertEqual(self.s.snapshot().session_id,generation);self.assertIsNone(self.s.snapshot().bbox)
    def test_watchdog_during_blocked_worker(self):
        entered=threading.Event();release=threading.Event();original=RoiTracker.select
        def delayed(core,*args):
            entered.set();release.wait(1);return original(core,*args)
        with patch.object(RoiTracker,'select',delayed):
            self.s.offer(self.frame());self.s.select((45,35,60,60));entered.wait(1)
            wait_for(lambda:self.s.snapshot().state=='LOST');release.set()
    def test_exception_error(self):
        with patch.object(RoiTracker,'select',side_effect=RuntimeError('failure')):
            self.s.offer(self.frame());self.s.select((45,35,60,60))
            wait_for(lambda:self.s.snapshot().state=='ERROR')
    def test_close_and_vehicle_change(self):
        self.start();self.s.simulate();self.s.stop('vehicle_changed')
        self.assertFalse(self.s.sink.enabled)
        self.s.close();self.s.offer(self.frame(2));self.s.select((1,1,20,20))
        self.assertEqual(self.s.snapshot().state,'STOPPED')
    def test_configuration_and_sink_reject_vehicle(self):
        for value in (float('nan'),float('inf'),0,-1,11,True):
            with self.assertRaises(ValueError): TrackingSession(value)
        with self.assertRaises(TypeError): SimulationSink(MagicMock())

    def test_duplicate_and_regression(self):
        self.start();self.s.offer(self.frame(1))
        self.assertEqual(self.s.snapshot().reason,'out_of_order_frame')
        self.start()
        f=FrameEnvelope.create('test','camera1',2,self.pixels,time.monotonic()-2)
        self.s.offer(f)
        self.assertEqual(self.s.snapshot().reason,'out_of_order_frame')

    def test_source_clear_prevents_old_roi(self):
        self.start();self.s.clear_source();self.s.select((45,35,60,60))
        self.assertEqual(self.s.snapshot().reason,'no_frame')
