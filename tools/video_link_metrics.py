"""Linker-side video link metrics for the direct / direct×2 / relay comparison (docs/video_relay.md).

Opens ONLY the source given on the command line, through the same _SharedCapture decoder
path the app uses, and records once per second:
  t_s, frames, recv_fps, source_fps, frame_age_ms_p50, frame_age_ms_max, gaps, reconnects,
  session_changes, decode_warning
Frame age here is decoder receipt → sampling on this Mac (it does NOT include camera,
encoder and radio latency; use Sentinel's tools/measure_video_latency.py read-only for
glass-to-glass latency). A gap is an inter-frame interval longer than --gap-ms.

    .venv/bin/python tools/video_link_metrics.py rtsp://127.0.0.1:8554/cam --seconds 120 \\
        --label relay --out tracking_validation/link_relay.csv
Credentials in the URL are masked in every output.
"""
import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tracking_service import mask_url  # noqa: E402
from video_grid import _SharedCapture  # noqa: E402

FIELDS = ['t_s', 'frames', 'recv_fps', 'source_fps', 'frame_age_ms_p50', 'frame_age_ms_max', 'gaps',
          'reconnects', 'session_changes', 'decode_warning', 'status']


def run(source, seconds=60.0, transport='tcp', gap_ms=500.0, out=None, label='', poll_s=.005):
    capture = _SharedCapture.acquire(source, transport)
    rows, ages, last_key, last_session, last_receipt = [], [], None, None, None
    total = dict(frames=0, gaps=0, session_changes=0)
    window = dict(frames=0, gaps=0, ages=[])
    start = next_row = time.monotonic()
    try:
        while time.monotonic() - start < seconds:
            frame = capture.latest()
            now = time.monotonic()
            if frame is not None and (frame.session_id, frame.frame_seq) != last_key:
                last_key = frame.session_id, frame.frame_seq
                if last_session is not None and frame.session_id != last_session:
                    total['session_changes'] += 1
                last_session = frame.session_id
                age = (now - frame.decoder_received_monotonic) * 1000
                window['ages'].append(age)
                ages.append(age)
                if last_receipt is not None and (frame.decoder_received_monotonic - last_receipt) * 1000 > gap_ms:
                    window['gaps'] += 1
                    total['gaps'] += 1
                last_receipt = frame.decoder_received_monotonic
                window['frames'] += 1
                total['frames'] += 1
            if now >= next_row + 1.0:
                next_row += 1.0
                health = capture.health.snapshot(now)
                values = window['ages']
                rows.append(dict(
                    t_s=round(now - start, 1), frames=window['frames'], recv_fps=window['frames'],
                    source_fps=health['source_fps'],
                    frame_age_ms_p50=round(statistics.median(values), 1) if values else '',
                    frame_age_ms_max=round(max(values), 1) if values else '',
                    gaps=window['gaps'], reconnects=capture.reconnects, session_changes=total['session_changes'],
                    decode_warning=health['warning'] or '', status=capture.status[0]))
                window = dict(frames=0, gaps=0, ages=[])
            time.sleep(poll_s)
    finally:
        reconnects = capture.reconnects
        capture.release()
    summary = dict(source=mask_url(source), label=label, seconds=seconds, transport=transport,
                   frames=total['frames'], gaps=total['gaps'], reconnects=reconnects,
                   session_changes=total['session_changes'],
                   recv_fps_mean=round(total['frames'] / seconds, 2),
                   frame_age_ms=dict(p50=round(statistics.median(ages), 1) if ages else None,
                                     max=round(max(ages), 1) if ages else None),
                   decode_warning_seconds=sum(1 for r in rows if r['decode_warning']),
                   note='frame age = decoder receipt → sampling on this Mac, not glass-to-glass latency')
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        out.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', help='rtsp://…, file path or camera index (opened only when you run this)')
    parser.add_argument('--seconds', type=float, default=60.0)
    parser.add_argument('--transport', choices=('tcp', 'udp'), default='tcp')
    parser.add_argument('--gap-ms', type=float, default=500.0)
    parser.add_argument('--label', default='', help='①direct / ②direct×2 / ③relay')
    parser.add_argument('--out', default=str(ROOT / 'tracking_validation' / 'video_link_metrics.csv'))
    args = parser.parse_args(argv)
    print(f'측정 대상 {mask_url(args.source)} · {args.seconds:.0f}s', flush=True)
    _, summary = run(args.source, args.seconds, args.transport, args.gap_ms, args.out, args.label)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
