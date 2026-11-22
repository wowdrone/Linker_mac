"""Simulated (memory / file) output of tracking image error. Never a flight command.

The sink has no vehicle, link or encoder reference and cannot be given one: the
constructor is keyword-only and takes no transport. Output authority is bound to one
tracking session id and an expiry; a late result of another session, or one past its
expiry, cannot re-enable output.
"""
from collections import deque
import json
import math
import time


class SimulationSink:
    def __init__(self, *, maxlen=2000, clock=time.monotonic):
        self.rows = deque(maxlen=maxlen)
        self.clock = clock
        self.current = None
        self.enabled = False
        self.session = None

    def enable(self, session):
        if not isinstance(session, str) or not session:
            raise TypeError('simulation authority needs a tracking session id')
        self.enabled, self.session, self.current = True, session, None
        self.rows.append(dict(session=session, t=self.clock(), event='enabled'))

    def revoke(self, reason, session):
        was = self.enabled or self.current is not None
        self.enabled, self.session, self.current = False, None, None
        if was:
            self.rows.append(dict(session=session, t=self.clock(), stop_reason=reason))

    def record(self, result, expiry):
        from tracking_models import TrackingResult
        if type(result) is not TrackingResult or type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise TypeError('simulation accepts only TrackingResult and finite expiry')
        if (not self.enabled or result.session_id != self.session or result.state != 'TRACKING'
                or expiry <= self.clock()):
            return False
        self.current = dict(session=result.session_id, frame_seq=result.frame_seq, expiry=expiry,
                            stop_reason=None, image_error=result.normalized_image_error)
        self.rows.append(self.current.copy())
        return True

    def output(self):
        """Current simulated output, or None once it has expired or been revoked."""
        current = self.current
        if current is None or self.clock() >= current['expiry']:
            return None
        return current

    def export(self, path):
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(list(self.rows), stream, ensure_ascii=False, indent=1, allow_nan=False)
