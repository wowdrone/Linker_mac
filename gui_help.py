"""On-demand guides for Linker screens (UI guide principle, user decision 2026-11-22).

Usage text (how to use, button meaning, units, general cautions, "~하세요" instructions)
is not shown permanently. It opens only from a "?" button next to a screen/card title or
from the main window "가이드" button. State, short reasons, errors and real-time safety
warnings stay on screen and are never moved here.

    from gui_help import help_button, set_guide, show_guide
    row.addWidget(help_button(self, 'Fleet · Links', ['항목 1', '항목 2']))
    set_guide(page_widget, 'Fleet', [...])        # used by the main window "가이드" button
"""
from html import escape

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QScrollArea,
                             QToolButton, QVBoxLayout, QWidget)

GUIDE_ATTR = '_linker_guide'
BUTTON_SIZE = 36

_BUTTON_STYLE = ('QToolButton{min-width:36px;min-height:36px;max-width:36px;max-height:36px;'
                 'font-size:20px;font-weight:900;border-radius:18px;border:2px solid #ffd23f;'
                 'color:#ffd23f;background:#171b24;}'
                 'QToolButton:hover{background:#2a2410;} QToolButton:focus{border:3px solid #ffffff;}')
_DIALOG_STYLE = ('QDialog{background:#0e1118;} QLabel{color:#f5f7fb;font-size:17px;font-weight:600;}'
                 'QLabel#guideTitle{color:#ffd23f;font-size:22px;font-weight:900;}'
                 'QPushButton{font-size:16px;font-weight:800;min-height:36px;padding:4px 18px;}')


def _items_html(items):
    """Bullet list; each item is plain text (escaped) unless it starts with '<' (trusted HTML)."""
    lines = []
    for item in items:
        text = item if str(item).lstrip().startswith('<') else escape(str(item)).replace('\n', '<br>')
        lines.append(f'<li style="margin-bottom:8px">{text}</li>')
    return '<ul style="margin-left:-18px">' + ''.join(lines) + '</ul>'


class GuideDialog(QDialog):
    """Non-modal guide window with large, bold, high-contrast text."""

    def __init__(self, title, items, parent=None):
        super().__init__(parent)
        self.setObjectName('linkerGuide')
        self.setWindowTitle(f'{title} · 가이드')
        self.setStyleSheet(_DIALOG_STYLE)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        heading = QLabel(f'{title} · 가이드')
        heading.setObjectName('guideTitle')
        layout.addWidget(heading)
        self.body = QLabel(_items_html(items))
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidget(self.body)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText('닫기')
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.resize(640, 520)


def show_guide(parent, title, items):
    """Open (or raise) the guide for title. Returns the dialog (non-modal: never blocks)."""
    dialogs = getattr(parent, '_open_guides', None) if parent is not None else None
    if dialogs is None and parent is not None:
        dialogs = {}
        setattr(parent, '_open_guides', dialogs)
    existing = (dialogs or {}).get(title)
    try:
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return existing
    except RuntimeError:          # deleted on close
        pass
    dialog = GuideDialog(title, items, parent)
    if dialogs is not None:
        dialogs[title] = dialog
    dialog.show()
    return dialog


def help_button(parent, title, items):
    """36×36 "?" button with an accessible name that opens the guide for title."""
    button = QToolButton(parent)
    button.setText('?')
    button.setObjectName('guideButton')
    button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
    button.setStyleSheet(_BUTTON_STYLE)
    button.setAccessibleName(f'{title} 가이드')
    button.setToolTip(f'{title} 가이드')
    button.setCursor(Qt.PointingHandCursor)
    button.setProperty('guideTitle', title)
    button.setProperty('guideItems', list(items))
    button.clicked.connect(lambda _=False: show_guide(parent, title, items))
    return button


def title_row(parent, text, title, items, style=None):
    """A title label followed by its "?" button (returns the row widget and the label)."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    label = QLabel(text)
    if style:
        label.setStyleSheet(style)
    layout.addWidget(label)
    layout.addWidget(help_button(parent, title, items))
    layout.addStretch(1)
    return row, label


def set_guide(widget, title, items):
    setattr(widget, GUIDE_ATTR, (title, list(items)))
    return widget


def get_guide(widget):
    """Guide registered on widget or the first registered descendant."""
    if widget is None:
        return None
    guide = getattr(widget, GUIDE_ATTR, None)
    if guide:
        return guide
    for child in widget.findChildren(QWidget):
        guide = getattr(child, GUIDE_ATTR, None)
        if guide:
            return guide
    return None
