"""Offline integration: real window/layout, inert map page, isolated preferences."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication,QWidget,QPushButton
import main_window

class InertMap(QWidget):
    def page(self): return MagicMock()

class MainWindowTest(unittest.TestCase):
    def test_entrypoints_and_no_auto_connect(self):
        app=QApplication.instance() or QApplication([])
        with TemporaryDirectory() as directory:
            QSettings.setDefaultFormat(QSettings.IniFormat)
            QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,directory)
            with patch.object(main_window,'create_map_widget',side_effect=lambda *a:(InertMap(),MagicMock())), \
                 patch('fleet.FleetManager.add_link') as link, patch('video_grid.VideoWorker._open') as capture:
                window=main_window.MainWindow()
                window.show();app.processEvents()
                texts={b.text():b for b in window.findChildren(QPushButton)}
                self.assertIn('Tracking',texts);self.assertIn('Sentinel Legacy',texts)
                texts['Tracking'].click();app.processEvents()
                self.assertTrue(window.tracking_panel.isVisible())
                self.assertIsNone(window.tracking_panel.capture)
                self.assertIsNone(window.guide_vehicle.session)
                link.assert_not_called();capture.assert_not_called()
                window.tracking_panel.start_demo()
                for _ in range(3):window.tracking_panel.tick();app.processEvents()
                Path('tracking_validation').mkdir(exist_ok=True)
                window.grab().save('tracking_validation/main_window_offscreen.png')
                window.tracking_panel.grab().save('tracking_validation/tracking_themed_offscreen.png')
                window.close();app.processEvents()
                window.tracking_panel.session.worker.join(2)
                self.assertFalse(window.tracking_panel.session.worker.is_alive())
