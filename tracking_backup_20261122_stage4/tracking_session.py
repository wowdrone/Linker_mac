"""Cancellation authority and independent watchdog; only memory/file simulation output.

One TrackingSession is shared by the PyQt Tracking view and the browser dashboard. All
mutating calls happen on the owner (Qt) thread; the worker and watchdog only report back
through _deliver / _watch, which validate the generation id under the lock.

ROI selection is bound to a frozen frame: begin_selection() pins the displayed frame,
both views draw on that pinned frame, and select_roi() must name it exactly
(source/session/frame_seq/width/height). The tracker is initialised on that frame and
catches up through a bounded set of frames retained since the pin.
"""
from collections import deque
from dataclasses import asdict, replace
import json
import math
import threading
import time
import uuid
from tracking_models import TrackingResult
from tracking_simulation import SimulationSink  # noqa: F401  (re-exported for callers/tests)
from tracking_worker import TrackingWorker

SELECTION_WINDOW_S = 8.0     # default time the operator may take to draw on the frozen frame
SELECTION_WINDOW_RANGE = (2.0, 20.0)
# Catch-up policy: the tracker is initialised on the frozen frame and advanced through the
# retained frames only if the ROI arrives within CATCHUP_SPAN_S of that frame. The retained
# set is bounded (CATCHUP_MAX, thinned when full), so the step between retained frames grows
# with the elapsed time and optical flow becomes unreliable. Beyond the span (but inside the
# window), or when catch-up loses the features, the ROI is NOT applied and NOT moved to another
# object: the newest frame is frozen instead and the operator is asked to draw again there.
CATCHUP_SPAN_S = 4.0
CATCHUP_MAX = 12


def frame_ref(frame):
    return None if frame is None else (frame.source_id, frame.session_id, frame.frame_seq, frame.width, frame.height)


class TrackingSession:
    def __init__(self, timeout=.3, selection_window=SELECTION_WINDOW_S):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 10:
            raise ValueError('timeout must be 0.05..10 seconds')
        low, high = SELECTION_WINDOW_RANGE
        if type(selection_window) not in (int, float) or not math.isfinite(selection_window) \
                or not low <= selection_window <= high:
            raise ValueError(f'selection window must be {low:g}..{high:g} seconds')
        self.timeout, self.selection_window = timeout, selection_window
        self.lock = threading.RLock()
        self.result = TrackingResult(session_id=uuid.uuid4().hex)
        self.frame = None
        self.pinned = None
        self.catchup = []
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
                              bbox=None, center=None, normalized_image_error=None, trail=(), quality=0.,
                              tracking_since=None)
        self.deadline = None
        if state != 'SELECTING':
            self.pinned, self.catchup = None, []
        self.sink.revoke(reason, session)
        self.events.append(asdict(self.result))

    def stop(self, reason='operator_stop'):
        with self.lock:
            self._invalidate('STOPPED', reason)

    def clear_source(self, reason='source_disconnected'):
        with self.lock:
            self._invalidate('STOPPED', reason)
            self.frame = self.identity = self.submitted = None

    # ------------------------------------------------------------ selection
    def begin_selection(self):
        """Freeze the current frame for drawing. Returns its reference or None."""
        with self.lock:
            if self.closed.is_set():
                return None
            self._invalidate('SELECTING', 'draw_roi' if self.frame is not None else 'no_frame')
            self.pinned, self.catchup = self.frame, []
            return frame_ref(self.pinned)

    selecting = begin_selection  # backwards compatible name

    def display_frame(self):
        """Frame both views must show: the pinned one while selecting, else the latest."""
        with self.lock:
            return self.pinned if self.result.state == 'SELECTING' and self.pinned is not None else self.frame

    def select_roi(self, roi, ref=None):
        """Apply an ROI in ORIGINAL pixel coordinates of the frame named by ref.

        Returns (accepted, reason). On an expired or mismatching frame the request is not
        applied silently: a fresh frame is pinned and the operator must draw again.
        """
        with self.lock:
            if self.closed.is_set():
                return False, 'closed'
            if self.pinned is None or self.result.state != 'SELECTING':
                if ref is not None or self.frame is None:
                    self._invalidate('LOST', 'no_frame' if self.frame is None else 'selection_not_active')
                    return False, self.result.reason
                self.pinned, self.catchup = self.frame, []   # legacy direct call: latest frame
            pinned = self.pinned
            if ref is not None and tuple(ref) != frame_ref(pinned):
                return self._reselect('roi_frame_mismatch_reselect')
            now = time.monotonic()
            elapsed = now - pinned.decoder_received_monotonic
            if elapsed >= self.selection_window:
                return self._reselect('selection_frame_expired_reselect')
            if elapsed > CATCHUP_SPAN_S and self.frame is not pinned:
                return self._reselect('selection_stale_redraw_latest')
            try:
                x, y, w, h = (float(v) for v in roi)
            except (TypeError, ValueError):
                return False, 'invalid_roi'
            if not (all(math.isfinite(v) for v in (x, y, w, h)) and x >= 0 and y >= 0 and w >= 8 and h >= 8
                    and x + w <= pinned.width and y + h <= pinned.height):
                return False, 'invalid_roi'
            catchup = tuple(self.catchup)
            self._invalidate('SELECTING', 'roi_pending')
            self.pinned = None
            self.deadline = now + self.timeout + .05 * (1 + len(catchup))
            last = catchup[-1] if catchup else pinned
            self.submitted = last.session_id, last.frame_seq
            self.worker.submit(self.result.session_id, pinned, (x, y, w, h), catchup,
                               min(self.selection_window, CATCHUP_SPAN_S + self.timeout))
            return True, 'roi_pending'

    def _reselect(self, reason):
        self._invalidate('SELECTING', reason)
        self.pinned, self.catchup = self.frame, []
        return False, reason

    def set_selection_window(self, seconds):
        low, high = SELECTION_WINDOW_RANGE
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not low <= seconds <= high:
            raise ValueError(f'selection window must be {low:g}..{high:g} seconds')
        with self.lock:
            self.selection_window = float(seconds)

    def selection_info(self):
        """Countdown data for both views while a frame is frozen for drawing."""
        with self.lock:
            if self.result.state != 'SELECTING' or self.pinned is None:
                return None
            elapsed = time.monotonic() - self.pinned.decoder_received_monotonic
            return dict(elapsed_s=round(elapsed, 1), window_s=self.selection_window, catchup_s=CATCHUP_SPAN_S,
                        remaining_s=round(max(0., self.selection_window - elapsed), 1),
                        catchup_remaining_s=round(max(0., CATCHUP_SPAN_S - elapsed), 1))

    def select(self, roi):
        """Backwards compatible: apply to the pinned frame (or latest if nothing is pinned)."""
        return self.select_roi(roi)

    # ------------------------------------------------------------ frames
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
            if self.result.state == 'SELECTING' and self.pinned is not None:
                if frame.frame_seq > (self.catchup[-1] if self.catchup else self.pinned).frame_seq:
                    self.catchup.append(frame)
                    if len(self.catchup) > CATCHUP_MAX:
                        # Keep memory bounded: thin older frames, always keep the newest.
                        self.catchup = self.catchup[-2::-2][::-1] + [self.catchup[-1]]
            if self.result.state == 'TRACKING':
                key = frame.session_id, frame.frame_seq
                if key != self.submitted:
                    self.submitted = key
                    self.worker.submit(self.result.session_id, frame)

    def _deliver(self, generation, frame, obs, started, finished, error, catchup_failed=False):
        with self.lock:
            if self.closed.is_set() or generation != self.result.session_id:
                return   # late result of a cancelled/replaced session
            if catchup_failed and not error:
                self._reselect('catchup_failed_redraw_latest')
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
            x, y, w, h = obs.bbox
            center = x + w / 2, y + h / 2
            trail = (self.result.trail + (center,))[-120:]
            self.result = TrackingResult('TRACKING', obs.reason, generation, frame.source_id,
                frame.session_id, frame.frame_seq, obs.bbox, center,
                (2 * center[0] / frame.width - 1, 2 * center[1] / frame.height - 1), trail, obs.quality,
                frame.decoder_received_monotonic, finished, 1 / max(finished - started, 1e-6),
                tracking_since=self.result.tracking_since or finished)
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

    # ------------------------------------------------------------ simulation
    def simulate(self):
        with self.lock:
            if self.result.state == 'TRACKING':
                self.sink.enable(self.result.session_id)
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
