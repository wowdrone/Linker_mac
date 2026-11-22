"""Measure Qt (UI) thread time per frame for the Tracking window + browser publishing.

Synthetic frames at several resolutions are injected through a fake capture (the decoder
copy is excluded, as it runs on the capture thread). Per frame the Qt thread does:
service.tick (offer, supervision, web state + JPEG ≤10 Hz), panel.refresh (BGR→RGB QImage,
labels) and the canvas paint. No camera, vehicle or network link is opened.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/measure_tracking_ui.py [--frames 120]
"""
import argparse
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from tracking_models import FrameEnvelope  # noqa: E402
from tracking_panel import TrackingPanel  # noqa: E402

RESOLUTIONS = {'720p': (1280, 720), '1080p': (1920, 1080), '4K': (3840, 2160)}


class FakeCapture:
    status = ('Connected', True)

    def __init__(self):
        self.frame = None

    def latest(self):
        return self.frame

    def release(self):
        pass


def frames(w, h, count):
    rng = np.random.default_rng(3)
    base = cv2.GaussianBlur(rng.integers(0, 256, (h, w, 3), np.uint8), (5, 5), 2)
    # Textured patch with coarse structure (pure per-pixel noise has no pyramid-level features).
    patch = cv2.GaussianBlur(rng.integers(0, 256, (h // 6, w // 8, 3), np.uint8), (0, 0), max(1, w / 1280))
    out = []
    for i in range(count):
        img = base.copy()
        x = w // 3 + int(4 * i * w / 1920)
        img[h // 2:h // 2 + patch.shape[0], x:x + patch.shape[1]] = patch
        out.append(img)
    return out, (w // 3, h // 2, patch.shape[1], patch.shape[0])


def stats(values):
    values = sorted(values)
    return statistics.fmean(values), values[int(.95 * (len(values) - 1))], values[-1]


def measure(app, name, size, count):
    w, h = size
    images, roi = frames(w, h, count + 1)
    panel = TrackingPanel()
    panel.resize(1440, 900)
    panel.show()
    app.processEvents()
    service = panel.service
    service.changed.disconnect(panel.refresh)          # time the view separately
    service.timer.stop()                               # drive ticks manually
    service.open_dashboard(('127.0.0.1', 0))
    capture = FakeCapture()
    service.capture = capture
    service.source_text = f'synthetic-{name}'
    rows = dict(tick=[], refresh=[], paint=[], total=[])
    receipt = time.monotonic()
    for i, img in enumerate(images):
        receipt = time.monotonic()
        capture.frame = FrameEnvelope.create(f'bench-{name}', 'bench', i + 1, img, receipt)
        service.dashboard._frame_request = time.monotonic()      # a browser is reading frames
        if i == 1:
            ref = service.begin_selection()
            service.select_roi(roi, ref)
        t0 = time.perf_counter()
        service.tick()
        t1 = time.perf_counter()
        panel.refresh()
        t2 = time.perf_counter()
        panel.canvas.repaint()
        t3 = time.perf_counter()
        r = service.session.snapshot()
        if r.state == 'TRACKING':
            rows.setdefault('fps', []).append(r.processing_fps)
        if i >= 3:
            for key, value in (('tick', t1 - t0), ('refresh', t2 - t1), ('paint', t3 - t2), ('total', t3 - t0)):
                rows[key].append(value * 1000)
        app.processEvents()
        time.sleep(max(0, 1 / 30 - (time.monotonic() - receipt)))
    state = service.session.snapshot()
    panel.shutdown()
    panel.close()
    lk = statistics.median(rows.pop('fps', [0]) or [0])
    return {key: stats(v) for key, v in rows.items()}, f'{state.state} / {state.reason} · LK {lk:.0f} fps'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--out', default=str(ROOT / 'tracking_validation' / 'perf_ui_thread.md'))
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    lines = ['| 해상도 | tick 평균/p95/최대 (ms) | refresh | paint | 합계 평균/p95/최대 | 33 ms 예산 | 최종 추적 상태 · 광류 처리 FPS(워커 스레드) |',
             '|---|---|---|---|---|---|---|']
    for name, size in RESOLUTIONS.items():
        result, state = measure(app, name, size, args.frames)
        fmt = lambda k: '{:.1f} / {:.1f} / {:.1f}'.format(*result[k])
        ok = '초과 없음' if result['total'][1] < 33 else 'p95 초과'
        lines.append(f'| {name} {size[0]}×{size[1]} | {fmt("tick")} | {fmt("refresh")} | {fmt("paint")} | '
                     f'{fmt("total")} | {ok} | {state} |')
        print(lines[-1], flush=True)
    note = ('\n측정 조건: Apple Silicon, offscreen Qt, 창 1440×900, 브라우저 프레임 요청 활성(JPEG ≤10 Hz, 1280 폭 축소), '
            '30 fps 합성 프레임. 디코더 복사(캡처 스레드)는 제외. 실제 화면(Retina) 합성 비용은 포함되지 않음.\n')
    Path(args.out).parent.mkdir(exist_ok=True)
    Path(args.out).write_text('\n'.join(lines) + '\n' + note, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
