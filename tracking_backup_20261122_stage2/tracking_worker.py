"""One bounded work slot, no Qt events, sockets or vehicle references."""
import threading
import time
from tracking_core import RoiTracker

class TrackingWorker(threading.Thread):
    def __init__(self, timeout, deliver):
        super().__init__(name='Tracking optical flow', daemon=True)
        self.timeout, self.deliver = timeout, deliver
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False
        self.generation = None
        self.core = None
        self.start()

    def submit(self, generation, frame, roi=None):
        with self.condition:
            if self.closed:
                return
            # Never replace a pending explicit selection with an update.
            if self.pending and self.pending[0] == generation and self.pending[2] is not None and roi is None:
                return
            self.pending = generation, frame, roi
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                generation, frame, roi = self.pending
                self.pending = None
            started = time.monotonic()
            try:
                if generation != self.generation:
                    self.core = RoiTracker(self.timeout)
                    self.generation = generation
                args = (frame.frame_seq, frame.decoder_received_monotonic, started)
                obs = (self.core.select(frame.pixels, roi, *args) if roi is not None
                       else self.core.update(frame.pixels, *args))
                error = None
            except Exception as exc:
                obs, error = None, type(exc).__name__
            self.deliver(generation, frame, obs, started, time.monotonic(), error)

    def close(self):
        with self.condition:
            self.closed, self.pending = True, None
            self.condition.notify_all()
        # Python worker owns itself until native LK returns. UI never joins it.
