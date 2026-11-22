"""PyInstaller spec bundles every Tracking module/resource and never the backup folders."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / 'Linker_mac.spec').read_text()
        tree = ast.parse(self.text)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'Analysis')
        kw = {k.arg: k.value for k in call.keywords}
        self.hidden = {e.value for e in kw['hiddenimports'].elts}
        self.datas = [tuple(getattr(x, 'value', None) for x in e.elts) for e in kw['datas'].elts]
        self.pathex = [e.value for e in kw['pathex'].elts]

    def test_tracking_modules_and_html(self):
        modules = {p.stem for p in ROOT.glob('tracking_*.py')} | {'dashboard_server', 'flight_mode_controller', 'gui_help', 'video_health'}
        self.assertTrue(modules <= self.hidden, modules - self.hidden)
        self.assertIn(('tracking_dashboard.html', '.'), self.datas)
        self.assertIn(('drive_dashboard.html', '.'), self.datas)
        for name in ('tracking_dashboard.html', *(m + '.py' for m in modules)):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_backups_and_validation_not_bundled(self):
        for entry in [d[0] for d in self.datas if d[0]] + self.pathex:
            self.assertNotRegex(entry, r'tracking_backup|tracking_validation|\.tgz|build_backup')
        self.assertNotIn('tracking_backup', self.text)


if __name__ == '__main__':
    unittest.main()
