"""Offscreen captures of every Linker screen with its guide closed and open.

Output: tracking_validation/guide/<screen>_closed.png and <screen>_guide.png.
No vehicle link, camera or network beyond 127.0.0.1 is opened; the map page is replaced by
an inert widget (as in the tests) and QSettings go to a temporary folder.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/capture_guides.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'tracking_validation' / 'guide'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

import main_window  # noqa: E402  (QtWebEngine before QApplication)
from PyQt5.QtCore import QSettings  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402


class InertMap(QWidget):
    def page(self):
        return MagicMock()


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(.01)


def save_pair(app, widget, name, open_guide):
    widget.grab().save(str(OUT / f'{name}_closed.png'))
    dialog = open_guide()
    pump(app, .2)
    dialog.grab().save(str(OUT / f'{name}_guide.png'))
    dialog.close()
    pump(app, .05)


def chrome(url, path, size, app, wall=False):
    """wall=True: wall-clock --timeout (pages with an open SSE stream never let virtual time settle)."""
    if not os.path.exists(CHROME):
        print('Chrome 없음 · 브라우저 캡처 생략')
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen([CHROME, '--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
                                 f'--user-data-dir={profile}', f'--window-size={size[0]},{size[1]}',
                                 '--timeout=5000' if wall else '--virtual-time-budget=3000', f'--screenshot={path}', url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 40
        while proc.poll() is None and time.monotonic() < end:
            pump(app, .05)
        if proc.poll() is None:
            proc.kill()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as settings_dir:
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings_dir)
        app = QApplication.instance() or QApplication([])
        with patch.object(main_window, 'create_map_widget', side_effect=lambda *a: (InertMap(), MagicMock())), \
             patch('fleet.FleetManager.add_link'), patch('video_grid.VideoWorker._open'):
            window = main_window.MainWindow()
            window.resize(1400, 900)
            window.show()
            pump(app, .3)
            for mode, name in (('fly', 'main_fly'), ('planning', 'main_plan'), ('health', 'main_health'),
                               ('safety', 'main_safety'), ('support', 'main_payload'), ('ai_tuner', 'main_ai_tuner'),
                               ('fleet', 'main_fleet'), ('video', 'main_video4ch')):
                window.on_mode_button_clicked(mode)
                pump(app, .2)
                save_pair(app, window, name, window.show_page_guide)
            dialog = main_window.PreArmDialog(0, window)
            dialog.show(); pump(app, .1)
            button = next(b for b in dialog.findChildren(QWidget) if b.objectName() == 'guideButton')
            save_pair(app, dialog, 'prearm_dialog', lambda: (button.click(), dialog._open_guides['PreArm Checks'])[1])
            dialog.close()

            panel = window.tracking_panel
            panel.resize(1500, 950); panel.show(); panel.start_demo(); pump(app, 1.0)
            save_pair(app, panel, 'tracking_window', lambda: (panel.guide_button.click(),
                                                              panel._open_guides['Linker Tracking'])[1])
            legacy = window.guide_vehicle
            legacy.show(); pump(app, .2)
            button = next(b for b in legacy.findChildren(QWidget) if b.objectName() == 'guideButton')
            save_pair(app, legacy, 'sentinel_legacy', lambda: (button.click(), legacy._open_guides['Sentinel Legacy'])[1])

            url = window.tracking_service.open_dashboard(('127.0.0.1', 0))
            pump(app, .3)
            chrome(url, OUT / 'web_tracking_closed.png', (1500, 1300), app)
            chrome(url + '#guide', OUT / 'web_tracking_guide.png', (1500, 2600), app)
            from drive_dashboard import Dashboard
            drive = Dashboard(('127.0.0.1', 0))
            chrome(drive.url, OUT / 'web_drive_closed.png', (1500, 1200), app, wall=True)
            chrome(drive.url + '#guide', OUT / 'web_drive_guide.png', (1500, 1500), app, wall=True)
            drive.close()
            window.close()
            pump(app, .2)
    print('\n'.join(sorted(p.name for p in OUT.glob('*.png'))))


if __name__ == '__main__':
    main()
