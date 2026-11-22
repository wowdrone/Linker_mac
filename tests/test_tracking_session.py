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


class SelectionPolicyTests(unittest.TestCase):
    """Frozen-frame ROI: configurable window, bounded catch-up, redraw on the latest frame."""
    def setUp(self):
        import tracking_session as ts
        self.ts = ts
        self.s = TrackingSession(.3)
        self.addCleanup(self.s.close)
        rng = np.random.default_rng(7)
        self.texture = rng.integers(0, 256, (60, 60, 3), np.uint8)

    def frame(self, seq, received, x=50, noise=False):
        pixels = np.zeros((160, 240, 3), np.uint8)
        if noise:
            pixels = np.random.default_rng(seq).integers(0, 256, pixels.shape, np.uint8)
        else:
            pixels[50:110, x:x+60] = self.texture
        return FrameEnvelope.create('cam', 'sess', seq, pixels, received)

    def test_window_default_range_and_validation(self):
        self.assertEqual(self.s.selection_window, 8.0)
        for bad in (1.9, 20.1, float('nan'), float('inf'), True, '8'):
            with self.assertRaises(ValueError):
                TrackingSession(.3, bad)
            with self.assertRaises(ValueError):
                self.s.set_selection_window(bad)
        for good in (2, 20):
            TrackingSession(.3, good).close()
        self.s.set_selection_window(12)
        self.assertEqual(self.s.selection_window, 12.0)

    def test_countdown_and_catchup_within_span(self):
        now = time.monotonic()
        self.s.offer(self.frame(1, now - 1.0))
        ref = self.s.begin_selection()
        info = self.s.selection_info()
        self.assertAlmostEqual(info['remaining_s'], 7.0, delta=.2)
        self.assertGreater(info['catchup_remaining_s'], 2.5)
        for seq, x in ((2, 52), (3, 54), (4, 56)):
            self.s.offer(self.frame(seq, now - 1.0 + seq * .05, x))
        self.s.offer(self.frame(5, time.monotonic(), 58))
        ok, reason = self.s.select_roi((50, 50, 60, 60), ref)
        self.assertTrue(ok, reason)
        wait_for(lambda: self.s.snapshot().state == 'TRACKING')
        r = self.s.snapshot()
        self.assertEqual(r.frame_seq, 5)                     # caught up to the newest retained frame
        self.assertAlmostEqual(r.bbox[0], 58, delta=1.5)

    def test_long_elapsed_asks_redraw_on_latest_frame(self):
        now = time.monotonic()
        self.s.offer(self.frame(1, now - self.ts.CATCHUP_SPAN_S - .5))
        ref = self.s.begin_selection()
        latest = self.frame(2, time.monotonic(), 80)
        self.s.offer(latest)
        ok, reason = self.s.select_roi((50, 50, 60, 60), ref)
        self.assertEqual((ok, reason), (False, 'selection_stale_redraw_latest'))
        r = self.s.snapshot()
        self.assertEqual(r.state, 'SELECTING'); self.assertIsNone(r.bbox)
        self.assertIs(self.s.display_frame(), latest)       # the newest frame is frozen for redrawing

    def test_expired_window_and_mismatched_reference(self):
        now = time.monotonic()
        self.s.offer(self.frame(1, now - 9))
        ref = self.s.begin_selection()
        self.assertEqual(self.s.select_roi((50, 50, 60, 60), ref), (False, 'selection_frame_expired_reselect'))
        self.s.offer(self.frame(2, time.monotonic()))
        ref = self.s.begin_selection()
        wrong = ref[:2] + (ref[2] + 1,) + ref[3:]
        self.assertEqual(self.s.select_roi((50, 50, 60, 60), wrong), (False, 'roi_frame_mismatch_reselect'))
        self.assertEqual(self.s.snapshot().state, 'SELECTING')

    def test_catchup_failure_reselects_without_switching_object(self):
        now = time.monotonic()
        self.s.offer(self.frame(1, now - .5))
        ref = self.s.begin_selection()
        self.s.offer(self.frame(2, now - .4, noise=True))   # scene changed completely
        latest = self.frame(3, time.monotonic(), noise=True)
        self.s.offer(latest)
        self.assertTrue(self.s.select_roi((50, 50, 60, 60), ref)[0])
        wait_for(lambda: self.s.snapshot().reason == 'catchup_failed_redraw_latest')
        r = self.s.snapshot()
        self.assertEqual(r.state, 'SELECTING'); self.assertIsNone(r.bbox)
        self.assertIs(self.s.display_frame(), latest)

    def test_catchup_memory_is_bounded(self):
        now = time.monotonic()
        self.s.offer(self.frame(1, now - 1))
        self.s.begin_selection()
        for seq in range(2, 80):
            self.s.offer(self.frame(seq, now - 1 + seq * .01))
            self.assertLessEqual(len(self.s.catchup), self.ts.CATCHUP_MAX)
        self.assertEqual(self.s.catchup[-1].frame_seq, 79)
