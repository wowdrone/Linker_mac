"""Model-free ROI observation tracker. No vehicle or network access.

Single-owner API: call tick() from an independent watchdog even without frames.
Timestamps must be decoder receipt times from the same monotonic clock as now.
Partial affine motion handles modest rotation/scale; no range or ego-motion inference.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np


@dataclass(frozen=True)
class Observation:
    state: str
    reason: str
    bbox: tuple | None
    quality: float  # surviving feature fraction, NOT classification probability
    frame_seq: int | None
    received_at: float | None


class RoiTracker:
    def __init__(self, max_age_s=0.3):
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("max_age_s must be positive and finite")
        self.max_age_s = max_age_s
        self.stop()

    def stop(self):
        self.state, self.reason = "IDLE", "operator_stop"
        self.bbox = self.points = self.gray = None
        self.seq = self.received_at = self.last_now = None
        self.quality = 0.0
        return self.snapshot()

    def snapshot(self):
        return Observation(self.state, self.reason, self.bbox, self.quality,
                           self.seq, self.received_at)

    def _lost(self, reason):
        self.state, self.reason = "LOST", reason
        self.bbox = self.points = self.gray = None
        self.quality = 0.0
        return self.snapshot()

    def tick(self, now):
        if not math.isfinite(now) or (self.last_now is not None and now < self.last_now):
            return self._lost("invalid_clock")
        self.last_now = now
        if self.state == "TRACKING" and now - self.received_at >= self.max_age_s:
            return self._lost("frame_timeout")
        return self.snapshot()

    @staticmethod
    def _gray(frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.size == 0:
            raise ValueError("expected uint8 frame")
        if frame.ndim == 2:
            return frame.copy()
        if frame.ndim == 3 and frame.shape[2] == 3:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raise ValueError("expected gray or BGR frame")

    def _limit(self, max_age):
        if max_age is None:
            return self.max_age_s
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 0 < max_age <= 30:
            raise ValueError("max_age must be in (0, 30] seconds")
        return max_age

    def _fresh(self, seq, received_at, now, max_age=None):
        return (type(seq) is int and seq >= 0 and math.isfinite(now)
                and math.isfinite(received_at) and 0 <= now - received_at < self._limit(max_age))

    def select(self, frame, bbox, seq, received_at, now, max_age=None):
        """Explicit user selection is the ONLY way to leave LOST.

        max_age widens the accepted frame age for a frame the operator froze while drawing
        the ROI (bounded by the session's selection window). Later updates use max_age_s.
        """
        self.stop()
        if not self._fresh(seq, received_at, now, max_age):
            return self._lost("invalid_frame_time")
        try:
            gray = self._gray(frame)
            x, y, w, h = (float(v) for v in bbox)
            height, width = gray.shape
            if not (all(math.isfinite(v) for v in (x, y, w, h))
                    and x >= 0 and y >= 0 and w >= 8 and h >= 8
                    and x + w <= width and y + h <= height):
                raise ValueError("invalid ROI")
            mask = np.zeros_like(gray)
            mask[int(y):int(y+h), int(x):int(x+w)] = 255
            points = cv2.goodFeaturesToTrack(gray, 100, .02, 4, mask=mask)
            if points is None or len(points) < 6:
                return self._lost("insufficient_texture")
        except (ValueError, TypeError, cv2.error):
            return self._lost("invalid_roi_or_frame")
        self.initial_points = len(points)
        self.gray, self.points, self.bbox = gray, points, (x, y, w, h)
        self.seq, self.received_at, self.last_now = seq, received_at, now
        self.state, self.reason, self.quality = "TRACKING", "roi_selected", 1.0
        return self.snapshot()

    def update(self, frame, seq, received_at, now, max_age=None):
        """max_age is used only to catch up through retained frames after a frozen selection."""
        limit = self._limit(max_age)
        if not math.isfinite(now) or (self.last_now is not None and now < self.last_now):
            return self._lost("invalid_clock")
        self.last_now = now
        if self.state == "TRACKING" and now - self.received_at >= limit:
            return self._lost("frame_timeout")
        if self.state != "TRACKING":
            return self.snapshot()
        if not self._fresh(seq, received_at, now, max_age):
            return self._lost("invalid_frame_time")
        if seq <= self.seq or received_at <= self.received_at:
            return self._lost("out_of_order_frame")
        try:
            gray = self._gray(frame)
            if gray.shape != self.gray.shape:
                return self._lost("resolution_changed")
            forward, status, err = cv2.calcOpticalFlowPyrLK(self.gray, gray, self.points, None)
            if forward is None or status is None or err is None:
                return self._lost("flow_failed")
            backward, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray, forward, None)
            if backward is None or back_status is None:
                return self._lost("flow_failed")
            old, new, back = (v.reshape(-1, 2) for v in (self.points, forward, backward))
            height, width = gray.shape
            good = ((status.ravel() == 1) & (back_status.ravel() == 1)
                    & np.isfinite(new).all(axis=1) & np.isfinite(back).all(axis=1)
                    & (np.linalg.norm(old - back, axis=1) < 1.5)
                    & (err.ravel() < 45) & (new[:, 0] >= 0) & (new[:, 0] < width)
                    & (new[:, 1] >= 0) & (new[:, 1] < height))
            if good.sum() < 6 or good.mean() < .5:
                return self._lost("low_quality")
            shift = np.median(new[good] - old[good], axis=0)
            coherent = good & (np.linalg.norm((new - old) - shift, axis=1) < 3)
            if coherent.sum() < 6 or coherent.mean() < .5:
                return self._lost("incoherent_motion")
            dx, dy = np.median(new[coherent] - old[coherent], axis=0)
            x, y, w, h = self.bbox
            matrix, inliers = cv2.estimateAffinePartial2D(old[coherent], new[coherent],
                method=cv2.RANSAC, ransacReprojThreshold=2)
            if matrix is None or not np.isfinite(matrix).all() or inliers.mean() < .6:
                return self._lost("incoherent_affine")
            scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
            if not .85 <= scale <= 1.18:
                return self._lost("scale_jump")
            center = matrix @ np.array([x+w/2, y+h/2, 1.])
            w, h = w*scale, h*scale
            x, y = float(center[0]-w/2), float(center[1]-h/2)
            if x < 0 or y < 0 or x+w > width or y+h > height:
                return self._lost("out_of_frame")
            self.quality = float(coherent.sum() / self.initial_points)
            if self.quality < .4:
                return self._lost("feature_attrition")
            self.bbox = (x, y, w, h)
            self.points = new[coherent].reshape(-1, 1, 2)
            self.gray, self.seq, self.received_at = gray, seq, received_at
            self.reason = "flow_updated"
        except (ValueError, TypeError, cv2.error):
            return self._lost("processing_error")
        return self.snapshot()
