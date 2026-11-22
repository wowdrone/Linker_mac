"""Monitor-only Qt view over the shared TrackingService.

The PyQt Tracking window and the browser dashboard show the same TrackingService (one
TrackingSession, one shared decoder). Vehicle objects stay read only.
"""
import time
import cv2
from PyQt5.QtCore import Qt, QSettings, QRectF, QPointF, QUrl, pyqtSignal
from PyQt5.QtGui import QImage, QPainter, QColor, QPen, QDesktopServices
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QComboBox, QDoubleSpinBox, QFileDialog)
from video_grid import VideoCanvas, VIDEO_SOURCES
from tracking_service import TrackingService, STALE_S


def image_geometry(iw, ih, ww, wh, zoom):
    # Match VideoCanvas QSize.scaled's integer KeepAspectRatio rectangle.
    from PyQt5.QtCore import QSize
    size = QSize(iw, ih).scaled(QSize(ww, wh), Qt.KeepAspectRatio)
    return ((ww-size.width())/2, (wh-size.height())/2, size.width(), size.height(),
            (iw-iw/zoom)/2, (ih-ih/zoom)/2, iw/zoom, ih/zoom)


def image_point(point, geometry):
    x,y,w,h,sx,sy,sw,sh = geometry
    px,py = point
    if w <= 0 or h <= 0 or not x <= px <= x+w or not y <= py <= y+h:
        return None
    return sx+(px-x)*sw/w, sy+(py-y)*sh/h

class TrackingCanvas(VideoCanvas):
    roiSelected = pyqtSignal(tuple)

    def __init__(self, parent=None):
        super().__init__(0, parent)
        self.selecting = False
        self.anchor = self.drag = None
        self.result = None

    def geometry_map(self):
        if self.image is None:
            return None
        return image_geometry(self.image.width(), self.image.height(), self.width(), self.height(), self.zoom)

    def mousePressEvent(self, event):
        g = self.geometry_map()
        if self.selecting and event.button() == Qt.LeftButton and g:
            self.anchor = image_point((event.x(),event.y()), g)
            self.drag = self.anchor

    def mouseMoveEvent(self, event):
        if self.anchor and self.geometry_map():
            self.drag = image_point((event.x(),event.y()), self.geometry_map())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor and self.geometry_map():
            end = image_point((event.x(),event.y()), self.geometry_map())
            if end:
                a,b = self.anchor, end
                self.roiSelected.emit((min(a[0],b[0]),min(a[1],b[1]),abs(a[0]-b[0]),abs(a[1]-b[1])))
                self.selecting = False
        self.anchor = self.drag = None
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        g = self.geometry_map()
        if not g:
            return
        x,y,w,h,sx,sy,sw,sh = g
        def point(a,b): return QPointF(x+(a-sx)*w/sw,y+(b-sy)*h/sh)
        p = QPainter(self)
        p.setClipRect(QRectF(x,y,w,h))
        p.setPen(QPen(QColor('#6c8ca1'),1))
        p.drawLine(QPointF(x+w/2,y),QPointF(x+w/2,y+h))
        p.drawLine(QPointF(x,y+h/2),QPointF(x+w,y+h/2))
        p.setPen(QPen(QColor('#00ffbb'),2))
        if self.result and self.result.bbox:
            bx,by,bw,bh = self.result.bbox
            p.drawRect(QRectF(point(bx,by),point(bx+bw,by+bh)))
            for a,b in zip(self.result.trail,self.result.trail[1:]):
                p.drawLine(point(*a),point(*b))
        if self.anchor and self.drag:
            p.setPen(QPen(QColor('#ffcf57'),2,Qt.DashLine))
            p.drawRect(QRectF(point(*self.anchor),point(*self.drag)).normalized())
        p.end()

class TrackingPanel(QDialog):
    """PyQt Tracking window. All state lives in TrackingService (shared with the browser)."""

    def __init__(self, parent=None, service=None):
        super().__init__(parent)
        self.setWindowTitle('Linker · Tracking · MONITOR ONLY')
        self.resize(1100, 760)
        self.owns_service = service is None
        self.service = service or TrackingService(self)
        self._image_key = None
        self._pinned_ref = None
        self.closed = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.source = QLineEdit()
        settings = QSettings('WowDrone', 'LinkerGCS')
        self.source.setText(settings.value('video/fly/url', '', str) or settings.value('video/ch0/url', '', str))
        self.source.setPlaceholderText('기존 RTSP / 파일 / 장치 주소 — 수동 연결')
        self.sources = QComboBox()
        for title, value in VIDEO_SOURCES:
            self.sources.addItem(title, value)
        self.sources.currentIndexChanged.connect(lambda i: self.source.setText(self.sources.itemData(i) or ''))
        self.transport = QComboBox(); self.transport.addItems(['tcp', 'udp'])
        self.transport.setCurrentText(settings.value('video/ch0/transport', 'tcp', str))
        self.mount = QComboBox(); self.mount.addItems(['기체 고정 · 비짐벌', '지상 고정'])
        self.mount.currentIndexChanged.connect(self.change_mount)
        for widget in (self.sources, self.source, self.transport, self.mount):
            row.addWidget(widget)
        layout.addLayout(row)
        row = QHBoxLayout()
        for title, callback in [('영상 연결', self.connect_source), ('영상 해제', self.disconnect_source),
                                ('ROI 선택', self.selecting), ('추적 정지', self.operator_stop),
                                ('합성 모의 시험', self.start_demo), ('모의 출력 허용', self.simulate),
                                ('조종자 개입', lambda: self.service.stop('operator_intervention')),
                                ('로그 저장', self.save), ('브라우저 대시보드', self.open_browser)]:
            button = QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(.05, 10); self.timeout.setDecimals(2)
        self.timeout.setValue(self.service.session.timeout); self.timeout.setSuffix(' s timeout')
        self.timeout.setSingleStep(.05)
        self.timeout.valueChanged.connect(self.change_timeout); row.addWidget(self.timeout)
        layout.addLayout(row)
        self.status = QLabel('MONITOR ONLY · IDLE · UNKNOWN'); layout.addWidget(self.status)
        self.canvas = TrackingCanvas(self); layout.addWidget(self.canvas, 1)
        self.canvas.roiSelected.connect(self.session_select)
        self.links = QLabel('영상 — · Pixhawk — · Sentinel Legacy —'); layout.addWidget(self.links)
        self.telemetry = QLabel('기체 미선택 —'); self.telemetry.setWordWrap(True); layout.addWidget(self.telemetry)
        self.web = QLabel('브라우저 대시보드 꺼짐'); self.web.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.web)
        # The service owns the 33 ms tick; this view only redraws when it changes.
        self.service.changed.connect(self.refresh)

    # ---- compatibility accessors (tests and main window use these names) ----
    @property
    def session(self):
        return self.service.session

    @property
    def capture(self):
        return self.service.capture

    @property
    def vehicle(self):
        return self.service.vehicle

    @property
    def demo(self):
        return self.service.demo

    @demo.setter
    def demo(self, value):
        self.service.demo = bool(value)

    # ---- operator actions ----
    def change_mount(self):
        self.service.mount = self.mount.currentText()
        self.service.stop('camera_mode_changed')

    def change_timeout(self, value):
        self.service.change_timeout(value)

    def selecting(self):
        self._pinned_ref = self.service.begin_selection()
        self.canvas.selecting = self._pinned_ref is not None

    def session_select(self, roi):
        self.service.select_roi(roi, self._pinned_ref)
        self._pinned_ref = None

    def operator_stop(self):
        self.canvas.selecting = False
        self.service.stop('operator_stop')

    def disconnect_source(self):
        self.service.disconnect_source()
        self._image_key = None
        self.canvas.clear()

    def connect_source(self):
        self._image_key = None
        self.service.connect_source(self.source.text(), self.transport.currentText())

    def start_demo(self):
        self._image_key = None
        self.service.start_demo()

    def simulate(self):
        self.service.simulate()

    def pixhawk_fresh(self):
        return self.service.pixhawk_fresh()

    def set_vehicle(self, vehicle):
        self.service.set_vehicle(vehicle)

    def open_browser(self):
        url = self.service.open_dashboard()
        self.web.setText(f'브라우저 대시보드 · {url} · 같은 추적 세션을 표시합니다')
        QDesktopServices.openUrl(QUrl(url))

    def tick(self):
        """Advance the shared service once (normally driven by its own timer)."""
        if not self.closed:
            self.service.tick()

    # ---- view ----
    def refresh(self):
        if self.closed:
            return
        frame, result = self.service.view()
        if frame is not None:
            key = frame.session_id, frame.frame_seq
            if key != self._image_key:
                self._image_key = key
                rgb = cv2.cvtColor(frame.pixels, cv2.COLOR_BGR2RGB)
                self.canvas.set_image(QImage(rgb.data, frame.width, frame.height, rgb.strides[0],
                                             QImage.Format_RGB888).copy())
        state = self.session.snapshot()
        if state.state != 'SELECTING' and self.canvas.selecting:
            self.canvas.selecting = False
        self.canvas.result = result
        self.canvas.update()
        live = self.session.frame
        age = time.monotonic() - live.decoder_received_monotonic if live else None
        label = 'SIMULATION' if self.service.demo or self.session.sink.enabled else 'MONITOR ONLY'
        age_text = f'{age * 1000:.0f} ms' if age is not None else '—'
        self.status.setText(f'{label} · {state.state} / {state.reason} · UNKNOWN · quality {state.quality:.2f} '
                            f'· 처리 {state.processing_fps:.1f} fps · 프레임 나이 {age_text}\n'
                            f'source {live.source_id if live else "—"} / session {live.session_id if live else "—"}')
        legacy = getattr(self.parent(), 'guide_vehicle', None)
        guide = getattr(legacy, 'session', None)
        sentinel = '—' if guide is None else ('동기화' if guide.synchronized else '동기화 대기')
        self.links.setText(f'영상 {"fresh" if age is not None and age < self.session.timeout else "stale / —"} · '
                           f'Pixhawk {"fresh" if self.pixhawk_fresh() else "stale / —"} · Sentinel Legacy {sentinel}')
        self.telemetry.setText(self.telemetry_text())

    def telemetry_text(self):
        v = self.vehicle
        if v is None:
            return '기체 미선택 —'
        times = getattr(v, 'telemetry_received', {})
        samples = getattr(v, 'tracking_telemetry', {})

        def group(kind):
            if kind not in times:
                return '—'
            stale = ' stale' if time.monotonic() - times[kind] > STALE_S else ''
            values = samples.get(kind, {})
            invalid = {'voltage_battery': (0, 65535), 'battery_remaining': (-1,), 'satellites_visible': (255,)}
            values = {k: ('—' if value is None or value in invalid.get(k, ()) else value) for k, value in values.items()}
            return str(values) + stale
        firmware = {3: 'ArduPilot', 12: 'PX4'}.get(v.autopilot, v.autopilot)
        hb = times.get('HEARTBEAT')
        age = f'{time.monotonic() - hb:.1f}s' if hb else '—'
        return (f'link {v.link.link_id} / sys {v.sysid} / comp {v.compid} · firmware {group("AUTOPILOT_VERSION")} '
                f'· autopilot {firmware} · mode {v.last_mode if hb else "—"} · armed {v.last_armed if hb else "—"} '
                f'· heartbeat {age} {"stale" if not self.pixhawk_fresh() else ""}\n'
                f'자세(rad) {group("ATTITUDE")} · GPS {group("GPS_RAW_INT")} · 배터리(mV/%) {group("SYS_STATUS")}\n'
                f'고도 GLOBAL_POSITION_INT(mm MSL / home-relative) {group("GLOBAL_POSITION_INT")} '
                f'· VFR_HUD(m MSL, AGL 아님) {group("VFR_HUD")}')

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Tracking 로그 저장', 'tracking.json', 'JSON (*.json)')
        if path:
            self.session.save(path)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.service.changed.disconnect(self.refresh)
        except (TypeError, RuntimeError):
            pass
        self.service.shutdown()

    def closeEvent(self, event):
        # While the browser dashboard is open it keeps using the same session: leave it running.
        if self.service.dashboard is None and not self.closed:
            self.service.disconnect_source('panel_closed')
            self.session.stop('panel_closed')
            self._image_key = None
        event.accept()
