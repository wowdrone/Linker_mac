"""Decoder timing health (no Qt): sustained low receive rate and bursty read() delivery.

Both patterns mean frames are queuing somewhere (network, demuxer or decoder) and that the
displayed/tracked image may be older than its receipt time suggests. The check only uses
receipt times that the capture thread already records.

- lagging: average receive rate over the last `window_s` seconds is below `ratio` × source
  FPS, and the samples cover at least `window_s` (default 5 s, 80 %).
- bursting: at least `burst_run` consecutive read() intervals shorter than
  `burst_fraction` × nominal frame period (default 8 × 25 %); kept for `hold_s` after the
  last burst so a short burst is still visible.
Thresholds are validated finite values and can be changed in QSettings (see from_settings).
"""
from collections import deque
import math

DEFAULTS = dict(window_s=5.0, ratio=.8, burst_fraction=.25, burst_run=8, hold_s=5.0)
LIMITS = dict(window_s=(1.0, 60.0), ratio=(.1, 1.0), burst_fraction=(.01, .9), burst_run=(2, 120),
              hold_s=(0.0, 60.0))
WARNING = '디코딩 밀림·지연 누적 가능'


def _check(name, value):
    low, high = LIMITS[name]
    if name == 'burst_run':
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} must be an integer in {low}..{high}')
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be finite in {low}..{high}')
    return float(value)


class DecodeHealth:
    def __init__(self, **thresholds):
        unknown = set(thresholds) - set(DEFAULTS)
        if unknown:
            raise ValueError(f'unknown thresholds {sorted(unknown)}')
        values = dict(DEFAULTS, **thresholds)
        for name, value in values.items():
            setattr(self, name, _check(name, value))
        self.reset()

    @classmethod
    def from_settings(cls, settings, prefix='video/decode_health/'):
        """Read optional overrides; invalid stored values fall back to the defaults."""
        values = {}
        for name, default in DEFAULTS.items():
            raw = settings.value(prefix + name, None)
            if raw is None:
                continue
            try:
                value = int(raw) if name == 'burst_run' else float(raw)
                values[name] = _check(name, value)
            except (TypeError, ValueError):
                values[name] = default
        return cls(**values)

    def reset(self):
        self.times = deque(maxlen=2000)
        self.source_fps = None
        self.run = 0
        self.last_burst = None

    def observe(self, received, source_fps):
        if source_fps and math.isfinite(source_fps) and source_fps > 0:
            self.source_fps = float(source_fps)
        if self.times and self.source_fps:
            interval = received - self.times[-1]
            if 0 <= interval < self.burst_fraction / self.source_fps:
                self.run += 1
                if self.run >= self.burst_run:
                    self.last_burst = received
            else:
                self.run = 0
        self.times.append(received)
        cutoff = received - 2 * self.window_s
        while self.times and self.times[0] < cutoff:
            self.times.popleft()

    def snapshot(self, now):
        actual = None
        lagging = False
        if self.times:
            start = now - self.window_s
            recent = [t for t in self.times if t >= start]
            covered = self.times[0] <= start
            if covered:
                actual = len(recent) / self.window_s
                lagging = bool(self.source_fps) and actual < self.ratio * self.source_fps and len(recent) > 0
        bursting = self.last_burst is not None and now - self.last_burst <= self.hold_s
        return dict(actual_fps=None if actual is None else round(actual, 1),
                    source_fps=self.source_fps, lagging=lagging, bursting=bursting,
                    warning=WARNING if (lagging or bursting) else None)
