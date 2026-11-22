"""AI Tuner workspace: analysis, review and opt-in parameter transactions."""

from dataclasses import asdict
import json
from pathlib import Path
import threading

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QStandardPaths, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from gui_theme import DEFAULT_THEME, THEMES
from gui_help import help_button, set_guide
from tuner_analysis import AXES, from_bin, from_telemetry
from tuner_parameters import Change, FILTER, Identity, PID, ParameterSession, save_record


def _label(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _display(value):
    return "계산 불가" if value is None else f"{value:.3g}"


class AITunerTab(QWidget):
    jobFinished = pyqtSignal(object, object, object, object)

    def __init__(self, linker=None, parent=None):
        super().__init__(parent)
        self.theme = THEMES[DEFAULT_THEME]
        self.linker = linker
        self.selection = threading.Event()
        self.identity = None
        self.values = {}
        self.changes = {}
        self.result = None
        self.baseline = None
        self.sitl_report = None
        self.busy = False
        self.controls = []
        self.store = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "tuner"
        self.jobFinished.connect(self._finished)
        self._build_ui()
        if linker is not None:
            linker.activeChanged.connect(self._active_changed)
            linker.connectionStateChanged.connect(self._refresh_dashboard)
            linker.armedChanged.connect(self._refresh_dashboard)
        self._active_changed()

    GUIDE = [
        "DataFlash 로그 또는 선택한 기체의 수신 데이터를 분석하세요.",
        "검증된 기체 모델이나 변경 근거가 없는 경우 숫자 추천을 생성하지 않습니다.",
        "쓰기 직전 기체 ID, heartbeat, Disarmed, 지상 상태 및 읽은 값을 재검증합니다.",
        "Dashboard: 기체 식별 읽기 → 기체 프로파일 편집.",
        "Flight Analysis: DataFlash BIN 열기 또는 최근 MAVLink 수신 데이터 분석, 축을 골라 각속도를 봅니다.",
        "PID Tuning: 연결 기체의 PID 파라미터 읽기 → 읽은 PID 값에서 검토 후보 지정 → SITL 비교 결과 가져오기.",
        "Vibration: 연결 기체의 필터 파라미터 읽기, 센서를 골라 FFT를 봅니다.",
        "Apply & Restore: 「이 세션에서 승인된 지상 PID 변경 허용」을 체크해야 Apply가 동작합니다. "
        "Apply는 백업 및 readback 검증, Restore는 동일 기체 백업을 검토합니다.",
    ]

    def _build_ui(self):
        root = QVBoxLayout(self)
        head = QHBoxLayout()
        self.status = _label("분석 대기")
        head.addWidget(self.status, 1)
        head.addWidget(help_button(self, "AI Tuner", self.GUIDE))
        root.addLayout(head)
        set_guide(self, "AI Tuner", self.GUIDE)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        dashboard = self._page("Dashboard")
        self.dashboard = _label()
        dashboard.addWidget(self.dashboard)
        self._button(dashboard, "기체 식별 읽기", lambda: self._read(()))
        self._button(dashboard, "기체 프로파일 편집", self._edit_profile)
        dashboard.addStretch(1)

        flight = self._page("Flight Analysis")
        row = QHBoxLayout()
        self._button(row, "DataFlash BIN 열기", self._open_bin)
        self._button(row, "최근 MAVLink 수신 데이터 분석", self._analyze_live)
        flight.addLayout(row)
        self.axis = QComboBox()
        self.axis.addItems(AXES)
        self.axis.currentTextChanged.connect(self._render_rate)
        flight.addWidget(self.axis)
        self.rate_plot = self._plot("시간 (s)", "각속도 (deg/s)")
        self.rate_plot.addLegend()
        flight.addWidget(self.rate_plot, 1)
        self.rate_text = _label()
        flight.addWidget(self.rate_text)

        pid = self._page("PID Tuning")
        self._button(pid, "연결 기체의 PID 파라미터 읽기", lambda: self._read(tuple(sorted(PID))))
        self._button(pid, "읽은 PID 값에서 검토 후보 지정", self._edit_change)
        self._button(pid, "SITL 비교 결과 가져오기", self._load_sitl)
        self.pid_text = self._output(pid)
        self.pid_text.setPlaceholderText("PID 검토 결과 없음")

        vibration = self._page("Vibration")
        self._button(vibration, "연결 기체의 필터 파라미터 읽기", lambda: self._read(tuple(sorted(FILTER))))
        self.sensor = QComboBox()
        self.sensor.currentTextChanged.connect(self._render_fft)
        vibration.addWidget(self.sensor)
        self.fft_plot = self._plot("주파수 (Hz)", "진폭 (센서 단위)")
        vibration.addWidget(self.fft_plot, 1)
        self.fft_text = _label()
        vibration.addWidget(self.fft_text)

        apply = self._page("Apply & Restore")
        self.safety_text = _label()
        apply.addWidget(self.safety_text)
        self.preview = self._output(apply)
        self.write_opt_in = QCheckBox("이 세션에서 승인된 지상 PID 변경 허용")
        apply.addWidget(self.write_opt_in)
        self._button(apply, "Apply · 백업 및 readback 검증", lambda: self._confirm(False))
        self._button(apply, "Restore · 동일 기체 백업 검토", lambda: self._confirm(True))
        self._button(apply, "변경 목록 비우기", self._clear_changes)

        history = self._page("History")
        row = QHBoxLayout()
        self._button(row, "현재 로그를 비교 기준으로 지정", self._set_baseline)
        self._button(row, "현재 분석 결과를 선택 기체에 기록", self._save_analysis)
        self._button(row, "기체별 이력 새로고침", self._refresh_history)
        history.addLayout(row)
        self.history = self._output(history)
        self._render_rate()
        self._render_fft()

    def _page(self, name):
        widget = QWidget()
        self.tabs.addTab(widget, name)
        return QVBoxLayout(widget)

    def _button(self, layout, caption, slot):
        button = QPushButton(caption)
        button.clicked.connect(slot)
        layout.addWidget(button)
        self.controls.append(button)

    @staticmethod
    def _output(layout):
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        layout.addWidget(editor, 1)
        return editor

    @staticmethod
    def _plot(x, y):
        plot = pg.PlotWidget(background=THEMES[DEFAULT_THEME].field)
        plot.setLabel("bottom", x)
        plot.setLabel("left", y)
        plot.showGrid(x=True, y=True, alpha=.2)
        plot.setDownsampling(auto=True, mode="peak")
        plot.setClipToView(True)
        return plot

    def apply_theme(self, theme):
        self.theme = theme
        for plot in (self.rate_plot, self.fft_plot):
            plot.setBackground(theme.field)
            for edge in ("left", "bottom"):
                axis = plot.getAxis(edge)
                axis.setPen(theme.border)
                axis.setTextPen(theme.muted)
                axis.setLabel(axis.labelText, units=axis.labelUnits, color=theme.muted)
        self.rate_plot.plotItem.legend.setLabelTextColor(theme.text)
        self._render_rate()
        self._render_fft()

    def _active_changed(self, *_):
        self.selection.clear()
        self.selection = threading.Event()
        self.selection.set()
        self.identity, self.values, self.changes = None, {}, {}
        self.result, self.baseline = None, None
        self.sitl_report = None
        self.sensor.clear()
        self._render_rate()
        self._render_fft()
        self.write_opt_in.setChecked(False)
        self._render_params()
        self._refresh_dashboard()
        self._refresh_history()

    def shutdown(self):
        self.selection.clear()

    def _vehicle(self):
        return self.linker.vehicle if self.linker is not None else None

    def _session(self):
        vehicle = self._vehicle()
        if vehicle is None or not vehicle.connected:
            self.status.setText("먼저 연결된 기체를 선택하세요.")
            return None
        return ParameterSession(vehicle.tuner_endpoint, self.selection.is_set)

    def _job(self, work, done, scoped=True):
        if self.busy:
            return
        self.busy = True
        for widget in self.controls:
            widget.setEnabled(False)
        self.status.setText("작업 중…")
        token = self.selection if scoped else None

        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, str(exc)
            try:
                self.jobFinished.emit(done, value, error, token)
            except RuntimeError:
                pass  # Window closed while a read/analysis was finishing.

        threading.Thread(target=run, name="AI Tuner", daemon=False).start()

    def _finished(self, done, value, error, token):
        self.busy = False
        for widget in self.controls:
            widget.setEnabled(True)
        if token is not None and (token is not self.selection or not token.is_set()):
            self.status.setText("선택 기체가 바뀌었습니다. 완료된 작업은 해당 기체의 이력에 기록됩니다.")
            return
        if error is not None:
            self.status.setText(error)
            return
        self.status.setText("작업 완료")
        done(value)

    def _open_bin(self):
        path, _ = QFileDialog.getOpenFileName(self, "DataFlash 로그", "", "DataFlash (*.BIN *.bin)")
        if path:
            self._job(lambda: from_bin(Path(path)), self._set_result)

    def _analyze_live(self):
        vehicle = self._vehicle()
        if vehicle is None:
            self.status.setText("기체를 먼저 선택하세요.")
            return
        self._job(lambda: from_telemetry(vehicle.tuner_endpoint.snapshot()), self._set_result)

    def _set_result(self, result):
        self.result = result
        self.sensor.blockSignals(True)
        self.sensor.clear()
        self.sensor.addItems(result["spectra"])
        self.sensor.blockSignals(False)
        self._render_rate()
        self._render_fft()
        self._render_params()
        self._refresh_dashboard()
        self._refresh_history()

    def _render_rate(self, *_):
        self.rate_plot.clear()
        if not self.result:
            self.rate_text.setText("로그를 열거나 선택 기체에서 수신된 데이터를 분석하세요.")
            return
        rows, metric = self.result["rates"][self.axis.currentText()]
        finite = np.flatnonzero(np.isfinite(rows[:, 0]))
        if len(finite):
            t = rows[:, 0] - rows[finite[0], 0]
            break_at = np.r_[False, np.diff(t) <= 0]
            for i, pen, name in ((1, self.theme.warning, "목표"), (2, self.theme.accent, "실제")):
                values = rows[:, i].copy()
                values[break_at] = np.nan
                self.rate_plot.plot(t, values, pen=pen, name=name, connect="finite")
        summary = []
        for axis, (_, m) in self.result["rates"].items():
            summary.append(f"{axis}  RMS {_display(m['rms'])} deg/s  ·  "
                           f"Overshoot {_display(m['overshoot'])}%  ·  Settling {_display(m['settling'])} s  ·  "
                           f"Oscillation {_display(m['oscillation'])}회  ·  유효 step {m['steps']}")
        self.rate_text.setText("\n".join(summary + self.result["notes"]))

    def _render_fft(self, *_):
        self.fft_plot.clear()
        lines = []
        if self.result:
            spec = self.result["spectra"].get(self.sensor.currentText())
            if spec:
                self.fft_plot.plot(spec["freq"], spec["magnitude"], pen=self.theme.accent)
                lines.append(f"샘플링 {spec['hz']:.1f} Hz · 피크 " +
                             (", ".join(f"{p:.1f} Hz" for p in spec["peaks"]) or "검출되지 않음"))
                for peak in spec["peaks"]:
                    self.fft_plot.addLine(x=peak, pen=self.theme.warning)
            if self.result.get("onboard_fft"):
                lines.append(f"기체 FFT 로그의 피크 중앙값 {self.result['onboard_fft']:.1f} Hz")
            if self.result.get("esc_hz"):
                lo, hi = self.result["esc_hz"]
                lines.append(f"ESC 회전 주파수 관측 범위 {lo:.1f}–{hi:.1f} Hz")
            if self.result.get("onboard_fft") and isinstance(self.result["params"].get("INS_HNTCH_MODE"), float):
                lines.append("필터 검토 후보: 기체 내 FFT 추적. 같은 로그에 FFT 피크와 notch 모드가 기록됐습니다. "
                             "정확한 빌드에서 모드 지원·FFT 건강도·반복성을 확인하기 전에는 값이나 모드를 제안하지 않습니다.")
        if not lines:
            lines.append("검증된 원시 샘플 또는 기체 FFT 데이터가 없습니다.")
        lines.append("피크 하나만으로 모터, 센서 노이즈, 제어 불안정을 구분할 수 없습니다.")
        logged = self.result.get("params", {}) if self.result else {}
        for name in sorted(FILTER):
            if name in self.values or name in logged:
                lines.append(f"{name} · 로그 {logged.get(name, '—')} · 현재 {self.values.get(name, '—')}")
        self.fft_text.setText("\n".join(lines))

    def _read(self, names):
        session = self._session()
        if session:
            self._job(lambda: session.inspect(names), self._read_done)

    def _read_done(self, response):
        identity, values = response
        if identity != self.identity:
            self.values.clear()
            self.changes.clear()
            self.write_opt_in.setChecked(False)
        self.identity = identity
        self.values.update(values)
        self._refresh_dashboard()
        self._render_params()
        self._render_fft()
        self._refresh_history()

    def _render_params(self):
        logged = self.result.get("params", {}) if self.result else {}
        rows = [f"{name:<16}  로그 {logged.get(name, '—')}  ·  현재 {self.values.get(name, '—')}"
                for name in sorted(PID) if name in logged or name in self.values]
        rows.append("자동 수치 추천: 보류. 폐루프 추종 지표만으로 안전한 게인 변화 방향을 식별할 수 없습니다.")
        rows.append("사용자 후보의 위험도·예상 효과·신뢰도는 미검증입니다. SITL에서 기준과 비교하세요.")
        if self.sitl_report:
            report = self.sitl_report
            rows.append(f"SITL 비교 파일 · {report['firmware']} · {report['status']}")
            rows.append(f"비교 가능 축: {', '.join(report['comparable_axes']) or '없음'}")
            logged_changes = report["logged_gain_changes"]
            matched = [name for name, c in self.changes.items()
                       if name in logged_changes and np.allclose(logged_changes[name], [c.old, c.new],
                                                                 rtol=1e-6, atol=0.)]
            rows.append(f"후보 값과 일치하는 로그 변경: {', '.join(matched) or '없음'}. 비교 자료는 사용자 검토가 필요합니다.")
        self.pid_text.setPlainText("\n".join(rows))
        self.preview.setPlainText("\n".join(
            f"{c.name}   {c.old:.8g} → {c.new:.8g}   변화율 "
            + (f"{100*(c.new-c.old)/c.old:+.2f}%" if c.old else "계산 불가 (기존값 0)")
            for c in self.changes.values()) or "검토할 PID 변경 목록이 비어 있습니다.")

    def _edit_change(self):
        choices = sorted(k for k, value in self.values.items() if k in PID and isinstance(value, float))
        if not choices:
            self.status.setText("지원되는 현재 PID 값을 먼저 읽으세요.")
            return
        name, ok = QInputDialog.getItem(self, "PID 후보", "파라미터", choices, editable=False)
        if not ok:
            return
        value, ok = QInputDialog.getText(self, "PID 후보", f"{name}의 검토 값")
        if ok:
            try:
                candidate = float(value)
                if not np.isfinite(candidate):
                    raise ValueError
            except ValueError:
                self.status.setText("유한한 수치를 입력하세요.")
                return
            self.changes[name] = Change(name, self.values[name], candidate)
            self._render_params()

    def _clear_changes(self):
        self.changes.clear()
        self._render_params()

    def _load_sitl(self):
        path, _ = QFileDialog.getOpenFileName(self, "SITL 비교 결과", "", "JSON (*.json)")
        if not path:
            return
        try:
            report = json.loads(Path(path).read_text(encoding="utf-8"))
            if (not isinstance(report["firmware"], str) or
                    report["status"] not in ("operator_review_required", "insufficient_evidence") or
                    not isinstance(report["comparable_axes"], list) or
                    not all(axis in AXES for axis in report["comparable_axes"])):
                raise ValueError("SITL 비교 결과 형식이 일치하지 않습니다.")
            for name, pair in report["logged_gain_changes"].items():
                if name not in PID or np.asarray(pair, dtype=float).shape != (2,) or not np.isfinite(pair).all():
                    raise ValueError("SITL 비교 파일의 변경값이 올바르지 않습니다.")
        except (OSError, KeyError, AttributeError, TypeError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self.sitl_report = report
        self._render_params()

    def _refresh_dashboard(self, *_):
        vehicle, identity = self._vehicle(), self.identity
        target = f"SYS {vehicle.sysid} / COMP {vehicle.compid} · {vehicle.link_name}" if vehicle else "기체 미선택"
        state = ("Armed" if vehicle.last_armed else "Disarmed") if vehicle and vehicle.connected else "연결 대기"
        fw = (f"{'ArduCopter' if identity.copter else f'MAV_AUTOPILOT {identity.autopilot}'} "
              f"{identity.firmware} · {identity.uid or '고유 ID 없음'}\n"
              f"version {identity.version} · build {identity.build} · board {identity.board} · "
              f"autopilot {identity.autopilot} · vehicle_type {identity.vehicle_type}"
              if identity else "기체 식별 읽기 필요")
        source = self.result["source"] if self.result else "분석 대기"
        valid = [a for a, (_, m) in self.result["rates"].items() if m["rms"] is not None] if self.result else []
        history = "없음"
        if identity and identity.uid:
            files = list((self.store / identity.uid).glob("transaction-*.json"))
            if files:
                history = max(files).stem
        self.dashboard.setText(f"{target}\n{state}\nFirmware {fw}\n{source}\n"
                               f"RMS 계산 가능 축: {', '.join(valid) or '없음'}\n마지막 튜닝 이력: {history}")
        self.safety_text.setText(f"{target}\n{state}\n{fw}")

    def _confirm(self, restore):
        identity, session = self.identity, self._session()
        if not session or not identity or not identity.uid or not self.write_opt_in.isChecked():
            self.status.setText("고유 ID를 읽고 이 세션의 쓰기를 명시적으로 허용하세요.")
            return
        if restore:
            path, _ = QFileDialog.getOpenFileName(self, "동일 기체 백업", str(self.store / identity.uid), "JSON (*.json)")
            if not path:
                return
            try:
                backup = json.loads(Path(path).read_text(encoding="utf-8"))
                if Identity(**backup["identity"]) != identity:
                    raise ValueError("기체 ID 또는 빌드가 일치하지 않습니다.")
                changes = tuple(Change(name, self.values[name], float(value))
                                for name, value in backup["values"].items()
                                if float(value) != self.values[name])
                if any(not isinstance(c.old, float) for c in changes):
                    raise ValueError("복원 대상의 현재 값을 먼저 읽으세요.")
            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.status.setText(f"백업 검토 실패: {exc}")
                return
        else:
            changes = tuple(c for c in self.changes.values() if c.old != c.new)
        if not changes:
            self.status.setText("변경할 값이 없습니다.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "일치하는 펌웨어 파라미터 메타데이터", "", "JSON (*.json)")
        if not path:
            return
        try:
            metadata = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        preview = "\n".join(f"{c.name}: {c.old:.8g} → {c.new:.8g}" for c in changes)
        verb = "복원" if restore else "적용"
        if QMessageBox.question(self, f"{verb} 승인",
                                f"SYS {session.ids[0]} · COMP {session.ids[1]} · {identity.uid}\n\n{preview}\n\n"
                                f"변경 전 값을 파일로 백업하고 {verb}합니까?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.write_opt_in.setChecked(False)
        self._job(lambda: session.transact(identity, changes, metadata, self.store, approved=True),
                  self._transaction_done)

    def _transaction_done(self, report):
        self.status.setText(report["error"] or f"Readback 검증 완료: {', '.join(report['verified'])}")
        self.values.clear()
        self._clear_changes()
        self._refresh_dashboard()
        self._refresh_history()

    def _edit_profile(self):
        if not self.identity or not self.identity.uid:
            self.status.setText("기체 고유 ID를 먼저 읽으세요.")
            return
        path = self.store / self.identity.uid / "profile.json"
        keys = ("중량", "모터", "프로펠러", "배터리", "임무 탑재장비")
        try:
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            form = "\n".join(f"{k}: {old.get(k, '')}" for k in keys)
            value, ok = QInputDialog.getMultiLineText(self, "기체 프로파일", "\n".join(keys), form)
            if ok:
                data = {k.strip(): v.strip() for line in value.splitlines() if ":" in line
                        for k, v in [line.split(":", 1)] if k.strip() in keys}
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(path)
        except (ValueError, OSError) as exc:
            self.status.setText(f"프로파일 오류: {exc}")

    @staticmethod
    def _metrics(result):
        return {axis: metrics for axis, (_, metrics) in result["rates"].items()}

    def _set_baseline(self):
        if self.result:
            self.baseline = self.result["source"], self._metrics(self.result)
            self._refresh_history()

    def _save_analysis(self):
        if not self.result or not self.identity or not self.identity.uid:
            self.status.setText("분석 결과와 기체 고유 ID가 필요합니다.")
            return
        if QMessageBox.question(self, "로그 이력 연결", "현재 분석 결과를 선택 기체의 기록으로 연결합니까?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        result, identity = self.result, self.identity
        self._job(lambda: save_record(self.store / identity.uid, "analysis",
                                      dict(source=result["source"], association="user",
                                           firmware=result["firmware"], metrics=self._metrics(result))),
                  lambda _: self._refresh_history())

    def _refresh_history(self):
        lines = []
        if self.baseline and self.result:
            source, before = self.baseline
            lines.append(f"{source} → {self.result['source']}")
            for axis, after in self._metrics(self.result).items():
                lines.append(f"{axis}: " + " · ".join(
                    f"{k} {_display(before[axis][k])} → {_display(after[k])}"
                    for k in ("rms", "overshoot", "settling", "oscillation")))
        if self.identity and self.identity.uid:
            folder = self.store / self.identity.uid
            for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if path.name.startswith("transaction-"):
                        state = data.get("state", "unknown")
                        lines.append(f"{path.stem} · {state} · 검증 {data['verified']} · {data['error'] or ''}\n"
                                     f"  변경 {data['requested']} · 백업 {data['backup']}")
                    elif path.name.startswith("analysis-"):
                        lines.append(f"{path.stem} · {data['source']} · {data['metrics']}")
                    else:
                        lines.append(path.stem)
                except (OSError, KeyError, ValueError):
                    lines.append(f"{path.name} · 읽기 실패")
        self.history.setPlainText("\n".join(lines) or "비교 기준 또는 현재 기체의 저장 이력이 없습니다.")
