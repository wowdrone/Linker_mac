import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
import unittest
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from pathlib import Path
from PyQt5.QtWidgets import QApplication
from tracking_panel import TrackingPanel, image_geometry, image_point
from Linker import Linker

class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def test_geometry_letterbox_zoom_resize(self):
        for ww,wh in ((1000,1000),(1920,1080),(800,450)):
            for zoom in (1,2,8):
                g=image_geometry(3840,2160,ww,wh,zoom)
                p=image_point((ww/2,wh/2),g)
                self.assertAlmostEqual(p[0],1920);self.assertAlmostEqual(p[1],1080)
                self.assertIsNone(image_point((-1,0),g))
                if g[1]>0:self.assertIsNone(image_point((ww/2,0),g))
    def test_demo_no_real_commands_and_shutdown(self):
        with ExitStack() as stack:
            methods=('send_virtual_rc','arm','disarm','takeoff','_set_mode','_send_rc_override_now','land','rtl')
            spies=[stack.enter_context(patch.object(Linker,name)) for name in methods]
            panel=TrackingPanel();panel.show();panel.start_demo()
            end=time.monotonic()+.6
            while time.monotonic()<end:
                self.app.processEvents();time.sleep(.01)
            self.assertEqual(panel.session.snapshot().state,'TRACKING')
            panel.simulate()
            end=time.monotonic()+.2
            while time.monotonic()<end:self.app.processEvents();time.sleep(.01)
            self.assertIsNotNone(panel.session.sink.current)
            Path('tracking_validation').mkdir(exist_ok=True)
            panel.grab().save('tracking_validation/tracking_offscreen.png')
            panel.set_vehicle(None);panel.operator_stop();self.assertIsNone(panel.session.sink.current)
            panel.shutdown();panel.session.worker.join(2);panel.session.watchdog.join(2);panel.close()
            self.assertFalse(panel.session.worker.is_alive())
            for spy in spies:spy.assert_not_called()

    def test_mouse_roi_original_coordinates_and_margin(self):
        from PyQt5.QtTest import QTest
        from PyQt5.QtCore import Qt,QPoint
        from PyQt5.QtGui import QImage
        from tracking_panel import TrackingCanvas
        canvas=TrackingCanvas();canvas.resize(800,800)
        canvas.set_image(QImage(3840,2160,QImage.Format_RGB888));canvas.set_zoom(2)
        values=[];canvas.roiSelected.connect(values.append);canvas.selecting=True;canvas.show();self.app.processEvents()
        QTest.mousePress(canvas,Qt.LeftButton,pos=QPoint(400,10))
        QTest.mouseRelease(canvas,Qt.LeftButton,pos=QPoint(500,500))
        self.assertEqual(values,[])
        QTest.mousePress(canvas,Qt.LeftButton,pos=QPoint(200,300))
        QTest.mouseRelease(canvas,Qt.LeftButton,pos=QPoint(600,500))
        self.assertEqual(len(values),1)
        self.assertAlmostEqual(values[0][0],1440)
        self.assertAlmostEqual(values[0][2],960)
        self.assertAlmostEqual(values[0][3],480)
        canvas.close()

    def test_no_transport_or_control_attributes_in_tracking(self):
        import ast
        prohibited={'send_virtual_rc','arm','disarm','takeoff','set_mode','_set_mode',
                    'rc_channels_override_send','set_attitude_target_send',
                    'set_position_target_local_ned_send','set_position_target_global_int_send',
                    'recv_match','mavlink_connection'}
        for path in Path('.').glob('tracking_*.py'):
            tree=ast.parse(path.read_text())
            used={node.attr for node in ast.walk(tree) if isinstance(node,ast.Attribute)}
            self.assertFalse(used & prohibited, str(path))

    def test_telemetry_stale_only_revokes_simulation(self):
        from types import SimpleNamespace
        from PyQt5.QtCore import QObject,pyqtSignal
        class Vehicle(QObject):
            commandFeedback=pyqtSignal(str,bool,str)
            modeChanged=pyqtSignal(str)
            armedChanged=pyqtSignal(bool)
            connectionStateChanged=pyqtSignal(bool)
        v=Vehicle();v.link=SimpleNamespace(link_id=1);v.sysid=2;v.compid=1;v.autopilot=3
        v.telemetry_received={'HEARTBEAT':time.monotonic()};v.tracking_telemetry={}
        v.last_mode='STABILIZE';v.last_armed=False
        panel=TrackingPanel();panel.set_vehicle(v);panel.start_demo()
        end=time.monotonic()+.12
        while time.monotonic()<end:self.app.processEvents();time.sleep(.005)
        panel.demo=False  # keep latest frame; no capture required for this read-only check
        panel.simulate();self.assertTrue(panel.session.sink.enabled)
        v.telemetry_received['HEARTBEAT']=time.monotonic()-10
        panel.tick()
        self.assertFalse(panel.session.sink.enabled)
        self.assertEqual(panel.session.snapshot().state,'TRACKING')
        self.assertIn('STALE',panel.telemetry_text());self.assertIn('—',panel.telemetry_text())
        panel.set_vehicle(None)
        self.assertEqual(panel.session.snapshot().reason,'vehicle_changed')
        panel.shutdown();panel.session.worker.join(2);panel.close()


class PanelBehaviour(unittest.TestCase):
    """Rebuilt Tracking window: transport rule, close choices, confirmed mode request, browser bar."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = TrackingPanel()
        self.addCleanup(self.finish)

    def finish(self):
        self.panel.shutdown()
        self.panel.close()

    def test_transport_disabled_for_device_and_file(self):
        p = self.panel
        for text, enabled in (('rtsp://192.168.144.25:8554/main', True), ('0', False),
                              (str(Path(__file__)), False), ('', False)):
            p.source.setText(text)
            self.assertEqual(p.transport.isEnabled(), enabled, text)

    def test_panel_style_tracks_main_theme(self):
        from gui_theme import THEMES
        theme = THEMES['light']
        self.panel.apply_theme(theme)
        style = self.panel.styleSheet()
        self.assertIn(f'background:{theme.background}', style)
        self.assertIn(f'background:{theme.panel}', style)
        self.assertIn(f'background:{theme.accent}', style)
        self.assertEqual(self.panel.canvas.theme, theme)

    def test_reason_text_and_tooltip(self):
        p = self.panel
        p.start_demo(); p.service.disconnect_source('source_changed'); p.refresh()
        self.assertEqual(p.reason.text(), '영상 소스 변경')
        self.assertIn('source_changed', p.reason.toolTip())
        self.assertNotIn('source_changed', p.reason.text())
        self.assertEqual(p.state_badge.text(), 'STOPPED')

    def test_confirmed_mode_request_sends_once_and_cancel_sends_none(self):
        import sys; sys.path.insert(0, str(Path(__file__).parent))
        from test_tracking_service import FakeVehicle
        p, v = self.panel, FakeVehicle(1)
        p.set_vehicle(v); p.refresh()
        p.mode_combo.setCurrentIndex(p.mode_combo.findData('LOITER'))
        with patch.object(TrackingPanel, 'confirm_mode', return_value=False):
            p.request_mode()
        self.assertEqual((v.master.mav.sent, p.service.modes.state), ([], 'CANCELLED'))
        with patch.object(TrackingPanel, 'confirm_mode', return_value=True) as confirm:
            p.request_mode()
        self.assertEqual(len(v.master.mav.sent), 1)
        ticket = confirm.call_args[0][0]
        self.assertEqual((ticket['current_mode'], ticket['mode'], ticket['source']), ('STABILIZE', 'LOITER', 'app'))
        p.refresh()
        self.assertFalse(p.mode_button.isEnabled())      # result pending: no second click
        disabled = [p.mode_combo.itemText(i) for i in range(p.mode_combo.count()) if p.mode_combo.itemData(i) is None]
        self.assertIn('AUTO (비활성)', disabled)

    def test_browser_bar_and_close_choices(self):
        p = self.panel
        p.service.open_dashboard(('127.0.0.1', 0)); p.refresh()
        self.assertFalse(p.chips['webmode'].isHidden())              # short badge while the dashboard is open
        self.assertEqual(p.chips['webmode'].text(), '웹 모드변경 ON')
        self.assertIn('같은 추적 세션 공유 중', p.web.text())
        self.assertIn(p.service.dashboard.url, p.web.text())
        p.show()
        with patch.object(TrackingPanel, 'ask_close_choice', return_value='cancel'):
            p.close()
        self.assertTrue(p.isVisible())
        p.start_demo()
        with patch.object(TrackingPanel, 'ask_close_choice', return_value='keep'):
            p.close()
        self.assertFalse(p.isVisible()); self.assertIsNotNone(p.service.dashboard); self.assertTrue(p.service.demo)
        p.show()
        with patch.object(TrackingPanel, 'ask_close_choice', return_value='close_all'):
            p.close()
        self.assertIsNone(p.service.dashboard); self.assertFalse(p.service.demo)
