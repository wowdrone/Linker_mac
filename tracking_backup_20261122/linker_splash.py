# linker_splash.py

from pathlib import Path

from PyQt5.QtWidgets import QSplashScreen, QProgressBar, QLabel
from PyQt5.QtGui import QPixmap, QPainter, QColor, QBrush, QFont
from PyQt5.QtCore import Qt, QTimer, QRect


class LinkerSplashScreen(QSplashScreen):
    """
    LINKER GCS 전용 스플래시 화면
      - 배경 + 로고 + 타이틀 + 버전 정보
      - ProgressBar + 단계별 상태 메시지
      - start(on_finished, interval_ms) 로 애니메이션 시작
    """

    def __init__(self, version: str = "v0.9.0", logo_path: str = None, parent=None):
        # ------------------------------------------------------------------
        # 0) 로고 경로 확정 (기본값: 이 파일과 같은 폴더의 logo1.png)
        # ------------------------------------------------------------------
        if logo_path is None:
            base_dir = Path(__file__).resolve().parent
            logo_path = base_dir / "logo1.png"
        else:
            logo_path = Path(logo_path)

        # ------------------------------------------------------------------
        # 1) 배경 Pixmap 그리기
        # ------------------------------------------------------------------
        pix = QPixmap(800, 450)
        pix.fill(QColor("#0b0f16"))

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)

        # 안쪽 큰 라운드 박스
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#111826")))
        painter.drawRoundedRect(40, 40, 720, 370, 18, 18)

        # 상단 헤더 바
        header_rect = QRect(40, 40, 720, 70)
        painter.setBrush(QBrush(QColor("#182335")))
        painter.drawRoundedRect(header_rect, 18, 18)

        # 타이틀 텍스트
        painter.setPen(QColor("#4aa3ff"))
        title_font = QFont("Apple SD Gothic Neo", 24, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(
            QRect(60, 50, 400, 50),
            Qt.AlignVCenter | Qt.AlignLeft,
            "Linker",
        )

        # 버전 텍스트
        painter.setPen(QColor("#9fb7ff"))
        ver_font = QFont("Apple SD Gothic Neo", 11)
        painter.setFont(ver_font)
        painter.drawText(
            QRect(60, 90, 300, 25),
            Qt.AlignVCenter | Qt.AlignLeft,
            f"version {version}",
        )

        # ------------------------------------------------------------------
        # 2) 로고 이미지 영역
        # ------------------------------------------------------------------
        logo_target = QRect(70, 130, 160, 160)

        logo_pix = QPixmap()
        if logo_path.exists():
            logo_pix.load(str(logo_path))

        if not logo_pix.isNull():
            # 로고를 target 크기에 맞춰서 비율 유지 스케일
            scaled = logo_pix.scaled(
                logo_target.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            # 가운데 정렬
            x = logo_target.x() + (logo_target.width() - scaled.width()) // 2
            y = logo_target.y() + (logo_target.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            # 로고 로드 실패 시 플레이스홀더
            painter.setBrush(QBrush(QColor("#222b3f")))
            painter.setPen(QColor("#345"))
            painter.drawRoundedRect(logo_target, 12, 12)
            painter.setPen(QColor("#6b84ff"))
            painter.drawText(
                logo_target,
                Qt.AlignCenter,
                "WOW\nDRONE",
            )

        # 오른쪽 설명 텍스트 블럭
        painter.setPen(QColor("#e0f2ff"))
        body_font = QFont("Apple SD Gothic Neo", 10)
        painter.setFont(body_font)
        painter.drawText(
            QRect(260, 135, 470, 140),
            Qt.AlignLeft | Qt.AlignTop,
            "Initializing LINKER Ground Control Station...\n"
            "• Map / mission planner / health monitor\n"
            "• Serial / UDP link manager\n"
            "• Video streaming panel\n\n"
            "Please wait while systems are being prepared.",
        )

        painter.end()

        # ------------------------------------------------------------------
        # 3) QSplashScreen 초기화
        # ------------------------------------------------------------------
        super().__init__(pix)

        # ------------------------------------------------------------------
        # 4) ProgressBar & 상태 메시지 라벨
        # ------------------------------------------------------------------
        self._progress = QProgressBar(self)
        self._progress.setGeometry(140, 330, 520, 24)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar {"
            "  background-color:#111826;"
            "  border:1px solid #2a3954;"
            "  border-radius:4px;"
            "}"
            "QProgressBar::chunk {"
            "  background-color:#4aa3ff;"
            "  border-radius:4px;"
            "}"
        )

        self._label = QLabel("Starting Linker...", self)
        self._label.setGeometry(140, 300, 520, 24)
        self._label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._label.setStyleSheet(
            "QLabel { color:#e0f2ff; font-size:10pt; font-family:'Apple SD Gothic Neo'; }"
        )

        # ------------------------------------------------------------------
        # 5) 타이머 / 상태 관리
        # ------------------------------------------------------------------
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._value = 0
        self._on_finished_cb = None

        self._phases = [
            (0, 25, "Loading LINKER core modules..."),
            (25, 55, "Initializing UI / map / video components..."),
            (55, 80, "Preparing communication links..."),
            (80, 99, "Finalizing startup..."),
        ]

    # 외부에서 호출하는 시작 함수
    def start(self, on_finished=None, interval_ms: int = 30):
        self._on_finished_cb = on_finished
        self._value = 0
        self._progress.setValue(0)
        self._label.setText("Starting Linker...")
        self._timer.start(interval_ms)

    # 타이머 tick
    def _on_tick(self):
        self._value += 1

        if self._value > 100:
            self._timer.stop()
            self._progress.setValue(100)
            self._label.setText("Ready. Launching Linker...")
            if self._on_finished_cb:
                self._on_finished_cb()
            return

        self._progress.setValue(self._value)

        for lo, hi, msg in self._phases:
            if lo <= self._value < hi:
                self._label.setText(msg)
                break
