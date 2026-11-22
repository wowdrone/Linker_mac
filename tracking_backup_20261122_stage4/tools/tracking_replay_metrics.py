"""Replay a recorded video file through the real TrackingSession and log metrics to CSV.

Purpose: measure frame age (decoder receipt → processed), tracker FPS and LOST reasons on
recordings (e.g. an HM30 recording) BEFORE tuning the 0.3 s timeout. Local files only:
network / RTSP sources are refused so a live HM30 is never connected automatically.
The ROI is given explicitly and applied once; there is no automatic re-selection.

    .venv/bin/python tools/tracking_replay_metrics.py VIDEO.mp4 --roi 600,320,120,90 \\
        [--select-frame 30] [--timeout 0.3] [--fast] [--out tracking_validation/replay.csv]

CSV columns: wall_s, frame_seq, state, reason, quality, processing_fps, frame_age_ms,
processing_ms. A summary (age percentiles, LOST reasons, hold time) goes to stdout and to
<out>.summary.json.
"""
import argparse
from collections import Counter
import csv
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from tracking_models import FrameEnvelope  # noqa: E402
from tracking_session import TrackingSession  # noqa: E402


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * (len(values) - 1)))], 1)


def run(path, roi, select_frame=30, timeout=.3, fast=False, out=None, max_frames=None):
    if '://' in str(path) or not os.path.isfile(path):
        raise SystemExit('로컬 녹화 파일만 지원합니다 (RTSP/네트워크 주소는 자동 연결하지 않음).')
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit(f'파일을 열 수 없습니다: {path}')
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    period = 0 if fast else 1 / fps
    session = TrackingSession(timeout)
    rows, seen = [], None
    start = time.monotonic()
    seq = 0
    holds, hold_start = [], None
    try:
        while max_frames is None or seq < max_frames:
            tick = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                break
            seq += 1
            session.offer(FrameEnvelope.create(str(path), 'replay', seq, frame, time.monotonic()))
            if seq == select_frame:
                ok_roi, reason = session.select_roi(roi, None)
                if not ok_roi:
                    print(f'ROI 적용 실패: {reason}', file=sys.stderr)
            # Give the worker the frame period (or a short slot in --fast mode) before sampling.
            time.sleep(max(0.0, period - (time.monotonic() - tick)) if period else .004)
            r = session.snapshot()
            key = (r.session_id, r.frame_seq, r.state, r.reason)
            if key == seen:
                continue
            seen = key
            age = None if r.processed_at is None or r.received_at is None else (r.processed_at - r.received_at) * 1000
            rows.append(dict(wall_s=round(time.monotonic() - start, 3), frame_seq=r.frame_seq, state=r.state,
                             reason=r.reason, quality=round(r.quality, 3),
                             processing_fps=round(r.processing_fps, 1) if r.state == 'TRACKING' else '',
                             frame_age_ms=round(age, 1) if age is not None and r.state == 'TRACKING' else '',
                             processing_ms=round(1000 / r.processing_fps, 1)
                             if r.state == 'TRACKING' and r.processing_fps else ''))
            if r.state == 'TRACKING' and hold_start is None:
                hold_start = time.monotonic()
            elif r.state != 'TRACKING' and hold_start is not None:
                holds.append(time.monotonic() - hold_start)
                hold_start = None
    finally:
        cap.release()
        session.close()
    if hold_start is not None:
        holds.append(time.monotonic() - hold_start)
    ages = [row['frame_age_ms'] for row in rows if row['frame_age_ms'] != '']
    proc = [row['processing_fps'] for row in rows if row['processing_fps'] != '']
    lost = Counter(row['reason'] for row in rows if row['state'] in ('LOST', 'ERROR'))
    summary = dict(file=os.path.basename(str(path)), source_fps=round(fps, 2), frames=seq, timeout_s=timeout,
                   roi=list(roi), select_frame=select_frame, results=len(rows),
                   frame_age_ms=dict(p50=percentile(ages, .5), p95=percentile(ages, .95),
                                     max=max(ages) if ages else None,
                                     over_timeout=sum(a >= timeout * 1000 for a in ages)),
                   processing_fps_median=percentile(proc, .5),
                   lost_reasons=dict(lost), tracking_holds_s=[round(h, 2) for h in holds])
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ['wall_s'])
            writer.writeheader()
            writer.writerows(rows)
        out.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('video')
    parser.add_argument('--roi', required=True, help='x,y,w,h in original pixels')
    parser.add_argument('--select-frame', type=int, default=30)
    parser.add_argument('--timeout', type=float, default=.3)
    parser.add_argument('--fast', action='store_true', help='do not pace at the file frame rate')
    parser.add_argument('--max-frames', type=int)
    parser.add_argument('--out', default=str(ROOT / 'tracking_validation' / 'replay_metrics.csv'))
    args = parser.parse_args(argv)
    roi = tuple(float(v) for v in args.roi.split(','))
    if len(roi) != 4 or not all(math.isfinite(v) for v in roi):
        parser.error('--roi x,y,w,h')
    _, summary = run(args.video, roi, args.select_frame, args.timeout, args.fast, args.out, args.max_frames)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
