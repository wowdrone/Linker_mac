# video_grid.py
# 다중 영상 채널(기본 4채널) 수신 페이지
#
#  - 같은 소스는 한 번 수신하고 Fly / 다중 화면에 프레임을 공유
#      rtsp://...            RTSP (IP 카메라, SIYI, Herelink, 짐벌 카메라 등)
#      http(s)://...         MJPEG / HLS
#      udp://@:5600          MPEG-TS over UDP
#      rtp-h264://5600       raw RTP H.264 (GStreamer udpsink 계열) → SDP 자동 생성
#      rtp-h265://5600       raw RTP H.265
#      gst-pipeline://...    GStreamer pipeline
#      0, 1 ...              로컬 캡처 장치 (USB 캡처보드 / 웹캠)
#      ~/Movies/file.mp4       파일 재생 (테스트용)
#  - 끊기면 자동 재접속, 항상 최신 프레임만 표시 (지연 최소화)
#  - 채널별 기체 바인딩 → 영상 위에 해당 기체 OSD 표시
#  - 1x1 / 2x2 / 1+3 레이아웃, 더블클릭 시 해당 채널 확대
#  - 스냅샷 / 녹화(mp4)

import os
import tempfile
import threading
import time
import uuid
from tracking_models import FrameEnvelope
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, QTimer, QSettings, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QFrame,
)

from gstreamer_capture import GStreamerCapture
from gui_theme import set_status_tone

try:
    import cv2
except ImportError:  # 실행은 되도록 하고 화면에 안내
    cv2 = None

NUM_CHANNELS = 4
SNAPSHOT_DIR = Path.home() / "Movies" / "LinkerGCS"

# 장치 번호는 macOS/OpenCV가 정하므로 카메라 이름과의 대응을 가정하지 않는다.
VIDEO_SOURCES = (
    ("직접 입력", None),
    ("SIYI IP67 / HDMI", "rtsp://192.168.144.25:8554/main.264"),
    ("SIYI 짐벌 메인", "rtsp://192.168.144.25:8554/video1"),
    ("SIYI 짐벌 서브", "rtsp://192.168.144.25:8554/video2"),
    ("UDP MPEG-TS :5600", "udp://@:5600"),
    ("RTP H.264 :5600", "rtp-h264://5600"),
    ("RTP H.265 :5600", "rtp-h265://5600"),
    *((f"카메라 장치 {i}", str(i)) for i in range(4)),
)

_open_lock = threading.Lock()   # FFmpeg 옵션 환경변수 → open 까지 원자적으로
_capture_pool_lock = threading.Lock()
_capture_pool = {}
_retired_workers = set()


def _source_key(source):
    source = source.strip()
    if source.isdigit():
        return str(int(source))
    if os.path.isfile(source):
        return os.path.normcase(os.path.abspath(source))
    return source


def _make_rtp_sdp(codec: str, port: int) -> str:
    """raw RTP 수신용 SDP 파일 생성 (FFmpeg 는 SDP 없이 RTP 를 못 연다)"""
    enc = "H264" if codec == "h264" else "H265"
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=LinkerGCS\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        f"m=video {port} RTP/AVP 96\r\n"
        f"a=rtpmap:96 {enc}/90000\r\n"
    )
    path = Path(tempfile.gettempdir()) / f"linker_rtp_{codec}_{port}.sdp"
    path.write_text(sdp, encoding="ascii")
    return str(path)


class VideoWorker(QThread):
    """화면별 구독. 수신은 공유하고 녹화/스냅샷은 화면별로 유지한다."""

    frameReady = pyqtSignal(QImage)
    statusChanged = pyqtSignal(str, bool)   # text, ok

    def __init__(self, source: str, transport: str = "tcp", parent=None):
        super().__init__(parent)
        self.source = source.strip()
        self.transport = transport
        self._running = True
        self._recording = False
        self._writer = None
        self.record_path = None
        self.fps = 0.0
        self.size = (0, 0)
        self._snapshot_req = None
        self._latest_image = None
        self._display_timer = None

    def attach_canvas(self, canvas):
        # GUI timer pulls one image; decoder/recording never enqueue frame signals.
        self._display_timer = QTimer(canvas)
        self._display_timer.setInterval(33)
        def display():
            image, self._latest_image = self._latest_image, None
            if image is not None:
                canvas.set_image(image)
        self._display_timer.timeout.connect(display)
        self._display_timer.start()

    def detach_canvas(self):
        if self._display_timer is not None:
            self._display_timer.stop()
            self._display_timer.deleteLater()
            self._display_timer = None
        self._latest_image = None

    # ---- 외부 API ----
    def stop(self):
        self._running = False
        self.wait(3000)

    def request_snapshot(self, path: str):
        self._snapshot_req = path

    def set_recording(self, on: bool, path: str = None):
        if on:
            self.record_path = path
        self._recording = on

    # ---- 내부 ----
    def _open(self):
        src = self.source
        if src.startswith("gst-pipeline://"):
            return GStreamerCapture(src.removeprefix("gst-pipeline://"))
        if src.isdigit():
            cap = cv2.VideoCapture(int(src), cv2.CAP_AVFOUNDATION)
            if not cap.isOpened():
                cap = cv2.VideoCapture(int(src))
            return cap

        if os.path.isfile(src):
            # Network nobuffer flags may discard the first keyframe in recorded files.
            return cv2.VideoCapture(src)

        low = src.lower()
        opts = ["fflags;nobuffer", "flags;low_delay", "max_delay;500000", "stimeout;5000000"]
        if low.startswith("rtsp"):
            opts.insert(0, f"rtsp_transport;{self.transport}")
        if low.startswith(("rtp-h264://", "rtp-h265://")):
            codec = "h264" if low.startswith("rtp-h264") else "h265"
            port = int(src.split("://", 1)[1].strip("/@:") or 5600)
            src = _make_rtp_sdp(codec, port)
            opts.append("protocol_whitelist;file,udp,rtp")

        with _open_lock:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000,
            ])
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def _close_writer(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            self.statusChanged.emit(f"REC saved: {self.record_path}", True)

    def run(self):
        if cv2 is None:
            self.statusChanged.emit("opencv-python 미설치 (pip install opencv-python)", False)
            return

        capture = _SharedCapture.acquire(self.source, self.transport)
        frame_seq = status_seq = -1
        frames, t0 = 0, time.monotonic()
        try:
            while self._running:
                with capture.condition:
                    capture.condition.wait_for(
                        lambda: capture.frame_seq != frame_seq or capture.status_seq != status_seq,
                        timeout=.1,
                    )
                    frame, src_fps = capture.frame, capture.src_fps
                    new_frame_seq, new_status_seq = capture.frame_seq, capture.status_seq
                    status = capture.status
                if not self._running:
                    break
                if new_status_seq != status_seq:
                    status_seq = new_status_seq
                    self.statusChanged.emit(*status)
                if new_frame_seq == frame_seq or frame is None:
                    frame_seq = new_frame_seq
                    continue
                frame_seq = new_frame_seq
                h, w = frame.shape[:2]
                self.size = (w, h)
                self._handle_record(frame, src_fps)
                if self._snapshot_req:
                    cv2.imwrite(self._snapshot_req, frame)
                    self.statusChanged.emit(f"Snapshot: {self._snapshot_req}", True)
                    self._snapshot_req = None

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                if self._display_timer is not None:
                    self._latest_image = img
                else:
                    self.frameReady.emit(img)

                frames += 1
                dt = time.monotonic() - t0
                if dt >= 1.0:
                    self.fps = frames / dt
                    frames, t0 = 0, time.monotonic()
                    self.statusChanged.emit(f"{w}x{h}  {self.fps:.1f} fps", True)

        finally:
            self._close_writer()
            capture.release()
        self.statusChanged.emit("Stopped", False)

    def _handle_record(self, frame, fps):
        if self._recording and self._writer is None and self.record_path:
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(self.record_path, fourcc, min(max(fps, 5.0), 60.0), (w, h))
        if not self._recording and self._writer is not None:
            self._close_writer()
        if self._writer is not None:
            self._writer.write(frame)

class _SharedCapture(threading.Thread):
    """One decoder per source, including exclusive USB and UDP sources.

    Only the latest frame is retained. The first subscriber chooses the RTSP
    transport; subsequent subscribers reuse that connection. A Python thread
    keeps an outstanding native read alive safely when its last view closes.
    """

    @classmethod
    def acquire(cls, source, transport):
        key = _source_key(source)
        with _capture_pool_lock:
            capture = _capture_pool.get(key)
            if capture is None:
                capture = cls(key, source, transport)
                _capture_pool[key] = capture
                capture.users = 1
                capture.start()
            else:
                capture.users += 1
            return capture

    def __init__(self, key, source, transport):
        super().__init__(name="Video capture", daemon=True)
        self.key, self.source, self.transport = key, source, transport
        self.users = 0
        self.condition = threading.Condition()
        self.frame, self.frame_seq = None, 0
        self.envelope = None
        self.session_id = uuid.uuid4().hex
        self.resolution = None
        self.src_fps = 30.0
        self.status, self.status_seq = ("Connecting...", False), 0

    def latest(self):
        """Nonblocking latest-only read. Immutable owned pixels; no queued Qt signals."""
        with self.condition:
            return self.envelope

    def release(self):
        with _capture_pool_lock:
            self.users -= 1

    def _retire_if_unused(self, cap):
        # Release the device before another reader can acquire the same source.
        with _capture_pool_lock:
            if self.users:
                return False
            if cap is not None:
                cap.release()
            _capture_pool.pop(self.key, None)
            return True

    def _status(self, text, ok=False):
        with self.condition:
            self.status = text, ok
            self.status_seq += 1
            self.condition.notify_all()

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with _capture_pool_lock:
                if not self.users:
                    return
            time.sleep(min(.05, max(0, end - time.monotonic())))

    def run(self):
        cap, backoff, fails = None, 1.0, 0
        is_file = os.path.isfile(self.source)
        while True:
            if self._retire_if_unused(cap):
                return
            try:
                if cap is None:
                    self._status("Connecting...")
                    cap = VideoWorker._open(self)
                    if cap is None or not cap.isOpened():
                        if cap is not None:
                            cap.release()
                        cap = None
                        self._status(f"Open failed, retry in {backoff:.0f}s")
                        self._sleep(backoff)
                        backoff = min(backoff * 2, 8.0)
                        continue
                    self.src_fps = 30.0 if isinstance(cap, GStreamerCapture) else cap.get(cv2.CAP_PROP_FPS) or 30.0
                    backoff, fails = 1.0, 0
                    self.session_id = uuid.uuid4().hex
                    self.resolution = None
                    self._status("Connected", True)
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        self.session_id = uuid.uuid4().hex
                    if fails > 30:
                        cap.release()
                        cap = None
                        self._status("Stream lost, reconnecting...")
                        self._sleep(1.0)
                    else:
                        self._sleep(.02)
                    continue
                received = time.monotonic()
                fails = 0
                with self.condition:
                    if isinstance(cap, GStreamerCapture):
                        self.src_fps = cap.fps or 30.0
                    if self.resolution is not None and self.resolution != frame.shape:
                        self.session_id = uuid.uuid4().hex
                    self.resolution = frame.shape
                    self.frame_seq += 1
                    self.envelope = FrameEnvelope.create(self.key, self.session_id, self.frame_seq, frame, received)
                    self.frame = self.envelope.pixels
                    self.condition.notify_all()
                if is_file:
                    self._sleep(1.0 / max(self.src_fps, 1.0))
            except Exception as exc:
                if cap is not None:
                    cap.release()
                    cap = None
                self._status(f"Stream error: {type(exc).__name__}")
                self._sleep(1.0)


class VideoCanvas(QWidget):
    """최신 프레임 + OSD 그리기"""

    doubleClicked = pyqtSignal()

    def __init__(self, channel: int, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.image = None
        self.frame_at = 0.0
        self.osd_lines = []
        self.osd_color = QColor("#00ffea")
        self.message = "No stream"
        self.zoom = 1.0
        self.setToolTip("마우스 휠 또는 − / + 버튼으로 영상 확대·축소")
        self.zoom_controls = QWidget(self)
        controls = QHBoxLayout(self.zoom_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_reset = QPushButton("1.0×")
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_out.setToolTip("영상 축소")
        self.btn_zoom_reset.setToolTip("원래 배율로 복원")
        self.btn_zoom_in.setToolTip("영상 확대")
        for button in (self.btn_zoom_out, self.btn_zoom_reset, self.btn_zoom_in):
            button.setAccessibleName(button.toolTip())
            controls.addWidget(button)
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_in.setFixedWidth(28)
        self.btn_zoom_reset.setFixedWidth(60)
        self.btn_zoom_out.clicked.connect(lambda: self.set_zoom(self.zoom / 1.25))
        self.btn_zoom_reset.clicked.connect(lambda: self.set_zoom(1.0))
        self.btn_zoom_in.clicked.connect(lambda: self.set_zoom(self.zoom * 1.25))
        self.set_zoom(1.0)
        self.setMinimumSize(144, 81)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def set_image(self, img: QImage):
        self.image = img
        self.frame_at = time.monotonic()
        self.update()

    def clear(self, message="No stream"):
        self.image = None
        self.message = message
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()

    def set_zoom(self, zoom):
        self.zoom = min(8.0, max(1.0, zoom))
        self.btn_zoom_reset.setText(f"{self.zoom:.1f}×")
        self.btn_zoom_out.setEnabled(self.zoom > 1.0)
        self.btn_zoom_in.setEnabled(self.zoom < 8.0)
        self._position_zoom_controls()
        self.update()

    def _position_zoom_controls(self):
        self.zoom_controls.adjustSize()
        self.zoom_controls.move(max(0, self.width() - self.zoom_controls.width() - 6),
                                max(0, self.height() - self.zoom_controls.height() - 6))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_zoom_controls()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.set_zoom(self.zoom * (1.25 if delta > 0 else 0.8))
            event.accept()
        else:
            event.ignore()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self.image is not None and not self.image.isNull():
            scaled = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
            x = (self.width() - scaled.width()) / 2
            y = (self.height() - scaled.height()) / 2
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            source_width = self.image.width() / self.zoom
            source_height = self.image.height() / self.zoom
            source = QRectF((self.image.width() - source_width) / 2,
                            (self.image.height() - source_height) / 2,
                            source_width, source_height)
            p.drawImage(QRectF(x, y, scaled.width(), scaled.height()), self.image, source)
        else:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, f"CH{self.channel + 1}\n{self.message}")

        # OSD
        p.setFont(QFont("Menlo", 10, QFont.Bold))
        fm = p.fontMetrics()
        y = 6
        for line in self.osd_lines:
            w = fm.horizontalAdvance(line) + 10
            p.fillRect(6, y, w, fm.height() + 2, QColor(0, 0, 0, 150))
            p.setPen(self.osd_color)
            p.drawText(11, y + fm.ascent() + 1, line)
            y += fm.height() + 3

        # 채널 번호 (우상단)
        tag = f"CH{self.channel + 1}"
        w = fm.horizontalAdvance(tag) + 10
        p.fillRect(self.width() - w - 6, 6, w, fm.height() + 2, QColor(0, 0, 0, 150))
        p.setPen(QColor("#ffffff"))
        p.drawText(self.width() - w - 1, 6 + fm.ascent() + 1, tag)
        p.end()


class VideoTile(QFrame):
    """채널 1개: 컨트롤 바 + 캔버스"""

    maximizeRequested = pyqtSignal(int)

    def __init__(self, channel: int, fleet, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.fleet = fleet
        self.worker = None
        self.recording = False
        self.setObjectName("videoTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.lbl_ch = QLabel(f"CH{channel + 1}")
        self.lbl_ch.setProperty("role", "title")
        self.edit_url = QLineEdit()
        self.edit_url.setPlaceholderText("rtsp://192.168.144.25:8554/main.264  |  udp://@:5600  |  rtp-h264://5600  |  0")
        self.cmb_source = QComboBox()
        self.cmb_source.setToolTip("영상 소스를 선택하거나 주소를 직접 입력하세요")
        for label, source in VIDEO_SOURCES:
            self.cmb_source.addItem(label, source)
        self.cmb_transport = QComboBox()
        self.cmb_transport.addItems(["tcp", "udp"])
        self.cmb_transport.setToolTip("RTSP transport")
        self.cmb_vehicle = QComboBox()
        self.cmb_vehicle.setToolTip("영상에 OSD 를 표시할 기체")
        self.cmb_vehicle.setMinimumWidth(80)
        self.cmb_vehicle.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_vehicle.setMinimumContentsLength(6)
        self.btn_start = QPushButton("▶")
        self.btn_start.setToolTip("Start / Stop")
        self.btn_snap = QPushButton("📷")
        self.btn_snap.setToolTip("Snapshot")
        self.btn_rec = QPushButton("REC")
        self.btn_rec.setCheckable(True)
        self.btn_rec.setProperty("role", "record")
        self.btn_max = QPushButton("⛶")
        self.btn_max.setToolTip("확대 / 복귀 (더블클릭)")

        bar.addWidget(self.lbl_ch)
        bar.addWidget(self.cmb_source)
        bar.addWidget(self.edit_url, 1)
        bar.addWidget(self.cmb_transport)
        lay.addLayout(bar)

        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addWidget(self.cmb_vehicle)
        controls.addStretch()
        for button in (self.btn_start, self.btn_snap, self.btn_rec, self.btn_max):
            controls.addWidget(button)
        lay.addLayout(controls)

        self.canvas = VideoCanvas(channel)
        lay.addWidget(self.canvas, 1)

        self.lbl_status = QLabel("Idle")
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_status.setProperty("role", "caption")
        self.lbl_status.setProperty("statusTone", "neutral")
        lay.addWidget(self.lbl_status)

        self.btn_start.clicked.connect(self.toggle)
        self.btn_snap.clicked.connect(self.snapshot)
        self.btn_rec.toggled.connect(self.set_recording)
        self.btn_max.clicked.connect(lambda: self.maximizeRequested.emit(self.channel))
        self.canvas.doubleClicked.connect(lambda: self.maximizeRequested.emit(self.channel))
        self.edit_url.returnPressed.connect(self.start)
        self.cmb_source.activated.connect(self._select_source)
        self.edit_url.textChanged.connect(self._sync_source)
        self.cmb_vehicle.currentIndexChanged.connect(lambda _i: self.refresh_osd())

        self.refresh_vehicles()

    def _select_source(self, index):
        source = self.cmb_source.itemData(index)
        if source is not None:
            self.edit_url.setText(source)
        else:
            self.edit_url.setFocus()

    def _sync_source(self, text):
        index = self.cmb_source.findData(text.strip()) if text.strip() else 0
        self.cmb_source.setCurrentIndex(max(index, 0))

    # ---- 기체 바인딩 ----
    def refresh_vehicles(self):
        cur = self.cmb_vehicle.currentData()
        self.cmb_vehicle.blockSignals(True)
        self.cmb_vehicle.clear()
        self.cmb_vehicle.addItem("No OSD", None)
        for v in self.fleet.vehicle_list():
            self.cmb_vehicle.addItem(v.name, v.key_str)
        idx = self.cmb_vehicle.findData(cur)
        self.cmb_vehicle.setCurrentIndex(max(idx, 0))
        self.cmb_vehicle.blockSignals(False)

    def bind_vehicle_key(self, key):
        idx = self.cmb_vehicle.findData(key)
        if idx >= 0:
            self.cmb_vehicle.setCurrentIndex(idx)

    def bound_vehicle(self):
        return self.fleet.vehicle_by_str(self.cmb_vehicle.currentData())

    def refresh_osd(self):
        v = self.bound_vehicle()
        if v is None:
            self.canvas.osd_lines = []
        else:
            s = v.summary()
            batt = "-" if s["batt_v"] is None else f"{s['batt_v']:.1f}V"
            if s["batt_p"] is not None:
                batt += f" {s['batt_p']:.0f}%"
            state = "ARMED" if s["armed"] else "DISARMED"
            if not s["connected"]:
                state = "LINK LOST"
            self.canvas.osd_lines = [
                f"{v.name}  {s['mode']}  {state}",
                f"ALT {s['alt']:.1f}m  GS {s['gs']:.1f}m/s  HDG {s['heading']:.0f}°",
                f"BAT {batt}  GPS {s['gps']}",
            ]
            self.canvas.osd_color = QColor(v.color)
        self.canvas.update()

    # ---- 수신 ----
    def toggle(self):
        if self.worker is not None:
            self.stop()
        else:
            self.start()

    def start(self):
        url = self.edit_url.text().strip()
        if not url:
            self.canvas.clear("URL 을 입력하세요")
            return
        self.stop()
        self.worker = VideoWorker(url, self.cmb_transport.currentText())
        self.worker.attach_canvas(self.canvas)
        self.worker.statusChanged.connect(self._on_status)
        self.worker.start()
        self.btn_start.setText("■")

    def stop(self):
        if self.worker is not None:
            w, self.worker = self.worker, None
            w.detach_canvas()
            _retired_workers.add(w)
            def finished(worker=w):
                _retired_workers.discard(worker)
                worker.deleteLater()
            w.finished.connect(finished)
            w.stop()
            if not w.isRunning():
                finished()
        self.btn_rec.blockSignals(True)
        self.btn_rec.setChecked(False)
        self.btn_rec.blockSignals(False)
        self.btn_start.setText("▶")
        self.canvas.clear("Stopped")
        self._set_status_text("Idle")
        set_status_tone(self.lbl_status, "neutral")

    def _set_status_text(self, text):
        self.lbl_status.setText(text)
        self.lbl_status.setToolTip(text)

    def _on_status(self, text, ok):
        self._set_status_text(text)
        set_status_tone(self.lbl_status, "success" if ok else "warning")
        if not ok and self.canvas.image is None:
            self.canvas.message = text
            self.canvas.update()

    def _out_path(self, ext):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(SNAPSHOT_DIR / f"CH{self.channel + 1}_{ts}.{ext}")

    def snapshot(self):
        if self.worker is None:
            return
        self.worker.request_snapshot(self._out_path("jpg"))

    def set_recording(self, on):
        if self.worker is None:
            self.btn_rec.blockSignals(True)
            self.btn_rec.setChecked(False)
            self.btn_rec.blockSignals(False)
            return
        self.worker.set_recording(on, self._out_path("mp4") if on else None)
        if on:
            self._set_status_text(f"● REC {self.worker.record_path}")

    # ---- 설정 저장 ----
    def save(self, st: QSettings):
        st.setValue(f"video/ch{self.channel}/url", self.edit_url.text())
        st.setValue(f"video/ch{self.channel}/transport", self.cmb_transport.currentText())
        st.setValue(f"video/ch{self.channel}/running", self.worker is not None)

    def load(self, st: QSettings):
        self.edit_url.setText(st.value(f"video/ch{self.channel}/url", "", str))
        self.cmb_transport.setCurrentText(st.value(f"video/ch{self.channel}/transport", "tcp", str))
        return st.value(f"video/ch{self.channel}/running", False, bool)


class VideoGridPage(QWidget):
    """4채널 영상 페이지"""

    def __init__(self, fleet, num_channels: int = NUM_CHANNELS, parent=None):
        super().__init__(parent)
        self.setObjectName("page_video")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.fleet = fleet
        self.settings = QSettings("WowDrone", "LinkerGCS")
        self.layout_mode = "2x2"
        self.maximized = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        top = QHBoxLayout()
        lbl = QLabel("Layout:")
        lbl.setProperty("role", "title")
        top.addWidget(lbl)
        self.layout_buttons = {}
        for mode in ("1x1", "2x2", "1+3"):
            b = QPushButton(mode)
            b.setCheckable(True)
            b.clicked.connect(lambda _c, m=mode: self.set_layout(m))
            top.addWidget(b)
            self.layout_buttons[mode] = b

        top.addSpacing(16)
        self.btn_start_all = QPushButton("Start All")
        self.btn_stop_all = QPushButton("Stop All")
        self.btn_auto_bind = QPushButton("Auto Bind (CHn ↔ 기체 n)")
        for b in (self.btn_start_all, self.btn_stop_all, self.btn_auto_bind):
            top.addWidget(b)
        self.lbl_dir = QLabel(f"Save: {SNAPSHOT_DIR}")
        self.lbl_dir.setProperty("role", "caption")
        self.lbl_dir.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_dir.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_dir.setToolTip(self.lbl_dir.text())
        top.addWidget(self.lbl_dir, 1)
        root.addLayout(top)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        root.addWidget(self.grid_host, 1)

        self.tiles = []
        for ch in range(num_channels):
            t = VideoTile(ch, fleet, self.grid_host)
            t.maximizeRequested.connect(self.toggle_maximize)
            self.tiles.append(t)

        self.btn_start_all.clicked.connect(lambda: [t.start() for t in self.tiles if t.edit_url.text().strip()])
        self.btn_stop_all.clicked.connect(lambda: [t.stop() for t in self.tiles])
        self.btn_auto_bind.clicked.connect(self.auto_bind)

        fleet.vehicleAdded.connect(lambda _v: self._refresh_vehicle_lists())
        fleet.vehicleRemoved.connect(lambda _v: self._refresh_vehicle_lists())

        self._osd_timer = QTimer(self)
        self._osd_timer.setInterval(200)
        self._osd_timer.timeout.connect(self._refresh_osd)
        self._osd_timer.start()

        self._load_settings()

    def _refresh_vehicle_lists(self):
        for t in self.tiles:
            t.refresh_vehicles()

    def _refresh_osd(self):
        if not self.isVisible():
            return
        for t in self.tiles:
            if t.isVisible():
                t.refresh_osd()

    def auto_bind(self):
        vehicles = self.fleet.vehicle_list()
        for t, v in zip(self.tiles, vehicles):
            t.bind_vehicle_key(v.key_str)

    # ---- 레이아웃 ----
    def set_layout(self, mode):
        self.layout_mode = mode
        self.maximized = None
        for m, b in self.layout_buttons.items():
            b.setChecked(m == mode)
        self._apply_layout()

    def toggle_maximize(self, ch):
        self.maximized = None if self.maximized == ch else ch
        self._apply_layout()

    def _apply_layout(self):
        for t in self.tiles:
            self.grid.removeWidget(t)
            t.hide()
        for i in range(4):
            self.grid.setRowStretch(i, 0)
            self.grid.setColumnStretch(i, 0)

        if self.maximized is not None:
            placements = [(self.maximized, 0, 0, 1, 1)]
        elif self.layout_mode == "1x1":
            placements = [(0, 0, 0, 1, 1)]
        elif self.layout_mode == "1+3":
            # 큰 화면 1 + 오른쪽 작은 화면 3
            placements = [(0, 0, 0, 3, 3)] + [
                (i, i - 1, 3, 1, 1) for i in range(1, min(4, len(self.tiles)))
            ]
        else:
            placements = [(i, i // 2, i % 2, 1, 1) for i in range(min(4, len(self.tiles)))]

        for ch, r, c, rs, cs in placements:
            t = self.tiles[ch]
            self.grid.addWidget(t, r, c, rs, cs)
            t.show()
        rows = max(r + rs for _, r, _, rs, _ in placements)
        cols = max(c + cs for _, _, c, _, cs in placements)
        for i in range(rows):
            self.grid.setRowStretch(i, 1)
        for i in range(cols):
            self.grid.setColumnStretch(i, 1)

    # ---- 설정 ----
    def _load_settings(self):
        autostart = [t.load(self.settings) for t in self.tiles]
        self.set_layout(self.settings.value("video/layout", "2x2", str))
        for t, run in zip(self.tiles, autostart):
            if run and t.edit_url.text().strip():
                QTimer.singleShot(500, t.start)

    def save_settings(self):
        for t in self.tiles:
            t.save(self.settings)
        self.settings.setValue("video/layout", self.layout_mode)
        self.settings.sync()

    def shutdown(self):
        self.save_settings()
        for t in self.tiles:
            t.stop()
