"""UI guide principle (2026-11-22): usage text opens on demand; state and warnings stay visible."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from html.parser import HTMLParser
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QLabel, QToolButton, QWidget
import main_window   # imports QtWebEngine: must happen before any QApplication exists

ROOT = Path(__file__).resolve().parents[1]

# Sentences that used to be always visible; each must now live only in a guide.
MOVED = [
    '각 그래프의 종류와 표시 항목을 선택하세요',
    '기준값은 JSON 객체로 입력',
    '검증된 기체 모델이나 변경 근거가 없는 경우 숫자 추천을 생성하지 않습니다',
    '쓰기 직전 기체 ID, heartbeat, Disarmed, 지상 상태 및 읽은 값을 재검증합니다',
    'DataFlash 로그 또는 선택한 기체의 수신 데이터를 분석하세요',
    '기체마다 링크 1개씩, 또는 UDP 1포트로 여러 SYSID 수신',
    '클릭 = 조종 대상 선택, ⌘/Shift = 그룹 명령 대상 다중 선택',
    'ARMING_CHECK 비트마스크를 편집합니다',
    '품질은 특징점 잔존·이동 일관성 지표이며 객체 확률이 아닙니다',
    'ARM·이륙·미션 시작을 하지 않으며 자동 재시도하지 않습니다',
    '무늬가 뚜렷한 곳(모서리·글자·경계)',
]


class InertMap(QWidget):
    def page(self):
        return MagicMock()


def visible_texts(root):
    texts = [label.text() for label in root.findChildren(QLabel) if label.isVisibleTo(root)]
    texts += [box.title() for box in root.findChildren(__import__('PyQt5.QtWidgets', fromlist=['QGroupBox']).QGroupBox)]
    return '\n'.join(texts)


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_button_and_dialog(self):
        from gui_help import GuideDialog, help_button, get_guide, set_guide, show_guide
        host = QWidget()
        button = help_button(host, 'Fleet · Links', ['항목 <b>하나</b>', '둘'])
        self.assertEqual((button.width(), button.height()), (36, 36))
        self.assertEqual(button.accessibleName(), 'Fleet · Links 가이드')
        button.click()
        dialogs = [w for w in QApplication.topLevelWidgets() if isinstance(w, GuideDialog) and w.isVisible()]
        self.assertEqual(len(dialogs), 1)
        self.assertFalse(dialogs[0].isModal())
        self.assertIn('&lt;b&gt;하나', dialogs[0].body.text())          # plain items are escaped
        self.assertIs(show_guide(host, 'Fleet · Links', []), dialogs[0])  # raised, not duplicated
        child = QWidget(host)
        set_guide(child, 'X', ['a'])
        self.assertEqual(get_guide(host), ('X', ['a']))
        dialogs[0].close()


class MainWindowGuides(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_every_page_has_a_guide_and_moved_text_is_hidden(self):
        from gui_help import GuideDialog, get_guide
        with TemporaryDirectory() as directory:
            QSettings.setDefaultFormat(QSettings.IniFormat)
            QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, directory)
            with patch.object(main_window, 'create_map_widget', side_effect=lambda *a: (InertMap(), MagicMock())), \
                 patch('fleet.FleetManager.add_link'), patch('video_grid.VideoWorker._open'):
                window = main_window.MainWindow()
                window.show(); self.app.processEvents()
                self.assertEqual(window.guide_button.text(), '가이드')
                titles = {}
                for mode, title in (('fly', 'Fly'), ('planning', 'Plan'), ('health', 'Health'), ('safety', 'Safety'),
                                    ('support', 'Payload Data'), ('ai_tuner', 'AI Tuner'), ('fleet', 'Fleet'),
                                    ('video', 'Video 4CH')):
                    window.on_mode_button_clicked(mode); self.app.processEvents()
                    guide = get_guide(window.current_page())
                    self.assertIsNotNone(guide, mode)
                    self.assertEqual(guide[0], title)
                    dialog = window.show_page_guide()
                    self.assertIsInstance(dialog, GuideDialog)
                    titles[mode] = dialog.windowTitle()
                    dialog.close()
                    page_text = visible_texts(window.current_page())
                    for sentence in MOVED:
                        self.assertNotIn(sentence, page_text, f'{mode}: {sentence}')
                buttons = [b for b in window.findChildren(QToolButton) if b.objectName() == 'guideButton']
                self.assertGreaterEqual(len(buttons), 8)
                for b in buttons:
                    self.assertTrue(b.accessibleName().endswith('가이드'))
                    self.assertGreaterEqual(min(b.width(), b.height()), 36)
                all_guides = ' '.join(' '.join(b.property('guideItems') or []) for b in buttons)
                for sentence in MOVED[:7]:
                    self.assertTrue(sentence in all_guides, sentence)
                dialog = main_window.PreArmDialog(0, window)
                arming = [b for b in dialog.findChildren(QToolButton) if b.objectName() == 'guideButton']
                self.assertEqual(len(arming), 1)
                self.assertIn(MOVED[7], ' '.join(arming[0].property('guideItems')))
                self.assertNotIn(MOVED[7], visible_texts(dialog))
                dialog.close()
                window.close(); self.app.processEvents()

    def test_tracking_window_groups(self):
        from tracking_panel import GUIDES, TrackingPanel
        panel = TrackingPanel()
        self.addCleanup(lambda: (panel.shutdown(), panel.close()))
        panel.show(); self.app.processEvents()
        buttons = [b for b in panel.findChildren(QToolButton) if b.objectName() == 'guideButton']
        titles = {b.property('guideTitle') for b in buttons}
        self.assertTrue(set(GUIDES) <= titles, titles)
        text = visible_texts(panel)
        for sentence in MOVED[8:]:
            self.assertNotIn(sentence, text)
        # state and warnings stay visible
        panel.service.open_dashboard(('127.0.0.1', 0)); panel.refresh()
        self.assertTrue(panel.chips['webmode'].isVisibleTo(panel))
        self.assertTrue(panel.banner.isVisibleTo(panel)); self.assertTrue(panel.state_badge.isVisibleTo(panel))


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


class _Html(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.helps, self.boxes, self.stack, self.outside = {}, [], {}, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'id' in a:
            self.ids[a['id']] = a
        if tag == 'button' and ('help' in (a.get('class') or '').split() or 'guide-q' in (a.get('class') or '').split()):
            self.helps.append(a)
        if 'helpbox' in (a.get('class') or '').split():
            self.boxes[a['id']] = a
        if tag not in VOID:
            inside = bool(self.stack and self.stack[-1])
            self.stack.append(inside or 'helpbox' in (a.get('class') or '').split())

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if data.strip() and not (self.stack and self.stack[-1]):
            self.outside.append(data.strip())


class HtmlGuides(unittest.TestCase):
    def check(self, name, header_id='guide-all'):
        source = (ROOT / name).read_text(encoding='utf-8')
        body = re.sub(r'(?s)<script.*?</script>|<style.*?</style>', '', source)
        parser = _Html(); parser.feed(body)
        self.assertIn(header_id, parser.ids)
        self.assertGreaterEqual(len(parser.helps), 3)
        for button in parser.helps:
            self.assertEqual(button.get('aria-expanded'), 'false')
            self.assertIn(button.get('aria-controls'), parser.boxes)
            self.assertTrue(button.get('aria-label', '').endswith('가이드'))
        for box in parser.boxes.values():
            self.assertIn('hidden', box)
        return '\n'.join(parser.outside)

    def test_tracking_dashboard(self):
        visible = self.check('tracking_dashboard.html')
        for sentence in ('객체 확률이 아닙니다', '자동 재시도하지 않습니다', '고정된 프레임 위를 드래그', 'JPEG 스냅샷'):
            self.assertNotIn(sentence, visible)
        self.assertIn('ARMED · 비행 중일 수 있습니다', visible)      # safety warning stays in the confirm panel

    def test_drive_dashboard(self):
        visible = self.check('drive_dashboard.html')
        for sentence in ('패널 오른쪽 아래를 끌어 크기 조절', 'Fly 또는 Video 4CH에서 영상을 열면',
                         '선택한 정보가 주행 대시보드의 차량 정보 패널에 표시됩니다'):
            self.assertNotIn(sentence, visible)


if __name__ == '__main__':
    unittest.main()
