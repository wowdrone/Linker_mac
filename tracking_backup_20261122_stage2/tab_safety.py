"""Pre-flight safety profiles and vehicle parameter review."""
import csv
import json
import math
from pathlib import Path
import threading
from datetime import datetime, timezone

from PyQt5.QtCore import Qt, pyqtSignal, QStandardPaths
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox,
    QInputDialog, QHeaderView)
from pymavlink.dialects.v20 import ardupilotmega as mav
from safety_parameters import SafetySession
from safety_validation import evaluate_profile


class SafetyTab(QWidget):
    completed = pyqtSignal(object, object, object)

    def __init__(self, linker, parent=None):
        super().__init__(parent)
        self.linker = linker
        self.vehicle = None
        self.profile = None
        self.params = {}
        self.complete = False
        self.mock = False
        self.busy = False
        self.store = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / 'safety'
        self.completed.connect(self._finished)
        self._build()
        linker.activeChanged.connect(self.set_vehicle)

    def _build(self):
        self.setStyleSheet('SafetyTab { font-size: 16px; } QTableWidget, QPushButton, QLabel, QCheckBox { font-size: 16px; } QTableWidget::item { padding: 6px; }')
        layout = QVBoxLayout(self)
        title = QLabel('비행 전 안전 설정 대시보드'); title.setStyleSheet('font-size: 22px; font-weight: bold;'); layout.addWidget(title)
        self.summary = QLabel('기체를 선택하고 파라미터를 읽으세요.'); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        controls = QHBoxLayout()
        for text, method in [('새로 읽기', self.read), ('모의 데이터', self.load_mock), ('기준 적용', self.apply_criteria), ('프로파일 열기', self.load_profile), ('프로파일 저장', self.save_profile), ('복제', self.clone_profile), ('CSV 내보내기', self.export_csv)]:
            button = QPushButton(text); button.clicked.connect(method); controls.addWidget(button)
        self.edit = QCheckBox('수정 모드'); self.edit.toggled.connect(self.refresh); controls.addWidget(self.edit)
        apply = QPushButton('변경 검토 및 적용'); apply.clicked.connect(self.apply); controls.addWidget(apply)
        layout.addLayout(controls)
        self.hint = QLabel('기준값은 JSON 객체로 입력: {"min": 10, "max": 100}, {"equal": 1}, {"allowed": [1, 2]}. 기준 미설정은 확인 필요입니다.')
        self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(['그룹', '설명 / 파라미터', '현재값', '단위', '기준값 JSON', '판정', '변경할 값'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(42)
        layout.addWidget(self.table)

    def set_vehicle(self, vehicle):
        self.vehicle = vehicle; self.params = {}; self.complete = False; self.mock = False; self.edit.setChecked(False)
        if vehicle:
            autopilot = vehicle.autopilot
            from tuner_parameters import COPTER_TYPES
            key = 'ardupilot_copter' if autopilot == mav.MAV_AUTOPILOT_ARDUPILOTMEGA and vehicle.mav_type in COPTER_TYPES else 'px4' if autopilot == mav.MAV_AUTOPILOT_PX4 else None
            if key:
                try:
                    self.profile = json.loads((Path(__file__).resolve().parent / 'config' / 'safety' / (key + '.json')).read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    self.profile = None; self.summary.setText(str(exc))
            else:
                self.profile = None
        else:
            self.profile = None
        self.refresh()

    def _run(self, action, fn):
        if self.busy: return
        self.busy = True; vehicle = self.vehicle
        self.summary.setText('처리 중…')
        def run():
            try: self.completed.emit(action, vehicle, fn())
            except Exception as exc: self.completed.emit(action, vehicle, exc)
        threading.Thread(target=run, daemon=True).start()

    def _session(self, vehicle):
        return SafetySession(vehicle.tuner_endpoint, lambda: self.vehicle is vehicle)

    def read(self):
        if not self.vehicle or not self.profile:
            QMessageBox.warning(self, '안전 점검', '지원되는 기체를 선택하세요.'); return
        vehicle = self.vehicle
        self.params = {}; self.complete = False; self.mock = False
        self._run('read', lambda: self._session(vehicle).read_all())

    def load_mock(self):
        if self.busy: return
        if not self.profile:
            self.profile = json.loads((Path(__file__).resolve().parent / 'config' / 'safety' / 'ardupilot_copter.json').read_text(encoding='utf-8'))
        self.params = {item['parameter']: {'value': 0.0, 'type': mav.MAV_PARAM_TYPE_REAL32} for item in self.profile['items']}
        self.complete = True; self.mock = True; self.edit.setChecked(False); self.refresh(); self.save_record()

    def _finished(self, action, vehicle, result):
        self.busy = False
        if vehicle is not self.vehicle: return
        if isinstance(result, Exception):
            self.summary.setText('실패: ' + str(result)); return
        if action == 'read':
            self.params = result; self.complete = True; self.refresh(); self.save_record()
        elif action == 'apply':
            QMessageBox.information(self, '적용 결과', '\n'.join(f'{name}: {message}' for name, ok, message in result))
            self.params = {}; self.complete = False; self.refresh()
            self.summary.setText('변경 작업 종료. 실제 값을 새로 읽어 확인하세요.')

    def rows(self):
        return evaluate_profile(self.profile, {name: p['value'] for name, p in self.params.items()}, complete=self.complete) if self.profile else []

    def refresh(self):
        rows = self.rows(); self.table.setRowCount(len(rows))
        labels = {'ok':'✔ 정상', 'warn':'⚠ 경고', 'error':'✖ 오류', 'na':'N/A 해당 없음', 'pending':'… 수신 대기', 'unconfigured':'⚠ 기준 미설정'}
        colors = {'ok':'#006b32', 'warn':'#815000', 'error':'#b00020', 'na':'#505050', 'pending':'#505050', 'unconfigured':'#815000'}
        for row, item in enumerate(rows):
            vals = [item['group'], item['description'] + '\n' + item['parameter'], '—' if item['value'] is None else str(item['value']), item.get('unit',''), json.dumps(item.get('criteria'), ensure_ascii=False), labels[item['status']], '']
            for col, val in enumerate(vals):
                cell=QTableWidgetItem(val)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable if col != 4 and not (col == 6 and self.edit.isChecked() and item['value'] is not None) else cell.flags())
                if col == 5:
                    from PyQt5.QtGui import QColor
                    cell.setForeground(QColor('white')); cell.setBackground(QColor(colors[item['status']]))
                self.table.setItem(row,col,cell)
        counts = {s: sum(x['status'] == s for x in rows) for s in ('error','warn','ok','unconfigured','pending')}
        ready = self.complete and bool(rows) and counts['ok'] > 0 and not any(counts[s] for s in ('error','warn','unconfigured','pending'))
        self.summary.setText(f"{'[모의 데이터] ' if self.mock else ''}{self.profile['name'] if self.profile else '지원 기체 미선택'} · 오류 {counts['error']} / 경고 {counts['warn']} / 정상 {counts['ok']} · {'기준 통과' if ready and not self.mock else '확인 필요'}")

    def _update_criteria(self):
        if not self.profile: raise ValueError('프로파일이 없습니다.')
        for i, item in enumerate(self.profile['items']):
            value = json.loads(self.table.item(i,4).text())
            if value is not None and (not isinstance(value,dict) or not set(value).issubset({'min','max','equal','allowed'})):
                raise ValueError(f"{item['parameter']}: 기준값 JSON을 확인하세요.")
            item['criteria'] = value

    def apply_criteria(self):
        try:
            self._update_criteria(); self.refresh()
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, '기준값 오류', str(exc))

    def save_profile(self):
        try: self._update_criteria()
        except (ValueError, TypeError) as exc: QMessageBox.warning(self, '기준값 오류', str(exc)); return
        path,_=QFileDialog.getSaveFileName(self, '프로파일 저장', str(self.store), 'JSON (*.json)')
        if path:
            Path(path).write_text(json.dumps(self.profile, ensure_ascii=False, indent=2, allow_nan=False),encoding='utf-8'); self.refresh()

    def clone_profile(self):
        if not self.profile: return
        name,ok=QInputDialog.getText(self,'프로파일 복제','새 프로파일 이름:')
        if ok and name.strip():
            self.profile['name']=name.strip(); self.save_profile()

    def load_profile(self):
        path,_=QFileDialog.getOpenFileName(self,'프로파일 열기',str(self.store),'JSON (*.json)')
        if not path: return
        try:
            profile=json.loads(Path(path).read_text(encoding='utf-8'))
            if not self.profile or profile['firmware'] != self.profile['firmware'] or not isinstance(profile['items'],list): raise ValueError('현재 기체에 맞는 프로파일이 아닙니다.')
            for item in profile['items']:
                if not all(k in item for k in ('id','group','parameter','description','severity')) or item['severity'] not in ('warn','error'): raise ValueError('점검 항목 형식 오류')
            self.profile=profile; self.refresh()
        except (OSError, ValueError, KeyError, TypeError) as exc: QMessageBox.warning(self,'프로파일 오류',str(exc))

    def apply(self):
        if not self.edit.isChecked() or not self.vehicle or not self.complete or self.mock:
            QMessageBox.warning(self,'변경 불가','기체를 읽고 수정 모드를 켜세요.'); return
        vehicle=self.vehicle
        if vehicle.last_armed: QMessageBox.warning(self,'변경 불가','시동 상태에서는 변경할 수 없습니다.'); return
        changes=[]
        try:
            for i,item in enumerate(self.profile['items']):
                text=self.table.item(i,6).text().strip(); name=item['parameter']
                if text:
                    new=float(text)
                    if not math.isfinite(new) or name not in self.params: raise ValueError(name + ': 값 오류')
                    p=self.params[name]; changes.append((name,p['value'],new,p['type']))
        except (ValueError, KeyError) as exc: QMessageBox.warning(self,'입력 오류',str(exc)); return
        if not changes: return
        preview='\n'.join(f'{name}: {old} → {new}' for name,old,new,_ in changes)
        if QMessageBox.question(self,'파라미터 변경 확인',preview+'\n\n기체에 전송하시겠습니까?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No) != QMessageBox.Yes: return
        self._run('apply',lambda:self._session(vehicle).apply(changes,approved=True))

    def save_record(self):
        try:
            self._update_criteria(); self.store.mkdir(parents=True,exist_ok=True)
            record={'time':datetime.now(timezone.utc).isoformat(),'vehicle':'mock' if self.mock else self.vehicle.key_str,'profile':self.profile['name'],'items':self.rows()}
            path=self.store / ('check-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
            path.write_text(json.dumps(record,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        except (ValueError,OSError) as exc: self.summary.setText('기록 실패: '+str(exc))

    def export_csv(self):
        if not self.profile: return
        path,_=QFileDialog.getSaveFileName(self,'점검 결과 내보내기','safety.csv','CSV (*.csv)')
        if not path: return
        try:
            self._update_criteria()
            with open(path,'w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f);writer.writerow(['시각 UTC','기체','프로파일','그룹','파라미터','설명','현재값','기준값','상태'])
                now=datetime.now(timezone.utc).isoformat()
                for item in self.rows():writer.writerow([now,self.vehicle.key_str if self.vehicle else '',self.profile['name'],item['group'],item['parameter'],item['description'],item['value'],json.dumps(item.get('criteria'),ensure_ascii=False),item['status']])
        except (OSError,ValueError) as exc: QMessageBox.warning(self,'내보내기 실패',str(exc))
