# linker_intro.py
# LINKER GCS 인트로 스플래시 전용 모듈

from PyQt5.QtGui import QPixmap, QPainter, QBrush, QColor, QFont
from PyQt5.QtWidgets import QSplashScreen
from PyQt5.QtCore import Qt


def create_splash() -> QSplashScreen:
    """
    LINKER GCS 로딩 스플래시 화면(QSplashScreen)을 생성해서 반환.
    main_window.py 의 main()에서 show() 및 finish()를 호출해 사용한다.
    """
    pix = QPixmap(640, 360)
    pix.fill(QColor("#1f2833"))

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor("#0b1c10")))
    painter.drawRoundedRect(40, 40, 560, 280, 18, 18)

    painter.setPen(QColor("#4aa3ff"))
    title_font = QFont("Apple SD Gothic Neo", 48, QFont.Black)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.drawText(
        pix.rect(),
        Qt.AlignHCenter | Qt.AlignVCenter,
        "Linker\n\nloading...",
    )
    painter.end()

    splash = QSplashScreen(pix)
    return splash
