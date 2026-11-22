"""Color and component styles shared by the Linker desktop interface."""

from dataclasses import dataclass
from string import Template

from PyQt5.QtGui import QColor, QPalette


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    header: str
    panel: str
    field: str
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    selected: str
    hover: str
    secondary: str
    success: str
    warning: str
    danger: str
    radius: int
    button_radius: int
    light: bool = False
    layered: bool = False

    def palette(self):
        palette = QPalette()
        for role, value in (
            (QPalette.Window, self.background), (QPalette.WindowText, self.text),
            (QPalette.Base, self.field), (QPalette.AlternateBase, self.panel),
            (QPalette.Text, self.text), (QPalette.Button, self.panel),
            (QPalette.ButtonText, self.text), (QPalette.Highlight, self.selected),
            (QPalette.HighlightedText, self.text), (QPalette.ToolTipBase, self.panel),
            (QPalette.ToolTipText, self.text), (QPalette.PlaceholderText, self.muted),
            (QPalette.Mid, self.border), (QPalette.Link, self.accent),
        ):
            palette.setColor(role, QColor(value))
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            palette.setColor(QPalette.Disabled, role, QColor(self.muted))
        return palette

    def series_color(self, value):
        color = QColor(value)
        return color.darker(165) if self.light else color

    def stylesheet(self):
        style = _STYLE.substitute(vars(self))
        if self.layered:
            style += _NEO_STYLE.substitute(vars(self))
        return style


THEMES = {
    "neo": Theme("Neo", "#101319", "#191d26", "#242933", "#181c24",
                 "#3a4352", "#f3f6fa", "#a8b3c4", "#60d7ff", "#092332",
                 "#21394c", "#343b49", "#c3a5ff", "#7cebd1", "#ffc177", "#ff82aa",
                 18, 11, layered=True),
    "dark": Theme("Dark", "#141821", "#191e28", "#202632", "#171d27",
                  "#353f50", "#eef3fc", "#a2b1c5", "#63d0ff", "#0c2433",
                  "#254259", "#2b3647", "#ba9bff", "#72dfba", "#ffc181", "#ff8295", 14, 7),
    "light": Theme("Light", "#edf2fa", "#ffffff", "#ffffff", "#f6f8fd",
                   "#c9d5e5", "#172941", "#546884", "#165cbe", "#ffffff",
                   "#d9e8ff", "#e8effa", "#6947ad", "#106b54", "#8b4d10", "#ac2340", 12, 7, True),
    "cyber": Theme("Cyber", "#0c0b18", "#131026", "#19142c", "#0e1120",
                   "#4b3870", "#edf4ff", "#b4a6d1", "#3cf5ed", "#081e22",
                   "#213c54", "#2e244a", "#fc8cee", "#6bf2af", "#ffcc74", "#ff83a2", 4, 3),
    "simple": Theme("심플", "#f2f2ee", "#fafaf7", "#fafaf7", "#ffffff",
                    "#d5d8d0", "#252a26", "#5c675f", "#395a49", "#ffffff",
                    "#e0e9df", "#eaece5", "#4b6154", "#246146", "#7f5419", "#a83a40", 4, 4, True),
}
DEFAULT_THEME = "dark"
THEME_SETTING = "appearance/theme"


_STYLE = Template("""
QMainWindow, QDialog, QScrollArea, QStackedWidget,
QWidget#page_flying, QWidget#page_planning, QWidget#page_health,
QWidget#page_support, QWidget#page_fleet, QWidget#page_video {
    background-color: $background; color: $text;
}
QWidget#linkerHeader, QStatusBar#linkerStatus {
    background-color: $header; color: $muted;
}
QWidget#linkerHeader { border-bottom: 1px solid $border; }
QStatusBar#linkerStatus { border-top: 1px solid $border; }
QStatusBar#linkerStatus QLabel { color: $muted; }
QLabel#linkerBrand { color: $text; font-size: 15pt; font-weight: bold; }
QFrame[dashboardCard="true"], QFrame#videoTile, QWidget[chartCard="true"] {
    background-color: $panel; border: 1px solid $border; border-radius: ${radius}px;
}
QLabel { color: $text; background: transparent; }
QLabel[role="title"] { font-weight: bold; font-size: 11pt; }
QLabel[role="caption"] { color: $muted; font-size: 9pt; }
QLabel[role="metric"] { color: $accent; font-size: 19pt; font-weight: bold; }
QLabel[role="vehicle"] { color: $secondary; font-size: 13pt; font-weight: bold; }
QLabel[statusTone] { color: $muted; }
QLabel[statusTone="success"] { color: $success; }
QLabel[statusTone="warning"] { color: $warning; }
QLabel[statusBadge="true"] {
    background-color: $panel; padding: 4px 7px; border: 1px solid $border;
    border-radius: ${button_radius}px;
}
QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QListWidget, QTableWidget {
    background-color: $field; color: $text; border: 1px solid $border;
    selection-background-color: $selected; selection-color: $text;
    border-radius: ${button_radius}px; padding: 4px;
}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus,
QPushButton:focus, QToolButton:focus { border-color: $accent; }
QComboBox { padding-right: 19px; }
QComboBox QAbstractItemView {
    background-color: $panel; color: $text; border: 1px solid $border;
    selection-background-color: $selected; selection-color: $text;
}
QPushButton, QToolButton {
    background-color: $panel; color: $text; border: 1px solid $border;
    border-radius: ${button_radius}px; padding: 5px 8px; font-weight: bold;
}
QPushButton[role="navigation"] {
    background-color: transparent; border-color: transparent; padding: 6px 7px; font-size: 10pt;
}
QPushButton#appearanceButton { padding-right: 20px; }
QPushButton#appearanceButton::menu-indicator { subcontrol-position: right center; right: 6px; }
QPushButton:hover, QToolButton:hover { background-color: $hover; }
QPushButton:checked, QToolButton:checked {
    background-color: $selected; color: $accent; border-color: $accent;
}
QPushButton[role="primary"], QPushButton[role="primary"]:hover {
    background-color: $accent; color: $accent_text; border-color: $accent;
}
QPushButton[role="return"] { color: $warning; }
QPushButton[role="danger"] { color: $danger; }
QPushButton[role="record"]:checked { color: $danger; border-color: $danger; }
QPushButton:disabled, QToolButton:disabled {
    color: $muted; background-color: $field; border-color: $border;
}
QPushButton[flightAction="true"] { padding: 4px; font-size: 10pt; }
QMenu {
    background-color: $panel; color: $text; border: 1px solid $border; padding: 4px;
}
QMenu::item { padding: 5px 22px; }
QMenu::item:selected { background-color: $selected; }
QMenu::item:disabled { color: $muted; }
QListWidget::item { padding: 6px 8px; margin: 1px 0; border-radius: ${button_radius}px; }
QListWidget::item:selected { background-color: $selected; color: $text; }
QListWidget::item:hover:!selected { background-color: $hover; }
QTableWidget { gridline-color: $border; padding: 0; }
QHeaderView::section {
    background-color: $panel; color: $muted; padding: 4px;
    border: 0; border-bottom: 1px solid $border; font-weight: bold;
}
QGroupBox {
    background-color: $panel; color: $text; border: 1px solid $border;
    border-radius: ${radius}px; margin-top: 14px; padding-top: 6px; font-weight: bold;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QTabWidget::pane { border: 1px solid $border; background-color: $panel; }
QTabBar::tab {
    background-color: $panel; color: $muted; padding: 5px 10px;
    border-bottom: 2px solid $border;
}
QTabBar::tab:selected { color: $accent; background-color: $selected; border-color: $accent; }
QProgressBar {
    color: $text; background-color: $field; border: 1px solid $border; border-radius: 4px;
}
QProgressBar::chunk { background-color: $accent; border-radius: 3px; }
QSplitter::handle { background-color: transparent; }
QSplitter::handle:hover { background-color: $selected; }
QToolTip { background-color: $panel; color: $text; border: 1px solid $border; }
""")

_NEO_STYLE = Template("""
QWidget#linkerHeader {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #272c38, stop:1 #171b23);
    border-bottom-color: #394454;
}
QPushButton[role="navigation"]:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1c3045, stop:1 #263041);
    border: 1px solid #41647c; border-bottom: 2px solid $accent;
    padding-bottom: 5px;
}
QFrame[dashboardCard="true"], QFrame#videoTile, QWidget[chartCard="true"], QGroupBox {
    background: qlineargradient(x1:0, y1:0, x2:0.8, y2:1, stop:0 #29303b, stop:1 #1c2028);
    border-top-color: #414b5a; border-left-color: #363f4e;
    border-right-color: #262c36; border-bottom: 2px solid #11151c;
}
QFrame[dashboardCard="true"] QLabel[role="title"] {
    color: $text;
}
QFrame[flightMetric="speed"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #243a4a, stop:1 #18262e);
    border-left: 3px solid #55cfff;
}
QFrame[flightMetric="altitude"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3a304e, stop:1 #251f34);
    border-left: 3px solid #b791ff;
}
QFrame[flightMetric="distance"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4a342c, stop:1 #2a2220);
    border-left: 3px solid #f3a561;
}
QFrame[flightMetric="altitude"] QLabel[role="metric"] { color: #d0b7ff; }
QFrame[flightMetric="distance"] QLabel[role="metric"] { color: #ffc98f; }
QFrame[flightMetric] QLabel[role="caption"] { color: #c4ccda; }
QPushButton#appearanceButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2b3949, stop:1 #202630);
    border-color: #4b6078;
}
QPushButton#flyOpenStream { background-color: #25465b; border-color: #42728c; }
QPushButton#flyOpenStream:hover { background-color: #30586f; }
""")


def set_status_tone(widget, tone):
    if widget.property("statusTone") != tone:
        widget.setProperty("statusTone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()
