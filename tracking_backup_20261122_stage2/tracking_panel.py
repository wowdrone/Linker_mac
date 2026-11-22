"""Monitor-only Qt view. Vehicle objects stay in this read-only presentation layer."""
import time
import uuid
import cv2
import numpy as np
from PyQt5.QtCore import Qt, QTimer, QSettings, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import QImage, QPainter, QColor, QPen
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QComboBox, QDoubleSpinBox, QFileDialog)
from video_grid import VideoCanvas, _SharedCapture, VIDEO_SOURCES
from tracking_models import FrameEnvelope
from tracking_session import TrackingSession


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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Linker · Tracking · MONITOR ONLY')
        self.resize(1100,760)
        self.session = TrackingSession()
        self.capture = None
        self.vehicle = None
        self.demo = False
        self.last_key = None
        self.demo_seq = 0
        self.closed = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.source = QLineEdit()
        settings = QSettings('WowDrone','LinkerGCS')
        self.source.setText(settings.value('video/fly/url','',str) or settings.value('video/ch0/url','',str))
        self.source.setPlaceholderText('기존 RTSP / 파일 / 장치 주소 — 수동 연결')
        self.sources = QComboBox()
        for title,value in VIDEO_SOURCES: self.sources.addItem(title,value)
        self.sources.currentIndexChanged.connect(lambda i: self.source.setText(self.sources.itemData(i) or ''))
        self.transport = QComboBox(); self.transport.addItems(['tcp','udp'])
        self.transport.setCurrentText(settings.value('video/ch0/transport','tcp',str))
        self.mount = QComboBox(); self.mount.addItems(['기체 고정 · 비짐벌','지상 고정'])
        self.mount.currentIndexChanged.connect(lambda: self.session.stop('camera_mode_changed'))
        for widget in (self.sources,self.source,self.transport,self.mount): row.addWidget(widget)
        layout.addLayout(row)
        row = QHBoxLayout()
        for title,callback in [('영상 연결',self.connect_source),('영상 해제',self.disconnect_source),
                               ('ROI 선택',self.selecting),('추적 정지',self.operator_stop),
                               ('합성 모의 시험',self.start_demo),('모의 출력 허용',self.simulate),
                               ('조종자 개입',lambda: self.session.stop('operator_intervention')),
                               ('로그 저장',self.save)]:
            button=QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(.05,10); self.timeout.setDecimals(2)
        self.timeout.setValue(.3); self.timeout.setSuffix(' s timeout'); self.timeout.setSingleStep(.05)
        self.timeout.valueChanged.connect(self.change_timeout); row.addWidget(self.timeout)
        layout.addLayout(row)
        self.status = QLabel('MONITOR ONLY · IDLE · UNKNOWN'); layout.addWidget(self.status)
        self.canvas = TrackingCanvas(self); layout.addWidget(self.canvas,1)
        self.canvas.roiSelected.connect(self.session_select)
        self.links = QLabel('영상 — · Pixhawk — · Sentinel Legacy —'); layout.addWidget(self.links)
        self.telemetry = QLabel('기체 미선택 —'); self.telemetry.setWordWrap(True); layout.addWidget(self.telemetry)
        self.timer = QTimer(self); self.timer.setInterval(33); self.timer.timeout.connect(self.tick); self.timer.start()

    def change_timeout(self, value):
        old = self.session
        self.session = TrackingSession(value)
        old.close()
        self.last_key = None

    def selecting(self):
        self.session.selecting(); self.canvas.selecting = True

    def session_select(self, roi):
        self.session.select(roi)

    def operator_stop(self):
        self.canvas.selecting = False
        self.session.stop()

    def disconnect_source(self):
        self.session.clear_source()
        self.demo = False
        if self.capture:
            self.capture.release(); self.capture = None
        self.last_key = None
        self.canvas.clear()

    def connect_source(self):
        self.disconnect_source()
        if self.source.text().strip():
            self.capture = _SharedCapture.acquire(self.source.text().strip(),self.transport.currentText())

    def start_demo(self):
        self.disconnect_source()
        self.demo = True
        self.demo_session = uuid.uuid4().hex
        self.demo_seq = 0
        self.texture = np.random.default_rng(42).integers(0,256,(90,100,3),np.uint8)

    def simulate(self):
        # Synthetic demonstration needs no vehicle. Live images require fresh telemetry.
        if self.demo or self.pixhawk_fresh(): self.session.simulate()

    def pixhawk_fresh(self):
        stamp = getattr(self.vehicle,'telemetry_received',{}).get('HEARTBEAT')
        return stamp is not None and 0 <= time.monotonic()-stamp < 3

    def vehicle_event(self, *args):
        self.session.revoke_simulation('pixhawk_state_or_command_event')

    def set_vehicle(self, vehicle):
        if vehicle is not self.vehicle:
            self.session.stop('vehicle_changed')
            for signal in ('commandFeedback', 'modeChanged', 'armedChanged', 'connectionStateChanged'):
                if self.vehicle is not None:
                    try:
                        getattr(self.vehicle, signal).disconnect(self.vehicle_event)
                    except (TypeError, RuntimeError, AttributeError):
                        pass
                if vehicle is not None and hasattr(vehicle, signal):
                    getattr(vehicle, signal).connect(self.vehicle_event)
            self.vehicle = vehicle
            self.vehicle_mode = None

    def tick(self):
        if self.closed: return
        frame = self.capture.latest() if self.capture else None
        if self.demo:
            pixels = np.zeros((480,640,3),np.uint8)
            x = 120+int(80*np.sin(self.demo_seq/60))
            pixels[180:270,x:x+100] = self.texture
            self.demo_seq += 1
            frame = FrameEnvelope.create('synthetic',self.demo_session,self.demo_seq,pixels,time.monotonic())
        if frame:
            key = frame.session_id,frame.frame_seq
            if key != self.last_key:
                self.last_key = key
                self.session.offer(frame)
                rgb = cv2.cvtColor(frame.pixels,cv2.COLOR_BGR2RGB)
                self.canvas.set_image(QImage(rgb.data,frame.width,frame.height,rgb.strides[0],QImage.Format_RGB888).copy())
                if self.demo and self.demo_seq == 1:
                    self.session.select((120,180,100,90))
        if self.vehicle is not None:
            if getattr(self.vehicle, '_rc_override_active', False):
                self.session.revoke_simulation('operator_rc_override')
            mode = (getattr(self.vehicle,'last_mode',None),getattr(self.vehicle,'last_armed',None))
            if getattr(self,'vehicle_mode',None) is not None and mode != self.vehicle_mode:
                self.session.revoke_simulation('vehicle_mode_or_armed_changed')
            self.vehicle_mode = mode
        if not self.demo and not self.pixhawk_fresh() and self.session.sink.enabled:
            self.session.revoke_simulation('pixhawk_stale')
        result = self.session.snapshot()
        self.canvas.result = result; self.canvas.update()
        age = time.monotonic()-frame.decoder_received_monotonic if frame else None
        label = 'SIMULATION' if self.demo or self.session.sink.enabled else 'MONITOR ONLY'
        age_text = f'{age*1000:.0f} ms' if age is not None else '—'
        self.status.setText(f'{label} · {result.state} / {result.reason} · UNKNOWN · quality {result.quality:.2f} '
                            f'· 처리 {result.processing_fps:.1f} fps · 프레임 나이 {age_text}\n'
                            f'source {frame.source_id if frame else "—"} / session {frame.session_id if frame else "—"}')
        legacy = getattr(self.parent(),'guide_vehicle',None)
        guide = getattr(legacy,'session',None)
        sentinel = '—' if guide is None else ('동기화' if guide.synchronized else '동기화 대기')
        self.links.setText(f'영상 {"fresh" if age is not None and age < self.session.timeout else "stale / —"} · '
                           f'Pixhawk {"fresh" if self.pixhawk_fresh() else "stale / —"} · Sentinel Legacy {sentinel}')
        self.telemetry.setText(self.telemetry_text())

    def telemetry_text(self):
        v = self.vehicle
        if v is None: return '기체 미선택 —'
        times = getattr(v,'telemetry_received',{})
        samples = getattr(v,'tracking_telemetry',{})
        def group(kind):
            if kind not in times: return '—'
            stale = ' stale' if time.monotonic()-times[kind]>3 else ''
            values = samples.get(kind,{})
            invalid = {'voltage_battery': (0,65535), 'battery_remaining': (-1,), 'satellites_visible': (255,)}
            values = {k: ('—' if value is None or value in invalid.get(k,()) else value) for k,value in values.items()}
            return str(values) + stale
        firmware = {3: 'ArduPilot', 12: 'PX4'}.get(v.autopilot, v.autopilot)
        hb = times.get('HEARTBEAT')
        age = f'{time.monotonic()-hb:.1f}s' if hb else '—'
        return (f'link {v.link.link_id} / sys {v.sysid} / comp {v.compid} · firmware {group("AUTOPILOT_VERSION")} '
                f'· autopilot {firmware} · mode {v.last_mode if hb else "—"} · armed {v.last_armed if hb else "—"} '
                f'· heartbeat {age} {"stale" if not self.pixhawk_fresh() else ""}\n'
                f'자세(rad) {group("ATTITUDE")} · GPS {group("GPS_RAW_INT")} · 배터리(mV/%) {group("SYS_STATUS")}\n'
                f'고도 GLOBAL_POSITION_INT(mm MSL / home-relative) {group("GLOBAL_POSITION_INT")} '
                f'· VFR_HUD(m MSL, AGL 아님) {group("VFR_HUD")}')

    def save(self):
        path,_ = QFileDialog.getSaveFileName(self,'Tracking 로그 저장','tracking.json','JSON (*.json)')
        if path: self.session.save(path)

    def shutdown(self):
        self.timer.stop()
        self.disconnect_source()
        self.set_vehicle(None)
        self.session.close()
        self.closed = True

    def closeEvent(self,event):
        self.disconnect_source()
        self.session.stop('panel_closed')
        event.accept()
