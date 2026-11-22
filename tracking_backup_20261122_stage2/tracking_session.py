"""Cancellation authority and independent watchdog; only memory simulation output."""
from collections import deque
from dataclasses import asdict, replace
import json
import math
import threading
import time
import uuid
from tracking_models import TrackingResult
from tracking_worker import TrackingWorker

class SimulationSink:
    def __init__(self):
        self.rows = deque(maxlen=2000)
        self.current = None
        self.enabled = False

    def revoke(self, reason, session):
        self.enabled = False
        self.current = None
        self.rows.append(dict(session=session, expiry=time.monotonic(), stop_reason=reason))

    def record(self, result, expiry):
        if type(result) is not TrackingResult or type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise TypeError('simulation accepts only TrackingResult and finite expiry')
        if self.enabled:
            self.current = dict(session=result.session_id, expiry=expiry, stop_reason=None,
                                image_error=result.normalized_image_error)
            self.rows.append(self.current.copy())

class TrackingSession:
    def __init__(self, timeout=.3):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 10:
            raise ValueError('timeout must be 0.05..10 seconds')
        self.timeout = timeout
        self.lock = threading.RLock()
        self.result = TrackingResult(session_id=uuid.uuid4().hex)
        self.frame = None
        self.identity = None
        self.submitted = None
        self.deadline = None
        self.closed = threading.Event()
        self.sink = SimulationSink()
        self.events = deque(maxlen=2000)
        self.worker = TrackingWorker(timeout, self._deliver)
        self.watchdog = threading.Thread(target=self._watch, name='Tracking watchdog', daemon=True)
        self.watchdog.start()

    def _invalidate(self, state, reason):
        session = uuid.uuid4().hex
        self.result = replace(self.result, state=state, reason=reason, session_id=session,
                              bbox=None, center=None, normalized_image_error=None, trail=(), quality=0.)
        self.deadline = None
        self.sink.revoke(reason, session)
        self.events.append(asdict(self.result))

    def stop(self, reason='operator_stop'):
        with self.lock:
            self._invalidate('STOPPED', reason)

    def clear_source(self):
        with self.lock:
            self._invalidate("STOPPED", "source_disconnected")
            self.frame = self.identity = self.submitted = None

    def selecting(self):
        with self.lock:
            if not self.closed.is_set():
                self._invalidate('SELECTING', 'draw_roi')

    def offer(self, frame):
        with self.lock:
            if self.closed.is_set():
                return
            identity = frame.source_id, frame.session_id, frame.width, frame.height
            if self.identity is not None and self.identity != identity:
                self._invalidate('LOST', 'source_session_or_resolution_changed')
            if (self.frame is not None and self.identity == identity and self.result.state == 'TRACKING'
                    and (frame.frame_seq <= self.frame.frame_seq
                         or frame.decoder_received_monotonic <= self.frame.decoder_received_monotonic)):
                self._invalidate('LOST', 'out_of_order_frame')
            self.identity, self.frame = identity, frame
            if self.result.state == 'TRACKING':
                key = frame.session_id, frame.frame_seq
                if key != self.submitted:
                    self.submitted = key
                    self.worker.submit(self.result.session_id, frame)

    def select(self, roi):
        with self.lock:
            if self.closed.is_set():
                return
            self._invalidate('SELECTING', 'roi_pending')
            if self.frame is None:
                self._invalidate('LOST', 'no_frame')
                return
            now = time.monotonic()
            self.deadline = min(now + self.timeout, self.frame.decoder_received_monotonic + self.timeout)
            self.submitted = self.frame.session_id, self.frame.frame_seq
            self.worker.submit(self.result.session_id, self.frame, roi)

    def _deliver(self, generation, frame, obs, started, finished, error):
        with self.lock:
            if self.closed.is_set() or generation != self.result.session_id:
                return
            if error:
                self._invalidate('ERROR', 'worker_exception:' + error)
                return
            if finished - frame.decoder_received_monotonic >= self.timeout:
                self._invalidate('LOST', 'processing_timeout')
                return
            if obs.state != 'TRACKING':
                self._invalidate('ERROR' if obs.reason == 'processing_error' else 'LOST', obs.reason)
                return
            x,y,w,h = obs.bbox
            center = x+w/2, y+h/2
            trail = (self.result.trail + (center,))[-120:]
            self.result = TrackingResult('TRACKING', obs.reason, generation, frame.source_id,
                frame.session_id, frame.frame_seq, obs.bbox, center,
                (2*center[0]/frame.width-1, 2*center[1]/frame.height-1), trail, obs.quality,
                frame.decoder_received_monotonic, finished, 1/max(finished-started, 1e-6))
            self.deadline = frame.decoder_received_monotonic + self.timeout
            self.sink.record(self.result, self.deadline)
            self.events.append(asdict(self.result))

    def _watch(self):
        while not self.closed.wait(.02):
            with self.lock:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self._invalidate('LOST', 'frame_or_worker_timeout')

    def snapshot(self):
        with self.lock:
            return self.result

    def simulate(self):
        with self.lock:
            if self.result.state == 'TRACKING':
                self.sink.enabled = True
                return True
            return False

    def revoke_simulation(self, reason):
        with self.lock:
            self.sink.revoke(reason, self.result.session_id)

    def save(self, path):
        with self.lock:
            data = dict(events=list(self.events), simulation=list(self.sink.rows))
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)

    def close(self):
        with self.lock:
            self._invalidate('STOPPED', 'app_shutdown')
            self.frame = None
            self.closed.set()
            self.worker.close()
