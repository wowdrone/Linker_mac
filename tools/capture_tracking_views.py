"""Offscreen captures of the PyQt Tracking window and the browser dashboard (synthetic only).

No camera, vehicle link or network beyond 127.0.0.1 is used: the video is the synthetic
SIMULATION source and the vehicle is an in-memory fake with recorded telemetry. Browser
captures use a local headless Chrome against the loopback dashboard if Chrome exists.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/capture_tracking_views.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
OUT = ROOT / 'tracking_validation'

from PyQt5.QtWidgets import QApplication  # noqa: E402

from tracking_panel import TrackingPanel  # noqa: E402
from test_tracking_service import FakeVehicle  # noqa: E402

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def pump(app, seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(.01)
    return False


def vehicle(armed=True):
    v = FakeVehicle(1, 'LOITER', armed)
    v.name = 'SYS 1'
    now = time.monotonic()
    v.tracking_telemetry.update({
        'ATTITUDE': dict(roll=.03, pitch=-.05, yaw=1.57), 'VFR_HUD': dict(alt=62.4, groundspeed=2.1, heading=90),
        'GLOBAL_POSITION_INT': dict(lat=375665000, lon=1269780000, alt=62400, relative_alt=18200),
        'SYS_STATUS': dict(voltage_battery=15620, battery_remaining=71)})
    v.telemetry_received.update({k: now for k in v.tracking_telemetry})
    return v


def browser(url, path, size, app, pump_s=7):
    if not os.path.exists(CHROME):
        print('Chrome 없음 · 브라우저 캡처 생략')
        return False
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen([CHROME, '--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
                                 f'--user-data-dir={profile}', f'--window-size={size[0]},{size[1]}',
                                 '--virtual-time-budget=4000', f'--screenshot={path}', url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 40
        while proc.poll() is None and time.monotonic() < end:
            pump(app, .05)
        if proc.poll() is None:
            proc.kill()
    return Path(path).exists()


def main():
    OUT.mkdir(exist_ok=True)
    app = QApplication.instance() or QApplication([])
    panel = TrackingPanel()
    panel.resize(1500, 950)
    panel.show()
    s = panel.service
    pump(app, .3)
    panel.grab().save(str(OUT / 'pyqt_tracking_no_video.png'))

    v = vehicle(armed=True)
    panel.set_vehicle(v)
    panel.start_demo()
    pump(app, 3, lambda: s.session.snapshot().state == 'TRACKING')
    pump(app, .5)
    url = s.open_dashboard(('127.0.0.1', 0))
    keep_fresh = lambda: v.beat('LOITER', True)
    pump(app, .3)
    panel.grab().save(str(OUT / 'pyqt_tracking_active.png'))
    keep_fresh()
    browser(url, OUT / 'web_tracking_active.png', (1500, 1250), app)
    keep_fresh()
    browser(url, OUT / 'web_tracking_narrow.png', (820, 2300), app)

    s.begin_selection()
    pump(app, .6)
    panel.grab().save(str(OUT / 'pyqt_tracking_selecting.png'))
    keep_fresh()
    browser(url, OUT / 'web_tracking_selecting.png', (1500, 1250), app)

    s.stop('operator_stop')
    keep_fresh()
    s.prepare_mode('RTL', 'web')                  # browser confirmation panel with ARMED warning
    browser(url, OUT / 'web_mode_confirm_armed.png', (1500, 1500), app)
    s.cancel_mode('캡처 종료', 'web')

    s.start_demo()
    pump(app, 3, lambda: s.session.snapshot().state == 'TRACKING')
    last = s.session.frame                        # a live capture that stops delivering (stalled RTSP)
    s.demo = False
    s.capture = type('Stalled', (), dict(status=('Connected', True), latest=lambda self: last,
                                         release=lambda self: None))()
    pump(app, 2, lambda: s.session.snapshot().state == 'LOST')
    keep_fresh()
    pump(app, .2)
    panel.grab().save(str(OUT / 'pyqt_tracking_lost.png'))
    keep_fresh()
    browser(url, OUT / 'web_tracking_lost.png', (1500, 1250), app)

    print('sent flight packets:', v.master.mav.sent)
    panel.shutdown()
    panel.close()
    print('\n'.join(sorted(p.name for p in OUT.glob('*tracking*.png')) + ['web_mode_confirm_armed.png']))


if __name__ == '__main__':
    main()
