from __future__ import annotations
import sys
import time
from dataclasses import asdict
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, QSettings, QStandardPaths, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget, QInputDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox, QToolButton,)
from delsys.collection import CollectionInfoRecorder, SubjectInfo, analysisOutputPath, infoOutputPath
from delsys.emg.body_map import BodyMapPanel
from delsys.emg.sensor_mapping import (SensorMapping,duplicateMappingSlots,loadMappings,saveMappings,matchesCsvColumn,recordingChannelSides,)
from delsys.emg.trigno import CHANNEL_COUNT, SAMPLE_RATE
from delsys.emg.baseline import BaselineManager, CalibrationSession, EMGPreprocessor, FilterSettings, RMSFeatureExtractor, load_raw_emg_csv
from delsys.emg.workers import ConnectionWorker, StreamWorker
from delsys.resistance.recording import TrainerCsvRecorder, trainerOutputPath
from delsys.resistance.trainer import BikeTelemetry, NumericRange, TrainerWorker
from delsys.pedal.recording import PedalCsvRecorder, pedalOutputPath
from delsys.pedal.analysis import analyze_pedal_records, pedal_analysis_lines, pedal_summary_lines, summarize_pedal_records
from delsys.pedal.worker import PedalWorker
from delsys.pedal.fit_export import exportPedalFit
from delsys.pedal.fit_import import load_pedal_fit
from delsys.pedal.angle_emg import (AngleSettings, analysis_summary_text, analyze_angle_emg,export_angle_emg, phase_intensities)
from delsys.cycling_bluetooth import CyclingDeviceScanWorker
from delsys.fluent_theme import FluentThemeManager
from delsys.session_analysis import (add_point, analyze, compare_rms, export_html,load_session, new_session, peak_envelope,save_session)
from delsys.session_compare import compare_sessions
from delsys.storage_db import DatabaseConfig, DatabaseWriter, SecondAggregator, SqlServerStorage, prefixed_id

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().parents[2] / "sensor_mapping.json"

class AngleAnalysisWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    def __init__(self, emg_path: Path, pedal_path: Path, sides: dict[str, str],
                 settings: AngleSettings, side_source: str) -> None:
        super().__init__()
        self.emg_path, self.pedal_path, self.sides, self.settings = emg_path, pedal_path, sides, settings
        self.side_source = side_source
    def run(self) -> None:
        try:
            result = analyze_angle_emg(self.emg_path, self.pedal_path, self.sides, self.settings)
            result["sync"]["side_source"] = self.side_source
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

class AngleExportWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)
    def __init__(self, result: dict, directory: Path, include_raw: bool) -> None:
        super().__init__()
        self.result, self.directory, self.include_raw = result, directory, include_raw
    def run(self) -> None:
        try:
            export_angle_emg(self.result, self.directory, self.include_raw)
            self.completed.emit(str(self.directory))
        except Exception as exc:
            self.failed.emit(str(exc))

class EMGAnalysisImportWorker(QThread):
    completed = Signal(object, str)
    failed = Signal(str)
    def __init__(self, path: Path, mappings: list[SensorMapping], baselines: dict,subject: dict, settings: FilterSettings) -> None:
        super().__init__()
        self.path = path
        self.mappings = mappings
        self.baselines = baselines
        self.subject = subject
        self.settings = settings

    def run(self) -> None:
        try:
            times, columns, sample_rate = load_raw_emg_csv(self.path, limit_seconds=None)
            channel_columns: list[tuple[int, str]] = []
            for channel, mapping in enumerate(self.mappings):
                header = next((name for name in columns if matchesCsvColumn(mapping, name)), None)
                if header is not None:
                    channel_columns.append((channel, header))
            if not channel_columns:
                raise ValueError("CSV does not contain a mapped EMG channel.")

            session = new_session(self.subject, self.baselines, sample_rate)
            session["filter"] = asdict(self.settings)
            session["raw_files"] = {"emg": self.path.name}
            session["channels"] = {
                str(channel + 1): {"mapping_name": f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}",
                                   "label": mapping.chinese_name,
                                   "sensor_serial": "Imported CSV"}
                for channel, mapping in ((channel, self.mappings[channel]) for channel, _ in channel_columns)
            }
            frames = np.column_stack([columns[header] for _, header in channel_columns])
            preprocessor = EMGPreprocessor(len(channel_columns), sample_rate, self.settings)
            extractor = RMSFeatureExtractor(len(channel_columns), sample_rate)
            update_samples = max(1, round(sample_rate * 0.2))
            for start in range(0, len(frames), update_samples):
                end = min(len(frames), start + update_samples)
                rms_values = extractor.add(preprocessor.process(frames[start:end]))
                for compact_channel, (channel, _header) in enumerate(channel_columns):
                    mapping = self.mappings[channel]
                    record = self.baselines.get(str(channel + 1))
                    mapping_name = f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"
                    compatible = bool(record and record.get("mapping_name") == mapping_name
                                      and abs(float(record.get("sample_rate_hz", 0)) - sample_rate) <= 0.01
                                      and record.get("filter") == asdict(self.settings))
                    comparison = compare_rms(rms_values.get(compact_channel),
                                             record.get("rms_mv") if compatible else None)
                    if record is not None and not compatible:
                        comparison["quality"] = "基準不相容"
                    add_point(session, "emg", {
                        "time_s": float(times[end - 1]), "slot": channel + 1,
                        "rms_mv": rms_values.get(compact_channel), **comparison,})
            self.completed.emit(session, str(self.path))
        except (OSError, ValueError, TypeError) as exc:
            self.failed.emit(str(exc))

def baselineSettingsPath() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(location) / "emg_baseline_settings.json"

class SensorLegendToggle(QWidget):
    toggled = Signal(bool)
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = False
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.title = QLabel(objectName="sensorLegendTitle")
        self.english = QLabel(objectName="sensorLegendEnglish")
        for label in (self.title, self.english):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.english)

    def setLabels(self, title: str, english: str) -> None:
        self.title.setText(title)
        self.english.setText(english)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.toggled.emit(checked)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            event.accept()
            return
        super().mousePressEvent(event)


class PowerBalanceRing(QWidget):
    def __init__(self, side: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.side = side
        self.balance: float | None = None
        self.power_w: float | None = None
        self.active_color = QColor("#107c10")
        self.track_color = QColor("#d1d1d1")
        self.text_color = QColor("#242424")
        # Keep the summary visual compact while retaining enough space for
        # the power and balance labels at normal desktop text scaling.
        self.setMinimumSize(145, 170)
        self.setMaximumSize(165, 185)
        self.setAccessibleName(f"{side}腳估計功率與平衡")

    def setValues(self, balance: float | None, power_w: float | None) -> None:
        self.balance = balance if balance is not None and 0 <= balance <= 100 else None
        self.power_w = power_w
        if self.balance is None or self.power_w is None:
            self.setAccessibleDescription("沒有有效的左右平衡資料。")
        else:
            self.setAccessibleDescription(f"平均估計功率 {self.power_w:.1f} W，平衡 {self.balance:.1f}%。")
        self.update()

    def setThemeColors(self, active: str, track: str, text: str) -> None:
        self.active_color = QColor(active)
        self.track_color = QColor(track)
        self.text_color = QColor(text)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        content = self.rect().adjusted(12, 12, -12, -12)
        diameter = min(content.width(), content.height())
        ring = QRectF(content.center().x() - diameter / 2, content.center().y() - diameter / 2,diameter, diameter,)
        pen = QPen(self.track_color, 18)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring)
        if self.balance is not None:
            pen.setColor(self.active_color)
            painter.setPen(pen)
            painter.drawArc(ring, 90 * 16, -round(self.balance * 3.6 * 16))
        painter.setPen(self.text_color)
        def label_rect(ratio: float) -> QRectF:
            return QRectF(ring.left(), ring.top() + diameter * ratio, diameter, 19)
        painter.drawText(label_rect(.20), Qt.AlignmentFlag.AlignHCenter, self.side)
        power_text = "--" if self.power_w is None else f"{self.power_w:.1f} W"
        painter.drawText(label_rect(.42), Qt.AlignmentFlag.AlignHCenter, power_text)
        balance_text = "無有效資料" if self.balance is None else f"平衡 {self.balance:.1f}%"
        painter.drawText(label_rect(.64), Qt.AlignmentFlag.AlignHCenter, balance_text)

class MappingEditorDialog(QDialog):
    def __init__(self, mappings: list[SensorMapping], active_slots: set[int], baseline_records: dict[int, object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯感測器／肌肉對應")
        self.resize(920, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("基準 RMS 使用 mV；可直接修改，留白表示未設定，清空既有數字會移除該通道基準。", objectName="muted"))
        layout.addWidget(QLabel("修改後按「儲存並套用」。SDK Slot 是固定編號，無法變更。"))
        headings = QGridLayout()
        for column, title in enumerate(("Slot", "狀態", "中文肌肉名稱", "English EMG name", "縮寫", "基準 RMS (mV)")):
            headings.addWidget(QLabel(title, objectName="editorHeader"), 0, column)
        layout.addLayout(headings)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        self.edits: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        self.baselineEdits: list[QLineEdit] = []
        self._initialBaselines: list[float | None] = []
        for row, mapping in enumerate(mappings):
            grid.addWidget(QLabel(f"S{mapping.slot:02d}"), row, 0)
            state = QLabel("● 啟用" if mapping.slot in active_slots else "未啟用")
            state.setStyleSheet("color: #16a34a; font-weight: 700;" if mapping.slot in active_slots else "color: #94a3b8;")
            grid.addWidget(state, row, 1)
            chinese, english, abbreviation = QLineEdit(mapping.chinese_name), QLineEdit(mapping.english_name), QLineEdit(mapping.abbreviation)
            grid.addWidget(chinese, row, 2)
            grid.addWidget(english, row, 3)
            grid.addWidget(abbreviation, row, 4)
            record = baseline_records.get(mapping.slot)
            initial = record.rms_mv if record is not None else None
            baseline = QLineEdit("" if initial is None else f"{initial:.6f}")
            baseline.setPlaceholderText("未設定")
            baseline.setAccessibleName(f"S{mapping.slot:02d} 基準 RMS (mV)")
            grid.addWidget(baseline, row, 5)
            self.edits.append((chinese, english, abbreviation))
            self.baselineEdits.append(baseline)
            self._initialBaselines.append(initial)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Save).setText("儲存並套用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mappings(self) -> list[SensorMapping]:
        return [
            SensorMapping(slot, chinese.text().strip() or "未指定", english.text().strip() or "UNASSIGNED", abbreviation.text().strip())
            for slot, (chinese, english, abbreviation) in enumerate(self.edits, 1) ]

    def baselineChanges(self) -> dict[int, float | None]:
        changes: dict[int, float | None] = {}
        for slot, (edit, initial) in enumerate(zip(self.baselineEdits, self._initialBaselines, strict=True), 1):
            text = edit.text().strip()
            value = None if not text else float(text)
            if value != initial:
                changes[slot] = value
        return changes

    def accept(self) -> None:
        duplicates = duplicateMappingSlots(self.mappings())
        if duplicates:
            conflicts = "\n".join(f"{muscle.title()} ({side.upper() or '?' }): "+ ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items())
            QMessageBox.warning(self,"重複肌肉對應","同一側的同一肌肉只能對應一個感測器：\n" + conflicts,)
            return
        try:
            for value in self.baselineChanges().values():
                if value is not None and (not np.isfinite(value) or value <= 0):
                    raise ValueError
        except ValueError:
            QMessageBox.warning(self, "基準值無效", "基準RMS必須是大於0的數值，或留白以移除。")
            return
        super().accept()


class BaselineSettingsDialog(QDialog):
    """Show saved baseline RMS values and configure deviation thresholds."""

    def __init__(self, mappings: list[SensorMapping], manager: BaselineManager,
                 settings: FilterSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("基準值與判定設定")
        self.resize(840, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        description_row = QHBoxLayout()
        description_row.addWidget(QLabel("顯示此電腦已儲存的基準 RMS；尚未匯入或量測的肌肉會顯示「--」。",
                                         objectName="muted"), 1)
        self.import_requested = False
        import_button = QPushButton("匯入基準 CSV")
        FluentThemeManager.set_property(import_button, "fluentAppearance", "primary")
        import_button.clicked.connect(self.request_import)
        description_row.addWidget(import_button)
        layout.addLayout(description_row)

        self.table = QTableWidget(0, 6)
        table = self.table
        table.setHorizontalHeaderLabels(("肌肉／側別", "基準 RMS", "來源", "取樣率", "濾波", "狀態"))
        table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        rows = [mapping for mapping in mappings
                if mapping.english_name.strip().casefold() != "unassigned"
                or mapping.slot in manager.records]
        self._row_slots = []
        self._initial_baselines: dict[int, float | None] = {}
        table.setRowCount(len(rows))
        for row, mapping in enumerate(rows):
            self._row_slots.append(mapping.slot)
            record = manager.records.get(mapping.slot)
            self._initial_baselines[mapping.slot] = record.rms_mv if record is not None else None
            identity = f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"
            if record is None:
                baseline, source, sample_rate, filter_text, status = "--", "--", "--", "--", "尚無基準"
            else:
                baseline = f"{record.rms_mv:.4f} mV"
                source = {"imported_csv": "匯入 CSV", "measured": "量測", "manual": "手動"}.get(
                    record.source, record.source)
                sample_rate = f"{record.sample_rate_hz:.1f} Hz"
                notch = record.filter.get("notch_hz")
                filter_text = (f"{record.filter.get('highpass_hz', '--')}–"
                               f"{record.filter.get('lowpass_hz', '--')} Hz"
                               + (f"；陷波 {notch:g} Hz" if notch else ""))
                if record.mapping_name != identity:
                    status = "肌肉對應已變更"
                elif record.filter != asdict(settings):
                    status = "濾波設定已變更"
                else:
                    status = "可用"
            values = (f"S{mapping.slot:02d} {mapping.chinese_name}（{mapping.abbreviation or mapping.english_name}）",
                      baseline, source, sample_rate, filter_text, status)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column != 1:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setToolTip("雙擊後可輸入新的基準 RMS（mV）。")
                table.setItem(row, column, item)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, table.columnCount()):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(table, 1)

        threshold_frame = QFrame(objectName="panel")
        threshold_layout = QGridLayout(threshold_frame)
        threshold_layout.setContentsMargins(12, 10, 12, 10)
        threshold_layout.addWidget(QLabel("偏離判定", objectName="sectionTitle"), 0, 0, 1, 4)
        self.warning_spin = QDoubleSpinBox()
        self.warning_spin.setRange(0.1, 1000.0)
        self.warning_spin.setDecimals(1)
        self.warning_spin.setSuffix(" %")
        self.warning_spin.setValue(manager.warning_percent)
        self.focus_spin = QDoubleSpinBox()
        self.focus_spin.setRange(0.2, 1000.0)
        self.focus_spin.setDecimals(1)
        self.focus_spin.setSuffix(" %")
        self.focus_spin.setValue(manager.focus_percent)
        self.warning_spin.valueChanged.connect(self._update_focus_minimum)
        self._update_focus_minimum(self.warning_spin.value())
        threshold_row = QHBoxLayout()
        threshold_row.setSpacing(6)
        threshold_row.addWidget(QLabel("警告偏離"))
        threshold_row.addWidget(self.warning_spin)
        threshold_row.addSpacing(16)
        threshold_row.addWidget(QLabel("重點察看偏離"))
        threshold_row.addWidget(self.focus_spin)
        threshold_row.addStretch()
        threshold_layout.addLayout(threshold_row, 1, 0, 1, 4)
        threshold_layout.addWidget(QLabel("偏離提示固定啟用；偏離以目前 RMS 相對於基準 RMS 的百分比計算。", objectName="muted"),
                                   2, 0, 1, 4)
        layout.addWidget(threshold_frame)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("儲存設定")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_focus_minimum(self, warning: float) -> None:
        self.focus_spin.setMinimum(warning + 0.1)

    def request_import(self) -> None:
        self.import_requested = True
        self.accept()

    def values(self) -> tuple[float, float]:
        return self.warning_spin.value(), self.focus_spin.value()

    def baseline_changes(self) -> dict[int, float]:
        changes: dict[int, float] = {}
        for row, slot in enumerate(self._row_slots):
            text = self.table.item(row, 1).text().replace("mV", "").strip()
            if text in {"", "--"}:
                continue
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError(f"S{slot:02d} 的基準 RMS 必須是數字。") from exc
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"S{slot:02d} 的基準 RMS 必須大於 0。")
            if value != self._initial_baselines[slot]:
                changes[slot] = value
        return changes

    def accept(self) -> None:
        try:
            self.baseline_changes()
        except ValueError as exc:
            QMessageBox.warning(self, "基準值格式無效", str(exc))
            return
        super().accept()


class PedalSummaryDialog(QDialog):
    def __init__(self, summary: dict[str, float | int | None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("本次騎乘摘要")
        self.resize(480, 310)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("本次踏板功率／踏頻資料", objectName="sectionTitle"))
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText("\n".join(pedal_summary_lines(summary)))
        layout.addWidget(details, 1)
        note = QLabel("左右功率由總功率與踏板回報的左右平衡估算，並非 Power Phase。", objectName="muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EMG Cycling Monitor")
        self.resize(1500, 920)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
        self.emgTested = False
        self.trainerWorker = TrainerWorker()
        self.trainerIsConnected = False
        self.pedalWorker = PedalWorker()
        self.pedalIsConnected = False
        self.bluetoothScanWorker: CyclingDeviceScanWorker | None = None
        self.trainerBluetoothReady = False
        self.pedalBluetoothReady = False
        self.initialBluetoothScanStarted = False
        self.selectedScannedDevice: tuple[str, str] | None = None
        self.settings = QSettings("Delsys", "EMG Cycling Monitor")
        self.savedPedalAddress = str(self.settings.value("pedal/address", ""))
        self.pedalScanInProgress = False
        self.histories = [deque(maxlen=int(SAMPLE_RATE * 20)) for _ in range(CHANNEL_COUNT)]
        self.mappings = loadMappings(MAPPING_PATH)
        self.sensorChecks: list[SensorLegendToggle] = []
        self.sensorIcons: list[QLabel] = []
        self.sensorRows: list[QFrame] = []
        self.sensorActive = [False] * CHANNEL_COUNT
        self.sensorSerials: dict[int, str] = {}
        self.curves: dict[int, tuple[pg.PlotItem, pg.PlotDataItem, pg.TextItem]] = {}
        self.collectionSummary: tuple[int, float, float] | None = None
        self.collectionError: str | None = None
        self.collectionStoppedEarly = False
        self.collectionOutput: Path | None = None
        self.collectionSubject = SubjectInfo()
        self.infoRecorder: CollectionInfoRecorder | None = None
        self.infoOutput: Path | None = None
        self.trainerRecorder: TrainerCsvRecorder | None = None
        self.trainerOutput: Path | None = None
        self.pedalRecorder: PedalCsvRecorder | None = None
        self.pedalOutput: Path | None = None
        self.latestPedalRecords: list[dict] = []
        self.summaryRecords: list[dict] = []
        self.pedalSummarySource = "current"
        self.rideSession: dict | None = None
        self.latestRideSession: dict | None = None
        self.collectedRideSession: dict | None = None
        self.referenceRideSession: dict | None = None
        self.rideStartedAt = 0.0
        self.lastEmgAnalysisAt = 0.0
        self.databaseWriter: DatabaseWriter | None = None
        self.databaseWriters: set[DatabaseWriter] = set()
        self.databaseAggregator: SecondAggregator | None = None
        self.databaseTestId: str | None = None
        self.databaseLastTestId: str | None = None
        self.databaseDiscardAfterWriter: dict[DatabaseWriter, str] = {}
        self.databaseStartedAt: datetime | None = None
        self.activeDatabaseConfig: DatabaseConfig | None = None
        self.discardCurrentCollection = False
        self.bikeOnlySession = False
        self.commandedNativeResistance: float | None = None
        self.currentSampleRate = SAMPLE_RATE
        self.baselineManager = BaselineManager(baselineSettingsPath())
        self.baselineManager.thresholds_enabled = True
        self.filterSettings = FilterSettings()
        self.preprocessor: EMGPreprocessor | None = None
        self.rmsExtractor: RMSFeatureExtractor | None = None
        self.emgAnalysisImportWorker: EMGAnalysisImportWorker | None = None
        self.angleAnalysisWorker: AngleAnalysisWorker | None = None
        self.angleExportWorker: AngleExportWorker | None = None
        self.angleAnalysisResult: dict | None = None
        self.angleEmgPath: Path | None = None
        self.anglePedalPath: Path | None = None
        self.calibrationSession: CalibrationSession | None = None
        self.calibrationOnly = False
        self.calibrationRecording = False
        # Per-slot live comparison state drives the body-map research cue.
        self.muscleStatuses: dict[int, str] = {}
        self.baselineMetricLabels: list[QLabel] = []
        self.baselineStatusLabels: list[QLabel] = []
        self.lastTrainerDistanceMeters: int | None = None
        self.collectionDistanceBaselineMeters: int | None = None
        self.collectionDistanceMeters: int | None = None
        self.distanceSessionActive = False
        self.buildUi()
        if hasattr(QApplication.instance().styleHints(), "colorSchemeChanged"):
            QApplication.instance().styleHints().colorSchemeChanged.connect(self.onSystemThemeChanged)
        self.applySummaryPlotTheme()
        self.connectTrainerSignals()
        self.trainerWorker.start()
        self.connectPedalSignals()
        self.pedalWorker.start()
        self.connectionStateTimer = QTimer(self)
        self.connectionStateTimer.setInterval(500)
        self.connectionStateTimer.timeout.connect(self.refreshConnectionStates)
        self.connectionStateTimer.start()
        self.analysisTimer = QTimer(self)
        self.analysisTimer.setInterval(1000)
        self.analysisTimer.timeout.connect(self.refreshLiveRideSummary)
        self.analysisTimer.start()
        self.setStreaming(False)
        self.setEmgStatus("尚未連線", "idle")
        self.setTrainerConnectionState(False)
        self.setPedalConnectionState(False)
        self.setEmgConnectionState(False)
        self.setDatabaseConnectionState(False)

    def buildUi(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 12)
        self.tabs = QTabWidget()
        statusCorner = QWidget()
        header = QHBoxLayout(statusCorner)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.trainerConnectionLabel = QLabel(objectName="connectionIndicator")
        self.pedalConnectionLabel = QLabel(objectName="connectionIndicator")
        self.emgConnectionLabel = QLabel(objectName="connectionIndicator")
        self.databaseConnectionLabel = QLabel(objectName="connectionIndicator")
        header.addWidget(self.trainerConnectionLabel)
        header.addWidget(self.pedalConnectionLabel)
        header.addWidget(self.emgConnectionLabel)
        header.addWidget(self.databaseConnectionLabel)
        self.tabs.setCornerWidget(statusCorner, Qt.Corner.TopRightCorner)
        self.resistanceTab = self.createResistanceTab()
        self.trainerTab = self.createTrainerTab()
        self.collectionTab = self.createCollectionTab()
        self.summaryTab = self.createSummaryTab()
        self.settingsTab = self.createSettingsTab()
        self.startButton.setText("開始收集資料")
        self.tabs.addTab(self.settingsTab, "設定")
        self.tabs.addTab(self.collectionTab, "收集資料")
        self.tabs.addTab(self.summaryTab, "後續分析")
        self.tabs.addTab(self.comparisonTab, "測試比較")
        outer.addWidget(self.tabs, 1)

    def createPedalPanel(self) -> QWidget:
        panel = QFrame(objectName="panel")
        layout = QGridLayout(panel)
        layout.addWidget(QLabel("踏板", objectName="sectionTitle"), 0, 0)
        self.pedalDevices = QComboBox()
        self.pedalDevices.setEditable(True)
        self.pedalDevices.setPlaceholderText("掃描所有藍牙裝置，或輸入 Bluetooth MAC 位址")
        layout.addWidget(self.pedalDevices, 0, 1)
        self.scanPedalButton = QPushButton("使用上方掃描")
        self.connectPedalButton = QPushButton("連線", objectName="primary")
        layout.addWidget(self.scanPedalButton, 0, 2)
        layout.addWidget(self.connectPedalButton, 0, 3)
        self.pedalStatus = QLabel("踏板：尚未連線", objectName="status")
        self.pedalTelemetryLabel = QLabel("踏板資料：等待連線", objectName="muted")
        layout.addWidget(self.pedalStatus, 1, 0, 1, 4)
        layout.addWidget(self.pedalTelemetryLabel, 2, 0, 1, 4)
        self.scanPedalButton.clicked.connect(self.scanCyclingDevices)
        self.connectPedalButton.clicked.connect(self.connectPedal)
        return panel

    def createSettingsTab(self) -> QWidget:
        tab = QWidget(objectName="settingsPage")
        page = QVBoxLayout(tab)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(6)

        heading = QFrame()
        FluentThemeManager.set_property(heading, "fluentRole", "surface")
        headingLayout = QVBoxLayout(heading)
        headingLayout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("測試設定", objectName="title")
        subtitle = QLabel("先設定受試者與本次設備，再掃描並連線騎乘裝置。", objectName="muted")
        headingLayout.addWidget(title)
        headingLayout.addWidget(subtitle)
        heading.setParent(None)
        heading.deleteLater()

        subject = QFrame()
        FluentThemeManager.set_property(subject, "fluentRole", "surface")
        subjectLayout = QGridLayout(subject)
        subjectLayout.setContentsMargins(14, 12, 14, 12)
        subjectLayout.setHorizontalSpacing(12)
        subjectLayout.setVerticalSpacing(10)
        subjectLayout.addWidget(QLabel("受試者資料", objectName="sectionTitle"), 0, 0, 1, 6)
        self.patientIdEdit = QLineEdit()
        self.patientIdEdit.setPlaceholderText("必填")
        self.patientNameEdit = QLineEdit()
        self.patientNameEdit.setPlaceholderText("必填")
        self.genderCombo = QComboBox()
        self.genderCombo.addItems(("未指定", "男性", "女性"))
        subjectLayout.addWidget(QLabel("ID"), 1, 0)
        subjectLayout.addWidget(self.patientIdEdit, 1, 1)
        subjectLayout.addWidget(QLabel("姓名"), 1, 2)
        subjectLayout.addWidget(self.patientNameEdit, 1, 3)
        subjectLayout.addWidget(QLabel("性別"), 1, 4)
        subjectLayout.addWidget(self.genderCombo, 1, 5)
        self.patientIdEdit.setFixedWidth(160)
        self.patientNameEdit.setFixedWidth(180)
        self.genderCombo.setFixedWidth(100)
        subjectLayout.setColumnStretch(6, 1)
        layout.addWidget(subject)

        devices = QFrame()
        FluentThemeManager.set_property(devices, "fluentRole", "surface")
        devicesLayout = QVBoxLayout(devices)
        devicesLayout.setContentsMargins(14, 12, 14, 12)
        deviceHeader = QHBoxLayout()
        deviceHeader.setAlignment(Qt.AlignLeft)
        deviceHeader.addWidget(QLabel("本次設備", objectName="sectionTitle"))
        checks = QHBoxLayout()
        checks.setSpacing(18)
        self.useEmgCheck = QCheckBox("EMG")
        self.useTrainerCheck = QCheckBox("阻力計")
        self.usePedalCheck = QCheckBox("踏板")
        for check in (self.useEmgCheck, self.useTrainerCheck, self.usePedalCheck):
            check.setChecked(True)
            checks.addWidget(check)
        deviceHeader.addSpacing(28)
        deviceHeader.addLayout(checks)
        devicesLayout.addLayout(deviceHeader)
        layout.addWidget(devices)

        bluetooth = QFrame()
        FluentThemeManager.set_property(bluetooth, "fluentRole", "surface")
        bluetoothLayout = QVBoxLayout(bluetooth)
        bluetoothLayout.setContentsMargins(14, 12, 14, 12)
        bluetoothLayout.setSpacing(8)
        bluetoothHeader = QHBoxLayout()
        bluetoothHeader.addWidget(QLabel("騎乘裝置藍牙", objectName="sectionTitle"))
        bluetoothHeader.addStretch()
        bluetoothHeader.itemAt(0).widget().setText("裝置連線")
        self.scanTrainerButton.setText("掃描所有藍牙裝置")
        FluentThemeManager.set_property(self.scanTrainerButton, "fluentAppearance", "primary")
        self.scanTrainerButton.setText("重新掃描")
        bluetoothHeader.addWidget(self.scanTrainerButton)
        bluetoothLayout.addLayout(bluetoothHeader)
        bluetoothLayout.addWidget(self.trainerTab)
        assignmentRow = QHBoxLayout()
        assignmentRow.setSpacing(8)
        assignmentRow.addWidget(QLabel("已選擇裝置："))
        self.selectedScannedDeviceLabel = QLabel("尚未選擇", objectName="muted")
        self.selectedScannedDeviceLabel.setFixedWidth(310)
        assignmentRow.addWidget(self.selectedScannedDeviceLabel)
        self.assignTrainerButton = QPushButton("設定為阻力計")
        self.assignPedalButton = QPushButton("設定為踏板")
        self.assignTrainerButton.setEnabled(False)
        self.assignPedalButton.setEnabled(False)
        FluentThemeManager.set_property(self.assignTrainerButton, "fluentAppearance", "dark")
        FluentThemeManager.set_property(self.assignPedalButton, "fluentAppearance", "dark")
        assignmentRow.addWidget(self.assignTrainerButton)
        assignmentRow.addWidget(self.assignPedalButton)
        assignmentRow.addStretch()
        bluetoothLayout.addLayout(assignmentRow)
        assignmentRow.itemAt(0).widget().hide()
        self.assignTrainerButton.clicked.connect(lambda: self.assignSelectedScannedDevice("阻力計"))
        self.assignPedalButton.clicked.connect(lambda: self.assignSelectedScannedDevice("踏板"))

        emg = QFrame()
        FluentThemeManager.set_property(emg, "fluentRole", "surface")
        emgLayout = QGridLayout(emg)
        emgLayout.setContentsMargins(14, 12, 14, 12)
        emgLayout.setHorizontalSpacing(12)
        emgLayout.setVerticalSpacing(10)
        emgLayout.addWidget(QLabel("EMG 連線", objectName="sectionTitle"), 0, 0, 1, 4)
        emgLayout.addWidget(QLabel("Server"), 1, 0)
        emgLayout.addWidget(self.hostEdit, 1, 1)
        self.testButton.setText("測試 EMG 連線")
        FluentThemeManager.set_property(self.testButton, "fluentAppearance", "dark")
        self.testButton.setFixedWidth(180)
        emgLayout.addWidget(self.testButton, 1, 2)
        mappingButton = QPushButton("設定感測器對應")
        mappingButton.setAccessibleName("設定感測器對應")
        mappingButton.setFixedWidth(180)
        mappingButton.clicked.connect(self.editMappings)
        emgLayout.addWidget(mappingButton, 1, 3)
        emgLayout.setColumnStretch(4, 1)
        self.activeSlotsLabel = QLabel("尚未取得 EMG 感測器狀態", objectName="muted")
        self.activeSlotsLabel.setWordWrap(True)
        emgLayout.addWidget(self.activeSlotsLabel, 2, 0, 1, 4)
        layout.addWidget(emg)
        layout.addWidget(bluetooth)

        scanResults = QFrame()
        FluentThemeManager.set_property(scanResults, "fluentRole", "scanResults")
        scanResultsLayout = QVBoxLayout(scanResults)
        scanResultsLayout.setContentsMargins(14, 10, 14, 10)
        scanResultsLayout.setSpacing(5)
        scanResultsLayout.addWidget(QLabel("掃描到的裝置", objectName="sectionTitle"))
        scanResultsLayout.addWidget(QLabel("為每個裝置選擇用途，指定後會套用至上方連線選單。", objectName="muted"))
        scanResultsLayout.itemAt(0).widget().hide()
        scanResultsLayout.itemAt(1).widget().hide()
        scanResultsHeader = QHBoxLayout()
        scanResultsHeader.addWidget(QLabel("掃描到的裝置", objectName="sectionTitle"))
        scanResultsHeader.addWidget(self.selectedScannedDeviceLabel)
        scanResultsHeader.addStretch()
        FluentThemeManager.set_property(self.assignTrainerButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.assignPedalButton, "fluentAppearance", "warning")
        scanResultsHeader.addWidget(self.assignTrainerButton)
        scanResultsHeader.addWidget(self.assignPedalButton)
        scanResultsHeader.addWidget(self.scanTrainerButton)
        scanResultsLayout.addLayout(scanResultsHeader)
        self.scanResultsScroll = QScrollArea()
        self.scanResultsScroll.setWidgetResizable(True)
        self.scanResultsScroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scanResultsScroll.setFixedHeight(180)
        scanResultsContent = QWidget()
        self.scanResultsLayout = QVBoxLayout(scanResultsContent)
        self.scanResultsLayout.setContentsMargins(0, 0, 0, 0)
        self.scanResultsLayout.setSpacing(3)
        self.scanResultsLayout.addStretch()
        self.scanResultsScroll.setWidget(scanResultsContent)
        scanResultsLayout.addWidget(self.scanResultsScroll)
        layout.addWidget(scanResults)

        database = QFrame()
        FluentThemeManager.set_property(database, "fluentRole", "surface")
        databaseLayout = QVBoxLayout(database)
        databaseLayout.setContentsMargins(14, 12, 14, 12)
        databaseLayout.setSpacing(6)
        databaseLayout.addWidget(QLabel("SQL Server 資料庫", objectName="sectionTitle"))
        self.databaseStatusLabel = QLabel("填入帳密後測試連線，即可載入資料庫。", objectName="muted")
        databaseLayout.addWidget(self.databaseStatusLabel)
        self.databaseServerEdit = QLineEdit("127.0.0.1")
        self.databaseUserEdit = QLineEdit("Rehab-User")
        self.databasePasswordEdit = QLineEdit()
        self.databasePasswordEdit.setEchoMode(QLineEdit.EchoMode.Password)
        self.databaseNameCombo = QComboBox()
        self.databaseNameCombo.setEnabled(False)
        self.databaseNameCombo.setPlaceholderText("先測試連線以載入資料庫")
        self.databaseTestButton = QPushButton("測試連線")
        FluentThemeManager.set_property(self.databaseTestButton, "fluentAppearance", "primary")
        self.databaseServerEdit.setFixedWidth(160)
        self.databaseUserEdit.setFixedWidth(130)
        self.databasePasswordEdit.setFixedWidth(130)
        self.databaseTestButton.setFixedWidth(96)
        credentialsRow = QHBoxLayout()
        credentialsRow.setSpacing(6)
        credentialsRow.addWidget(QLabel("Server"))
        credentialsRow.addWidget(self.databaseServerEdit)
        credentialsRow.addWidget(QLabel("帳號"))
        credentialsRow.addWidget(self.databaseUserEdit)
        credentialsRow.addWidget(QLabel("密碼"))
        credentialsRow.addWidget(self.databasePasswordEdit)
        credentialsRow.addWidget(self.databaseTestButton)
        credentialsRow.addStretch()
        databaseLayout.addLayout(credentialsRow)
        databaseRow = QHBoxLayout()
        databaseRow.setSpacing(6)
        databaseRow.addWidget(QLabel("資料庫"))
        databaseRow.addWidget(self.databaseNameCombo, 1)
        databaseLayout.addLayout(databaseRow)
        self.databaseTestButton.clicked.connect(self.testDatabaseConnection)
        self.databaseServerEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databaseUserEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databasePasswordEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databaseNameCombo.currentTextChanged.connect(self.databaseConnectionEdited)
        layout.addWidget(database)

        resistance = QFrame()
        FluentThemeManager.set_property(resistance, "fluentRole", "surface")
        resistanceLayout = QVBoxLayout(resistance)
        resistanceLayout.setContentsMargins(14, 12, 14, 12)
        resistanceLayout.addWidget(QLabel("阻力控制", objectName="sectionTitle"))

        eventLogPanel = QFrame(objectName="eventLogPanel")
        FluentThemeManager.set_property(eventLogPanel, "fluentRole", "surface")
        eventLogPanel.setFixedHeight(110)
        eventLayout = QVBoxLayout(eventLogPanel)
        eventLayout.setContentsMargins(10, 4, 10, 4)
        eventLayout.addWidget(QLabel("事件紀錄", objectName="sectionTitle"))
        self.eventLog = QPlainTextEdit(objectName="eventLog")
        self.eventLog.setReadOnly(True)
        self.eventLog.setMaximumBlockCount(200)
        self.eventLog.setPlaceholderText("連線與收集事件會顯示在這裡。")
        self.eventLog.setFixedHeight(78)
        eventLayout.addWidget(self.eventLog)
        layout.addStretch()
        scroll.setWidget(content)
        page.addWidget(scroll, 1)
        page.addWidget(eventLogPanel, 0)
        return tab

    def databaseConfig(self) -> DatabaseConfig:
        database = self.databaseNameCombo.currentText().strip() or "master"
        return DatabaseConfig(
            server=self.databaseServerEdit.text().strip(), database=database,
            username=self.databaseUserEdit.text().strip(), password=self.databasePasswordEdit.text(),
        )

    def databaseConnectionEdited(self, *_args) -> None:
        self.activeDatabaseConfig = None
        self.databaseStatusLabel.setText("連線資料已變更，請重新測試連線")
        self.setDatabaseConnectionState(False)

    def testDatabaseConnection(self) -> None:
        credentials = DatabaseConfig(
            server=self.databaseServerEdit.text().strip(), database="master",
            username=self.databaseUserEdit.text().strip(), password=self.databasePasswordEdit.text(),
        )
        try:
            store = SqlServerStorage(credentials)
            databases = store.list_databases()
            store.close()
            if not databases:
                raise RuntimeError("此帳號沒有可連線的資料庫。")
            selected = self.databaseNameCombo.currentText()
            self.databaseNameCombo.clear()
            self.databaseNameCombo.addItems(databases)
            self.databaseNameCombo.setEnabled(True)
            preferred = "RehabPatients-ylh"
            self.databaseNameCombo.setCurrentText(selected if selected in databases else preferred if preferred in databases else databases[0])
            config = self.databaseConfig()
            store = SqlServerStorage(config)
            name = store.test_connection()
            store.close()
        except Exception as exc:
            self.activeDatabaseConfig = None
            self.databaseStatusLabel.setText(f"連線失敗：{exc}")
            self.setDatabaseConnectionState(False)
            return
        self.activeDatabaseConfig = config
        self.databaseStatusLabel.setText(f"已連線：{name}；五張資料表可用")
        self.setDatabaseConnectionState(True)

    def startDatabaseRecording(self) -> None:
        config = self.activeDatabaseConfig
        if config is None:
            self.appendLog("資料庫未寫入：請先在設定頁測試 SQL Server 連線")
            return
        self.databaseLastTestId = None
        self.databaseTestId = prefixed_id("TEST")
        self.databaseStartedAt = datetime.now()
        self.databaseAggregator = SecondAggregator(self.databaseTestId, self.databaseStartedAt)
        baselines: list[dict] = []
        for slot, record in self.baselineManager.records.items():
            mapping = self.mappings[slot - 1]
            baselines.append({"muscle_name": f"{mapping.chinese_name} ({mapping.abbreviation})", "rms_mv": record.rms_mv,
                "base_time": 10, "action_type": record.protocol or "基準測試", "yellow_pct": self.baselineManager.warning_percent,
                "red_pct": self.baselineManager.focus_percent, "quality_status": "VALID"})
        subject = self.collectionSubject
        payload = {"test_id": self.databaseTestId, "patient_id": subject.patientId, "name": subject.name,
            "sex": subject.gender, "action_type": "固定阻力騎乘", "test_time": None,
            "resistance_level": self.resistanceSpin.value(), "has_baseline": bool(baselines), "notes": "EMG Cycling Monitor",
            "started_at": self.databaseStartedAt, "baselines": baselines}
        self.databaseWriter = DatabaseWriter(config)
        writer = self.databaseWriter
        self.databaseWriters.add(writer)
        self.databaseWriter.connected.connect(lambda name: self.appendLog(f"資料庫已連線：{name}；TestID {self.databaseTestId}"))
        self.databaseWriter.failed.connect(lambda message: self.appendLog(f"資料庫寫入失敗（CSV 收集未受影響）：{message}"))
        self.databaseWriter.completed.connect(self.appendLog)
        self.databaseWriter.finished.connect(lambda: self.databaseWriterFinished(writer))
        self.databaseWriter.start()
        self.databaseWriter.enqueue("start", payload)

    def databaseAddSample(self, kind: str, time_s: float, sample: dict) -> None:
        if self.databaseAggregator is None or self.databaseWriter is None:
            return
        rows = self.databaseAggregator.add(kind, time_s, sample)
        if rows:
            self.databaseWriter.enqueue("rows", (kind, rows))

    def finishDatabaseRecording(self, result: dict | None) -> None:
        writer, aggregator, test_id = self.databaseWriter, self.databaseAggregator, self.databaseTestId
        self.databaseAggregator = None
        self.databaseTestId = None
        if writer is None or aggregator is None or test_id is None:
            return
        for kind, rows in aggregator.flush_all().items():
            if rows:
                writer.enqueue("rows", (kind, rows))
        pedal = (result or {}).get("pedal", {})
        trainer = (result or {}).get("trainer", {})
        def stat(group: dict, key: str, field: str = "mean"):
            value = group.get(key, {})
            return value.get(field) if isinstance(value, dict) else None
        avg_power = stat(pedal, "power_w") if stat(pedal, "power_w") is not None else stat(trainer, "power_watts")
        max_power = stat(pedal, "power_w", "max") if stat(pedal, "power_w", "max") is not None else stat(trainer, "power_watts", "max")
        avg_cadence = stat(pedal, "cadence_rpm") if stat(pedal, "cadence_rpm") is not None else stat(trainer, "cadence_rpm")
        distance_m = stat(trainer, "session_distance_meters", "max")
        elapsed = int(round(self.rideTime()))
        status = "失敗" if self.collectionError else ("中斷" if self.collectionStoppedEarly else "完成")
        writer.enqueue("finish", {"test_id": test_id, "test_time": elapsed, "status": status,
            "avg_power_w": avg_power, "max_power_w": max_power,
            "avg_cadence_rpm": avg_cadence, "distance_km": None if distance_m is None else distance_m / 1000,
            "left_balance_pct": stat(pedal, "left_balance_percent"),
            "right_balance_pct": None if stat(pedal, "left_balance_percent") is None else 100 - stat(pedal, "left_balance_percent"),
            "notes": self.collectionError or ""})
        self.databaseLastTestId = test_id
        writer.enqueue("stop")

    def databaseWriterFinished(self, writer: DatabaseWriter) -> None:
        """Retain every writer until its QThread has actually exited."""
        discard_test_id = self.databaseDiscardAfterWriter.pop(writer, None)
        self.databaseWriters.discard(writer)
        if self.databaseWriter is writer:
            self.databaseWriter = None
        writer.deleteLater()
        if discard_test_id:
            self.startDatabaseDiscardWorker(discard_test_id)

    def startDatabaseDiscardWorker(self, test_id: str) -> None:
        """Delete a completed test after its original writer has closed."""
        config = self.activeDatabaseConfig
        if config is None:
            self.appendLog("資料庫捨棄略過：本次沒有可用的資料庫連線設定")
            return
        writer = DatabaseWriter(config)
        self.databaseWriters.add(writer)
        writer.failed.connect(lambda message: self.appendLog(f"資料庫捨棄失敗：{message}"))
        writer.completed.connect(lambda _message: self.appendLog("資料庫：已刪除本次測試資料"))
        writer.finished.connect(lambda: self.databaseWriterFinished(writer))
        writer.start()
        writer.enqueue("discard", test_id)
        writer.enqueue("stop")

    def discardDatabaseRecording(self, active_writer: bool) -> None:
        """Discard active rows, or wait until a completed writer has drained."""
        test_id = self.databaseTestId or self.databaseLastTestId
        writer = self.databaseWriter
        self.databaseAggregator = None
        self.databaseTestId = None
        self.databaseLastTestId = None
        if not test_id:
            return
        if active_writer and writer is not None and writer.isRunning():
            writer.enqueue("discard", test_id)
            writer.enqueue("stop")
            return
        if writer is not None and writer.isRunning():
            self.databaseDiscardAfterWriter[writer] = test_id
            return
        self.startDatabaseDiscardWorker(test_id)

    def createResistanceTab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.resistanceRangeLabel = QLabel("連線阻力計後，可在此手動調整阻力。",objectName="muted",)
        self.resistanceRangeLabel.hide()
        self.decreaseResistanceButton = QPushButton("降低")
        self.decreaseResistanceButton.setAccessibleName("降低阻力")
        self.resistanceSpin = QSpinBox()
        self.resistanceSpin.setRange(0, 30)
        self.resistanceSpin.setValue(0)
        self.resistanceSpin.setSingleStep(1)
        self.resistanceSpin.setSuffix(" / 30")
        self.resistanceSpin.setReadOnly(True)
        self.resistanceSpin.setAccessibleName("阻力等級")
        self.increaseResistanceButton = QPushButton("提高")
        self.increaseResistanceButton.setAccessibleName("提高阻力")
        layout.addWidget(self.decreaseResistanceButton)
        layout.addWidget(self.resistanceSpin)
        layout.addWidget(self.increaseResistanceButton)
        self.nativeResistanceLabel = QLabel("阻力計狀態：尚未連線", objectName="muted")
        self.nativeResistanceLabel.hide()
        self.resistanceStepLevels = 1
        self.decreaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(-1))
        self.increaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(1))
        self.decreaseResistanceButton.setEnabled(False)
        self.increaseResistanceButton.setEnabled(False)
        return tab

    def createSummaryTab(self) -> QWidget:
        tab = QWidget(objectName="summaryPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        self.summarySourceLabel = QLabel("尚未有騎乘資料。", objectName="muted")
        heading.addWidget(self.summarySourceLabel)
        heading.addStretch()
        self.rideStateLabel = QLabel("等待騎乘", objectName="status")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "info")
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.importFitButton = QPushButton("匯入FIT檔")
        self.importFitButton.setAccessibleName("匯入 FIT 活動檔")
        self.importFitButton.setToolTip("匯入 .fit 檔並顯示功率、踏頻與左右平衡摘要")
        FluentThemeManager.set_property(self.importFitButton, "fluentAppearance", "primary")
        self.importEmgAnalysisButton = QPushButton("匯入原始 EMG")
        self.importEmgAnalysisButton.setToolTip("匯入原始 EMG CSV，重新產生 RMS 平均、Peak 與包絡線分析")
        FluentThemeManager.set_property(self.importEmgAnalysisButton, "fluentAppearance", "primary")
        self.currentSessionSummaryButton = QPushButton("顯示本次收集")
        self.currentSessionSummaryButton.setEnabled(False)
        self.currentSessionSummaryButton.setToolTip("切換回本程式最新收集的踏板資料")
        FluentThemeManager.set_property(self.currentSessionSummaryButton, "fluentAppearance", "success")
        self.exportReportButton = QPushButton("匯出 HTML 報表")
        self.exportReportButton.setEnabled(False)
        FluentThemeManager.set_property(self.exportReportButton, "fluentAppearance", "success")
        self.loadReferenceButton = QPushButton("選取比較基準")
        self.loadReferenceButton.setToolTip("選取第一次騎乘，作為兩次騎乘比較的基準資料")
        FluentThemeManager.set_property(self.loadReferenceButton, "fluentAppearance", "warning")
        actions.addWidget(self.currentSessionSummaryButton)
        actions.addWidget(self.importEmgAnalysisButton)
        actions.addWidget(self.importFitButton)
        actions.addWidget(self.loadReferenceButton)
        actions.addWidget(self.exportReportButton)
        heading.addLayout(actions)
        heading.addSpacing(12)
        heading.addWidget(self.rideStateLabel)
        layout.addLayout(heading)
        metricStrip = QHBoxLayout()
        self.summaryMetricValues = {}
        for key, title, unit in (("duration", "收集時間", "s"), ("muscles", "肌肉", "部位")):
            frame = QFrame(objectName="metricCard")
            column = QVBoxLayout(frame)
            column.setContentsMargins(12, 8, 12, 8)
            column.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            value.setAccessibleName(title)
            valueRow = QHBoxLayout()
            valueRow.setContentsMargins(0, 0, 0, 0)
            valueRow.addWidget(value)
            valueRow.addStretch()
            if unit:
                valueRow.addWidget(QLabel(unit, objectName="metricUnit"))
            column.addLayout(valueRow)
            metricStrip.addWidget(frame, 1)
            self.summaryMetricValues[key] = value
        layout.addLayout(metricStrip)
        self.analysisFtpSpin = QSpinBox()
        self.analysisFtpSpin.setRange(1, 600)
        self.analysisFtpSpin.setSuffix(" W")
        self.analysisFtpSpin.setValue(int(self.settings.value("analysis/ftp_w", 200)))
        self.analysisFtpSpin.setAccessibleName("FTP 功率設定")
        self.analysisFtpSpin.setToolTip("用於計算 NP、IF、TSS 與功率區間；不是醫療判讀")
        analysisControls = QHBoxLayout()
        analysisControls.addWidget(QLabel("FTP"))
        analysisControls.addWidget(self.analysisFtpSpin)
        analysisControls.addWidget(QLabel("僅供騎乘訓練指標計算", objectName="muted"))
        analysisControls.addStretch()
        layout.addLayout(analysisControls)
        self.summaryTabs = QTabWidget()
        chartPage = QWidget()
        chartLayout = QVBoxLayout(chartPage)
        chartLayout.setContentsMargins(8, 8, 8, 8)
        self.summaryPlots = pg.GraphicsLayoutWidget()
        self.summaryPlots.setBackground("#ffffff")
        self.powerAnalysisPlot = self.summaryPlots.addPlot(row=0, col=0, title="功率 Power")
        self.cadenceAnalysisPlot = self.summaryPlots.addPlot(row=1, col=0, title="踏頻 Cadence")
        self.balanceAnalysisPlot = self.summaryPlots.addPlot(row=2, col=0, title="左右平衡 Left / Right Balance")
        self.powerAnalysisPlot.setLabel("left", "功率", units="W")
        self.cadenceAnalysisPlot.setLabel("left", "踏頻", units="rpm")
        self.balanceAnalysisPlot.setLabel("left", "平衡", units="%")
        self.balanceAnalysisPlot.setLabel("bottom", "時間", units="s")
        self.balanceAnalysisPlot.setYRange(0, 100, padding=0.04)
        self.cadenceAnalysisPlot.setXLink(self.powerAnalysisPlot)
        self.balanceAnalysisPlot.setXLink(self.powerAnalysisPlot)
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot):
            plot.showGrid(x=True, y=True, alpha=0.18)

        chartColumns = QHBoxLayout()
        chartColumns.setSpacing(18)
        pedalInfo = QWidget()
        pedalInfo.setMinimumWidth(300)
        pedalInfoLayout = QVBoxLayout(pedalInfo)
        pedalInfoLayout.setContentsMargins(4, 4, 4, 4)
        pedalInfoLayout.setSpacing(8)
        pedalInfoLayout.addWidget(QLabel("踏板資訊", objectName="sectionTitle"))
        self.summaryDetails = QLabel(objectName="muted")
        self.summaryDetails.setText("完成踏板收集，或匯入FIT檔後，摘要會顯示在這裡")
        self.summaryDetails.setTextFormat(Qt.TextFormat.PlainText)
        self.summaryDetails.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summaryDetails.setWordWrap(True)
        self.summaryDetails.setAccessibleName("騎乘數值摘要")
        pedalInfoLayout.addWidget(self.summaryDetails)
        pedalInfoLayout.addStretch(1)
        pedalInfoLayout.addWidget(QLabel("雙腳平衡與估計功率", objectName="sectionTitle"))
        rings = QHBoxLayout()
        rings.setSpacing(12)
        self.leftPowerBalanceRing = PowerBalanceRing("左腳")
        self.rightPowerBalanceRing = PowerBalanceRing("右腳")
        rings.addWidget(self.leftPowerBalanceRing, 1)
        rings.addWidget(self.rightPowerBalanceRing, 1)
        pedalInfoLayout.addLayout(rings)
        chartColumns.addWidget(pedalInfo, 1)
        self.summaryPlots.setMinimumWidth(760)
        self.summaryPlots.setMinimumHeight(620)
        chartColumns.addWidget(self.summaryPlots, 4)
        chartLayout.addLayout(chartColumns, 1)
        self.pedalMetricCards = {}
        self.pedalZoneTable = None
        self.summaryTabs.addTab(chartPage, "踏板分析")
        ridePage = QWidget()
        rideLayout = QVBoxLayout(ridePage)
        self.rideEmptyLabel = QLabel(objectName="muted")
        self.rideEmptyLabel.hide()
        self.rideMuscleTable = QTableWidget(0, 6)
        self.rideMuscleTable.setHorizontalHeaderLabels(("肌肉／側別", "平均 RMS", "最大 RMS", "基準比較", "前段 → 後段", "資料狀態"))
        self.rideMuscleTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rideMuscleTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rideMuscleTable.setAccessibleName("本次騎乘各肌肉 RMS 摘要")
        self.rideMuscleTable.setMaximumHeight(210)
        self.ridePlots = pg.GraphicsLayoutWidget()
        self.ridePlots.setBackground("#ffffff")
        self.ridePlots.setMinimumHeight(360)
        self.rmsAverageBarPlot = self.ridePlots.addPlot(row=0, col=0, title="各肌肉 RMS 平均值")
        self.rmsAverageBarPlot.setLabel("left", "RMS", units="mV")
        self.rmsPeakBarPlot = self.ridePlots.addPlot(row=0, col=1, title="各肌肉 RMS Peak 值")
        self.rmsPeakBarPlot.setLabel("left", "RMS", units="mV")
        self.emgEnvelopePlot = self.ridePlots.addPlot(row=1, col=0, colspan=2, title="EMG RMS Peak 包絡線（每 1 秒最高值）")
        self.emgEnvelopePlot.setLabel("left", "RMS", units="mV")
        for plot in (self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot):
            plot.showGrid(x=True, y=True, alpha=0.18)
        self.emgEnvelopePlot.setLabel("bottom", "騎乘時間", units="s")
        envelopeControls = QHBoxLayout()
        envelopeControls.addWidget(QLabel("肌肉"))
        self.envelopeMuscleCombo = QComboBox()
        self.envelopeMuscleCombo.setAccessibleName("選擇 Peak 包絡線顯示的肌肉")
        self.envelopeMuscleCombo.setToolTip("每 1 秒取該肌肉最高 RMS 並連線；一次顯示一個肌肉，避免曲線重疊")
        self.envelopeMuscleCombo.setMinimumWidth(260)
        self.envelopeMuscleCombo.setEnabled(False)
        envelopeControls.addWidget(self.envelopeMuscleCombo)
        envelopeControls.addStretch()
        rideLayout.addLayout(envelopeControls)
        rideLayout.addWidget(self.ridePlots, 2)
        rideLayout.addWidget(self.rideMuscleTable, 1)
        self.summaryTabs.insertTab(0, ridePage, "肌電分析")
        comparePage = QWidget()
        compareLayout = QVBoxLayout(comparePage)
        self.comparisonStatusLabel = QLabel("選取第一次騎乘，再使用本次或載入歷史騎乘作為第二次。", objectName="muted")
        self.comparisonStatusLabel.setWordWrap(True)
        compareLayout.addWidget(self.comparisonStatusLabel)
        self.comparisonTable = QTableWidget(0, 7)
        self.comparisonTable.setHorizontalHeaderLabels(("肌肉／側別", "第一次中位 mV", "第二次中位 mV", "差異 mV", "變化 %", "P90 第一次／第二次", "狀態"))
        self.comparisonTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.comparisonTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.comparisonTable.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.comparisonTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.comparisonTable.setAccessibleName("兩次騎乘肌肉活化比較表")
        compareLayout.addWidget(self.comparisonTable, 2)
        self.comparisonPlot = pg.PlotWidget(title="選取肌肉：兩次 RMS 隨各自騎乘時間變化")
        self.comparisonPlot.setBackground("#ffffff")
        self.comparisonPlot.setLabel("bottom", "各自騎乘時間", units="s")
        self.comparisonPlot.setLabel("left", "RMS", units="mV")
        self.comparisonPlot.showGrid(x=True, y=True, alpha=0.18)
        self.comparisonPlot.addLegend()
        compareLayout.addWidget(self.comparisonPlot, 2)
        self.comparisonNotes = QLabel("RMS 表示肌肉電活動；跨次比較需維持電極位置、姿勢與阻力流程。", objectName="muted")
        self.comparisonNotes.setWordWrap(True)
        compareLayout.addWidget(self.comparisonNotes)
        self.comparisonTab = comparePage
        anglePage = QWidget()
        angleLayout = QVBoxLayout(anglePage)
        angleLayout.setSpacing(12)
        angleSetupFrame = QFrame()
        FluentThemeManager.set_property(angleSetupFrame, "fluentRole", "surface")
        angleSetupLayout = QVBoxLayout(angleSetupFrame)
        angleSetupLayout.setContentsMargins(14, 12, 14, 12)
        angleSetupLayout.setSpacing(8)
        angleHeading = QHBoxLayout()
        headingText = QVBoxLayout()
        headingText.setSpacing(2)
        headingText.addWidget(QLabel("踩踏角度分析", objectName="sectionTitle"))
        headingText.addWidget(QLabel("將同次騎乘的 EMG 與踏板資料對齊，查看肌肉在各相位的相對活化。", objectName="muted"))
        angleHeading.addLayout(headingText)
        angleHeading.addStretch()
        angleSetupLayout.addLayout(angleHeading)
        angleFiles = QHBoxLayout()
        self.angleEmgButton = QPushButton("1. 選擇 EMG CSV")
        self.angleEmgButton.setAccessibleDescription("選擇原始肌電資料 CSV 檔")
        self.anglePedalButton = QPushButton("2. 選擇踏板 CSV")
        self.anglePedalButton.setAccessibleDescription("選擇同一次騎乘的踏板或校正角度 CSV 檔")
        self.angleRunButton = QPushButton("開始分析", objectName="primary")
        self.angleExportButton = QPushButton("匯出 CSV 與圖片")
        self.angleExportButton.setEnabled(False)
        FluentThemeManager.set_property(self.angleRunButton, "fluentAppearance", "primary")
        FluentThemeManager.set_property(self.angleExportButton, "fluentAppearance", "outline")
        for widget in (self.angleEmgButton, self.anglePedalButton, self.angleRunButton, self.angleExportButton):
            angleFiles.addWidget(widget)
        angleFiles.addStretch()
        angleSetupLayout.addLayout(angleFiles)

        self.angleStatusLabel = QLabel("依序選擇同一次騎乘的 EMG 與踏板 CSV，接著按「開始分析」。", objectName="muted")
        self.angleStatusLabel.setWordWrap(True)
        angleSetupLayout.addWidget(self.angleStatusLabel)

        self.angleAdvancedToggle = QToolButton()
        self.angleAdvancedToggle.setText("進階設定")
        self.angleAdvancedToggle.setCheckable(True)
        self.angleAdvancedToggle.setArrowType(Qt.RightArrow)
        self.angleAdvancedToggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.angleAdvancedToggle.setAccessibleName("展開進階分析設定")
        self.angleAdvancedToggle.setToolTip("調整分析時間、同步校正與匯出內容")
        angleSetupLayout.addWidget(self.angleAdvancedToggle, alignment=Qt.AlignLeft)
        self.angleAdvancedPanel = QFrame()
        FluentThemeManager.set_property(self.angleAdvancedPanel, "fluentRole", "surface")
        self.angleAdvancedPanel.setVisible(False)
        advancedOptions = QGridLayout(self.angleAdvancedPanel)
        advancedOptions.setContentsMargins(12, 8, 12, 8)
        advancedOptions.setHorizontalSpacing(10)
        advancedOptions.setVerticalSpacing(6)
        self.angleRangeSpins = []
        for label, key, default in (("前段起", "start0", 5), ("前段迄", "start1", 65),("後段起", "end0", 240), ("後段迄", "end1", 300)):
            spin = QDoubleSpinBox()
            spin.setRange(0, 100000)
            spin.setSuffix(" 秒")
            spin.setValue(float(self.settings.value(f"angle/{key}", default)))
            spin.setAccessibleName(label)
            self.angleRangeSpins.append(spin)
        advancedOptions.addWidget(QLabel("前段比較時間"), 0, 0)
        advancedOptions.addWidget(self.angleRangeSpins[0], 0, 1)
        advancedOptions.addWidget(QLabel("至"), 0, 2)
        advancedOptions.addWidget(self.angleRangeSpins[1], 0, 3)
        advancedOptions.addWidget(QLabel("後段比較時間"), 1, 0)
        advancedOptions.addWidget(self.angleRangeSpins[2], 1, 1)
        advancedOptions.addWidget(QLabel("至"), 1, 2)
        advancedOptions.addWidget(self.angleRangeSpins[3], 1, 3)
        self.angleAutoScaleCheck = QCheckBox("自動校正 EMG 時鐘")
        self.angleAutoScaleCheck.setChecked(str(self.settings.value("angle/auto_scale", "true")).lower() == "true")
        self.angleScaleSpin = QDoubleSpinBox()
        self.angleScaleSpin.setDecimals(5)
        self.angleScaleSpin.setRange(.8, 1.2)
        self.angleScaleSpin.setSingleStep(.001)
        self.angleScaleSpin.setValue(float(self.settings.value("angle/scale", 1.0)))
        self.angleScaleSpin.setEnabled(not self.angleAutoScaleCheck.isChecked())
        self.angleOffsetSpin = QDoubleSpinBox()
        self.angleOffsetSpin.setRange(-30, 30)
        self.angleOffsetSpin.setDecimals(3)
        self.angleOffsetSpin.setSuffix(" 秒")
        self.angleOffsetSpin.setValue(float(self.settings.value("angle/offset_s", 0)))
        self.angleAutoOffsetCheck = QCheckBox("依典型角度估起點")
        self.angleAutoOffsetCheck.setToolTip("僅適用已校正上死點的角度 CSV；估計值有文獻與同步誤差。")
        self.angleAutoOffsetCheck.setChecked(str(self.settings.value("angle/auto_offset", "false")).lower() == "true")
        self.angleOffsetSpin.setEnabled(not self.angleAutoOffsetCheck.isChecked())
        advancedOptions.addWidget(self.angleAutoScaleCheck, 2, 0, 1, 2)
        advancedOptions.addWidget(QLabel("手動比例"), 2, 2)
        advancedOptions.addWidget(self.angleScaleSpin, 2, 3)
        advancedOptions.addWidget(QLabel("起點偏移"), 3, 0)
        advancedOptions.addWidget(self.angleOffsetSpin, 3, 1)
        advancedOptions.addWidget(self.angleAutoOffsetCheck, 3, 2, 1, 2)
        self.angleIncludeRawCheck = QCheckBox("匯出時包含完整同步 EMG CSV（檔案較大）")
        self.angleIncludeRawCheck.setChecked(True)
        self.angleIncludeRawCheck.setAccessibleDescription("取消勾選可縮小匯出的資料夾")
        advancedOptions.addWidget(self.angleIncludeRawCheck, 4, 0, 1, 4)
        angleSetupLayout.addWidget(self.angleAdvancedPanel)
        angleLayout.addWidget(angleSetupFrame)
        self.angleViews = QTabWidget()
        angleOverviewPage = QWidget()
        angleOverviewLayout = QVBoxLayout(angleOverviewPage)
        angleOverviewLayout.setContentsMargins(8, 8, 8, 8)
        overviewFrame = QFrame()
        FluentThemeManager.set_property(overviewFrame, "fluentRole", "surface")
        overviewLayout = QVBoxLayout(overviewFrame)
        overviewLayout.setContentsMargins(12, 10, 12, 10)
        overviewLayout.addWidget(QLabel("本次分析重點", objectName="sectionTitle"))
        self.angleOverviewLabel = QLabel("完成分析後，這裡會顯示前後段的可讀摘要。", objectName="muted")
        self.angleOverviewLabel.setWordWrap(True)
        self.angleOverviewLabel.setAccessibleName("踩踏角度分析摘要")
        overviewLayout.addWidget(self.angleOverviewLabel)
        angleOverviewLayout.addWidget(overviewFrame)

        self.angleOverviewDataTabs = QTabWidget()
        phasePage = QWidget()
        phaseLayout = QVBoxLayout(phasePage)
        phaseLayout.setContentsMargins(8, 8, 8, 8)
        phaseRow = QHBoxLayout()
        phaseRow.addWidget(QLabel("查看踩踏相位", objectName="sectionTitle"))
        self.anglePhaseSpin = QSpinBox()
        self.anglePhaseSpin.setRange(0, 359)
        self.anglePhaseSpin.setSingleStep(15)
        self.anglePhaseSpin.setSuffix("°")
        self.anglePhaseSpin.setValue(0)
        self.anglePhaseSpin.setAccessibleName("要比較的踩踏相位")
        self.anglePhaseSpin.setToolTip("以 0° 至 359° 的相對踩踏相位查看各肌肉訊號")
        phaseRow.addWidget(self.anglePhaseSpin)
        phaseRow.addStretch()
        phaseLayout.addLayout(phaseRow)
        self.anglePhaseLabel = QLabel("完成分析後，會在這裡直接指出 0° 相對活化最高的肌肉。", objectName="muted")
        self.anglePhaseLabel.setWordWrap(True)
        self.anglePhaseLabel.setAccessibleName("踩踏相位肌肉訊號答案")
        phaseLayout.addWidget(self.anglePhaseLabel)
        self.anglePhasePlot = pg.PlotWidget(title="0° 肌肉相對 EMG 活化｜藍色：前段；紅色：後段")
        self.anglePhasePlot.setLabel("bottom", "相對自身參考值", units="%")
        self.anglePhasePlot.setMinimumHeight(270)
        phaseLayout.addWidget(self.anglePhasePlot, 1)
        phaseLayout.addWidget(QLabel("每條肌肉各自正規化；百分比用於比較該肌肉在不同相位的訊號，不代表實際出力或跨肌肉力量。", objectName="muted"))
        self.angleOverviewDataTabs.addTab(phasePage, "相位活化")

        heatPage = QWidget()
        heatLayout = QVBoxLayout(heatPage)
        heatLayout.setContentsMargins(8, 8, 8, 8)
        self.angleHeatPlot = pg.PlotWidget(title="全肌肉角度熱圖｜每列僅比較自身訊號")
        self.angleHeatPlot.setLabel("bottom", "相對踩踏相位", units="°")
        self.angleHeatPlot.setMinimumHeight(250)
        heatLayout.addWidget(self.angleHeatPlot, 1)
        self.angleOverviewDataTabs.addTab(heatPage, "全肌肉熱圖")

        comparisonPage = QWidget()
        comparisonLayout = QVBoxLayout(comparisonPage)
        comparisonLayout.setContentsMargins(8, 8, 8, 8)
        comparisonLayout.addWidget(QLabel("前後段數值比較", objectName="sectionTitle"))
        self.angleComparisonTable = QTableWidget(0, 10)
        self.angleComparisonTable.setHorizontalHeaderLabels(("肌肉／側別", "前段圈數", "後段圈數", "前段 RMS µV", "後段 RMS µV", "RMS 變化 %", "前段峰值 °", "後段峰值 °", "峰值位移 °", "MDF 變化 Hz"))
        self.angleComparisonTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleComparisonTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleComparisonTable.setAccessibleName("踩踏角度分析前後段比較")
        self.angleComparisonTable.setMinimumHeight(220)
        comparisonLayout.addWidget(self.angleComparisonTable, 1)
        self.angleOverviewDataTabs.addTab(comparisonPage, "前後段數值")
        angleOverviewLayout.addWidget(self.angleOverviewDataTabs, 1)
        self.angleViews.addTab(angleOverviewPage, "相位總覽")
        angleDetailPage = QWidget()
        angleDetailLayout = QVBoxLayout(angleDetailPage)
        angleDetailLayout.setContentsMargins(8, 8, 8, 8)
        selectorRow = QHBoxLayout()
        selectorRow.addWidget(QLabel("查看肌肉", objectName="sectionTitle"))
        self.angleMuscleCombo = QComboBox()
        self.angleMuscleCombo.setAccessibleName("選擇角度曲線肌肉")
        self.angleMuscleCombo.setMinimumWidth(300)
        selectorRow.addWidget(self.angleMuscleCombo)
        selectorRow.addStretch()
        angleDetailLayout.addLayout(selectorRow)
        self.angleDetailLabel = QLabel("選擇一個肌肉，查看前後段的相位曲線與峰值變化。", objectName="muted")
        self.angleDetailLabel.setWordWrap(True)
        angleDetailLayout.addWidget(self.angleDetailLabel)
        self.angleCurvePlot = pg.PlotWidget(title="選取肌肉的前段／後段相位曲線")
        self.angleCurvePlot.setLabel("left", "相對自身參考值", units="%")
        self.angleCurvePlot.setLabel("bottom", "相對踩踏相位", units="°")
        self.angleCurvePlot.addLegend()
        self.angleCurvePlot.showGrid(x=True, y=True, alpha=.18)
        angleDetailLayout.addWidget(self.angleCurvePlot, 1)
        angleDetailLayout.addWidget(QLabel("藍線＝前段，紅線＝後段；垂直虛線＝各段峰值相位。肌電訊號強度不等於施力大小。", objectName="muted"))
        self.angleViews.addTab(angleDetailPage, "肌肉細節")
        angleLayout.addWidget(self.angleViews, 1)
        self.summaryTabs.insertTab(2, anglePage, "踩踏角度分析")
        self.angleEmgButton.clicked.connect(self.chooseAngleEmg)
        self.anglePedalButton.clicked.connect(self.chooseAnglePedal)
        self.angleRunButton.clicked.connect(self.runAngleAnalysis)
        self.angleExportButton.clicked.connect(self.exportAngleAnalysis)
        self.angleMuscleCombo.currentIndexChanged.connect(self.refreshAngleCurve)
        self.anglePhaseSpin.valueChanged.connect(self.refreshAnglePhase)
        self.angleComparisonTable.itemSelectionChanged.connect(self.showSelectedAngleMuscle)
        self.angleAutoScaleCheck.toggled.connect(lambda checked: self.angleScaleSpin.setEnabled(not checked))
        self.angleAutoOffsetCheck.toggled.connect(lambda checked: self.angleOffsetSpin.setEnabled(not checked))
        self.angleAdvancedToggle.toggled.connect(self.setAngleAdvancedVisible)
        self.summaryTabs.setCurrentIndex(0)
        layout.addWidget(self.summaryTabs, 1)
        self.importFitButton.clicked.connect(self.importFitSummary)
        self.importEmgAnalysisButton.clicked.connect(self.importEmgAnalysis)
        self.currentSessionSummaryButton.clicked.connect(self.showCurrentSessionSummary)
        self.analysisFtpSpin.valueChanged.connect(self.refreshSummaryAnalysis)
        self.envelopeMuscleCombo.currentIndexChanged.connect(self.refreshRideAnalysis)
        self.exportReportButton.clicked.connect(self.exportRideReport)
        self.loadReferenceButton.clicked.connect(self.loadReferenceRide)
        self.comparisonTable.itemSelectionChanged.connect(self.updateComparisonPlot)
        return tab

    def applySummaryPlotTheme(self, *_args) -> None:
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        for widget in (self.summaryPlots, self.ridePlots, self.comparisonPlot, self.angleCurvePlot,
                       self.angleHeatPlot, self.anglePhasePlot):
            widget.setBackground(tokens["surface_background"])
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
                     self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
                     self.comparisonPlot.getPlotItem(), self.angleCurvePlot.getPlotItem(),
                     self.angleHeatPlot.getPlotItem(), self.anglePhasePlot.getPlotItem()):
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setPen(tokens["text_secondary"])
                axis.setTextPen(tokens["text_primary"])
        if hasattr(self, "plotWidget"):
            self.plotWidget.setBackground(tokens["surface_background"])
            for plot, _curve, stats in self.curves.values():
                stats.setColor(tokens["text_secondary"])
                for axis_name in ("left", "bottom"):
                    axis = plot.getAxis(axis_name)
                    axis.setPen(tokens["text_secondary"])
                    axis.setTextPen(tokens["text_primary"])
        if hasattr(self, "leftPowerBalanceRing"):
            self.leftPowerBalanceRing.setThemeColors(tokens["success"], tokens["border_primary"], tokens["text_primary"],)
            self.rightPowerBalanceRing.setThemeColors(tokens["brand_background"], tokens["border_primary"], tokens["text_primary"],)

    def onSystemThemeChanged(self, *_args) -> None:
        FluentThemeManager(QApplication.instance()).apply()
        self.applySummaryPlotTheme()

    def createCollectionTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        statusRow = QHBoxLayout()
        self.emgStatus = QLabel(tab, objectName="status")
        self.baseLabel = QLabel("Base: 尚未確認", tab, objectName="muted")
        statusRow.addWidget(self.emgStatus)
        statusRow.addWidget(self.baseLabel)
        statusRow.addStretch()
        self.emgRateLabel = QLabel("EMG 頻率：等待 SDK 查詢", objectName="muted" )
        statusRow.addWidget(self.emgRateLabel)
        self.emgStatus.hide()
        self.baseLabel.hide()
        controls = QHBoxLayout()
        self.hostEdit = QLineEdit("127.0.0.1")
        self.hostEdit.setMaximumWidth(200)
        self.testButton, self.startButton, self.stopButton = QPushButton("測試連線"), QPushButton("開始串流", objectName="record"), QPushButton("停止")
        self.discardButton = QPushButton("捨棄本次測試")
        self.calibrateButton = QPushButton("測量 20 秒基準")
        FluentThemeManager.set_property(self.calibrateButton, "fluentAppearance", "danger")
        self.calibrationLabel = QLabel(objectName="muted")
        self.calibrationLabel.hide()
        FluentThemeManager.set_property(self.startButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.stopButton, "fluentAppearance", "danger")
        FluentThemeManager.set_property(self.discardButton, "fluentAppearance", "danger")
        self.discardButton.setToolTip("刪除剛完成或正在收集的測試檔案與資料庫資料")
        for widget in (self.hostEdit, self.testButton, self.startButton, self.calibrateButton, self.stopButton, self.discardButton):
            controls.addWidget(widget)
        controls.addWidget(self.calibrationLabel)
        controls.addSpacing(10)
        controls.addWidget(QLabel("收集時間"))
        self.durationMinutesSpin = QSpinBox()
        self.durationMinutesSpin.setRange(0, 120)
        self.durationMinutesSpin.setSuffix(" m")
        self.durationSecondsSpin = QSpinBox()
        self.durationSecondsSpin.setRange(0, 59)
        self.durationSecondsSpin.setSuffix(" s")
        for widget in (self.durationMinutesSpin, self.durationSecondsSpin):
            controls.addWidget(widget)
        self.sampleLabel = QLabel("0 samples", objectName="muted")
        controls.addWidget(self.sampleLabel)
        controls.addStretch()
        self.recordingIndicator = QLabel("● 錄製中", objectName="recordingIndicator")
        self.recordingIndicator.hide()
        controls.addWidget(self.recordingIndicator)
        self.countdownLabel = QLabel(objectName="countdown")
        self.countdownLabel.setMinimumWidth(112)
        self.countdownLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.countdownLabel.hide()
        controls.addWidget(self.countdownLabel)
        layout.addLayout(controls)
        metrics = QHBoxLayout()
        self.bikeMetricValues: dict[str, QLabel] = {}
        for key, title, unit in (("speed", "速度", "km/h"),("cadence", "踏頻", "rpm"),("power", "即時功率", "W"),("resistance", "阻力", "level"),):
            card = QFrame(objectName="metricCard")
            cardLayout = QVBoxLayout(card)
            cardLayout.setContentsMargins(12, 8, 12, 8)
            cardLayout.setSpacing(1)
            cardLayout.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            valueRow = QHBoxLayout()
            valueRow.setSpacing(6)
            valueRow.addWidget(value)
            valueRow.addStretch()
            valueRow.addWidget(QLabel(unit, objectName="metricUnit"))
            cardLayout.addLayout(valueRow)
            self.bikeMetricValues[key] = value
            metrics.addWidget(card, 1)
        distanceCard = QFrame(objectName="metricCard")
        distanceLayout = QVBoxLayout(distanceCard)
        distanceLayout.setContentsMargins(12, 8, 12, 8)
        distanceLayout.setSpacing(1)
        distanceLayout.addWidget(QLabel("騎乘公里", objectName="metricTitle"))
        distanceValue = QLabel("--", objectName="metricValue")
        distanceRow = QHBoxLayout()
        distanceRow.setSpacing(6)
        distanceRow.addWidget(distanceValue)
        distanceRow.addStretch()
        distanceRow.addWidget(QLabel("km", objectName="metricUnit"))
        distanceLayout.addLayout(distanceRow)
        self.bikeMetricValues["distance"] = distanceValue
        metrics.insertWidget(0, distanceCard, 1)
        layout.addLayout(metrics)
        pedalMetrics = QHBoxLayout()
        self.pedalMetricValues: dict[str, QLabel] = {}
        for key, title, unit in (("left", "踏板左功率平衡", "%"), ("right", "踏板右功率平衡", "%")):
            card = QFrame(objectName="metricCard")
            cardLayout = QVBoxLayout(card)
            cardLayout.setContentsMargins(12, 8, 12, 8)
            cardLayout.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            row = QHBoxLayout()
            row.addWidget(value)
            row.addStretch()
            row.addWidget(QLabel(unit, objectName="metricUnit"))
            cardLayout.addLayout(row)
            self.pedalMetricValues[key] = value
            pedalMetrics.addWidget(card, 1)
        layout.addLayout(pedalMetrics)
        recording = QHBoxLayout()
        self.recordCheck = QCheckBox("測量完成後輸出 CSV")
        self.recordCheck.setChecked(True)
        self.recordCheck.setEnabled(False)
        self.pathEdit = QLineEdit()
        self.pathEdit.setPlaceholderText("請選擇資料輸出資料夾")
        self.browseButton = QPushButton("選擇資料夾")
        for widget in (self.recordCheck, self.pathEdit, self.browseButton):
            recording.addWidget(widget)
        layout.addLayout(recording)
        body = QHBoxLayout()
        panel = self.createSensorPanel()
        self.bodyMap = BodyMapPanel(COLORS)
        # Give the anatomy view more room while retaining the existing
        # sensor legend and the independently resizable waveform area.
        self.bodyMap.setFixedWidth(720)
        self.bodyMap.setLegendWidget(panel)
        self.bodyMap.slotToggled.connect(self.toggleSensorFromBodyMap)
        body.addWidget(self.bodyMap)
        charts = QWidget()
        chartLayout = QVBoxLayout(charts)
        chartLayout.setContentsMargins(0, 0, 0, 0)
        chartHeader = QHBoxLayout()
        chartHeader.addWidget(QLabel("即時波形", objectName="sectionTitle"))
        chartHeader.addStretch()
        chartLayout.addLayout(chartHeader)
        self.plotWidget = pg.GraphicsLayoutWidget()
        self.plotWidget.setBackground("#ffffff")
        chartLayout.addWidget(self.plotWidget)
        body.addWidget(charts, 1)
        layout.addLayout(body, 1)
        self.testButton.clicked.connect(self.testConnection)
        self.startButton.clicked.connect(self.startStream)
        self.stopButton.clicked.connect(self.stopStream)
        self.discardButton.clicked.connect(self.requestDiscardCurrentTest)
        self.browseButton.clicked.connect(self.chooseOutput)
        self.plotTimer = QTimer(self)
        self.plotTimer.setInterval(80)
        self.plotTimer.timeout.connect(self.refreshPlots)
        self.plotTimer.start()
        self.countdownTimer = QTimer(self)
        self.countdownTimer.setInterval(1000)
        self.countdownTimer.timeout.connect(self.updateCountdown)
        self.remainingSeconds = 0
        # The calibration clock is display-only.  Sample count, rather than
        # this wall clock, decides when a calibration is complete.
        self.countdownMode = "collection"
        self.countdownStopsStream = True
        self.durationMinutesSpin.valueChanged.connect(self.resetCountdownDisplay)
        self.durationSecondsSpin.valueChanged.connect(self.resetCountdownDisplay)
        self.applyStyles()
        self.syncMuscleMap()
        self.rebuildPlots()
        return tab

    def createSensorPanel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.baselineSettingsButton = QPushButton("基準值與判定設定")
        FluentThemeManager.set_property(self.baselineSettingsButton, "fluentAppearance", "warning")
        controls.addWidget(self.baselineSettingsButton)
        controls.addStretch()
        layout.addLayout(controls)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        items = QVBoxLayout(content)
        items.setContentsMargins(0, 0, 0, 0)
        for index in range(CHANNEL_COUNT):
            row = QFrame(objectName="sensorRow")
            rowLayout = QHBoxLayout(row)
            rowLayout.setContentsMargins(8, 6, 8, 6)
            rowLayout.setSpacing(3)
            icon = QLabel("〽", objectName="sensorIcon")
            icon.setFixedSize(28, 28)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet(f"background: {COLORS[index % len(COLORS)]}; color: white; border-radius: 5px; font-weight: bold; font-size: 16px;")
            check = SensorLegendToggle()
            check.setChecked(False)
            rowLayout.addWidget(icon, 0, Qt.AlignTop)
            rowLayout.addWidget(check, 1)
            items.addWidget(row)
            check.toggled.connect(self.sensorSelectionChanged)
            self.sensorChecks.append(check)
            self.sensorIcons.append(icon)
            self.sensorRows.append(row)
        items.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.updateSensorRows()
        self.baselineSettingsButton.clicked.connect(self.showBaselineSettings)
        self.calibrateButton.clicked.connect(self.startBaselineCalibration)
        return panel

    def sensorSelectionChanged(self) -> None:
        self.rebuildPlots()
        self.updateSensorRows()

    def toggleSensorFromBodyMap(self, slot: int) -> None:
        if 1 <= slot <= len(self.sensorChecks):
            check = self.sensorChecks[slot - 1]
            check.setChecked(not check.isChecked())

    def syncMuscleMap(self) -> None:
        if not hasattr(self, "bodyMap"):
            return
        selected = {index + 1 for index, check in enumerate(self.sensorChecks) if check.isChecked()}
        active = {index + 1 for index, isActive in enumerate(self.sensorActive) if isActive}
        self.bodyMap.setMappings(self.mappings, selected, active, self.muscleStatuses)

    def connectTrainerSignals(self) -> None:
        self.trainerWorker.ready.connect(self.trainerReady)
        self.trainerWorker.scanReady.connect(self.showTrainerDevices)
        self.trainerWorker.connected.connect(self.trainerConnected)
        self.trainerWorker.disconnected.connect(self.trainerDisconnected)
        self.trainerWorker.resistanceSet.connect(self.resistanceSet)
        self.trainerWorker.fixedResistanceSet.connect(self.fixedResistanceSet)
        self.trainerWorker.telemetryReceived.connect(self.updateTrainerTelemetry)
        self.trainerWorker.failed.connect(self.trainerFailed)

    def connectPedalSignals(self) -> None:
        self.pedalWorker.ready.connect(self.pedalReady)
        self.pedalWorker.devicesFound.connect(self.showPedalDevices)
        self.pedalWorker.connected.connect(self.pedalConnected)
        self.pedalWorker.disconnected.connect(self.pedalDisconnected)
        self.pedalWorker.dataReceived.connect(self.updatePedalTelemetry)
        self.pedalWorker.failed.connect(self.pedalFailed)

    def createTrainerTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        trainerRow = QHBoxLayout()
        trainerRow.setSpacing(8)
        trainerTitle = QLabel("阻力計", objectName="sectionTitle")
        trainerTitle.setFixedWidth(86)
        trainerRow.addWidget(trainerTitle)
        self.trainerDevices = QComboBox()
        self.trainerDevices.setEditable(True)
        self.trainerDevices.setFixedWidth(280)
        self.trainerDevices.setPlaceholderText("選擇阻力計")
        trainerRow.addWidget(self.trainerDevices)
        self.connectTrainerButton = QPushButton("連線")
        self.connectTrainerButton.setFixedWidth(100)
        FluentThemeManager.set_property(self.connectTrainerButton, "fluentAppearance", "dark")
        trainerRow.addWidget(self.connectTrainerButton)
        self.trainerTelemetryLabel = QLabel("阻力計即時騎乘資料：--", objectName="muted")
        trainerRow.addWidget(self.trainerTelemetryLabel)
        trainerRow.addStretch()
        self.resistanceTab.show()
        trainerRow.addWidget(self.resistanceTab)
        layout.addLayout(trainerRow)
        pedalRow = QHBoxLayout()
        pedalRow.setSpacing(8)
        pedalTitle = QLabel("踏板功率計", objectName="sectionTitle")
        pedalTitle.setFixedWidth(86)
        pedalRow.addWidget(pedalTitle)
        self.pedalDevices = QComboBox()
        self.pedalDevices.setEditable(True)
        self.pedalDevices.setFixedWidth(280)
        self.pedalDevices.setPlaceholderText("選擇主踏板")
        pedalRow.addWidget(self.pedalDevices)
        self.connectPedalButton = QPushButton("連線")
        self.connectPedalButton.setFixedWidth(100)
        FluentThemeManager.set_property(self.connectPedalButton, "fluentAppearance", "dark")
        pedalRow.addWidget(self.connectPedalButton)
        self.pedalTelemetryLabel = QLabel("踏板即時功率資料：--", objectName="muted")
        pedalRow.addWidget(self.pedalTelemetryLabel)
        pedalRow.addStretch()
        layout.addLayout(pedalRow)
        self.trainerStatus = QLabel()
        self.trainerStatus.hide()
        self.pedalStatus = QLabel()
        self.pedalStatus.hide()
        self.scanTrainerButton = QPushButton("重新掃描")
        self.scanPedalButton = self.scanTrainerButton
        FluentThemeManager.set_property(self.scanTrainerButton, "fluentAppearance", "primary")
        self.scanTrainerButton.clicked.connect(self.scanCyclingDevices)
        self.connectTrainerButton.clicked.connect(self.connectTrainer)
        self.connectPedalButton.clicked.connect(self.connectPedal)
        self.setTrainerControls(False)
        return tab

    def startInitialBluetoothScan(self) -> None:
        if (self.initialBluetoothScanStarted or not self.trainerBluetoothReady or not self.pedalBluetoothReady):
            return
        self.initialBluetoothScanStarted = True
        self.scanCyclingDevices()

    def scanPedal(self) -> None:
        self.scanCyclingDevices()

    def assignScannedDevice(self, address: str, name: str, role: str) -> None:
        label = f"{name or 'Unknown device'} · {address}"
        if role == "阻力計":
            self.trainerDevices.clear()
            self.trainerDevices.addItem(label, address)
            self.trainerDevices.setCurrentIndex(0)
            self.appendLog(f"Device assignment | trainer: {label}")
        elif role == "踏板":
            if name.strip().upper().endswith(" R"):
                self.appendLog(f"Device assignment skipped | right pedal: {label}")
                return
            self.pedalDevices.clear()
            self.pedalDevices.addItem(label, address)
            self.pedalDevices.setCurrentIndex(0)
            self.appendLog(f"Device assignment | pedal: {label}")

    def cyclingScanFailed(self, message: str) -> None:
        self.setTrainerStatus(f"藍牙掃描失敗：{message}", "error")
        self.setPedalStatus(f"藍牙掃描失敗：{message}", "error")

    def finishCyclingScan(self) -> None:
        self.pedalScanInProgress = False
        self.scanTrainerButton.setEnabled(True)
        self.scanPedalButton.setEnabled(True)

    def finishPedalScan(self) -> None:
        self.pedalScanInProgress = False
        self.scanPedalButton.setEnabled(True)

    def autoConnectPedal(self) -> None:
        """Compatibility hook: background Bluetooth scanning is intentionally disabled."""
        return

    def showPedalDevices(self, devices: list[tuple[str, str]]) -> None:
        self.pedalDevices.clear()
        # P715's left pedal is the Bluetooth master.  A name ending in "R"
        # identifies the right pedal, which should not be selected on its own.
        devices.sort(key=lambda device: (device[1].strip().upper().endswith(" R"), device[1]))
        p715Devices = [name for _address, name in devices if name.upper().startswith("P715")]
        onlyRightPedal = bool(p715Devices) and all(name.strip().upper().endswith(" R") for name in p715Devices)
        for address, name in devices:
            self.pedalDevices.addItem(f"{name} · {address}", address)
        remembered = next((index for index, (address, _name) in enumerate(devices) if address == self.savedPedalAddress), None)
        if remembered is not None and not self.pedalIsConnected:
            self.pedalDevices.setCurrentIndex(remembered)
            self.setPedalStatus("已找到記憶的踏板，正在自動連線…", "working")
            self.setPedalStatus("已選取記憶的踏板，請按「連線」。", "idle")
            return
        if onlyRightPedal:
            self.setPedalStatus("只找到右踏板（R）；請喚醒左踏板，並選擇沒有 R 的裝置", "working")
        else:
            self.setPedalStatus(f"找到 {len(devices)} 個藍牙裝置，請選擇沒有 R 的踏板連線" if devices else "找不到藍牙裝置；請確認踏板已喚醒","idle",)

    def pedalAddress(self) -> str:
        address = self.pedalDevices.currentData()
        return str(address) if address else self.pedalDevices.currentText().strip().rsplit("·", 1)[-1].strip()

    def connectPedal(self) -> None:
        address = self.pedalAddress()
        if not address:
            QMessageBox.warning(self, "缺少踏板位址", "請先掃描藍牙裝置或輸入 Bluetooth MAC 位址。")
            return
        name = self.pedalDevices.currentText().rsplit("·", 1)[0].strip().upper()
        if name.startswith("P715") and name.endswith(" R"):
            QMessageBox.warning(self, "請選左踏板", "請選擇不帶 R 的踏板（左側主踏板）。右踏板會由左踏板自動連線。")
            return
        self.setPedalStatus("正在連線踏板…", "working")
        self.connectPedalButton.setEnabled(False)
        self.pedalWorker.connectDevice(address)

    def pedalConnected(self, address: str) -> None:
        self.pedalIsConnected = True
        self.savedPedalAddress = address
        self.settings.setValue("pedal/address", address)
        self.setPedalConnectionState(True)
        self.setPedalStatus(f"踏板已連線：{address}", "connected")
        self.pedalTelemetryLabel.setText("踏板功率資訊：等待踩踏…")

    def pedalDisconnected(self) -> None:
        self.pedalIsConnected = False
        self.setPedalConnectionState(False)
        self.connectPedalButton.setEnabled(True)
        self.setPedalStatus("踏板已中斷", "idle")

    def pedalFailed(self, message: str) -> None:
        self.pedalIsConnected = False
        self.setPedalConnectionState(False)
        self.connectPedalButton.setEnabled(True)
        self.setPedalStatus(f"踏板錯誤：{message}", "error")

    def setPedalStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.pedalStatus, text, state)
        self.appendLog(f"踏板 | {text}")

    def setPedalConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.pedalConnectionLabel, "踏板", connected)
        if connected and hasattr(self, "pedalTelemetryLabel"):
            self.pedalTelemetryLabel.setToolTip("踏板已連線")

    def refreshConnectionStates(self) -> None:
        """Catch silent BLE link loss even when no telemetry is arriving."""
        trainer = self.trainerWorker.trainer
        trainerConnected = bool(trainer and trainer.client and trainer.client.is_connected)
        if self.trainerIsConnected and not trainerConnected:
            self.trainerDisconnected()
        pedal = self.pedalWorker.client
        pedalConnected = bool(pedal and pedal.is_connected)
        if self.pedalIsConnected and not pedalConnected:
            self.pedalDisconnected()
        # Unlike BLE devices, the short-lived EMG connection test has already
        # ended by the time this timer runs.  Preserve that test result while
        # still giving an active stream precedence in the header.
        self.updateEmgConnectionIndicator()

    def updatePedalTelemetry(self, data: dict) -> None:
        power, cadence, left = data["power_w"], data["cadence_rpm"], data["left_balance_percent"]
        # Power and cadence are shared cycling metrics; P715 takes precedence
        # when it is connected, while the trainer remains the speed/distance source.
        self.bikeMetricValues["power"].setText(f"{power:.0f}")
        if cadence is not None:
            self.bikeMetricValues["cadence"].setText(f"{cadence:.0f}")
        if left is not None:
            self.pedalMetricValues["left"].setText(f"{left:.1f}")
            self.pedalMetricValues["right"].setText(f"{100 - left:.1f}")
        fields = [f"功率 {power} W"]
        if cadence is not None:
            fields.append(f"踏頻 {cadence:.1f} rpm")
        if left is not None:
            fields.append(f"左右 {left:.1f}% / {100-left:.1f}%")
        self.pedalTelemetryLabel.setText("踏板功率資訊：" + " · ".join(fields))
        if self.rideSession is not None:
            point = {key: data.get(key) for key in ("power_w", "cadence_rpm", "left_balance_percent", "right_balance_percent", "left_power_w", "right_power_w", "crank_revolutions")}
            point["time_s"] = self.rideTime()
            add_point(self.rideSession, "pedal", point)
            point["quality_status"] = "VALID"
            self.databaseAddSample("pedal", point["time_s"], point)
        if self.pedalRecorder:
            try:
                self.pedalRecorder.append(data)
            except OSError as exc:
                self.appendLog(f"踏板 CSV 寫入失敗：{exc}")
                self.closePedalRecording()


    def resistanceSet(self, value: float) -> None:
        self.commandedNativeResistance = value
        self.nativeResistanceLabel.setText(f"原生阻力：{value:g}")
        self.setTrainerStatus(f"原生阻力已設定為 {value:g}", "connected")

    def adjustFixedResistance(self, direction: int) -> None:
        self.resistanceSpin.setValue(self.resistanceSpin.value() + direction * self.resistanceStepLevels)
        self.trainerWorker.setFixedResistanceLevel(self.resistanceSpin.value())

    def updateResistanceStep(self, step: int) -> None:
        self.resistanceStepLevels = step
        self.resistanceSpin.setSingleStep(step)
        self.decreaseResistanceButton.setText(f"− {step} 級")
        self.increaseResistanceButton.setText(f"+ {step} 級")

    def fixedResistanceSet(self, level: int, nativeResistance: float) -> None:
        self.commandedNativeResistance = nativeResistance
        self.nativeResistanceLabel.setText(f"原生阻力：{nativeResistance:g}（固定模式 · 等級 {level}/30）")
        self.bikeMetricValues["resistance"].setText(f"{nativeResistance:g}")
        self.setTrainerStatus(f"固定阻力等級 {level}/30：原生阻力 {nativeResistance:g}", "connected")

    def updateTrainerTelemetry(self, data: BikeTelemetry) -> None:
        if data.distanceMeters is not None:
            self.lastTrainerDistanceMeters = data.distanceMeters
        fields: list[str] = []
        if data.speedKph is not None:
            fields.append(f"速度 {data.speedKph:.1f} km/h")
        if data.cadenceRpm is not None:
            fields.append(f"踏頻 {data.cadenceRpm:.1f} rpm")
        if data.powerWatts is not None:
            fields.append(f"功率 {data.powerWatts} W")
        if data.heartRate is not None:
            fields.append(f"心率 {data.heartRate} bpm")
        if data.distanceMeters is not None:
            fields.append(f"距離 {data.distanceMeters / 1000:.2f} km")
        if data.resistance is not None:
            fields.append(f"原生阻力 {data.resistance:g}")
        self.trainerTelemetryLabel.setText("阻力計資訊：" + (" · ".join(fields) or "等待踩踏…"))
        if data.speedKph is not None:
            self.bikeMetricValues["speed"].setText(f"{data.speedKph:.1f}")
        if data.cadenceRpm is not None and not self.pedalIsConnected:
            self.bikeMetricValues["cadence"].setText(f"{data.cadenceRpm:.0f}")
        if data.powerWatts is not None and not self.pedalIsConnected:
            self.bikeMetricValues["power"].setText(f"{data.powerWatts}")
        sessionDistanceMeters = self.sessionDistanceMeters(data.distanceMeters)
        if sessionDistanceMeters is not None:
            self.bikeMetricValues["distance"].setText(f"{sessionDistanceMeters / 1000:.2f}")
        if data.resistance is not None:
            self.bikeMetricValues["resistance"].setText(f"{data.resistance:g}")
        if self.rideSession is not None:
            point = {
                "time_s": self.rideTime(), "resistance_level": self.resistanceSpin.value(),
                "resistance_native_commanded": self.commandedNativeResistance,
                "resistance_native_reported": data.resistance, "power_watts": data.powerWatts,
                "speed_kph": data.speedKph, "cadence_rpm": data.cadenceRpm,
                "heart_rate_bpm": data.heartRate, "session_distance_meters": sessionDistanceMeters,
                "energy_kcal": data.energyKcal}
            add_point(self.rideSession, "trainer", point)
            self.databaseAddSample("trainer", point["time_s"], {
                "distance_km": None if sessionDistanceMeters is None else sessionDistanceMeters / 1000,
                "speed_kph": data.speedKph, "power_w": data.powerWatts, "cadence_rpm": data.cadenceRpm,
                "quality_status": "VALID"})
        if self.trainerRecorder:
            try:
                self.trainerRecorder.append(data,self.resistanceSpin.value(),self.commandedNativeResistance,sessionDistanceMeters,)
            except OSError as exc:
                self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
                self.trainerRecorder.close()
                self.trainerRecorder = None

    def resetBikeMetrics(self) -> None:
        for value in self.bikeMetricValues.values():
            value.setText("--")

    def beginCollectionDistance(self) -> None:
        """Start a new distance counter for the current EMG collection."""
        self.collectionDistanceBaselineMeters = self.lastTrainerDistanceMeters
        self.collectionDistanceMeters = 0
        self.distanceSessionActive = True
        self.bikeMetricValues["distance"].setText("0.00")

    def sessionDistanceMeters(self, totalDistanceMeters: int | None) -> int | None:
        if totalDistanceMeters is None:
            return self.collectionDistanceMeters
        if not self.distanceSessionActive:
            return totalDistanceMeters
        if self.collectionDistanceBaselineMeters is None:
            self.collectionDistanceBaselineMeters = totalDistanceMeters
        self.collectionDistanceMeters = max(0,totalDistanceMeters - self.collectionDistanceBaselineMeters,)
        return self.collectionDistanceMeters

    def scanTrainer(self) -> None:
        self.scanCyclingDevices()

    def showTrainerDevices(self, devices: list[tuple[str, str]]) -> None:
        current = self.trainerDevices.currentText()
        self.trainerDevices.clear()
        for address, name in devices:
            self.trainerDevices.addItem(f"{name} · {address}", address)
        if not devices:
            self.trainerDevices.setEditText(current)
        else:
            self.trainerDevices.setCurrentIndex(0)
        self.setTrainerStatus(f"掃描完成：找到 {len(devices)} 個裝置", "idle")

    def trainerAddress(self) -> str:
        data = self.trainerDevices.currentData()
        if data:
            return str(data)
        text = self.trainerDevices.currentText().strip()
        return text.rsplit("·", 1)[-1].strip()

    def connectTrainer(self) -> None:
        address = self.trainerAddress()
        if not address:
            QMessageBox.warning(self, "缺少藍牙位址", "請掃描後選擇阻力計，或輸入 MAC 位址。")
            return
        self.setTrainerStatus("正在連線…", "working")
        self.trainerWorker.connectDevice(address)

    def trainerConnected(self, info: dict) -> None:
        resistance: NumericRange = info["resistance"]
        nativeMinimum = resistance.minimum
        nativeMaximum = resistance.maximum
        self.resistanceSpin.setRange(0, 30)
        self.resistanceSpin.setValue(0)
        self.updateResistanceStep(1)
        self.resistanceRangeLabel.setText(f"設備原生阻力：{nativeMinimum:g}–{nativeMaximum:g}；第 0–30 級自動換算")
        self.setTrainerControls(True)
        self.trainerIsConnected = True
        self.commandedNativeResistance = info["nativeResistance"]
        self.setTrainerConnectionState(True)
        self.resetBikeMetrics()
        self.bikeMetricValues["resistance"].setText(f"{info['nativeResistance']:g}")
        self.setTrainerStatus(f"已連線：{info['address']}，控制權已取得", "connected")
        if info["telemetryEnabled"]:
            self.trainerTelemetryLabel.setText("阻力計資訊：等待踩踏…")
        else:
            self.trainerTelemetryLabel.setText("阻力計資訊：此設備未提供 Indoor Bike Data")

    def trainerDisconnected(self) -> None:
        self.setTrainerControls(False)
        self.trainerIsConnected = False
        self.setTrainerConnectionState(False)
        self.resetBikeMetrics()
        self.setTrainerStatus("阻力計已中斷連線", "idle")
        self.trainerTelemetryLabel.setText("阻力計資訊：--")

    def trainerFailed(self, message: str) -> None:
        self.setTrainerControls(False)
        self.trainerIsConnected = False
        self.setTrainerConnectionState(False)
        self.setTrainerStatus(f"阻力計錯誤：{message}", "error")
        if "藍牙目前未開啟" in message:
            QMessageBox.warning(self, "Bluetooth 未開啟", message)

    def setTrainerControls(self, connected: bool) -> None:
        self.decreaseResistanceButton.setEnabled(connected)
        self.increaseResistanceButton.setEnabled(connected)
        self.connectTrainerButton.setEnabled(not connected)

    def setTrainerStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.trainerStatus, text, state)
        self.appendLog(f"trainer | {text}")

    def setTrainerConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.trainerConnectionLabel, "阻力計", connected)

    def setDatabaseConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.databaseConnectionLabel, "資料庫", connected)

    def setEmgConnectionState(self, tested: bool) -> None:
        """Record whether the Trigno SDK test has succeeded."""
        self.emgTested = tested
        self.updateEmgConnectionIndicator()

    def updateEmgConnectionIndicator(self) -> None:
        """Show the EMG lifecycle without mistaking an idle SDK for failure."""
        if self.streamWorker and self.streamWorker.isRunning():
            state, severity = "串流中", "info"
        elif self.emgTested:
            state, severity = "已測試（待收集）", "success"
        else:
            state, severity = "未連線", "danger"
        self.emgConnectionLabel.setText(f"● EMG：{state}")
        FluentThemeManager.set_property(self.emgConnectionLabel, "fluentSeverity", severity)

    @staticmethod
    def setConnectionIndicator(label: QLabel, name: str, connected: bool) -> None:
        state = "連線" if connected else "未連線"
        label.setText(f"● {name}：{state}")
        FluentThemeManager.set_property(label, "fluentSeverity", "success" if connected else "danger")

    def editMappings(self) -> None:
        active_slots = {index + 1 for index, active in enumerate(self.sensorActive) if active}
        dialog = MappingEditorDialog(self.mappings, active_slots, self.baselineManager.records, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.mappings = dialog.mappings()
            saveMappings(MAPPING_PATH, self.mappings)
            for slot, value in dialog.baselineChanges().items():
                if value is None:
                    self.baselineManager.records.pop(slot, None)
                    continue
                channel = slot - 1
                previous = self.baselineManager.records.get(slot)
                serial = previous.sensor_serial if previous else self.sensorSerials.get(slot, "Manual mapping edit")
                if serial == "Unknown SID":
                    serial = "Manual mapping edit"
                self.baselineManager.set_manual(slot, value, self.mappingIdentity(channel), serial,self.currentSampleRate, self.filterSettings, "Edited in muscle mapping",)
            self.baselineManager.save()
            self.updateSensorRows()
            self.syncMuscleMap()
            self.rebuildPlots()

    def updateSensorRows(self) -> None:
        for index, mapping in enumerate(self.mappings):
            side = mapping.abbreviation.rsplit("-", 1)[-1] if "-" in mapping.abbreviation else ""
            title = f"{side} {mapping.english_name.upper()}" if side in {"R", "L"} else mapping.english_name.upper()
            detected = self.sensorActive[index]
            self.sensorChecks[index].setLabels(f"S{mapping.slot:02d} · {mapping.chinese_name}", mapping.english_name)
            serial = self.sensorSerials.get(mapping.slot, "未取得 SID")
            tooltip = f"S{mapping.slot:02d} · {title}\nSID：{serial}"
            self.sensorRows[index].setToolTip(tooltip)
            self.sensorIcons[index].setToolTip(tooltip)
            self.sensorChecks[index].setToolTip(tooltip)
            self.sensorRows[index].setVisible(True)
            self.updateBaselineRow(index)
        if hasattr(self, "activeSlotsLabel"):
            active_text = ", ".join(f"S{index + 1:02d}" for index, active in enumerate(self.sensorActive) if active)
            self.activeSlotsLabel.setText(f"目前啟用感測器：{active_text or '無'}")
        self.syncMuscleMap()

    def mappingIdentity(self, channel: int) -> str:
        mapping = self.mappings[channel]
        return f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"

    def updateBaselineRow(self, channel: int, current_mv: float | None = None) -> None:
        slot = channel + 1
        result = self.baselineManager.evaluate(slot, current_mv, self.mappingIdentity(channel), self.sensorSerials.get(slot, "Unknown SID"), self.currentSampleRate, self.filterSettings)
        status_changed = self.muscleStatuses.get(slot) != result["state"]
        self.muscleStatuses[slot] = result["state"]
        current = "--" if result["current_mv"] is None else f"{result['current_mv']:.3f} mV"
        baseline = "--" if result["baseline_mv"] is None else f"{result['baseline_mv']:.3f} mV"
        delta = "--" if result["delta_mv"] is None else f"{result['delta_mv']:+.3f} mV"
        percent = "--" if result["percent"] is None else f"{result['percent']:+.1f}%"
        ratio = "--" if result["percent"] is None else f"{1 + result['percent'] / 100:.2f} 倍"
        if channel < len(self.baselineMetricLabels):
            comparison = f"差異：{delta}｜變化：{percent}"
            self.baselineMetricLabels[channel].setText(f"目前 RMS：{current}\n基準：{baseline}｜{comparison}")
            self.baselineStatusLabels[channel].setText(result["text"])
            self.baselineStatusLabels[channel].setToolTip("狀態是相對基準偏離程度，並非醫療診斷。")
        FluentThemeManager.set_property(self.sensorRows[channel], "emgStatus", result["state"])
        if status_changed:
            self.syncMuscleMap()

    def startBaselineCalibration(self) -> None:
        if self.streamWorker and self.streamWorker.isRunning():
            QMessageBox.information(self, "基準測量", "請先停止目前的串流，再開始基準測量。")
            return
        if not self.useEmgCheck.isChecked() or not any(self.sensorActive):
            QMessageBox.warning(self, "基準測量", "請先連線測試，確認至少一個 EMG 感測器啟用。")
            return
        protocol, accepted = QInputDialog.getText(self, "基準測量說明", "動作/姿勢/阻力/踏頻（待臨床確認）：", text=self.baselineManager.protocol)
        if not accepted:
            return
        self.baselineManager.protocol = protocol.strip()
        self.calibrationOnly = True
        self.calibrationSession = None
        self.startStream()

    def importBaselineCsv(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "匯入原始 EMG 基準 CSV", self.pathEdit.text().strip(), "CSV files (*.csv)")
        if not filename:
            return
        path = Path(filename)
        try:
            _, columns, sample_rate = load_raw_emg_csv(path)
            channel_columns: list[tuple[int, str]] = []
            for index, mapping in enumerate(self.mappings):
                header = next((name for name in columns if matchesCsvColumn(mapping, name)), None)
                if header is not None:
                    channel_columns.append((index, header))
            if not channel_columns:
                raise ValueError("CSV 欄位沒有符合目前肌肉對應的 EMG 名稱。")
            frames = np.column_stack([columns[header] for _, header in channel_columns])
            filtered = EMGPreprocessor(len(channel_columns), sample_rate, self.filterSettings).process(frames)
            session = CalibrationSession(sample_rate, set(range(len(channel_columns))))
            session.add(filtered)
            if not session.complete:
                raise ValueError("CSV 的前 20 秒資料不足。")
            imported, invalid = [], []
            for compact_channel, (channel, _header) in enumerate(channel_columns):
                rms_mv = session.rms_mv(compact_channel)
                if rms_mv is None or rms_mv <= 1e-9:
                    invalid.append(f"S{channel + 1:02d}")
                    continue
                self.baselineManager.set_imported(channel + 1, rms_mv, self.mappingIdentity(channel), sample_rate, self.filterSettings, path.name)
                imported.append(f"S{channel + 1:02d}")
            if not imported:
                raise ValueError("CSV 沒有可建立的有效基準。")
            self.baselineManager.save()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "匯入基準 CSV 失敗", str(exc))
            return
        self.currentSampleRate = sample_rate
        self.calibrationLabel.setText(f"已匯入 {path.name}：{', '.join(imported)}" + (f"；無效：{', '.join(invalid)}" if invalid else ""))
        self.updateSensorRows()
        self.showBaselineSettings()

    def editManualBaseline(self) -> None:
        choices = [f"S{index + 1:02d} {mapping.abbreviation or mapping.english_name}" for index, mapping in enumerate(self.mappings)]
        choice, accepted = QInputDialog.getItem(self, "手動修改基準", "肌肉/通道：", choices, 0, False)
        if not accepted:
            return
        channel = choices.index(choice)
        previous = self.baselineManager.records.get(channel + 1)
        value, accepted = QInputDialog.getDouble(self, "手動修改基準", "RMS (mV)：", previous.rms_mv if previous else 0.1, 0.000001, 10000, 6)
        if not accepted:
            return
        reason, accepted = QInputDialog.getText(self, "手動修改基準", "修改原因：")
        if not accepted:
            return
        try:
            self.baselineManager.set_manual(channel + 1, value, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"), self.currentSampleRate, self.filterSettings, reason.strip())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "無法保存基準", str(exc))
            return
        self.updateBaselineRow(channel)

    def showBaselineSettings(self) -> None:
        dialog = BaselineSettingsDialog(self.mappings, self.baselineManager, self.filterSettings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        warning, focus = dialog.values()
        for slot, value in dialog.baseline_changes().items():
            channel = slot - 1
            previous = self.baselineManager.records.get(slot)
            serial = previous.sensor_serial if previous else self.sensorSerials.get(slot, "Manual mapping edit")
            if serial == "Unknown SID":
                serial = "Manual mapping edit"
            self.baselineManager.set_manual(
                slot, value, self.mappingIdentity(channel), serial,
                self.currentSampleRate, self.filterSettings, "Edited in baseline settings",
            )
        self.baselineManager.thresholds_enabled = True
        self.baselineManager.warning_percent = warning
        self.baselineManager.focus_percent = focus
        self.baselineManager.save()
        self.updateSensorRows()
        if dialog.import_requested:
            self.importBaselineCsv()

    def editFilterSettings(self) -> None:
        highpass, accepted = QInputDialog.getDouble(self, "EMG 濾波設定", "高通截止頻率 (Hz)：", self.filterSettings.highpass_hz, 0.1, 1000, 1)
        if not accepted:
            return
        lowpass, accepted = QInputDialog.getDouble(self, "EMG 濾波設定", "低通截止頻率 (Hz)：", self.filterSettings.lowpass_hz, highpass + 0.1, 10000, 1)
        if not accepted:
            return
        try:
            settings = FilterSettings(highpass, lowpass)
            settings.validate(self.currentSampleRate)
        except ValueError as exc:
            QMessageBox.warning(self, "濾波設定無效", str(exc))
            return
        self.filterSettings = settings
        self.baselineManager.filter_settings = {"highpass_hz": highpass, "lowpass_hz": lowpass, "notch_hz": None}
        self.baselineManager.save()
        self.updateSensorRows()

    def applyStyles(self) -> None:
        FluentThemeManager(QApplication.instance()).apply()

    def subjectInfo(self) -> SubjectInfo:
        return SubjectInfo(patientId=self.patientIdEdit.text().strip(),name=self.patientNameEdit.text().strip(),gender=self.genderCombo.currentText(),)

    def collectionSetupIsValid(self) -> bool:
        subject = self.subjectInfo()
        missing = []
        if not subject.patientId:
            missing.append("病人 ID")
        if not subject.name:
            missing.append("姓名")
        if missing:
            QMessageBox.warning(self,"缺少病人資訊","開始收集前請填寫：" + "、".join(missing),)
            self.tabs.setCurrentWidget(self.settingsTab)
            (self.patientIdEdit if not subject.patientId else self.patientNameEdit).setFocus()
            return False

        duplicates = duplicateMappingSlots(self.mappings)
        if duplicates:
            conflicts = "\n".join(f"{muscle.title()} ({side.upper() or '?' }): "+ ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items()
            )
            QMessageBox.warning(self,"重複肌肉對應","請先修正感測器對應，再開始收集：\n" + conflicts,)
            self.tabs.setCurrentWidget(self.settingsTab)
            return False
        return True

    def outputPathForCollection(self, directory: Path) -> Path:
        patientId = self.subjectInfo().patientId
        invalid = '\\/:*?"<>|'
        safeId = "".join(character for character in patientId if character not in invalid).strip()
        date = datetime.now().strftime("%Y%m%d")
        candidate = directory / f"{safeId}_EMG_{date}.csv"
        suffix = 2
        while candidate.exists() or infoOutputPath(candidate).exists() or analysisOutputPath(candidate).exists() or (
            self.useTrainerCheck.isChecked() and self.trainerIsConnected and trainerOutputPath(candidate).exists()) or (
            self.usePedalCheck.isChecked() and self.pedalIsConnected and (
                pedalOutputPath(candidate).exists() or pedalOutputPath(candidate).with_suffix(".fit").exists())):
            candidate = directory / f"{safeId}_EMG_{date}_{suffix:02d}.csv"
            suffix += 1
        return candidate

    def setStatusLabel(self, label: QLabel, text: str, state: str) -> None:
        label.setText(text)
        severity = {"idle": "info", "working": "warning", "connected": "success", "streaming": "info", "error": "danger"}[state]
        FluentThemeManager.set_property(label, "fluentSeverity", severity)

    def setEmgStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.emgStatus, text, state)
        self.appendLog(f"EMG | {text}")

    def appendLog(self, message: str) -> None:
        if hasattr(self, "eventLog"):
            self.eventLog.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def setStreaming(self, streaming: bool) -> None:
        testing = self.testWorker is not None
        self.startButton.setEnabled(not streaming and not testing)
        self.calibrateButton.setEnabled(not streaming and not testing)
        self.stopButton.setEnabled(streaming)
        self.discardButton.setEnabled(streaming or self.collectedRideSession is not None)
        self.hostEdit.setEnabled(not streaming and not testing)
        self.testButton.setEnabled(not streaming and not testing)
        self.recordCheck.setEnabled(False)
        self.durationMinutesSpin.setEnabled(not streaming)
        self.durationSecondsSpin.setEnabled(not streaming)
        self.pathEdit.setEnabled(not streaming and self.recordCheck.isChecked())
        self.browseButton.setEnabled(not streaming and self.recordCheck.isChecked())
        self.recordingIndicator.setVisible(streaming)
        for check in (self.useEmgCheck, self.useTrainerCheck, self.usePedalCheck):
            check.setEnabled(not streaming)

    def chooseOutput(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "選擇 CSV 輸出資料夾", self.pathEdit.text())
        if directory:
            self.pathEdit.setText(directory)

    def testConnection(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
            return
        if not self.patientIdEdit.text().strip():
            QMessageBox.warning(self, "缺少病人 ID", "請先在設定頁填寫病人 ID，再開始收集。")
            self.tabs.setCurrentWidget(self.settingsTab)
            self.patientIdEdit.setFocus()
            return
        if not self.patientIdEdit.text().strip():
            QMessageBox.warning(self, "缺少病人 ID", "請先在設定頁填寫病人 ID，再開始收集。")
            self.tabs.setCurrentWidget(self.settingsTab)
            self.patientIdEdit.setFocus()
            return
        self.setEmgStatus("正在測試連線…", "working")
        self.testWorker = ConnectionWorker(host)
        self.testWorker.connected.connect(lambda banner: self.emgStatus.setToolTip(banner))
        self.testWorker.base_ready.connect(lambda serial: self.baseLabel.setText(f"Base: {serial}"))
        self.testWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.testWorker.failed.connect(self.emgConnectionFailed)
        self.testWorker.finished.connect(self.testFinished)
        self.setStreaming(False)
        self.testWorker.start()

    def testFinished(self) -> None:
        worker, self.testWorker = self.testWorker, None
        if worker:
            worker.deleteLater()
        if not self.emgStatus.text().startswith("連線失敗"):
            self.setEmgStatus("已連線；啟用感測器已更新", "connected")
            self.setEmgConnectionState(True)
        self.setStreaming(False)

    def emgConnectionFailed(self, message: str) -> None:
        self.setEmgStatus(f"連線失敗：{message}", "error")
        self.setEmgConnectionState(False)

    def updateSensorStates(self, paired: list[int], active: list[int], serials: dict[int, str]) -> None:
        self.sensorActive = [index + 1 in active for index in range(CHANNEL_COUNT)]
        self.sensorSerials = serials
        if self.rideSession is not None:
            self.rideSession["channels"] = {
                str(slot): {"mapping_name": self.mappingIdentity(slot - 1),
                            "label": self.mappings[slot - 1].chinese_name,
                            "sensor_serial": serials.get(slot, "Unknown SID")}
                for slot in active}
        self.updateSensorRows()
        for index, check in enumerate(self.sensorChecks):
            check.setChecked(self.sensorActive[index])
        if self.calibrationOnly and self.preprocessor is not None and self.calibrationSession is None:
            self.calibrationSession = CalibrationSession(self.currentSampleRate, {index for index, enabled in enumerate(self.sensorActive) if enabled})

    def rideTime(self) -> float:
        return max(0.0, time.monotonic() - self.rideStartedAt)

    def beginRideAnalysis(self) -> None:
        self.angleEmgPath = None
        self.anglePedalPath = None
        self.angleAnalysisResult = None
        self.discardCurrentCollection = False
        self.databaseLastTestId = None
        self.angleExportButton.setEnabled(False)
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.rideSession = new_session(asdict(self.collectionSubject), baselines)
        self.rideSession["filter"] = asdict(self.filterSettings)
        self.rideSession["channels"] = {}
        self.rideSession["raw_files"] = {
            name: path.name for name, path in (("emg", self.collectionOutput),
                ("pedal", self.pedalOutput), ("trainer", self.trainerOutput))
            if path is not None and (name != "emg" or self.useEmgCheck.isChecked())}
        self.rideStartedAt = time.monotonic()
        self.lastEmgAnalysisAt = 0.0
        self.latestRideSession = self.rideSession
        self.collectedRideSession = self.rideSession
        self.currentSessionSummaryButton.setEnabled(True)
        self.summarySourceLabel.setText("資料來源：本次騎乘，收集中")
        self.refreshRideAnalysis()
        self.refreshComparison()

    def finishRideAnalysis(self) -> None:
        session, self.rideSession = self.rideSession, None
        if session is None:
            return
        if self.discardCurrentCollection:
            self.finalizeDiscardedTest(active_database_writer=True)
            return
        result = analyze(session)
        self.finishDatabaseRecording(result)
        self.latestRideSession = session
        self.collectedRideSession = session
        self.angleEmgPath = self.collectionOutput if self.collectionOutput and self.collectionOutput.is_file() else None
        self.anglePedalPath = self.pedalOutput if self.pedalOutput and self.pedalOutput.is_file() else None
        self.summarySourceLabel.setText("資料來源：本次騎乘，分析完成")
        if self.collectionOutput:
            path = analysisOutputPath(self.collectionOutput)
            try:
                session["aligned_1s"] = result["aligned"]
                save_session(path, session)
                self.appendLog(f"騎乘分析已保存：{path.name}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "無法保存分析", str(exc))
        self.refreshRideAnalysis()
        self.refreshComparison()

    def requestDiscardCurrentTest(self) -> None:
        """Ask before permanently deleting the current collection and its database rows."""
        has_current_test = self.rideSession is not None or self.collectedRideSession is not None
        if not has_current_test:
            QMessageBox.information(self, "捨棄本次測試", "目前沒有可捨棄的本次測試。")
            return
        choice = QMessageBox.question(
            self, "捨棄本次測試",
            "將刪除本次測試的 EMG、踏板、阻力計、資訊與分析檔，並刪除資料庫中的同次測試資料。\n\n此操作無法復原，是否繼續？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Discard:
            return
        if self.rideSession is not None:
            self.discardCurrentCollection = True
            self.discardButton.setEnabled(False)
            self.stopStream()
            return
        self.finalizeDiscardedTest(active_database_writer=False)

    def finalizeDiscardedTest(self, active_database_writer: bool) -> None:
        """Clear the rejected session only after all recorders have stopped."""
        self.discardDatabaseRecording(active_database_writer)
        removed, failures = self.removeCurrentCollectionFiles()
        self.discardCurrentCollection = False
        self.rideSession = None
        self.latestRideSession = None
        self.collectedRideSession = None
        self.latestPedalRecords = []
        self.summaryRecords = []
        self.angleEmgPath = None
        self.anglePedalPath = None
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.currentSessionSummaryButton.setEnabled(False)
        self.exportReportButton.setEnabled(False)
        self.rideMuscleTable.setRowCount(0)
        self.rideStateLabel.setText("本次測試已捨棄")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "warning")
        self.summarySourceLabel.setText("本次測試已捨棄，未保留分析資料")
        self.refreshSummaryAnalysis()
        self.refreshComparison()
        self.discardButton.setEnabled(False)
        detail = f"已刪除 {removed} 個檔案" if removed else "沒有需要刪除的檔案"
        if failures:
            detail += f"；{len(failures)} 個檔案無法刪除"
        self.appendLog(f"本次測試已捨棄：{detail}")
        if failures:
            QMessageBox.warning(self, "部分檔案未刪除", "\n".join(failures))

    def removeCurrentCollectionFiles(self) -> tuple[int, list[str]]:
        """Delete only files derived from this collection's exact output names."""
        paths = {path for path in (self.collectionOutput, self.infoOutput, self.trainerOutput, self.pedalOutput) if path}
        if self.collectionOutput:
            paths.add(analysisOutputPath(self.collectionOutput))
        if self.pedalOutput:
            paths.add(self.pedalOutput.with_suffix(".fit"))
        removed, failures = 0, []
        for path in paths:
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                failures.append(f"{path.name}：{exc}")
        return removed, failures

    def refreshLiveRideSummary(self) -> None:
        if self.rideSession is None:
            return
        if self.tabs.currentWidget() is self.summaryTab:
            if self.summaryTabs.currentIndex() == 0:
                self.refreshRideAnalysis()
            elif self.summaryTabs.currentIndex() == 1 and self.pedalRecorder is not None and self.pedalSummarySource == "current":
                if len(self.summaryRecords) != len(self.pedalRecorder.records):
                    self.summaryRecords = list(self.pedalRecorder.records)
                    self.refreshSummaryAnalysis()
        elif self.tabs.currentWidget() is self.comparisonTab and self.referenceRideSession is not None:
            self.refreshComparison()

    def refreshRideAnalysis(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        normalized = False
        result = analyze(session, normalized)
        self.rideEmptyLabel.hide()
        self.rideMuscleTable.setRowCount(len(result["emg"]))
        for row, (slot, data) in enumerate(result["emg"].items()):
            channel = session.get("channels", {}).get(slot, {})
            label = f"S{int(slot):02d} {channel.get('label') or channel.get('mapping_name') or ''}"
            def fmt(value, suffix="", digits=3):
                return "--" if value is None else f"{value:.{digits}f}{suffix}"
            compare_value = fmt(data["comparison"]["mean"], " 倍" if normalized else " mV")
            phase = f"{fmt(data['early_rms_mv'])} → {fmt(data['late_rms_mv'])} mV"
            status = "、".join(data["quality"]) or "有效"
            for column, value in enumerate((label, fmt(data["rms_mv"]["mean"], " mV"),
                                            fmt(data["rms_mv"]["max"], " mV"), compare_value, phase, status)):
                self.rideMuscleTable.setItem(row, column, QTableWidgetItem(value))
        self.summaryMetricValues["duration"].setText(f"{result['duration_s']:.0f}")
        self.summaryMetricValues["muscles"].setText(str(sum(item["rms_mv"]["count"] > 0 for item in result["emg"].values())))
        self.rideStateLabel.setText("收集中" if self.rideSession is not None else "分析完成")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "info" if self.rideSession is not None else "success")
        self.exportReportButton.setEnabled(True)
        for plot in (self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot):
            plot.clear()
        bar_positions, average_heights, peak_heights, brushes, ticks = [], [], [], [], []
        for index, slot in enumerate(sorted(result["emg"], key=lambda value: int(value))):
            data = result["emg"][slot]["rms_mv"]
            if data["mean"] is not None:
                color = COLORS[(int(slot) - 1) % len(COLORS)]
                bar_positions.append(index)
                average_heights.append(data["mean"])
                peak_heights.append(data["max"] or 0.0)
                brushes.append(pg.mkBrush(color))
                ticks.append((index, f"S{int(slot):02d}"))
        if bar_positions:
            self.rmsAverageBarPlot.addItem(pg.BarGraphItem(
                x=bar_positions, height=average_heights, width=0.68, brushes=brushes))
            self.rmsPeakBarPlot.addItem(pg.BarGraphItem(
                x=bar_positions, height=peak_heights, width=0.68, brushes=brushes))
            tokens = (FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark"
                      else FluentThemeManager.LIGHT)
            for plot, values in ((self.rmsAverageBarPlot, average_heights),
                                 (self.rmsPeakBarPlot, peak_heights)):
                for position, value in zip(bar_positions, values):
                    label = pg.TextItem(f"{value:.3f}", color=tokens["text_primary"], anchor=(0.5, 1.0))
                    plot.addItem(label)
                    label.setPos(position, value)
            self.rmsAverageBarPlot.getAxis("bottom").setTicks([ticks])
            self.rmsPeakBarPlot.getAxis("bottom").setTicks([ticks])
            self.rmsAverageBarPlot.setXRange(-0.65, len(bar_positions) - 0.35, padding=0)
            self.rmsPeakBarPlot.setXRange(-0.65, len(bar_positions) - 0.35, padding=0)
        previous_slot = self.envelopeMuscleCombo.currentData()
        self.envelopeMuscleCombo.blockSignals(True)
        self.envelopeMuscleCombo.clear()
        for slot in sorted(result["emg"], key=lambda value: int(value)):
            channel = session.get("channels", {}).get(slot, {})
            label = channel.get("label") or channel.get("mapping_name") or ""
            self.envelopeMuscleCombo.addItem(f"S{int(slot):02d} {label}", slot)
        selected_index = self.envelopeMuscleCombo.findData(previous_slot)
        self.envelopeMuscleCombo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self.envelopeMuscleCombo.setEnabled(self.envelopeMuscleCombo.count() > 0)
        self.envelopeMuscleCombo.blockSignals(False)
        selected_slot = self.envelopeMuscleCombo.currentData()
        self.emgEnvelopePlot.setTitle(f"EMG RMS Peak 包絡線（每 1 秒最高值）— {self.envelopeMuscleCombo.currentText()}"
            if selected_slot is not None else "EMG RMS Peak 包絡線（每 1 秒最高值）")
        if selected_slot is not None:
            slot = str(selected_slot)
            rows = [row for row in session["emg"] if str(row.get("slot")) == slot]
            offset = result["emg_trainer_offset_s"]
            envelope = peak_envelope(rows)
            if envelope:
                stride = max(1, len(envelope) // 2000)
                self.emgEnvelopePlot.plot([point[0] + offset for point in envelope[::stride]],[point[1] for point in envelope[::stride]],
                    pen=pg.mkPen(COLORS[(int(slot) - 1) % len(COLORS)], width=2),name=f"S{int(slot):02d}",)

    def importEmgAnalysis(self) -> None:
        if self.emgAnalysisImportWorker is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "匯入 EMG 分析 CSV", self.pathEdit.text().strip(), "EMG CSV (*.csv)")
        if not filename:
            return
        path = Path(filename)
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.importEmgAnalysisButton.setEnabled(False)
        worker = EMGAnalysisImportWorker(path, list(self.mappings), baselines, asdict(self.collectionSubject), self.filterSettings,)
        self.emgAnalysisImportWorker = worker
        worker.completed.connect(self.emgAnalysisImported)
        worker.failed.connect(self.emgAnalysisImportFailed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.emgAnalysisImportFinished)
        worker.start()

    def emgAnalysisImported(self, session: dict, filename: str) -> None:
        self.latestRideSession = session
        self.angleEmgPath = Path(filename)
        paired_pedal = pedalOutputPath(self.angleEmgPath)
        self.anglePedalPath = paired_pedal if paired_pedal.is_file() else None
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.collectedRideSession = None
        self.summarySourceLabel.setText(f"資料來源：匯入 EMG CSV — {Path(filename).name}")
        self.refreshRideAnalysis()
        self.summaryTabs.setCurrentIndex(0)

    def emgAnalysisImportFailed(self, message: str) -> None:
        QMessageBox.warning(self, "匯入 EMG 資料失敗", message)

    def emgAnalysisImportFinished(self) -> None:
        self.importEmgAnalysisButton.setEnabled(True)
        self.emgAnalysisImportWorker = None

    def loadReferenceRide(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選取第一次騎乘", self.pathEdit.text(), "分析檔 (*.analysis.json)")
        if not path:
            return
        try:
            self.referenceRideSession = load_session(Path(path))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "無法載入第一次騎乘", str(exc))
            return
        self.loadReferenceButton.setText(f"第一次：{Path(path).name}")
        self.refreshComparison()
        self.tabs.setCurrentWidget(self.comparisonTab)

    def refreshComparison(self) -> None:
        first, second = self.referenceRideSession, self.rideSession or self.latestRideSession
        if first is None or second is None:
            self.comparisonStatusLabel.setText("選取第一次騎乘，再使用本次或載入歷史騎乘作為第二次。")
            self.comparisonTable.setRowCount(0)
            self.comparisonPlot.clear()
            return
        result = compare_sessions(first, second)
        self.comparisonStatusLabel.setText("第一次／第二次依相同肌肉、側別、感測器及濾波設定配對。差異 = 第二次 − 第一次。")
        bilateral = [f"{item['muscle']} 右−左相對活化：{item['first_right_minus_left_percent']:+.1f}% → {item['second_right_minus_left_percent']:+.1f}%"
                     for item in result["bilateral"]
                     if item["first_right_minus_left_percent"] is not None and item["second_right_minus_left_percent"] is not None]
        self.comparisonNotes.setText("\n".join([*bilateral, *result["notes"]]))
        previous_row = self.comparisonTable.currentRow()
        self.comparisonTable.blockSignals(True)
        self.comparisonTable.setRowCount(len(result["channels"]))
        for row, entry in enumerate(result["channels"]):
            a, b = entry["first"], entry["second"]
            def fmt(value, signed=False, digits=3):
                return "--" if value is None else f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
            cells = (entry["label"], fmt(a["median_mv"]), fmt(b["median_mv"]),
                     fmt(entry["median_difference_mv"], True), fmt(entry["median_change_percent"], True, 1),
                     f"{fmt(a['p90_mv'])} / {fmt(b['p90_mv'])}", entry["quality"])
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if column > 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.comparisonTable.setItem(row, column, item)
        self.comparisonTable.blockSignals(False)
        self.comparisonResult = result
        if result["channels"]:
            self.comparisonTable.selectRow(max(0, min(previous_row, len(result["channels"]) - 1)))
            self.updateComparisonPlot()
        else:
            self.comparisonPlot.clear()

    def updateComparisonPlot(self) -> None:
        self.comparisonPlot.clear()
        first, second = self.referenceRideSession, self.rideSession or self.latestRideSession
        if first is None or second is None or not hasattr(self, "comparisonResult"):
            return
        row = self.comparisonTable.currentRow()
        entries = self.comparisonResult["channels"]
        if row < 0 or row >= len(entries):
            return
        slot = entries[row]["slot"]
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        for session, name, color in ((first, "第一次", tokens["text_secondary"]),
                                     (second, "第二次", tokens["brand_background"])):
            points = [(sample["time_s"], sample["rms_mv"]) for sample in session.get("emg", [])
                      if str(sample.get("slot")) == slot and sample.get("rms_mv") is not None]
            if points:
                stride = max(1, len(points) // 1500)
                points = points[::stride]
                self.comparisonPlot.plot([point[0] for point in points], [point[1] for point in points],
                                         pen=pg.mkPen(color, width=2), name=name)

    def setAngleAdvancedVisible(self, visible: bool) -> None:
        self.angleAdvancedPanel.setVisible(visible)
        self.angleAdvancedToggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.angleAdvancedToggle.setText("收合進階設定" if visible else "進階設定")

    def updateAngleFileStatus(self) -> None:
        emg = getattr(self, "angleEmgPath", None)
        pedal = getattr(self, "anglePedalPath", None)
        if emg and pedal:
            self.angleStatusLabel.setText("檔案已選好，按「開始分析」即可使用建議設定；需要調整時再展開「進階設定」。")
        elif emg:
            self.angleStatusLabel.setText("已選擇 EMG CSV；請再選擇同一次騎乘的踏板 CSV。")
        elif pedal:
            self.angleStatusLabel.setText("已選擇踏板 CSV；請再選擇同一次騎乘的 EMG CSV。")

    def chooseAngleEmg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇原始 EMG CSV", self.pathEdit.text().strip(), "CSV (*.csv)")
        if path:
            self.angleEmgPath = Path(path)
            self.angleEmgButton.setText(f"EMG：{self.angleEmgPath.name}")
            self.angleEmgButton.setToolTip(str(self.angleEmgPath))
            self.angleAnalysisResult = None
            self.angleExportButton.setEnabled(False)
            self.updateAngleFileStatus()

    def chooseAnglePedal(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇踏板或校正角度 CSV", self.pathEdit.text().strip(), "CSV (*.csv)")
        if path:
            self.anglePedalPath = Path(path)
            self.anglePedalButton.setText(f"踏板：{self.anglePedalPath.name}")
            self.anglePedalButton.setToolTip(str(self.anglePedalPath))
            self.angleAnalysisResult = None
            self.angleExportButton.setEnabled(False)
            self.updateAngleFileStatus()

    def runAngleAnalysis(self) -> None:
        if self.angleAnalysisWorker is not None:
            return
        emg = self.angleEmgPath or (self.collectionOutput if self.collectionOutput and self.collectionOutput.is_file() else None)
        pedal = self.anglePedalPath or (self.pedalOutput if self.pedalOutput and self.pedalOutput.is_file() else None)
        if emg is None or pedal is None:
            self.angleStatusLabel.setText("請先選擇原始 EMG CSV 與踏板 CSV（或已校正角度 CSV）。")
            return
        import csv
        try:
            with emg.open("r", encoding="utf-8-sig", newline="") as stream:
                headers = next(csv.reader(stream))
            sides, side_source = recordingChannelSides(emg, headers, self.mappings)
            if not sides:
                raise ValueError("沒有通道符合設定頁的肌肉側別對應；請檢查感測器名稱與 -L/-R 縮寫。")
            values = [spin.value() for spin in self.angleRangeSpins]
            settings = AngleSettings((values[0], values[1]), (values[2], values[3]),
                                     scale=None if self.angleAutoScaleCheck.isChecked() else self.angleScaleSpin.value(),
                                     offset_s=None if self.angleAutoOffsetCheck.isChecked() else self.angleOffsetSpin.value())
            settings.validate()
        except (OSError, StopIteration, ValueError) as exc:
            self.angleStatusLabel.setText(f"無法分析：{exc}")
            return
        for key, value in zip(("start0", "start1", "end0", "end1"), values):
            self.settings.setValue(f"angle/{key}", value)
        self.settings.setValue("angle/offset_s", self.angleOffsetSpin.value())
        self.settings.setValue("angle/auto_offset", self.angleAutoOffsetCheck.isChecked())
        self.settings.setValue("angle/auto_scale", self.angleAutoScaleCheck.isChecked())
        self.settings.setValue("angle/scale", self.angleScaleSpin.value())
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.angleRunButton.setEnabled(False)
        self.angleEmgButton.setEnabled(False)
        self.anglePedalButton.setEnabled(False)
        self.angleStatusLabel.setText("正在背景分析原始 EMG 與踏板資料…")
        worker = AngleAnalysisWorker(emg, pedal, sides, settings, side_source)
        self.angleAnalysisWorker = worker
        worker.completed.connect(self.angleAnalysisCompleted)
        worker.failed.connect(self.angleAnalysisFailed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.angleAnalysisFinished)
        worker.start()

    def angleAnalysisCompleted(self, result: dict) -> None:
        self.angleAnalysisResult = result
        sync = result["sync"]
        reference_quality = (f'｜{len(sync["references"])} 個參考通道，最佳殘差 '
                             f'{sync["references"][0]["residual_std_s"] * 1000:.0f} ms'
                             if sync["references"] else "｜無可用時鐘參考通道")
        self.angleStatusLabel.setText(
            f'{sync["state"]}｜時鐘比例 {sync["scale"]:.5f}｜起點偏移 {sync["offset_s"]:+.3f} 秒｜有效圈數 {sync["valid_cycles"]}｜'
            f'{sync["angle_label"]}{reference_quality}｜側別依據：{sync.get("side_source", "輸入設定")}。'
            f'每個通道以自身第 {result["settings"]["percentile"]:g} 百分位為參考；'
            "跨肌肉顏色不可解讀為出力排名。")
        self.angleOverviewLabel.setText(result.get("overview") or analysis_summary_text(result))
        self.angleCurvePlot.setLabel("bottom", sync["angle_label"], units="°")
        self.angleHeatPlot.setLabel("bottom", sync["angle_label"], units="°")
        summary, profiles = result["summary"], result["profiles"]
        self.angleMuscleCombo.blockSignals(True)
        self.angleMuscleCombo.clear()
        for entry in summary.itertuples():
            self.angleMuscleCombo.addItem(f"{entry.location_zh} · {entry.channel_label}", entry.channel)
        self.angleMuscleCombo.blockSignals(False)
        self.refreshAngleCurve()
        self.angleComparisonTable.setRowCount(len(summary))
        for row, entry in enumerate(summary.itertuples()):
            cells = (f"{entry.location_zh} · {entry.channel_label}", str(entry.n_start), str(entry.n_end),
                     f"{entry.rms_uv_start:.1f}", f"{entry.rms_uv_end:.1f}",
                     f"{entry.rms_change_percent:+.1f}", f"{entry.peak_angle_start:.0f}",
                     f"{entry.peak_angle_end:.0f}", f"{entry.peak_angle_shift:+.0f}",
                     f"{entry.mdf_change_hz:+.1f}")
            for column, value in enumerate(cells):
                self.angleComparisonTable.setItem(row, column, QTableWidgetItem(value))
        names = list(summary.channel)
        matrix = np.column_stack([profiles[(profiles.channel == name) & (profiles.phase == "all")].mean_percent.to_numpy(float)
                                  for name in names])
        self.angleHeatPlot.clear()
        image = pg.ImageItem(matrix)
        image.setRect(QRectF(0, -.5, 360, len(names)))
        image.setColorMap(pg.colormap.get("CET-L4"))
        image.setLevels((0, 100))
        self.angleHeatPlot.addItem(image)
        self.angleHeatPlot.getAxis("left").setTicks([[(i, f'{summary.iloc[i].location_zh} · {sync["channel_names"][name]}')
                                                      for i, name in enumerate(names)]])
        self.angleHeatPlot.setXRange(0, 360, padding=0)
        self.angleHeatPhaseLine = pg.InfiniteLine(pos=self.anglePhaseSpin.value(), angle=90,pen=pg.mkPen("#555555", width=2, style=Qt.DashLine),)
        self.angleHeatPlot.addItem(self.angleHeatPhaseLine)
        self.refreshAnglePhase()
        self.angleExportButton.setEnabled(True)

    def refreshAnglePhase(self) -> None:
        """Show the direct, phase-specific answer before the detailed plots."""
        if self.angleAnalysisResult is None:
            return
        angle = self.anglePhaseSpin.value()
        values = phase_intensities(self.angleAnalysisResult, angle)
        if values.empty:
            return
        strongest_end = values.iloc[0]
        strongest_start = values.sort_values("start_percent", ascending=False).iloc[0]
        angle_label = self.angleAnalysisResult["sync"]["angle_label"]
        self.anglePhaseLabel.setText(
            f"{angle_label} {angle}°｜後段相對活化最高：{strongest_end.location_zh}"
            f"（{strongest_end.channel_label}）{strongest_end.end_percent:.0f}%；"
            f"前段相對活化最高：{strongest_start.location_zh}（{strongest_start.channel_label}）"
            f"{strongest_start.start_percent:.0f}%。藍色為前段、紅色為後段。"
            "此為各肌肉內的相對 EMG 活化，不代表哪條肌肉實際最費力。"
        )
        self.anglePhasePlot.clear()
        self.anglePhasePlot.setTitle(f"{angle}° 肌肉相對 EMG 活化｜藍色：前段；紅色：後段")
        axis = self.anglePhasePlot.getAxis("left")
        ticks = []
        tokens = (FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark"
                  else FluentThemeManager.LIGHT)
        for row, entry in values.iterrows():
            label = f"{entry.location_zh} · {entry.channel_label}"
            ticks.append((row, label))
            self.anglePhasePlot.plot([0, entry.start_percent], [row - .18, row - .18],pen=pg.mkPen(tokens["brand_background"], width=8))
            self.anglePhasePlot.plot([0, entry.end_percent], [row + .18, row + .18],pen=pg.mkPen(tokens["danger"], width=8))
        axis.setTicks([ticks])
        self.anglePhasePlot.setYRange(-.7, len(values) - .3, padding=0)
        self.anglePhasePlot.setXRange(0, max(100, float(values[["start_percent", "end_percent"]].max().max()) * 1.08), padding=0)
        self.anglePhasePlot.showGrid(x=True, y=False, alpha=.18)
        if hasattr(self, "angleHeatPhaseLine"):
            self.angleHeatPhaseLine.setPos(angle)
        self.refreshAngleCurve()

    def refreshAngleCurve(self) -> None:
        self.angleCurvePlot.clear()
        if self.angleAnalysisResult is None:
            return
        name = self.angleMuscleCombo.currentData()
        if not name:
            return
        profiles = self.angleAnalysisResult["profiles"]
        summary = self.angleAnalysisResult["summary"]
        entry = summary[summary.channel == name].iloc[0]
        self.angleCurvePlot.setTitle(
            f"{entry.location_zh}｜RMS {entry.rms_change_percent:+.1f}%｜"
            f"峰值相位 {entry.peak_angle_start:.0f}° → {entry.peak_angle_end:.0f}°（{entry.peak_angle_shift:+.0f}°）")
        self.angleDetailLabel.setText(
            f"{entry.location_zh}｜前段 {entry.n_start} 圈、後段 {entry.n_end} 圈。"
            f"RMS：{entry.rms_uv_start:.1f} → {entry.rms_uv_end:.1f} µV（{entry.rms_change_percent:+.1f}%）；"
            f"峰值：{entry.peak_angle_start:.0f}° → {entry.peak_angle_end:.0f}°（{entry.peak_angle_shift:+.0f}°）。")
        for phase, color, peak in (("start", "#1684d7", entry.peak_angle_start),
                                   ("end", "#d13438", entry.peak_angle_end)):
            sub = profiles[(profiles.channel == name) & (profiles.phase == phase)]
            self.angleCurvePlot.plot(sub.angle_bin_center.to_numpy(float), sub.mean_percent.to_numpy(float),
                                     pen=pg.mkPen(color, width=2),
                                     name=f'{"前段" if phase == "start" else "後段"} {entry.n_start if phase == "start" else entry.n_end} 圈，峰值 {peak:.0f}°')
            self.angleCurvePlot.addItem(pg.InfiniteLine(pos=peak, angle=90, pen=pg.mkPen(color, style=Qt.DashLine)))
        selected_angle = self.anglePhaseSpin.value()
        self.angleCurvePlot.addItem(pg.InfiniteLine(pos=selected_angle, angle=90, pen=pg.mkPen("#555555", width=2, style=Qt.DotLine),label=f"查看 {selected_angle}°", labelOpts={"position": .9, "color": "#555555"},))
        self.angleCurvePlot.setXRange(0, 360, padding=0)

    def showSelectedAngleMuscle(self) -> None:
        row = self.angleComparisonTable.currentRow()
        if row < 0 or self.angleAnalysisResult is None:
            return
        summary = self.angleAnalysisResult["summary"]
        if row >= len(summary):
            return
        channel = summary.iloc[row].channel
        index = self.angleMuscleCombo.findData(channel)
        if index >= 0:
            self.angleMuscleCombo.setCurrentIndex(index)
            self.angleViews.setCurrentIndex(1)

    def angleAnalysisFailed(self, message: str) -> None:
        self.angleStatusLabel.setText(f"分析失敗：{message}")

    def angleAnalysisFinished(self) -> None:
        self.angleAnalysisWorker = None
        self.angleRunButton.setEnabled(True)
        self.angleEmgButton.setEnabled(True)
        self.anglePedalButton.setEnabled(True)

    def exportAngleAnalysis(self) -> None:
        if self.angleAnalysisResult is None or self.angleExportWorker is not None:
            return
        folder = QFileDialog.getExistingDirectory(self, "選擇角度分析匯出資料夾", self.pathEdit.text().strip())
        if not folder:
            return
        base = Path(folder) / f"angle_analysis_{datetime.now():%Y%m%d_%H%M%S}"
        target = base
        suffix = 2
        while target.exists():
            target = Path(f"{base}_{suffix:02d}")
            suffix += 1
        self.angleExportButton.setEnabled(False)
        self.angleStatusLabel.setText("正在背景匯出同步 CSV、分析表與圖片…")
        worker = AngleExportWorker(self.angleAnalysisResult, target, self.angleIncludeRawCheck.isChecked())
        self.angleExportWorker = worker
        worker.completed.connect(lambda path: self.angleStatusLabel.setText(f"角度分析已匯出至 {path}"))
        worker.failed.connect(lambda message: self.angleStatusLabel.setText(f"匯出失敗：{message}"))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.angleExportFinished)
        worker.start()

    def angleExportFinished(self) -> None:
        self.angleExportWorker = None
        self.angleExportButton.setEnabled(self.angleAnalysisResult is not None)

    def exportRideReport(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        default = str((self.collectionOutput or Path(self.pathEdit.text()) / "cycling").with_suffix(".html"))
        path, _ = QFileDialog.getSaveFileName(self, "匯出 HTML 報表", default, "HTML (*.html)")
        if not path:
            return
        try:
            export_html(Path(path), session, False, self.referenceRideSession,
                        self.analysisFtpSpin.value())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "報表匯出失敗", str(exc))
            return
        self.appendLog(f"HTML 報表已匯出：{Path(path).name}")

    def startStream(self) -> None:
        if self.calibrationOnly:
            self.startCalibrationStream()
            return
        if not any((self.useEmgCheck.isChecked(), self.useTrainerCheck.isChecked(), self.usePedalCheck.isChecked())):
            QMessageBox.warning(self, "尚未選擇設備", "請在第一頁至少勾選一個本次要測試的設備。")
            self.tabs.setCurrentWidget(self.settingsTab)
            return
        if not self.useEmgCheck.isChecked():
            self.startBikeOnlySession()
            return
        if self.useTrainerCheck.isChecked() and not self.trainerIsConnected:
            QMessageBox.warning(self, "阻力計未連線", "請先連線阻力計，或取消勾選阻力計。")
            return
        if self.usePedalCheck.isChecked() and not self.pedalIsConnected:
            QMessageBox.warning(self, "踏板未連線", "請先連線踏板，或取消勾選踏板。")
            return
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
            return
        if not self.collectionSetupIsValid():
            return
        directoryText = self.pathEdit.text().strip()
        if not directoryText:
            QMessageBox.warning(self, "缺少輸出資料夾", "請選擇 CSV 輸出資料夾。")
            return
        directory = Path(directoryText).expanduser()
        if directory.exists() and not directory.is_dir():
            QMessageBox.warning(self, "輸出位置無效", "請選擇資料夾，而不是檔案。")
            return
        output = self.outputPathForCollection(directory)
        trainerOutput = trainerOutputPath(output)
        infoOutput = infoOutputPath(output)
        self.collectionSummary = None
        self.collectionError = None
        self.latestPedalRecords = []
        self.currentSessionSummaryButton.setEnabled(False)
        self.collectionOutput = output
        self.collectionSubject = self.subjectInfo()
        self.infoOutput = infoOutput
        self.trainerRecorder = None
        self.trainerOutput = trainerOutput if self.useTrainerCheck.isChecked() and self.trainerIsConnected else None
        self.pedalRecorder = None
        self.pedalOutput = pedalOutputPath(output) if self.usePedalCheck.isChecked() and self.pedalIsConnected else None
        self.infoRecorder = CollectionInfoRecorder(infoOutput, self.collectionSubject, output, self.trainerOutput)
        try:
            self.infoRecorder.start()
        except OSError as exc:
            self.infoRecorder = None
            QMessageBox.critical(self, "資訊檔輸出錯誤", str(exc))
            return
        self.beginRideAnalysis()
        self.collectionStoppedEarly = False
        self.startDatabaseRecording()
        self.emgRateLabel.setText("EMG 頻率：正在向 SDK 查詢…")
        for history in self.histories:
            history.clear()
        self.streamWorker = StreamWorker(host, output, self.mappings)
        self.streamWorker.status_changed.connect(self.streamStatusChanged)
        self.streamWorker.connected.connect(lambda banner: self.emgStatus.setToolTip(banner))
        self.streamWorker.base_ready.connect(lambda serial: self.baseLabel.setText(f"Base: {serial}"))
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.sample_rate_ready.connect(self.emgRateReady)
        self.streamWorker.collection_metadata_ready.connect(self.collectionMetadataReady)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.collection_completed.connect(self.collectionCompleted)
        self.streamWorker.failed.connect(self.streamFailed)
        self.streamWorker.finished.connect(self.streamFinished)
        self.setStreaming(True)
        self.streamWorker.start()

    def startCalibrationStream(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "基準測量", "請輸入 Trigno SDK Server IP。")
            self.calibrationOnly = False
            return
        self.collectionError = None
        self.collectionSummary = None
        self.calibrationRecording = self.trainerIsConnected or self.pedalIsConnected
        output = None
        self.trainerRecorder = None
        self.pedalRecorder = None
        self.infoRecorder = None
        self.trainerOutput = None
        self.pedalOutput = None
        if self.calibrationRecording:
            if not self.collectionSetupIsValid():
                self.calibrationOnly = False
                return
            directory_text = self.pathEdit.text().strip()
            directory = Path(directory_text).expanduser()
            if not directory_text or (directory.exists() and not directory.is_dir()):
                QMessageBox.warning(self, "基準測量", "請選擇有效的 CSV 輸出資料夾。")
                self.calibrationOnly = False
                return
            output = self.outputPathForCollection(directory)
            self.collectionOutput = output
            self.collectionSubject = self.subjectInfo()
            self.trainerOutput = trainerOutputPath(output) if self.trainerIsConnected else None
            self.pedalOutput = pedalOutputPath(output) if self.pedalIsConnected else None
            self.infoOutput = infoOutputPath(output)
            self.infoRecorder = CollectionInfoRecorder(self.infoOutput, self.collectionSubject, output, self.trainerOutput)
            try:
                self.infoRecorder.start()
            except OSError as exc:
                self.infoRecorder = None
                self.calibrationOnly = False
                QMessageBox.critical(self, "基準測量", str(exc))
                return
        self.preprocessor = None
        self.rmsExtractor = None
        self.streamWorker = StreamWorker(host, output, self.mappings)
        self.streamWorker.status_changed.connect(self.streamStatusChanged)
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.sample_rate_ready.connect(self.emgRateReady)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.collection_completed.connect(self.collectionCompleted)
        self.streamWorker.failed.connect(self.streamFailed)
        self.streamWorker.finished.connect(self.streamFinished)
        self.calibrationLabel.setText("基準測量：0 / 20 秒（前 10 秒捨棄，後 10 秒計算 RMS）")
        self.calibrationLabel.show()
        self.setStreaming(True)
        self.streamWorker.start()

    def startBikeOnlySession(self) -> None:
        """Record selected trainer/pedal data without starting the EMG SDK."""
        if not self.collectionSetupIsValid():
            return
        if self.useTrainerCheck.isChecked() and not self.trainerIsConnected:
            QMessageBox.warning(self, "阻力計未連線", "請先連線阻力計，或取消勾選阻力計。")
            return
        if self.usePedalCheck.isChecked() and not self.pedalIsConnected:
            QMessageBox.warning(self, "踏板未連線", "請先連線踏板，或取消勾選踏板。")
            return
        directoryText = self.pathEdit.text().strip()
        if not directoryText:
            QMessageBox.warning(self, "缺少輸出位置", "請選擇 CSV 輸出資料夾。")
            return
        directory = Path(directoryText).expanduser()
        if directory.exists() and not directory.is_dir():
            QMessageBox.warning(self, "輸出位置無效", "請選擇資料夾，而不是檔案。")
            return
        output = self.outputPathForCollection(directory)
        self.latestPedalRecords = []
        self.currentSessionSummaryButton.setEnabled(False)
        self.collectionOutput = output
        self.collectionSubject = self.subjectInfo()
        self.trainerOutput = trainerOutputPath(output) if self.useTrainerCheck.isChecked() else None
        self.pedalOutput = pedalOutputPath(output) if self.usePedalCheck.isChecked() else None
        self.infoOutput = infoOutputPath(output)
        self.infoRecorder = CollectionInfoRecorder(self.infoOutput, self.collectionSubject, output, self.trainerOutput)
        try:
            self.infoRecorder.start()
        except OSError as exc:
            self.infoRecorder = None
            QMessageBox.critical(self, "資料檔建立失敗", str(exc))
            return
        self.beginRideAnalysis()
        self.collectionStoppedEarly = False
        self.startDatabaseRecording()
        self.bikeOnlySession = True
        self.beginTrainerRecording()
        self.beginPedalRecording()
        self.setStreaming(True)
        self.setEmgStatus("僅收集騎乘裝置資料", "streaming")
        self.startCountdown()

    def stopStream(self) -> None:
        if self.remainingSeconds > 0:
            self.collectionStoppedEarly = True
        if self.bikeOnlySession:
            self.finishBikeOnlySession()
            return
        if self.streamWorker:
            self.setEmgStatus("正在停止…", "working")
            self.stopButton.setEnabled(False)
            self.streamWorker.request_stop()

    def finishBikeOnlySession(self) -> None:
        self.countdownTimer.stop()
        self.closeTrainerRecording()
        self.closePedalRecording()
        self.finishRideAnalysis()
        self.bikeOnlySession = False
        self.setStreaming(False)
        self.setEmgStatus("騎乘裝置資料收集完成", "connected")

    def streamStatusChanged(self, text: str) -> None:
        self.setEmgStatus(text, "streaming" if text == "串流中" else "working")
        if text == "串流中":
            # A live stream is also a verified SDK connection.  Retain this
            # state when collection stops normally.
            self.emgTested = True
            self.updateEmgConnectionIndicator()
        if text == "串流中" and (not self.calibrationOnly or self.calibrationRecording):
            self.beginTrainerRecording()
            self.beginPedalRecording()
        if text == "串流中":
            if self.calibrationOnly:
                self.startCalibrationCountdown()
            else:
                self.startCountdown()

    def beginTrainerRecording(self) -> None:
        self.beginCollectionDistance()
        if self.trainerOutput is None:
            self.appendLog("阻力計未連線；本次不輸出阻力計 CSV")
            return
        try:
            self.trainerRecorder = TrainerCsvRecorder(self.trainerOutput)
            self.trainerRecorder.start()
            if self.infoRecorder:
                native = (
                    f"{self.commandedNativeResistance:g}"
                    if self.commandedNativeResistance is not None else "設備未回報"
                )
                self.infoRecorder.setSettings("阻力計", [
                    f"固定阻力等級：{self.resistanceSpin.value()} / 30",
                    f"設備原生阻力：{native}",
                ])
            self.appendLog(f"阻力計 CSV 開始輸出：{self.trainerOutput.name}")
        except OSError as exc:
            self.trainerRecorder = None
            self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
            QMessageBox.warning(self, "阻力計 CSV 輸出錯誤", str(exc))

    def closeTrainerRecording(self) -> None:
        if self.trainerRecorder is None:
            self.distanceSessionActive = False
            return
        recorder, self.trainerRecorder = self.trainerRecorder, None
        try:
            elapsed = recorder.close()
        except OSError as exc:
            self.appendLog(f"阻力計 CSV 結束寫入失敗：{exc}")
            return
        self.appendLog(f"阻力計 CSV 已輸出 {recorder.rows} 筆 telemetry")
        if self.infoRecorder:
            self.infoRecorder.append("阻力計資料", [
                f"阻力計記錄筆數：{recorder.rows}",
                f"阻力計收集時間：{elapsed:.1f} 秒",
                f"本次騎乘距離：{self.collectionDistanceMeters or 0} m",
            ])
        self.distanceSessionActive = False

    def beginPedalRecording(self) -> None:
        if self.pedalOutput is None:
            self.appendLog("踏板未連線，本次不輸出踏板 CSV")
            return
        try:
            self.pedalRecorder = PedalCsvRecorder(self.pedalOutput)
            self.pedalRecorder.start()
            if self.infoRecorder:
                device = self.pedalDevices.currentText().strip() or "已連線踏板"
                self.infoRecorder.setSettings("踏板", [f"功率計裝置：{device}"])
            self.pedalSummarySource = "current"
            self.summaryRecords = []
            self.currentSessionSummaryButton.setEnabled(True)
            self.appendLog(f"踏板 CSV 開始輸出：{self.pedalOutput.name}")
        except OSError as exc:
            self.pedalRecorder = None
            self.appendLog(f"踏板 CSV 建立失敗：{exc}")
            QMessageBox.warning(self, "踏板 CSV 建立失敗", str(exc))

    def closePedalRecording(self) -> None:
        if self.pedalRecorder is None:
            return
        recorder, self.pedalRecorder = self.pedalRecorder, None
        elapsed = recorder.close()
        self.latestPedalRecords = list(recorder.records)
        summary = summarize_pedal_records(self.latestPedalRecords)
        self.showCurrentSessionSummary()
        self.appendLog(f"踏板 CSV 已完成：{recorder.rows} 筆資料")
        if recorder.records:
            try:
                fitPath = exportPedalFit(recorder.records, recorder.outputPath.with_suffix(".fit"))
                self.appendLog(f"踏板 FIT 已完成：{fitPath.name}")
            except Exception as exc:
                self.appendLog(f"踏板 FIT 輸出失敗：{exc}")
        if self.infoRecorder:
            self.infoRecorder.append("踏板資料", [
                *pedal_summary_lines(summary),
            ])

    def setSummaryRecords(self, records: list[dict], source: str) -> None:
        """Render one standardized summary on the persistent summary page."""
        self.summaryRecords = list(records)
        self.pedalSummarySource = "fit" if "匯入 FIT" in source else "current"
        self.summarySourceLabel.setText(source)
        self.refreshSummaryAnalysis()

    def refreshSummaryAnalysis(self) -> None:
        """Refresh the numerical report and time-series plots for the selected source."""
        if not self.summaryRecords:
            self.summaryDetails.setText("完成踏板收集，或匯入FIT檔後，摘要會顯示在這裡")
            for label in self.pedalMetricCards.values():
                label.setText("--")
            if self.pedalZoneTable is not None:
                self.pedalZoneTable.clearContents()
            for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot):
                plot.clear()
            self.leftPowerBalanceRing.setValues(None, None)
            self.rightPowerBalanceRing.setValues(None, None)
            return
        ftp = self.analysisFtpSpin.value()
        self.settings.setValue("analysis/ftp_w", ftp)
        summary = summarize_pedal_records(self.summaryRecords)
        analysis = analyze_pedal_records(self.summaryRecords, ftp)
        self.leftPowerBalanceRing.setValues(
            summary.get("average_left_balance_percent"), summary.get("average_left_power_w"),
        )
        self.rightPowerBalanceRing.setValues(
            summary.get("average_right_balance_percent"), summary.get("average_right_power_w"),
        )
        self.summaryDetails.setText("\n".join([
            *pedal_summary_lines(summary), "", *pedal_analysis_lines(self.summaryRecords, ftp),
        ]))
        for key, label in self.pedalMetricCards.items():
            value = analysis[key]
            label.setText("--" if value is None else f"{value:.0f}" if key in ("peak_5s_w", "coverage_percent") else f"{value:.2f}" if key == "intensity_factor" else f"{value:.0f}")
        if self.pedalZoneTable is not None:
            self.pedalZoneTable.setRowCount(len(analysis["zones"]))
            for row, zone in enumerate(analysis["zones"]):
                for column, value in enumerate((zone["name"], zone["bounds"], f"{zone['seconds']} 秒", f"{zone['percent']:.1f}%")):
                    self.pedalZoneTable.setItem(row, column, QTableWidgetItem(value))
        series = analysis["series"]
        x = np.array([point["elapsed_s"] for point in series], dtype=float)
        power = np.array([np.nan if point["power_w"] is None else point["power_w"] for point in series], dtype=float)
        cadence = np.array([np.nan if point["cadence_rpm"] is None else point["cadence_rpm"] for point in series], dtype=float)
        left = np.array([np.nan if point["left_balance_percent"] is None else point["left_balance_percent"] for point in series], dtype=float)
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot):
            plot.clear()
        if not len(x):
            return
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        power_color = QColor(tokens["danger"])
        power_fill = QColor(power_color)
        power_fill.setAlpha(64)
        self.powerAnalysisPlot.plot(
            x, power, pen=pg.mkPen(power_color, width=2), fillLevel=0,
            brush=pg.mkBrush(power_fill), connect="finite",
        )
        average_power = summary.get("average_power_w")
        if average_power is not None:
            self.powerAnalysisPlot.addItem(pg.InfiniteLine(pos=float(average_power), angle=0,
                                                           pen=pg.mkPen(tokens["text_secondary"], style=Qt.DashLine)))
        maximum_power = summary.get("maximum_power_w")
        if maximum_power is not None:
            self.powerAnalysisPlot.addItem(pg.InfiniteLine(pos=float(maximum_power), angle=0,
                                                           pen=pg.mkPen(tokens["danger"], style=Qt.DotLine)))
        if average_power is not None and maximum_power is not None:
            self.powerAnalysisPlot.setTitle(
                f"功率 Power ｜ 平均 {average_power:.0f} W ｜ 最大 {maximum_power:.0f} W"
            )
        else:
            self.powerAnalysisPlot.setTitle("功率 Power ｜ 無足夠的功率資料")
        cadence_color = QColor(tokens["warning"])
        cadence_fill = QColor(cadence_color)
        cadence_fill.setAlpha(64)
        self.cadenceAnalysisPlot.plot(
            x, cadence, pen=pg.mkPen(cadence_color, width=2), fillLevel=0,
            brush=pg.mkBrush(cadence_fill), connect="finite",
        )
        average_cadence = summary.get("average_cadence_rpm")
        if average_cadence is not None:
            self.cadenceAnalysisPlot.addItem(pg.InfiniteLine(pos=float(average_cadence), angle=0,
                                                             pen=pg.mkPen(tokens["text_secondary"], style=Qt.DashLine)))
        maximum_cadence = summary.get("maximum_cadence_rpm")
        if maximum_cadence is not None:
            self.cadenceAnalysisPlot.addItem(pg.InfiniteLine(pos=float(maximum_cadence), angle=0,
                                                             pen=pg.mkPen(tokens["danger"], style=Qt.DotLine)))
        if average_cadence is not None and maximum_cadence is not None:
            self.cadenceAnalysisPlot.setTitle(
                f"踏頻 Cadence ｜ 平均 {average_cadence:.0f} rpm ｜ 最大 {maximum_cadence:.0f} rpm"
            )
        else:
            self.cadenceAnalysisPlot.setTitle("踏頻 Cadence ｜ 無足夠的踏頻資料")
        valid = np.isfinite(left)
        if valid.any():
            self.balanceAnalysisPlot.plot(x[valid], left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#43a047", name="左")
            self.balanceAnalysisPlot.plot(x[valid], 100 - left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#1684d7", name="右")

    def showCurrentSessionSummary(self) -> None:
        if self.rideSession is not None and self.pedalRecorder is not None:
            self.setSummaryRecords(self.pedalRecorder.records, "資料來源：本次騎乘踏板即時資料")
            self.summaryTabs.setCurrentIndex(1)
            return
        if self.collectedRideSession is not None:
            if self.latestPedalRecords:
                self.summaryRecords = list(self.latestPedalRecords)
                self.pedalSummarySource = "current"
                self.refreshSummaryAnalysis()
            self.latestRideSession = self.collectedRideSession
            self.summarySourceLabel.setText("資料來源：本程式最新收集的騎乘資料")
            self.refreshRideAnalysis()
            self.refreshComparison()
            self.summaryTabs.setCurrentIndex(0)
            return
        if not self.latestPedalRecords:
            QMessageBox.information(self, "本次摘要", "本次尚未收集到騎乘資料。")
            return
        self.currentSessionSummaryButton.setEnabled(True)
        self.setSummaryRecords(self.latestPedalRecords, "資料來源：本程式最新收集的踏板資料")

    def importFitSummary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入 FIT", "", "FIT files (*.fit *.FIT)")
        if not path:
            return
        try:
            records = load_pedal_fit(Path(path))
        except Exception as exc:
            QMessageBox.warning(self, "無法匯入 FIT", f"讀取 FIT 檔失敗：{exc}")
            return
        self.setSummaryRecords(records, f"資料來源：匯入 FIT — {Path(path).name}")
        self.summaryTabs.setCurrentIndex(1)
        self.appendLog(f"已匯入 FIT 摘要：{Path(path).name}（{len(records)} 筆 record）")

    def collectionMetadataReady(self, metadata: dict) -> None:
        if self.infoRecorder is None:
            return
        duration = self.configuredDurationSeconds()
        lines = [
            f"收集時間：{duration} 秒" if duration else "收集時間：手動停止",
            "測試肌肉：",
        ]
        lines.extend(
            "  {chinese_name}（{english_name}）".format(**channel)
            for channel in metadata["channels"]
        )
        lines.append("基準 RMS：")
        for channel in metadata["channels"]:
            record = self.baselineManager.records.get(channel["slot"])
            if record is None:
                value = "未設定"
            else:
                value = f"{record.rms_mv:.6f} mV"
            lines.append(f"  {channel['chinese_name']}：{value}")
        self.infoRecorder.append("EMG 資料", lines)

    def configuredDurationSeconds(self) -> int:
        return self.durationMinutesSpin.value() * 60 + self.durationSecondsSpin.value()

    def updateCountdownLabel(self) -> None:
        if self.remainingSeconds <= 0:
            if self.countdownMode != "calibration" and self.configuredDurationSeconds() <= 0:
                self.countdownLabel.hide()
                return
        self.countdownLabel.setText(f"剩餘 {max(0, self.remainingSeconds)} 秒")
        self.countdownLabel.show()

    def resetCountdownDisplay(self) -> None:
        if not self.countdownTimer.isActive():
            self.remainingSeconds = self.configuredDurationSeconds()
            self.updateCountdownLabel()

    def startCountdown(self) -> None:
        self.countdownMode = "collection"
        self.countdownStopsStream = True
        self.remainingSeconds = self.configuredDurationSeconds()
        self.updateCountdownLabel()
        if self.remainingSeconds > 0:
            self.countdownTimer.start()

    def startCalibrationCountdown(self) -> None:
        """Show the fixed 20-second countdown without controlling capture."""
        self.countdownMode = "calibration"
        self.countdownStopsStream = False
        self.remainingSeconds = 20
        self.updateCountdownLabel()
        self.countdownTimer.start()

    def updateCountdown(self) -> None:
        self.remainingSeconds -= 1
        self.updateCountdownLabel()
        if self.remainingSeconds <= 0:
            self.countdownTimer.stop()
            if not self.countdownStopsStream:
                return
            self.appendLog("收集時間到，自動停止串流")
            self.stopStream()

    def framesReceived(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] != CHANNEL_COUNT:
            return
        activeChannels = [channel for channel, active in enumerate(self.sensorActive) if active]
        if not activeChannels:
            return
        for channel in activeChannels:
            self.histories[channel].extend(frames[:, channel].tolist())
        if self.preprocessor is not None and self.rmsExtractor is not None:
            filtered = self.preprocessor.process(frames)
            if self.calibrationSession is not None:
                self.calibrationSession.add(filtered)
                self.calibrationLabel.setText(f"基準測量：{self.calibrationSession.progress_seconds:.1f} / 20 秒（前 10 秒捨棄）")
                if self.calibrationSession.complete:
                    self.finishBaselineCalibration()
            else:
                rms_values = self.rmsExtractor.add(filtered)
                for channel, current in rms_values.items():
                    if channel in activeChannels:
                        self.updateBaselineRow(channel, current)
                if self.rideSession is not None and self.rideTime() - self.lastEmgAnalysisAt >= 0.2:
                    stamp = self.rideTime()
                    self.lastEmgAnalysisAt = stamp
                    self.rideSession["sample_rate_hz"] = self.currentSampleRate
                    for channel in activeChannels:
                        current = rms_values.get(channel)
                        record = self.baselineManager.records.get(channel + 1)
                        compatible = record is not None and self.baselineManager.compatible(
                            record, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"),
                            self.currentSampleRate, self.filterSettings)
                        comparison = compare_rms(current, record.rms_mv if compatible else None)
                        if record is not None and not compatible:
                            comparison["quality"] = "基準不相容"
                        add_point(self.rideSession, "emg", {"time_s": stamp, "slot": channel + 1,
                            "rms_mv": current, **comparison})
                        mapping = self.mappings[channel]
                        percent = comparison.get("percent")
                        alert = "NONE"
                        if isinstance(percent, (int, float)):
                            if abs(percent) >= self.baselineManager.focus_percent:
                                alert = "RED"
                            elif abs(percent) >= self.baselineManager.warning_percent:
                                alert = "YELLOW"
                            else:
                                alert = "NORMAL"
                        self.databaseAddSample("emg", stamp, {
                            "muscle_name": f"{mapping.chinese_name} ({mapping.abbreviation})",
                            "rms_mv": current, "base_rms_mv": record.rms_mv if compatible and record else None,
                            "deviation_pct": percent, "alert_status": alert,
                            "quality_status": comparison.get("quality", "VALID")})
        self.sampleLabel.setText(f"{len(self.histories[activeChannels[0]]):,} samples in view")

    def emgRateReady(self, sampleRate: float, samplesPerFrame: int, frameInterval: float) -> None:
        self.currentSampleRate = sampleRate
        try:
            self.preprocessor = EMGPreprocessor(CHANNEL_COUNT, sampleRate, self.filterSettings)
            self.rmsExtractor = RMSFeatureExtractor(CHANNEL_COUNT, sampleRate)
        except ValueError as exc:
            self.preprocessor = None
            self.rmsExtractor = None
            self.calibrationLabel.setText(f"RMS 未啟用：{exc}")
        self.emgRateLabel.setText(f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samplesPerFrame} samples/frame")

    def finishBaselineCalibration(self) -> None:
        session = self.calibrationSession
        if session is None:
            return
        saved, invalid = [], []
        for channel in session.channels:
            rms = session.rms_mv(channel)
            if rms is None or rms <= 1e-9:
                invalid.append(f"S{channel + 1:02d}")
                continue
            self.baselineManager.set_measured(channel + 1, rms, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"), self.currentSampleRate, self.filterSettings)
            saved.append(f"S{channel + 1:02d}")
        try:
            self.baselineManager.save()
        except OSError as exc:
            QMessageBox.warning(self, "無法保存基準", str(exc))
        self.calibrationLabel.setText(f"基準完成：{', '.join(saved) or '無有效通道'}" + (f"；無效：{', '.join(invalid)}" if invalid else ""))
        self.calibrationSession = None
        self.updateSensorRows()
        self.stopStream()

    def collectionCompleted(self, samples: int, elapsed: float, sampleRate: float) -> None:
        self.collectionSummary = (samples, elapsed, sampleRate)
        self.emgRateLabel.setText(f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samples:,} samples")
        output = self.collectionOutput.name if self.collectionOutput else "CSV"
        self.appendLog(f"EMG | 量測完成：{samples:,} samples，SDK 頻率 {sampleRate:.3f} Hz，已輸出 {output}")
        if self.infoRecorder:
            rms_values: dict[int, list[float]] = {}
            for point in (self.rideSession or {}).get("emg", []):
                slot, rms = point.get("slot"), point.get("rms_mv")
                if isinstance(slot, int) and isinstance(rms, (int, float)) and np.isfinite(rms):
                    rms_values.setdefault(slot, []).append(float(rms))
            rms_lines = ["EMG 肌肉 RMS 平均值："]
            for slot in sorted(rms_values):
                mapping = self.mappings[slot - 1]
                values = rms_values[slot]
                rms_lines.append(f"  {mapping.chinese_name}：{np.mean(values):.6f} mV")
            if len(rms_lines) == 1:
                rms_lines.append("  無足夠資料")
            self.infoRecorder.append("EMG 測試摘要", [
                f"EMG 樣本數：{samples}",
                f"EMG 收集時間：{elapsed:.1f} 秒",
                f"EMG RMS 記錄筆數：{sum(map(len, rms_values.values()))}",
                *rms_lines,
            ])

    def streamFailed(self, message: str) -> None:
        self.collectionError = message
        title = "CSV 輸出錯誤" if message.startswith("CSV 輸出失敗：") else "Trigno 串流錯誤"
        self.setEmgStatus(message, "error")
        if not message.startswith("CSV 輸出失敗："):
            self.setEmgConnectionState(False)
        QMessageBox.critical(self, title, message)

    def streamFinished(self) -> None:
        worker, self.streamWorker = self.streamWorker, None
        if worker:
            worker.deleteLater()
        self.countdownTimer.stop()
        self.closeTrainerRecording()
        self.closePedalRecording()
        if not self.calibrationOnly:
            self.finishRideAnalysis()
        self.setStreaming(False)
        if self.calibrationOnly:
            self.calibrationOnly = False
            self.calibrationRecording = False
            if self.collectionError:
                self.calibrationLabel.setText("基準測量失敗；既有基準未變更")
            return
        if self.collectionError:
            return
        if self.collectionSummary:
            samples, elapsed, sampleRate = self.collectionSummary
            output = self.collectionOutput.name if self.collectionOutput else "CSV"
            self.setEmgStatus(f"量測完成：{samples:,} samples · {sampleRate:.3f} Hz · 已輸出 {output}","connected",)
            return
        self.setEmgStatus("已停止", "idle")

    def rebuildPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        selected = [
            index for index, check in enumerate(self.sensorChecks)
            if check.isChecked() and self.sensorActive[index]
        ]
        self.plotWidget.clear()
        self.curves.clear()
        if not selected:
            # Keep the coordinate system visible before EMG sensors have been
            # connected or selected, so the live-waveform area is not blank.
            plot = self.plotWidget.addPlot(row=0, col=0)
            plot.setLabel("left", "Amplitude", units="mV")
            plot.setLabel("bottom", "Time", units="s")
            plot.showGrid(x=True, y=True, alpha=0.16)
            plot.setXRange(-10, 0, padding=0)
            plot.setYRange(-1, 1, padding=0)
            plot.setMenuEnabled(False)
            return
        for row, channel in enumerate(selected):
            mapping = self.mappings[channel]
            plot = self.plotWidget.addPlot(row=row, col=0)
            plot.setLabel("left", mapping.abbreviation or f"S{channel + 1:02d}", units="mV")
            if row == len(selected) - 1:
                plot.setLabel("bottom", "時間", units="s")
            else:
                plot.hideAxis("bottom")
            plot.showGrid(x=True, y=True, alpha=0.16)
            plot.setMenuEnabled(False)
            color = QColor(COLORS[channel % len(COLORS)])
            fill = QColor(color)
            fill.setAlpha(56)
            curve = plot.plot(
                pen=pg.mkPen(color, width=1), fillLevel=0, brush=pg.mkBrush(fill),
            )
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
            stats = pg.TextItem(color=tokens["text_secondary"], anchor=(1, 1))
            stats.setZValue(10)
            plot.addItem(stats)
            self.curves[channel] = (plot, curve, stats)
        self.refreshPlots()

    def refreshPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        duration, count = 10, int(10 * self.currentSampleRate)
        for channel, (plot, curve, stats) in self.curves.items():
            values = np.asarray(list(self.histories[channel])[-count:], dtype=np.float32) * 1000
            xAxis = (np.arange(len(values)) - len(values) + 1) / self.currentSampleRate
            limit = 1.0
            if len(values):
                limit = max(0.05, min(20, float(np.percentile(np.abs(values), 99.5)) * 1.25))
            plot.setXRange(-duration, 0, padding=0)
            plot.setYRange(-limit, limit, padding=0)
            curve.setData(xAxis, values)
            if len(values):
                amplitudes = np.abs(values)
                mean_amplitude = float(np.mean(amplitudes))
                max_amplitude = float(np.max(amplitudes))
                stats.setText(f"平均絕對振幅：{mean_amplitude:.3f} mV\n最大絕對振幅：{max_amplitude:.3f} mV")
                stats.setPos(-0.15, limit * 0.94)
            else:
                stats.setText("等待 EMG 樣本")
                stats.setPos(-0.15, limit * 0.94)

    def pedalReady(self) -> None:
        self.pedalBluetoothReady = True
        self.startInitialBluetoothScan()

    def trainerReady(self) -> None:
        self.trainerBluetoothReady = True
        self.startInitialBluetoothScan()

    # Final scan-assignment flow: scan feedback belongs in the event log, while
    # the connection panel only presents connected/disconnected state.
    def scanCyclingDevices(self) -> None:
        if self.bluetoothScanWorker and self.bluetoothScanWorker.isRunning():
            return
        self.pedalScanInProgress = True
        self.scanTrainerButton.setEnabled(False)
        self.scanPedalButton.setEnabled(False)
        self.appendLog("Bluetooth scan started")
        self.bluetoothScanWorker = CyclingDeviceScanWorker()
        self.bluetoothScanWorker.devicesFound.connect(self.showCyclingDevices)
        self.bluetoothScanWorker.failed.connect(self.cyclingScanFailed)
        self.bluetoothScanWorker.finished.connect(self.finishCyclingScan)
        self.bluetoothScanWorker.start()

    def showCyclingDevices(self, devices: list[tuple[str, str]]) -> None:
        self.showScanResults(devices)
        self.appendLog(f"Bluetooth scan | {len(devices)} devices found")

    def showScanResults(self, devices: list[tuple[str, str]]) -> None:
        if not hasattr(self, "scanResultsLayout"):
            return
        while self.scanResultsLayout.count():
            item = self.scanResultsLayout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        if not devices:
            self.scanResultsLayout.addWidget(QLabel("未找到藍牙裝置", objectName="muted"))
            self.scanResultsLayout.addStretch()
            return
        self.scanResultButtons: list[tuple[str, QPushButton]] = []
        for address, name in sorted(devices, key=lambda item: item[1].casefold()):
            button = QPushButton(f"{name or 'Unknown device'}  ·  {address}")
            button.setCheckable(True)
            button.setToolTip("點選此裝置，再在上方設定為阻力計或踏板")
            button.setFixedWidth(500)
            FluentThemeManager.set_property(button, "fluentRole", "scanResult")
            button.clicked.connect(
                lambda _checked=False, deviceAddress=address, deviceName=name: self.selectScannedDevice(
                    deviceAddress, deviceName
                )
            )
            self.scanResultsLayout.addWidget(button, 0, Qt.AlignLeft)
            self.scanResultButtons.append((address, button))
        self.scanResultsLayout.addStretch()

    def selectScannedDevice(self, address: str, name: str) -> None:
        self.selectedScannedDevice = (address, name)
        for buttonAddress, button in getattr(self, "scanResultButtons", []):
            selected = buttonAddress == address
            button.setChecked(selected)
            FluentThemeManager.set_property(button, "fluentSelected", "true" if selected else "false")
        label = f"{name or 'Unknown device'} · {address}"
        self.selectedScannedDeviceLabel.setText(label)
        self.assignTrainerButton.setEnabled(True)
        self.assignPedalButton.setEnabled(True)
        self.appendLog(f"Device selected | {label}")

    def assignSelectedScannedDevice(self, role: str) -> None:
        if self.selectedScannedDevice is None:
            return
        address, name = self.selectedScannedDevice
        label = f"{name or 'Unknown device'} · {address}"
        if role == "阻力計":
            self.trainerDevices.clear()
            self.trainerDevices.addItem(label, address)
            self.trainerDevices.setCurrentIndex(0)
        elif role == "踏板":
            if name.strip().upper().endswith(" R"):
                self.appendLog(f"Device assignment skipped | right pedal: {label}")
                return
            self.pedalDevices.clear()
            self.pedalDevices.addItem(label, address)
            self.pedalDevices.setCurrentIndex(0)
        self.appendLog(f"Device assignment | {role}: {label}")

    def closeEvent(self, event) -> None:  # noqa: N802
        # Every QThread must finish before its Python/Qt owner is destroyed.
        # In particular, a shared BLE scan can still be running during the
        # first few seconds after the application opens.
        for worker in (self.angleAnalysisWorker, self.angleExportWorker):
            if worker is not None and worker.isRunning():
                event.ignore()
                QTimer.singleShot(250, self.close)
                return
        if self.bluetoothScanWorker and self.bluetoothScanWorker.isRunning():
            self.bluetoothScanWorker.stop()
            if not self.bluetoothScanWorker.wait(3000):
                self.appendLog("Bluetooth scan is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return

        if self.testWorker and self.testWorker.isRunning():
            # ConnectionWorker uses a bounded socket timeout.  Waiting here
            # prevents a connection test from being destroyed mid-request.
            if not self.testWorker.wait(5000):
                self.appendLog("EMG connection test is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return

        if self.streamWorker and self.streamWorker.isRunning():
            self.streamWorker.request_stop()
            if not self.streamWorker.wait(6000):
                self.appendLog("EMG collection is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        if self.bikeOnlySession:
            self.finishBikeOnlySession()
        if self.trainerWorker.isRunning():
            self.trainerWorker.stopWorker()
            if not self.trainerWorker.wait(6000):
                self.appendLog("Resistance-meter worker is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        if self.pedalWorker.isRunning():
            self.pedalWorker.stopWorker()
            if not self.pedalWorker.wait(6000):
                self.appendLog("Pedal worker is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        for writer in tuple(self.databaseWriters):
            if writer.isRunning():
                writer.enqueue("stop")
                if not writer.wait(5000):
                    self.appendLog("Database writer is still stopping; close delayed")
                    event.ignore()
                    QTimer.singleShot(150, self.close)
                    return
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TrignoWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
