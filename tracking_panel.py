"""Monitor-only Qt view over the shared TrackingService.

The PyQt Tracking window and the browser dashboard show the same TrackingService (one
TrackingSession, one shared decoder). Vehicle objects stay read only; the only vehicle
request is an explicitly confirmed flight mode change through the service.

Readability: large bold text, high contrast, one value per label. State colours are shared
with the browser: TRACKING green, SELECTING yellow, LOST/ERROR red, SIMULATION purple.
"""
import time
import cv2
from PyQt5.QtCore import Qt, QSettings, QRectF, QPointF, QUrl, pyqtSignal
from PyQt5.QtGui import QImage, QPainter, QColor, QPen, QFont, QDesktopServices
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                             QPushButton, QComboBox, QDoubleSpinBox, QFileDialog, QSplitter,
                             QScrollArea, QWidget, QGroupBox, QTabWidget, QListWidget, QListWidgetItem,
                             QMessageBox, QSizePolicy)
from video_grid import VideoCanvas, VIDEO_SOURCES
from tracking_service import TrackingService, reason_text
from tracking_session import SELECTION_WINDOW_RANGE
from flight_mode_controller import ModeRequestError
from gui_help import help_button, set_guide

GREEN, YELLOW, RED, PURPLE, BLUE, ORANGE, MUTED = ('#3ee59a', '#ffd23f', '#ff5c7a', '#c9a2ff', '#8fd0ff',
                                                  '#ffae4d', '#c9d1de')
STATE_COLOR = {'TRACKING': GREEN, 'SELECTING': YELLOW, 'LOST': RED, 'ERROR': RED, 'STOPPED': BLUE, 'IDLE': BLUE}
LEVEL_COLOR = {'ok': GREEN, 'info': BLUE, 'warn': YELLOW, 'danger': RED}
DASH = '—'

# On-demand guides (UI guide principle 2026-11-22): usage text lives here, not on screen.
GUIDES = {
    '영상 소스': [
        '프리셋은 제품별 예시 주소입니다. 실제 HM30 주소라는 뜻이 아닙니다.',
        '「로컬 중계(권장)」는 이 Mac에서 실행한 중계 서버(rtsp://127.0.0.1:8554/cam)입니다. 중계는 '
        'tools/video_relay/video_relay_start.command로 직접 켭니다. Linker는 중계를 자동 실행하지 않습니다.',
        '중계 서버가 꺼져 있으면 「로컬 중계 서버 응답 없음」으로 표시됩니다.',
        'tcp/udp는 RTSP 전송 방식에만 적용됩니다. 카메라 장치·파일에는 적용되지 않습니다.',
        '기체 고정·비짐벌 / 지상 고정: 두 모드 모두 사용자 ROI 관찰만 하며 자동 검출·배경 보정은 없습니다.',
        '같은 카메라에 Linker가 직접 연결을 2개 이상 열면 경고합니다(무선 링크 부하 증가). Linker 밖의 연결(예: Sentinel)은 '
        '셀 수 없습니다.',
        '디코딩 밀림 경고: 실제 수신 FPS가 소스 FPS의 80% 미만으로 5초 이상 지속되거나 프레임이 몰려 들어오면 표시합니다 '
        '(QSettings video/decode_health/*로 조정).',
        '영상 연결 → ROI 선택 → 고정된 영상 위를 드래그하는 순서로 사용합니다.',
    ],
    '추적': [
        '품질은 특징점 잔존·이동 일관성 지표이며 객체 확률이 아닙니다.',
        '오차는 영상 기준(오른쪽·아래 +)이며 각도·거리가 아닙니다.',
        'ROI는 무늬가 뚜렷한 곳(모서리·글자·경계)을 선택하세요. 민무늬 영역은 「ROI 텍스처 부족」으로 거부됩니다. '
        'ROI는 8×8 픽셀 이상, 영상 안쪽이어야 합니다.',
        '선택 시간: ROI 선택 버튼을 누른 뒤 고정 프레임에 그릴 수 있는 시간입니다(기본 8초, 2~20초).',
        '고정 후 4초 안에 그리면 그 사이 프레임을 따라잡아 최신 프레임까지 추적합니다. 4초가 지나거나 따라잡기에 실패하면 '
        '최신 프레임이 다시 고정되며, 그 프레임에서 다시 그리세요. 다른 객체로 자동 전환하지 않습니다.',
        '지연 한도: 이 시간보다 오래된 프레임·처리 결과는 LOST로 처리합니다(HM30 실측 후 조정 필요).',
        'LOST는 새 영상이 들어와도 자동 재개되지 않습니다. ROI를 다시 선택해야 합니다.',
        '조종자 개입은 추적과 모의 출력을 즉시 정지합니다. 모의 출력은 메모리 기록일 뿐 비행 명령이 아닙니다.',
        '로그 저장: 추적 이벤트, 모의 출력, 비행 모드 요청 감사 기록을 하나의 JSON으로 저장합니다.',
    ],
    '비행 모드': [
        'ARM·이륙·미션 시작을 하지 않으며 자동 재시도하지 않습니다. 결과는 기체 HEARTBEAT로 확인합니다.',
        '확인 창에서 「요청 1회 송신」을 눌러야 송신합니다. 송신 직전 기체·연결·HEARTBEAT·현재 모드·시동 상태를 다시 확인합니다.',
        '(비활성) 모드는 이 창에서 요청할 수 없습니다. 항목에 마우스를 올리면 이유가 보입니다.',
        '한 번에 하나의 요청만 유효합니다. 브라우저에서 새로 요청하면 이 창의 확인 요청은 무효가 됩니다.',
    ],
    '선택 기체': [
        '읽기 전용 표시입니다. 기체 선택은 메인 창 상단 기체 목록 또는 Fleet에서 합니다.',
        '미수신 값은 —, 3초보다 오래된 값은 STALE로 표시합니다.',
        '고도: relative_alt는 홈 기준 상대고도이며 AGL이 아닙니다. VFR_HUD alt는 MSL입니다.',
    ],
    '브라우저 대시보드': [
        '「웹 모드변경 ON」: 대시보드가 켜져 있는 동안 브라우저에서도 비행 모드 변경을 요청할 수 있습니다. '
        'ARM·이륙·미션·RC·파라미터 변경은 브라우저에서 할 수 없습니다.',
        '브라우저는 이 창과 같은 추적 세션·영상을 공유합니다. 브라우저 영상은 JPEG 스냅샷(최대 약 10장/초)이라 이 창보다 늦습니다.',
        '대시보드는 127.0.0.1 전용이지만 같은 Mac의 다른 프로그램은 접근할 수 있습니다. 필요할 때만 열고 사용 후 '
        '「대시보드 닫기」로 끄세요.',
        '기본 포트 8766이 사용 중이면 임의 포트로 열리며 실제 주소를 아래에 표시합니다.',
        '이 창을 닫을 때 대시보드가 열려 있으면 「창만 닫기 / 대시보드도 닫기」를 묻습니다.',
    ],
}
ALL_GUIDES = [f'<b style="color:#ffd23f">{title}</b><br>' + '<br>'.join(f'• {line}' for line in lines)
              for title, lines in GUIDES.items()]

STYLE = """
QDialog#trackingWindow { background:#0e1118; }
QDialog#trackingWindow QLabel { color:#f5f7fb; font-size:16px; }
QDialog#trackingWindow QLabel[role="key"] { color:#c9d1de; font-weight:600; }
QDialog#trackingWindow QLabel[role="value"] { font-weight:800; font-family:Menlo; font-size:16px; }
QDialog#trackingWindow QGroupBox { border:2px solid #3a4352; border-radius:10px; margin-top:6px;
    padding:8px 10px 10px 10px; background:#171b24; }
QDialog#trackingWindow QLabel[role="groupTitle"] { color:#ffd23f; font-size:19px; font-weight:900; }
QDialog#trackingWindow QGroupBox::title { subcontrol-origin:margin; subcontrol-position:top left; left:10px;
    top:2px; padding:0 4px; color:#ffd23f; background:transparent; }
QDialog#trackingWindow QPushButton { font-size:16px; font-weight:800; min-height:38px; padding:4px 12px;
    color:#f5f7fb; background:#232a36; border:2px solid #4a5568; border-radius:8px; }
QDialog#trackingWindow QPushButton:hover { border-color:#ffd23f; }
QDialog#trackingWindow QPushButton:disabled { color:#6b7484; border-color:#2c3340; }
QDialog#trackingWindow QPushButton[kind="primary"] { background:#ffd23f; color:#17140a; border-color:#ffd23f; }
QDialog#trackingWindow QPushButton[kind="danger"] { background:#5a1424; border-color:#ff5c7a; }
QDialog#trackingWindow QPushButton[kind="sim"] { border-color:#c9a2ff; color:#c9a2ff; }
QDialog#trackingWindow QLineEdit, QDialog#trackingWindow QComboBox, QDialog#trackingWindow QDoubleSpinBox {
    font-size:16px; font-weight:700; min-height:34px; color:#f5f7fb; background:#131821;
    border:2px solid #4a5568; border-radius:7px; padding:2px 8px; }
QDialog#trackingWindow QComboBox:disabled { color:#6b7484; }
QDialog#trackingWindow QListWidget { font-size:15px; background:#131821; color:#f5f7fb; border:1px solid #3a4352; }
QDialog#trackingWindow QTabBar::tab { font-size:15px; font-weight:800; padding:7px 16px; color:#c9d1de; }
QDialog#trackingWindow QTabBar::tab:selected { color:#ffd23f; }
"""


def image_geometry(iw, ih, ww, wh, zoom):
    # Match VideoCanvas QSize.scaled's integer KeepAspectRatio rectangle.
    from PyQt5.QtCore import QSize
    size = QSize(iw, ih).scaled(QSize(ww, wh), Qt.KeepAspectRatio)
    return ((ww-size.width())/2, (wh-size.height())/2, size.width(), size.height(),
            (iw-iw/zoom)/2, (ih-ih/zoom)/2, iw/zoom, ih/zoom)


def image_point(point, geometry):
    x, y, w, h, sx, sy, sw, sh = geometry
    px, py = point
    if w <= 0 or h <= 0 or not x <= px <= x+w or not y <= py <= y+h:
        return None
    return sx+(px-x)*sw/w, sy+(py-y)*sh/h


def chip_style(color, filled=False):
    if filled:
        return (f'QLabel{{background:{color};color:#111;font-size:15px;font-weight:900;'
                f'border:2px solid {color};border-radius:13px;padding:3px 10px;}}')
    return (f'QLabel{{background:#131821;color:{color};font-size:15px;font-weight:800;'
            f'border:2px solid {color};border-radius:13px;padding:3px 10px;}}')


class TrackingCanvas(VideoCanvas):
    roiSelected = pyqtSignal(tuple)

    def __init__(self, parent=None):
        super().__init__(0, parent)
        self.selecting = False
        self.anchor = self.drag = None
        self.result = None
        self.countdown = None
        self.monitor = 'MONITOR ONLY'
        # Keep the inherited zoom buttons compact even under the large-button window style.
        self.zoom_controls.setStyleSheet('QPushButton{min-height:26px;padding:0;font-size:15px;font-weight:900;'
                                         'color:#fff;background:#232a36;border:1px solid #6b7484;border-radius:6px;}')

    def geometry_map(self):
        if self.image is None:
            return None
        return image_geometry(self.image.width(), self.image.height(), self.width(), self.height(), self.zoom)

    def mousePressEvent(self, event):
        g = self.geometry_map()
        if self.selecting and event.button() == Qt.LeftButton and g:
            self.anchor = image_point((event.x(), event.y()), g)
            self.drag = self.anchor

    def mouseMoveEvent(self, event):
        if self.anchor and self.geometry_map():
            self.drag = image_point((event.x(), event.y()), self.geometry_map())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor and self.geometry_map():
            end = image_point((event.x(), event.y()), self.geometry_map())
            if end:
                a, b = self.anchor, end
                self.roiSelected.emit((min(a[0], b[0]), min(a[1], b[1]), abs(a[0]-b[0]), abs(a[1]-b[1])))
                self.selecting = False
        self.anchor = self.drag = None
        self.update()

    def _label(self, p, x, y, text, color, size=16):
        p.setFont(QFont(self.font().family(), size, QFont.Black))
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + 14
        p.fillRect(QRectF(x, y, w, fm.height() + 6), QColor(0, 0, 0, 200))
        p.setPen(QColor(color))
        p.drawText(QPointF(x + 7, y + 3 + fm.ascent()), text)

    def paintEvent(self, event):
        super().paintEvent(event)
        g = self.geometry_map()
        if not g:
            return
        x, y, w, h, sx, sy, sw, sh = g

        def point(a, b):
            return QPointF(x+(a-sx)*w/sw, y+(b-sy)*h/sh)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setClipRect(QRectF(x, y, w, h))
        p.setPen(QPen(QColor(255, 255, 255, 140), 1.5, Qt.DashLine))
        p.drawLine(QPointF(x+w/2, y), QPointF(x+w/2, y+h))
        p.drawLine(QPointF(x, y+h/2), QPointF(x+w, y+h/2))
        r = self.result
        if r and r.bbox:
            bx, by, bw, bh = r.bbox
            rect = QRectF(point(bx, by), point(bx+bw, by+bh))
            p.setPen(QPen(QColor('#000'), 6)); p.drawRect(rect)
            p.setPen(QPen(QColor(GREEN), 3.5)); p.drawRect(rect)
            p.setPen(QPen(QColor(255, 210, 63, 200), 2.5))
            for a, b in zip(r.trail, r.trail[1:]):
                p.drawLine(point(*a), point(*b))
            cx, cy = r.center
            p.setPen(QPen(QColor(YELLOW), 2.5))
            p.drawLine(point(sx + sw/2, sy + sh/2), point(cx, cy))
            self._label(p, rect.left(), max(y + 2, rect.top() - 32), f'TRACKING · 품질 {r.quality:.2f}', GREEN)
        if self.anchor and self.drag:
            p.setPen(QPen(QColor(YELLOW), 3, Qt.DashLine))
            p.drawRect(QRectF(point(*self.anchor), point(*self.drag)).normalized())
        if self.countdown:
            self._label(p, x + 12, y + 12, self.countdown, YELLOW, 22)
        color = PURPLE if self.monitor == 'SIMULATION' else YELLOW
        self._label(p, x + 12, y + h - 44, self.monitor, color)   # bottom-left: zoom buttons sit bottom-right
        p.end()


class TrackingPanel(QDialog):
    """PyQt Tracking window. All state lives in TrackingService (shared with the browser)."""

    def __init__(self, parent=None, service=None):
        super().__init__(parent)
        self.setObjectName('trackingWindow')
        self.setStyleSheet(STYLE)
        self.setWindowTitle('Linker Tracking')
        self.resize(1440, 900)
        self.owns_service = service is None
        self.service = service or TrackingService(self)
        self._image_key = None
        self._pinned_ref = None
        self._mode_key = None
        self._event_key = None
        self.closed = False
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ---- status chips ----
        chips = QHBoxLayout()
        self.chips = {}
        for name in ('monitor', 'app', 'video', 'vehicle', 'heartbeat', 'mode', 'armed', 'tracking', 'sentinel',
                     'webmode'):
            label = QLabel(DASH)
            self.chips[name] = label
            chips.addWidget(label)
        chips.addStretch(1)
        self.guide_button = help_button(self, 'Linker Tracking', ALL_GUIDES)
        chips.addWidget(self.guide_button)
        set_guide(self, 'Linker Tracking', ALL_GUIDES)
        root.addLayout(chips)
        self.banner = QLabel('상태 확인 중')
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)
        self.video_warn = QLabel('')
        self.video_warn.setWordWrap(True)
        self.video_warn.setStyleSheet(f'QLabel{{color:{ORANGE};font-size:18px;font-weight:900;border:2px solid {ORANGE};'
                                      f'border-radius:8px;padding:6px 10px;}}')
        self.video_warn.hide()
        root.addWidget(self.video_warn)
        # ---- browser dashboard bar (placed at the bottom of the window) ----
        web = QHBoxLayout()
        self.web = QLabel('브라우저 대시보드 꺼짐')
        self.web.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.web.setWordWrap(True)
        self.web_open = self._button('브라우저 대시보드 열기', self.open_browser)
        self.web_close = self._button('대시보드 닫기', self.close_browser, 'danger')
        web.addWidget(self.web, 1); web.addWidget(self.web_open); web.addWidget(self.web_close)
        web.addWidget(help_button(self, '브라우저 대시보드', GUIDES['브라우저 대시보드']))

        # ---- body: video | controls ----
        split = QSplitter(Qt.Horizontal)
        self.canvas = TrackingCanvas(self)
        self.canvas.roiSelected.connect(self.session_select)
        split.addWidget(self.canvas)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.addWidget(self._source_box())
        side_layout.addWidget(self._tracking_box())
        side_layout.addWidget(self._mode_box())
        side_layout.addWidget(self._vehicle_box())
        side_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(440)
        split.addWidget(scroll)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([960, 500])          # video is the main work area
        bottom = QTabWidget()
        self.events = QListWidget()
        self.diag = QLabel(DASH)
        self.diag.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.diag.setWordWrap(True)
        self.diag.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        bottom.addTab(self.events, '이벤트')
        diag_scroll = QScrollArea(); diag_scroll.setWidget(self.diag); diag_scroll.setWidgetResizable(True)
        bottom.addTab(diag_scroll, '진단')
        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(split)
        vsplit.addWidget(bottom)
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        root.addWidget(vsplit, 1)
        root.addLayout(web)
        # The service owns the 33 ms tick; this view only redraws when it changes.
        self.service.changed.connect(self.refresh)
        self.refresh()

    # ---- construction helpers ----
    def _button(self, text, slot, kind=None):
        button = QPushButton(text)
        if kind:
            button.setProperty('kind', kind)
        button.clicked.connect(slot)
        return button

    def _box(self, title):
        """Group box with a short title and a "?" button opening GUIDES[title]."""
        box = QGroupBox()
        box.setObjectName('trackingGroup')
        box.setAccessibleName(title)
        layout = QVBoxLayout(box)
        head = QHBoxLayout()
        label = QLabel(title)
        label.setProperty('role', 'groupTitle')
        head.addWidget(label)
        head.addWidget(help_button(self, title, GUIDES[title]))
        head.addStretch(1)
        layout.addLayout(head)
        return box, layout

    @staticmethod
    def _grid(rows):
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        labels = {}
        for i, (key, title) in enumerate(rows):
            k = QLabel(title); k.setProperty('role', 'key')
            v = QLabel(DASH); v.setProperty('role', 'value'); v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(k, i, 0, Qt.AlignTop)
            grid.addWidget(v, i, 1)
            labels[key] = v
        grid.setColumnStretch(1, 1)
        return grid, labels

    def _source_box(self):
        box, layout = self._box('영상 소스')
        settings = QSettings('WowDrone', 'LinkerGCS')
        self.sources = QComboBox()
        for title, value in VIDEO_SOURCES:
            self.sources.addItem(title, value)
        self.sources.setToolTip('프리셋 (예시 주소)')
        self.source = QLineEdit()
        self.source.setText(settings.value('video/fly/url', '', str) or settings.value('video/ch0/url', '', str))
        self.source.setPlaceholderText('RTSP 주소 / 파일 경로 / 장치 번호')
        self.sources.currentIndexChanged.connect(lambda i: self.source.setText(self.sources.itemData(i) or ''))
        self.source.textChanged.connect(self._sync_transport)
        self.transport = QComboBox(); self.transport.addItems(['tcp', 'udp'])
        self.transport.setCurrentText(settings.value('video/ch0/transport', 'tcp', str))
        self.mount = QComboBox(); self.mount.addItems(['기체 고정 · 비짐벌', '지상 고정'])
        self.mount.currentIndexChanged.connect(self.change_mount)
        row = QHBoxLayout(); row.addWidget(self.transport); row.addWidget(self.mount, 1)
        buttons = QHBoxLayout()
        for widget in (self._button('영상 연결', self.connect_source, 'primary'),
                       self._button('영상 해제', self.disconnect_source),
                       self._button('합성 모의 영상', self.start_demo, 'sim')):
            buttons.addWidget(widget)
        for item in (self.sources, self.source):
            layout.addWidget(item)
        layout.addLayout(row)
        layout.addLayout(buttons)
        self._sync_transport(self.source.text())
        return box

    def _tracking_box(self):
        box, layout = self._box('추적')
        head = QHBoxLayout()
        self.state_badge = QLabel(DASH)
        self.state_badge.setAlignment(Qt.AlignCenter)
        self.state_badge.setMinimumWidth(190)
        self.reason = QLabel(DASH); self.reason.setWordWrap(True)
        self.reason.setStyleSheet('QLabel{font-size:17px;font-weight:800;}')
        head.addWidget(self.state_badge); head.addWidget(self.reason, 1)
        layout.addLayout(head)
        self.countdown = QLabel('')
        self.countdown.setStyleSheet(f'QLabel{{color:{YELLOW};font-size:26px;font-weight:900;}}')
        self.countdown.setWordWrap(True)
        layout.addWidget(self.countdown)
        grid, self.track = self._grid([('quality', '품질(잔존 특징점)'), ('hold', '유지 시간'),
                                       ('pfps', '처리 FPS'), ('error', '영상 중심 오차 x, y'),
                                       ('age', '프레임 나이'), ('dfps', '영상 수신 FPS'),
                                       ('cls', '객체 종류'), ('sim', '모의 출력'), ('route', '영상 경로'),
                                       ('rec', '재연결')])
        layout.addLayout(grid)
        rows = [[('ROI 선택', self.selecting, 'primary'), ('선택 취소', self.cancel_selection, None),
                 ('추적 정지', self.operator_stop, None)],
                [('조종자 개입', self.intervention, 'danger'), ('모의 출력 허용', self.simulate, 'sim'),
                 ('모의 출력 해제', self.revoke_simulation, None)],
                [('로그 저장', self.save, None)]]
        self.buttons = {}
        for row in rows:
            line = QHBoxLayout()
            for title, slot, kind in row:
                self.buttons[title] = self._button(title, slot, kind)
                line.addWidget(self.buttons[title])
            layout.addLayout(line)
        line = QHBoxLayout()
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(.05, 10); self.timeout.setDecimals(2)
        self.timeout.setValue(self.service.session.timeout); self.timeout.setSuffix(' s 지연 한도')
        self.timeout.setSingleStep(.05)
        self.timeout.setToolTip('지연 한도')
        self.timeout.valueChanged.connect(self.change_timeout)
        self.window = QDoubleSpinBox(); self.window.setRange(*SELECTION_WINDOW_RANGE); self.window.setDecimals(0)
        self.window.setValue(self.service.session.selection_window); self.window.setSuffix(' s 선택 시간')
        self.window.setToolTip('ROI 선택 시간')
        self.window.valueChanged.connect(self.service.set_selection_window)
        line.addWidget(self.timeout); line.addWidget(self.window)
        layout.addLayout(line)
        return box

    def _mode_box(self):
        box, layout = self._box('비행 모드')
        self.current_mode = QLabel(DASH)
        self.current_mode.setStyleSheet('QLabel{font-size:22px;font-weight:900;}')
        layout.addWidget(self.current_mode)
        row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_button = self._button('모드 변경 요청', self.request_mode, 'primary')
        row.addWidget(self.mode_combo, 1); row.addWidget(self.mode_button)
        layout.addLayout(row)
        self.mode_state = QLabel(DASH); self.mode_state.setWordWrap(True)
        layout.addWidget(self.mode_state)
        return box

    def _vehicle_box(self):
        box, layout = self._box('선택 기체')
        grid, self.veh = self._grid([('name', '기체'), ('link', '링크'), ('ids', 'SYS / COMP'),
                                     ('fw', '펌웨어'), ('hb', 'HEARTBEAT'), ('mode', '실제 모드'),
                                     ('armed', '시동'), ('rc', 'RC override'), ('att', '자세 R/P/Y'),
                                     ('alt', '고도 상대 / MSL'), ('gs', '지상속도'), ('gps', 'GPS'),
                                     ('bat', '배터리'), ('pos', '위치')])
        layout.addLayout(grid)
        self.alt_note = QLabel(DASH); self.alt_note.setWordWrap(True)
        self.alt_note.setStyleSheet(f'QLabel{{color:{MUTED};font-size:14px;}}')
        layout.addWidget(self.alt_note)
        return box

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
    def _sync_transport(self, text):
        applies = TrackingService.transport_applies(text)
        self.transport.setEnabled(applies)
        self.transport.setToolTip('RTSP 전송 방식' if applies else 'RTSP 전용 (장치·파일 미적용)')

    def change_mount(self):
        self.service.mount = self.mount.currentText()
        self.service.stop('camera_mode_changed')

    def change_timeout(self, value):
        self.service.change_timeout(value)

    def selecting(self):
        self._pinned_ref = self.service.begin_selection()
        self.canvas.selecting = self._pinned_ref is not None

    def cancel_selection(self):
        self.canvas.selecting = False
        self.service.stop('selection_cancelled')

    def session_select(self, roi):
        self.service.select_roi(roi, self._pinned_ref)
        self._pinned_ref = None

    def operator_stop(self):
        self.canvas.selecting = False
        self.service.stop('operator_stop')

    def intervention(self):
        self.canvas.selecting = False
        self.service.stop('operator_intervention')

    def disconnect_source(self):
        self.service.disconnect_source()
        self._image_key = None
        self.canvas.clear('영상 해제됨 · 영상 연결 버튼으로 다시 연결')

    def connect_source(self):
        self._image_key = None
        transport = self.transport.currentText() if self.transport.isEnabled() else None
        self.service.connect_source(self.source.text(), transport)

    def start_demo(self):
        self._image_key = None
        self.service.start_demo()

    def simulate(self):
        self.service.simulate()

    def revoke_simulation(self):
        self.service.session.revoke_simulation('operator_revoke')

    def pixhawk_fresh(self):
        return self.service.pixhawk_fresh()

    def set_vehicle(self, vehicle):
        self.service.set_vehicle(vehicle)

    def open_browser(self):
        url = self.service.open_dashboard()
        QDesktopServices.openUrl(QUrl(url))
        self.refresh()

    def close_browser(self):
        self.service.close_dashboard()
        self.refresh()

    # ---- flight mode request (explicit, confirmed) ----
    def request_mode(self):
        mode = self.mode_combo.currentData()
        if not mode:
            return
        try:
            ticket = self.service.prepare_mode(mode, 'app')
        except ModeRequestError as exc:
            self.mode_state.setText(f'요청 거부 · {exc}')
            return
        if self.confirm_mode(ticket):
            try:
                self.service.commit_mode(ticket['id'], 'app')
            except ModeRequestError as exc:
                self.mode_state.setText(f'송신 안 함 · {exc}')
        else:
            self.service.cancel_mode('사용자 취소', 'app')

    def confirm_mode(self, ticket):
        """Modal confirmation (patched in tests). Default button is No."""
        armed = ticket['armed']
        warn = ('<p style="color:#ff5c7a;font-size:22px;font-weight:900">ARMED · 비행 중일 수 있습니다. '
                '승인 즉시 기체 거동이 바뀝니다.</p>' if armed else
                '<p style="font-size:17px;font-weight:800">DISARMED · 모드만 바뀌며 시동·이륙하지 않습니다.</p>')
        items = ''.join(f'<li>{line}</li>' for line in ticket['impact'])
        text = (f'<p style="font-size:18px;font-weight:800">{ticket["name"]} · link #{ticket["link_id"]} · '
                f'SYS {ticket["sysid"]} / COMP {ticket["compid"]} · {ticket["firmware"]}</p>'
                f'<p style="font-size:22px;font-weight:900">{ticket["current_mode"]} → {ticket["mode"]}</p>'
                f'{warn}<ul style="font-size:16px">{items}</ul>')
        box = QMessageBox(QMessageBox.Warning if armed else QMessageBox.Question, '비행 모드 변경 확인', text,
                          QMessageBox.Yes | QMessageBox.No, self)
        box.setTextFormat(Qt.RichText)
        box.button(QMessageBox.Yes).setText('요청 1회 송신')
        box.button(QMessageBox.No).setText('취소')
        box.setDefaultButton(QMessageBox.No)
        return box.exec_() == QMessageBox.Yes

    def tick(self):
        """Advance the shared service once (normally driven by its own timer)."""
        if not self.closed:
            self.service.tick()

    # ---- view ----
    def _chip(self, name, text, color, filled=False, tip=''):
        label = self.chips[name]
        style = chip_style(color, filled)
        if label.styleSheet() != style:
            label.setStyleSheet(style)
        if label.text() != text:
            label.setText(text)
        label.setToolTip(tip)

    def refresh(self):
        if self.closed:
            return
        snap = self.service.snapshot()
        frame, result = self.service.view()
        if frame is not None:
            key = frame.session_id, frame.frame_seq
            if key != self._image_key:
                self._image_key = key
                rgb = cv2.cvtColor(frame.pixels, cv2.COLOR_BGR2RGB)
                self.canvas.set_image(QImage(rgb.data, frame.width, frame.height, rgb.strides[0],
                                             QImage.Format_RGB888).copy())
        elif self._image_key is not None or self.canvas.image is None:
            self._image_key = None
            self.canvas.clear('영상 없음 · 오른쪽 영상 소스에서 연결하세요')
        t, v, veh = snap['tracking'], snap['video'], snap['vehicle']
        if t['state'] != 'SELECTING' and self.canvas.selecting:
            self.canvas.selecting = False
        sel = t.get('selection')
        self.canvas.countdown = (f'ROI 선택 · 남은 {sel["remaining_s"]:.0f}초' if sel else None)
        self.canvas.monitor = snap['app']['monitor']
        self.canvas.result = result
        self.canvas.update()
        self._render_header(snap, t, v, veh)
        self._render_tracking(snap, t, v, sel)
        self._render_vehicle(veh)
        self._render_modes(snap['modes'], veh)
        self._render_events(snap['events'])
        self._render_web(snap)
        self.diag.setText(self._diagnostics(snap, t, v))

    def _render_header(self, snap, t, v, veh):
        sim = snap['app']['monitor'] == 'SIMULATION'
        self._chip('monitor', snap['app']['monitor'], PURPLE if sim else YELLOW, True)
        self._chip('app', '앱 연결됨', GREEN)
        vcolor = {'LIVE': GREEN, 'SIMULATION': PURPLE, 'CONNECTING': YELLOW, 'STALE': RED}.get(v['state'], MUTED)
        self._chip('video', f'영상 {v["state"]}', vcolor, tip=v['source'])
        if veh is None:
            for name, text in (('vehicle', '기체 미선택'), ('heartbeat', 'HB —'), ('mode', '모드 —'), ('armed', '시동 —')):
                self._chip(name, text, MUTED)
        else:
            self._chip('vehicle', f'{veh["name"]} · SYS {veh["sysid"]} / COMP {veh["compid"]}'
                       if not str(veh['name']).startswith('SYS') else f'{veh["name"]} · COMP {veh["compid"]}',
                       GREEN if veh['connected'] else RED)
            stale = veh['heartbeat_stale']
            self._chip('heartbeat', f'HB {"STALE" if stale else "OK"} {veh["heartbeat_age_s"] if veh["heartbeat_age_s"] is not None else DASH}s',
                       RED if stale else GREEN)
            self._chip('mode', f'모드 {veh["mode"] or DASH}', BLUE)
            armed = veh['armed']
            self._chip('armed', DASH if armed is None else ('ARMED' if armed else 'DISARMED'),
                       RED if armed else (GREEN if armed is False else MUTED), bool(armed))
        self._chip('tracking', f'Tracking {t["state"]}', STATE_COLOR.get(t['state'], MUTED), True, t['reason'])
        sentinel = snap['sentinel']['state']
        self._chip('sentinel', f'Sentinel Legacy {sentinel}', GREEN if sentinel not in ('미사용', None) else MUTED)
        level, text = snap['banner']['level'], snap['banner']['text']
        color = LEVEL_COLOR.get(level, BLUE)
        style = (f'QLabel{{font-size:20px;font-weight:900;padding:10px 14px;border-radius:10px;'
                 f'border:3px solid {color};color:{"#fff" if level == "danger" else color};'
                 f'background:{"#5a1424" if level == "danger" else "#171b24"};}}')
        if self.banner.styleSheet() != style:
            self.banner.setStyleSheet(style)
        self.banner.setText(text)

    def _render_tracking(self, snap, t, v, sel):
        color = STATE_COLOR.get(t['state'], MUTED)
        style = (f'QLabel{{background:{color};color:#111;font-size:30px;font-weight:900;'
                 f'border-radius:10px;padding:6px 12px;}}')
        if self.state_badge.styleSheet() != style:
            self.state_badge.setStyleSheet(style)
        self.state_badge.setText(t['state'])
        self.reason.setText(t['reason_text'])
        self.reason.setToolTip(f'내부 코드: {t["reason"]}')
        self.state_badge.setToolTip(f'내부 코드: {t["reason"]}')
        if sel:
            follow = (f' · 따라잡기 {sel["catchup_remaining_s"]:.0f}초' if sel['catchup_remaining_s'] > 0
                      else ' · 따라잡기 시간 초과')
            self.countdown.setText(f'ROI 선택 남은 시간 {sel["remaining_s"]:.0f}초{follow}')
        else:
            self.countdown.setText('')

        def val(x, fmt='{}', unit=''):
            return DASH if x is None else fmt.format(x) + unit
        err = t['image_error']
        self.track['quality'].setText(val(t['quality'], '{:.2f}'))
        self.track['hold'].setText(val(t['hold_s'], '{:.1f}', ' s'))
        self.track['pfps'].setText(val(t['processing_fps'], '{:.0f}'))
        self.track['error'].setText(DASH if not err else f'{err[0]:+.3f}, {err[1]:+.3f}')
        stale = v['state'] == 'STALE'
        self.track['age'].setText(val(v['frame_age_ms'], '{}', ' ms') + ('  STALE' if stale else ''))
        self.track['age'].setStyleSheet(f'QLabel{{color:{ORANGE if stale else "#f5f7fb"};}}')
        self.track['dfps'].setText(val(v['decode_fps'], '{:.1f}'))
        self.track['cls'].setText(t['object_class'])
        out = snap['simulation']
        self.track['sim'].setText('허용 · 메모리 기록' if out['enabled'] else '해제')
        self.track['route'].setText(v.get('route') or DASH)
        self.track['rec'].setText(str(v.get('reconnects', 0)))
        warnings = v.get('warnings') or []
        self.video_warn.setText('\n'.join(f'⚠ {w}' for w in warnings))
        self.video_warn.setVisible(bool(warnings))
        live = v['state'] in ('LIVE', 'SIMULATION')
        selecting = t['state'] == 'SELECTING'
        self.buttons['ROI 선택'].setEnabled(live and not selecting)
        self.buttons['선택 취소'].setEnabled(selecting)
        self.buttons['모의 출력 허용'].setEnabled(t['state'] == 'TRACKING' and not out['enabled'])
        self.buttons['모의 출력 해제'].setEnabled(out['enabled'])

    @staticmethod
    def _fmt_vehicle(veh):
        """(key, text, stale) rows for the read-only vehicle table. Missing = —, old = STALE."""
        if veh is None:
            return [('name', '선택된 기체 없음 · 메인 창에서 기체를 선택하세요', False)]

        def n(x, unit='', digits=1):
            return DASH if x is None else f'{x:.{digits}f}{unit}'
        a, alt, gs, g, b, pos = (veh[k] for k in ('attitude', 'altitude', 'groundspeed', 'gps', 'battery', 'position'))
        armed = veh['armed']
        return [
            ('name', f'{veh["name"]} · {"연결" if veh["connected"] else "끊김"}', not veh['connected']),
            ('link', f'{veh["link"] or DASH} (#{veh["link_id"] if veh["link_id"] is not None else DASH})', False),
            ('ids', f'{veh["sysid"]} / {veh["compid"]}', False),
            ('fw', f'{veh["firmware"]}' + (f' · {veh["sw_version"]}' if veh['sw_version'] else ''), False),
            ('hb', f'{n(veh["heartbeat_age_s"], " s 전")}', veh['heartbeat_stale']),
            ('mode', veh['mode'] or DASH, veh['heartbeat_stale']),
            ('armed', DASH if armed is None else ('ARMED' if armed else 'DISARMED'), veh['heartbeat_stale']),
            ('rc', '활성' if veh['rc_override'] else '없음', False),
            ('att', DASH if not a else f'{n(a["roll"], "°")} / {n(a["pitch"], "°")} / {n(a["yaw"], "°")}',
             bool(a and a['stale'])),
            ('alt', DASH if not alt else f'{n(alt["relative_m"], " m")} / {n(alt["msl_m"], " m")}',
             bool(alt and alt['stale'])),
            ('gs', DASH if not gs else n(gs['mps'], ' m/s'), bool(gs and gs['stale'])),
            ('gps', DASH if not g else f'{g["fix_text"]} · 위성 {g["sats"] if g["sats"] is not None else DASH}',
             bool(g and g['stale'])),
            ('bat', DASH if not b else f'{n(b["volt"], " V", 2)} · {DASH if b["pct"] is None else str(b["pct"]) + " %"}',
             bool(b and b['stale'])),
            ('pos', '위치 미수신' if not pos else f'{pos["lat"]:.6f}, {pos["lon"]:.6f}', bool(pos and pos['stale'])),
        ]

    def _render_vehicle(self, veh):
        rows = self._fmt_vehicle(veh)
        shown = {key for key, _, _ in rows}
        for key, text, stale in rows:
            label = self.veh[key]
            label.setText(text + ('  STALE' if stale else ''))
            label.setStyleSheet(f'QLabel{{color:{ORANGE if stale else "#f5f7fb"};}}')
        for key, label in self.veh.items():
            if key not in shown:
                label.setText(DASH)
                label.setStyleSheet('')
        alt = veh and veh['altitude']
        self.alt_note.setText(f'고도 기준 · {alt["reference"]}' if alt else '고도 미수신')

    def telemetry_text(self):
        """Plain-text form of the vehicle table (logs/tests)."""
        rows = self._fmt_vehicle(self.service.vehicle_state())
        return '\n'.join(f'{key}: {text}' + (' STALE' if stale else '') for key, text, stale in rows)

    def _render_modes(self, modes, veh):
        ctl = modes['controller']
        busy = ctl['state'] in ('CONFIRMING', 'REQUESTING')
        self.current_mode.setText(f'현재 모드 {veh["mode"] or DASH}' if veh else '현재 모드 — (기체 미선택)')
        key = (tuple((o['name'], o['enabled'], o['reason']) for o in modes['options']), veh and veh['mode'])
        if key != self._mode_key:
            self._mode_key = key
            current = self.mode_combo.currentData()
            self.mode_combo.clear()
            model = self.mode_combo.model()
            for o in modes['options']:
                label = o['name'] if o['enabled'] else f'{o["name"]} (비활성)'
                self.mode_combo.addItem(label, o['name'] if o['enabled'] else None)
                item = model.item(self.mode_combo.count() - 1)
                if not o['enabled']:
                    item.setEnabled(False)
                    item.setToolTip(o['reason'])
            index = self.mode_combo.findData(current)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
        stale = veh is None or veh['heartbeat_stale']
        self.mode_button.setEnabled(not busy and not stale and bool(modes['options']))
        color = {'CONFIRMED': GREEN, 'REJECTED': RED, 'TIMEOUT': RED, 'FAILED': RED, 'CANCELLED': ORANGE,
                 'REQUESTING': YELLOW, 'CONFIRMING': YELLOW}.get(ctl['state'], MUTED)
        src = f' · 출처 {ctl["source"]}' if ctl.get('source') else ''
        extra = ' · HEARTBEAT stale: 요청 불가' if veh is not None and stale else ''
        self.mode_state.setText(f'{ctl["state"]} · {ctl["detail"]}{src}{extra}')
        self.mode_state.setStyleSheet(f'QLabel{{color:{color};font-size:16px;font-weight:800;}}')

    def _render_events(self, events):
        key = (events[0]['t'], events[0]['text'], len(events)) if events else None
        if key == self._event_key:
            return
        self._event_key = key
        self.events.clear()
        for e in events:
            item = QListWidgetItem(f'{e["t"]}  [{e["kind"]}]  {e["text"]}')
            item.setForeground(QColor({'warn': YELLOW, 'danger': RED}.get(e['level'], '#f5f7fb')))
            self.events.addItem(item)

    def _render_web(self, snap):
        open_ = self.service.dashboard is not None
        self._chip('webmode', '웹 모드변경 ON', ORANGE, True, '브라우저 대시보드에서 비행 모드 변경 요청 가능')
        self.chips['webmode'].setVisible(open_)
        if open_:
            self.web.setText(f'브라우저 대시보드 {self.service.dashboard.url} · 같은 추적 세션 공유 중')
            self.web.setStyleSheet(f'QLabel{{color:{ORANGE};font-size:17px;font-weight:900;'
                                   f'border:2px solid {ORANGE};border-radius:8px;padding:6px 10px;}}')
        else:
            self.web.setText('브라우저 대시보드 꺼짐')
            self.web.setStyleSheet(f'QLabel{{color:{MUTED};font-size:15px;}}')
        self.web_open.setText('브라우저에서 열기' if open_ else '브라우저 대시보드 열기')
        self.web_close.setEnabled(open_)

    @staticmethod
    def _diagnostics(snap, t, v):
        return '\n'.join([
            f'tracking session  {t["session_id"]}',
            f'frame session     {t["frame_session_id"] or DASH} · seq {t["frame_seq"] if t["frame_seq"] is not None else DASH}',
            f'video source_id   {v["source_id"] or DASH} · session {v["session_id"] or DASH}',
            f'video             {v["source"]} · {v["transport"] or DASH} · {v["status"]} · '
            f'{v["width"] or DASH}×{v["height"] or DASH} · 장착 {v["mount"]}',
            f'내부 사유 코드     {t["reason"]}',
            f'bbox              {t["bbox"] or DASH}',
            f'timeout           {t["timeout_s"]} s · 모의 출력 {snap["simulation"]["output"] or DASH}',
            f'브라우저 마지막 요청 {snap["app"]["web_result"]}',
        ])

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Tracking 로그 저장', 'tracking.json', 'JSON (*.json)')
        if path:
            self.service.save(path)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.service.changed.disconnect(self.refresh)
        except (TypeError, RuntimeError):
            pass
        self.service.shutdown()

    def ask_close_choice(self):
        """'keep' (browser keeps the session), 'close_all' or 'cancel'. Patched in tests."""
        box = QMessageBox(QMessageBox.Question, 'Tracking 창 닫기',
                          '브라우저 대시보드가 열려 있어 같은 추적 세션과 영상이 계속 유지됩니다.\n'
                          '브라우저에서는 비행 모드 변경도 계속 가능합니다.', parent=self)
        keep = box.addButton('창만 닫기 (대시보드 유지)', QMessageBox.AcceptRole)
        both = box.addButton('대시보드도 닫기', QMessageBox.DestructiveRole)
        box.addButton('취소', QMessageBox.RejectRole)
        box.setDefaultButton(both)
        box.exec_()
        return 'keep' if box.clickedButton() is keep else ('close_all' if box.clickedButton() is both else 'cancel')

    def closeEvent(self, event):
        if self.closed:
            return event.accept()
        if self.service.dashboard is not None:
            choice = self.ask_close_choice()
            if choice == 'cancel':
                return event.ignore()
            if choice == 'keep':
                self.service.log_event('웹', 'Tracking 창 닫힘 · 브라우저 대시보드가 세션을 유지', 'warn')
                return event.accept()
            self.service.close_dashboard()
        self.service.disconnect_source('panel_closed')
        self.session.stop('panel_closed')
        self._image_key = None
        event.accept()
