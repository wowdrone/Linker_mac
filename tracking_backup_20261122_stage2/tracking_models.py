"""Internal observation records. No capture timestamp is invented."""
from dataclasses import dataclass
import hashlib
import math
import numpy as np

@dataclass(frozen=True)
class FrameEnvelope:
    source_id: str
    session_id: str
    frame_seq: int
    width: int
    height: int
    decoder_received_monotonic: float
    pixels: np.ndarray

    def __post_init__(self):
        p = self.pixels
        if (not isinstance(p, np.ndarray) or p.dtype != np.uint8 or p.ndim not in (2,3)
                or not p.size or (p.ndim == 3 and p.shape[2] != 3)
                or (self.height, self.width) != p.shape[:2]
                or type(self.frame_seq) is not int or self.frame_seq < 0
                or not isinstance(self.source_id, str) or not self.source_id
                or not isinstance(self.session_id, str) or not self.session_id
                or not math.isfinite(self.decoder_received_monotonic)):
            raise ValueError('invalid frame envelope metadata')
        # A directly constructed record also acquires immutable ownership.
        base = p
        while isinstance(base, np.ndarray):
            base = base.base
        if not isinstance(base, bytes):
            object.__setattr__(self, 'pixels', np.frombuffer(p.tobytes(), np.uint8).reshape(p.shape))

    @classmethod
    def create(cls, source, session, seq, pixels, received):
        if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8
                or pixels.ndim not in (2, 3) or not pixels.size
                or (pixels.ndim == 3 and pixels.shape[2] != 3)
                or type(seq) is not int or seq < 0 or not math.isfinite(received)):
            raise ValueError('invalid frame envelope')
        # Immutable bytes own the buffer: consumers cannot turn WRITEABLE back on.
        frozen = np.frombuffer(pixels.tobytes(), np.uint8).reshape(pixels.shape)
        h, w = frozen.shape[:2]
        return cls(hashlib.sha256(source.encode()).hexdigest()[:12], session, seq, w, h, received, frozen)

@dataclass(frozen=True)
class TrackingResult:
    state: str = 'IDLE'
    reason: str = 'awaiting_roi'
    session_id: str = ''
    source_id: str = ''
    frame_session_id: str = ''
    frame_seq: int | None = None
    bbox: tuple | None = None
    center: tuple | None = None
    normalized_image_error: tuple | None = None
    trail: tuple = ()
    quality: float = 0.
    received_at: float | None = None
    processed_at: float | None = None
    processing_fps: float = 0.
    object_class: str = 'UNKNOWN'
