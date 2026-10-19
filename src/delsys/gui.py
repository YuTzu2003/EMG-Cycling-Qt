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
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget, QInputDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox, QToolButton,)
from delsys.collection import CollectionInfoRecorder, SubjectInfo, infoOutputPath
from delsys.emg.body_map import BodyMapPanel
from delsys.emg.sensor_mapping import (SensorMapping,duplicateMappingSlots,futureMeasurementMappings,loadMappings,saveMappings,matchesCsvColumn,recordingChannelSides,test2LegacyMappings,)
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
        presets = QHBoxLayout()
        legacyButton = QPushButton("還原 Test2 舊版對照")
        legacyButton.setToolTip("用於既有 Test2 資料：S01/02 是股二頭肌、S03/04 是股內側肌。")
        legacyButton.clicked.connect(lambda: self.applyPreset(test2LegacyMappings()))
        futureButton = QPushButton("套用新版（之後測量）")
        futureButton.setToolTip("只在開始之後的新測量前套用。Test2 舊資料請保留或切回舊版對照。")
        futureButton.clicked.connect(lambda: self.applyPreset(futureMeasurementMappings()))
        presets.addWidget(legacyButton)
        presets.addWidget(futureButton)
        presets.addStretch(1)
        layout.addLayout(presets)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Save).setText("儲存並套用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def applyPreset(self, mappings: list[SensorMapping]) -> None:
        """Replace only anatomy fields; baseline RMS values stay untouched."""
        for edits, mapping in zip(self.edits, mappings, strict=True):
            chinese, english, abbreviation = edits
            chinese.setText(mapping.chinese_name)
            english.setText(mapping.english_name)
            abbreviation.setText(mapping.abbreviation)

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
        header.addWidget(self.trainerConnectionLabel)
        header.addWidget(self.pedalConnectionLabel)
        header.addWidget(self.emgConnectionLabel)
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

        # A single in-app report keeps the important PPT-style findings in the
        # same place as the plots and source data.  It is deliberately plain
        # text: users can select/copy it into a clinical note without needing
        # an external report generator.
        angleReportPage = QWidget()
        angleReportLayout = QVBoxLayout(angleReportPage)
        angleReportLayout.setContentsMargins(8, 8, 8, 8)
        angleReportLayout.addWidget(QLabel("\u5b8c\u6574\u5206\u6790\u5831\u544a", objectName="sectionTitle"))
        self.angleReportTabs = QTabWidget()
        reportChartsPage = QScrollArea()
        reportChartsPage.setWidgetResizable(True)
        reportChartsContent = QWidget()
        reportChartsContent.setMinimumHeight(700)
        reportChartsLayout = QVBoxLayout(reportChartsContent)
        reportChartsLayout.setContentsMargins(8, 8, 8, 8)
        reportChartsLayout.setSpacing(18)
        reportChartsLayout.addWidget(QLabel("\u6bcf\u500b\u808c\u8089\u4e00\u7d44\u76f4\u5411\u9577\u689d\uff1b\u85cd\u8272\u70ba\u5de6\u8173\u3001\u6a58\u8272\u70ba\u53f3\u8173\u3002", objectName="muted"))
        self.angleReportRmsPlot = pg.PlotWidget(title="RMS \u8b8a\u5316\uff08\u5f8c\u6bb5\u6bd4\u524d\u6bb5\uff09")
        self.angleReportRmsPlot.setLabel("bottom", "RMS \u8b8a\u5316", units="%")
        self.angleReportRmsPlot.setMinimumHeight(300)
        reportChartsLayout.addWidget(self.angleReportRmsPlot, 1)
        self.angleReportMdfPlot = pg.PlotWidget(title="MDF \u8b8a\u5316\uff08\u5f8c\u6bb5\u6e1b\u524d\u6bb5\uff09")
        self.angleReportMdfPlot.setLabel("bottom", "MDF \u8b8a\u5316", units="Hz")
        self.angleReportMdfPlot.setMinimumHeight(300)
        reportChartsLayout.addWidget(self.angleReportMdfPlot, 1)
        reportChartsPage.setWidget(reportChartsContent)
        self.angleReportChartTabs = QTabWidget()
        self.angleReportChartTabs.addTab(reportChartsPage, "\u75b2\u52de\u6458\u8981")

        fatigueTrendsPage = QScrollArea()
        fatigueTrendsPage.setWidgetResizable(True)
        fatigueTrendsContent = QWidget()
        # Four full-height plots need more space than a typical report tab.
        # Keep the page scrollable instead of clipping the lower two plots.
        fatigueTrendsContent.setMinimumHeight(760)
        fatigueTrendsLayout = QVBoxLayout(fatigueTrendsContent)
        fatigueTrendsLayout.setContentsMargins(8, 8, 8, 8)
        fatigueTrendControls = QHBoxLayout()
        fatigueTrendControls.addWidget(QLabel("\u808c\u8089", objectName="sectionTitle"))
        self.angleReportFatigueMuscleCombo = QComboBox()
        self.angleReportFatigueMuscleCombo.setMinimumWidth(260)
        self.angleReportFatigueMuscleCombo.setAccessibleName("PPT \u9010\u5708\u75b2\u52de\u8da8\u52e2\u808c\u8089\u9078\u64c7")
        fatigueTrendControls.addWidget(self.angleReportFatigueMuscleCombo)
        fatigueTrendControls.addStretch(1)
        fatigueTrendsLayout.addLayout(fatigueTrendControls)
        fatigueTrendsLayout.addWidget(QLabel("\u6bcf\u689d\u7dda\u70ba\u4e00\u5074\u808c\u8089\uff0c\u4ee5 9 \u5708\u79fb\u52d5\u5e73\u5747\u986f\u793a\u9010\u5708\u8b8a\u5316\u3002", objectName="muted"))
        fatigueTrendGrid = QGridLayout()
        self.angleReportRmsTrendPlot = pg.PlotWidget(title="\u9010\u5708 RMS \u8da8\u52e2\uff1a\u5de6\u53f3\u8173\u758a\u5716")
        self.angleReportMdfTrendPlot = pg.PlotWidget(title="\u9010\u5708 MDF \u8da8\u52e2\uff1a\u5de6\u53f3\u8173\u758a\u5716")
        self.angleReportFatigueTrendPlots = (
            self.angleReportRmsTrendPlot, self.angleReportMdfTrendPlot,
        )
        for plot in self.angleReportFatigueTrendPlots:
            plot.setMinimumHeight(330)
            plot.setLabel("bottom", "\u8e29\u8e0f\u5708\u6578")
            plot.showGrid(x=True, y=True, alpha=.16)
            plot.addLegend(offset=(8, 8), colCount=2)
        self.angleReportRmsTrendPlot.setLabel("left", "RMS", units="\u00b5V")
        self.angleReportMdfTrendPlot.setLabel("left", "MDF", units="Hz")
        fatigueTrendGrid.addWidget(self.angleReportRmsTrendPlot, 0, 0)
        fatigueTrendGrid.addWidget(self.angleReportMdfTrendPlot, 1, 0)
        fatigueTrendsLayout.addLayout(fatigueTrendGrid, 1)
        fatigueTrendsPage.setWidget(fatigueTrendsContent)
        self.angleReportChartTabs.addTab(fatigueTrendsPage, "\u9010\u5708\u75b2\u52de\u8da8\u52e2")

        reportProfilesPage = QWidget()
        reportProfilesLayout = QVBoxLayout(reportProfilesPage)
        reportProfilesLayout.setContentsMargins(8, 8, 8, 8)
        profileControls = QHBoxLayout()
        profileControls.addWidget(QLabel("\u808c\u8089", objectName="sectionTitle"))
        self.angleReportProfileMuscleCombo = QComboBox()
        self.angleReportProfileMuscleCombo.setMinimumWidth(260)
        self.angleReportProfileMuscleCombo.setAccessibleName("PPT \u808c\u8089\u89d2\u5ea6\u66f2\u7dda\u808c\u8089\u9078\u64c7")
        profileControls.addWidget(self.angleReportProfileMuscleCombo)
        profileControls.addStretch(1)
        reportProfilesLayout.addLayout(profileControls)
        reportProfilesLayout.addWidget(QLabel("\u6bcf\u500b\u808c\u8089\u7684\u5de6\u3001\u53f3\u8173\u4e26\u6392\uff1b\u85cd\u7dda\u70ba\u524d\u6bb5\u3001\u7d05\u7dda\u70ba\u5f8c\u6bb5\u3002", objectName="muted"))
        self.angleReportProfileScroll = QScrollArea()
        self.angleReportProfileScroll.setWidgetResizable(True)
        self.angleReportProfiles = pg.GraphicsLayoutWidget()
        self.angleReportProfiles.setMinimumHeight(1620)
        self.angleReportProfiles.setBackground("#ffffff")
        self.angleReportProfileScroll.setWidget(self.angleReportProfiles)
        reportProfilesLayout.addWidget(self.angleReportProfileScroll, 1)
        self.angleReportChartTabs.addTab(reportProfilesPage, "\u808c\u8089\u89d2\u5ea6\u66f2\u7dda")

        reportCyclesPage = QWidget()
        reportCyclesLayout = QVBoxLayout(reportCyclesPage)
        reportCyclesLayout.setContentsMargins(8, 8, 8, 8)
        cycleControls = QHBoxLayout()
        cycleControls.addWidget(QLabel("\u808c\u8089", objectName="sectionTitle"))
        self.angleReportCycleMuscleCombo = QComboBox()
        self.angleReportCycleMuscleCombo.setMinimumWidth(260)
        self.angleReportCycleMuscleCombo.setAccessibleName("PPT \u9010\u5708 8 \u89d2\u5ea6\u808c\u8089\u9078\u64c7")
        cycleControls.addWidget(self.angleReportCycleMuscleCombo)
        cycleControls.addWidget(QLabel("\u8e29\u8e0f\u89d2\u5ea6", objectName="sectionTitle"))
        self.angleReportCycleAngleCombo = QComboBox()
        self.angleReportCycleAngleCombo.setMinimumWidth(130)
        self.angleReportCycleAngleCombo.setAccessibleName("\u9010\u5708\u6d3b\u5316\u8e29\u8e0f\u89d2\u5ea6\u9078\u64c7")
        self.angleReportCycleAngleCombo.addItem("\u5168\u90e8\u89d2\u5ea6", None)
        for angle in range(0, 360, 45):
            self.angleReportCycleAngleCombo.addItem(f"{angle}\u00b0", angle)
        cycleControls.addWidget(self.angleReportCycleAngleCombo)
        cycleControls.addStretch()
        reportCyclesLayout.addLayout(cycleControls)
        self.angleReportCycleHelpLabel = QLabel(
            "\u53ef\u9078\u64c7\u55ae\u4e00\u89d2\u5ea6\u4f86\u6bd4\u8f03\u5de6\u3001\u53f3\u8173\u9010\u5708\u8b8a\u5316\uff1b\u300c\u5168\u90e8\u89d2\u5ea6\u300d\u986f\u793a 0\u00b0\u300145\u00b0\u300190\u00b0\u2026315\u00b0\u3002\u5df2\u4ee5 5 \u5708\u79fb\u52d5\u5e73\u5747\u5e73\u6ed1\uff0c\u6de1\u85cd\u70ba\u524d\u6bb5\u3001\u6de1\u7d05\u70ba\u5f8c\u6bb5\u3002",
            objectName="muted"
        )
        reportCyclesLayout.addWidget(self.angleReportCycleHelpLabel)
        cyclePlots = QHBoxLayout()
        self.angleReportCycleLeftPlot = pg.PlotWidget(title="\u5de6\u8173")
        self.angleReportCycleRightPlot = pg.PlotWidget(title="\u53f3\u8173")
        for plot in (self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot):
            plot.setMinimumHeight(410)
            plot.setLabel("left", "\u76f8\u5c0d\u6d3b\u5316", units="%")
            plot.setLabel("bottom", "\u8e29\u8e0f\u5708\u6578")
            plot.showGrid(x=True, y=True, alpha=.16)
            plot.addLegend(offset=(8, 8), colCount=4)
        cyclePlots.addWidget(self.angleReportCycleLeftPlot, 1)
        cyclePlots.addWidget(self.angleReportCycleRightPlot, 1)
        reportCyclesLayout.addLayout(cyclePlots, 1)
        self.angleReportChartTabs.addTab(reportCyclesPage, "\u9010\u5708 8 \u89d2\u5ea6")

        reportValuesPage = QWidget()
        reportValuesLayout = QVBoxLayout(reportValuesPage)
        reportValuesLayout.setContentsMargins(8, 8, 8, 8)
        valueControls = QHBoxLayout()
        valueControls.addWidget(QLabel("\u808c\u8089", objectName="sectionTitle"))
        self.angleReportPhaseMuscleCombo = QComboBox()
        self.angleReportPhaseMuscleCombo.setMinimumWidth(260)
        self.angleReportPhaseMuscleCombo.setAccessibleName("PPT \u524d\u5f8c\u6bb5 8 \u89d2\u5ea6\u8868\u683c\u808c\u8089\u9078\u64c7")
        valueControls.addWidget(self.angleReportPhaseMuscleCombo)
        valueControls.addStretch()
        reportValuesLayout.addLayout(valueControls)
        reportValuesLayout.addWidget(QLabel("\u6bcf\u500b\u89d2\u5ea6\u70ba \u00b115\u00b0 \u8996\u7a97\u5e73\u5747\uff1b\u0394 = \u5f8c\u6bb5 \u2212 \u524d\u6bb5\u3002", objectName="muted"))
        self.angleReportPhaseTable = QTableWidget(0, 7)
        self.angleReportPhaseTable.setHorizontalHeaderLabels(("\u8e29\u8e0f\u89d2\u5ea6", "\u5de6\u524d\u6bb5", "\u5de6\u5f8c\u6bb5", "\u5de6 \u0394", "\u53f3\u524d\u6bb5", "\u53f3\u5f8c\u6bb5", "\u53f3 \u0394"))
        self.angleReportPhaseTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleReportPhaseTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.angleReportPhaseTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleReportPhaseTable.setMinimumHeight(360)
        self.angleReportPhaseTable.setAccessibleName("PPT \u524d\u5f8c\u6bb5 8 \u89d2\u5ea6\u6bd4\u8f03\u8868")
        reportValuesLayout.addWidget(self.angleReportPhaseTable, 1)
        self.angleReportChartTabs.addTab(reportValuesPage, "\u524d\u5f8c\u6bb5\u8868\u683c")
        self.angleReportTabs.addTab(self.angleReportChartTabs, "\u5716\u8868\u5206\u6790")

        reportTextPage = QWidget()
        reportTextLayout = QVBoxLayout(reportTextPage)
        reportTextLayout.setContentsMargins(8, 8, 8, 8)
        self.angleReportDetailsScroll = QScrollArea()
        self.angleReportDetailsScroll.setWidgetResizable(True)
        reportTextContent = QWidget()
        reportTextContentLayout = QVBoxLayout(reportTextContent)
        reportTextContentLayout.setContentsMargins(4, 4, 4, 4)
        reportTextContentLayout.setSpacing(8)

        def report_table(headers: tuple[str, ...], accessible_name: str) -> QTableWidget:
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setAccessibleName(accessible_name)
            return table

        reportTextContentLayout.addWidget(QLabel("\u3010\u6e2c\u8a66\u8207\u540c\u6b65\u3011", objectName="sectionTitle"))
        self.angleReportSyncTable = report_table(("\u9805\u76ee", "\u6578\u503c"), "\u8e29\u8e0f\u89d2\u5ea6\u6e2c\u8a66\u8207\u540c\u6b65\u8868\u683c")
        self.angleReportSyncTable.setMinimumHeight(190)
        reportTextContentLayout.addWidget(self.angleReportSyncTable)
        reportTextContentLayout.addWidget(QLabel("\u3010\u901a\u9053\u8207\u8cbc\u7247\u5c0d\u61c9\u3011", objectName="sectionTitle"))
        self.angleReportMappingTable = report_table(("\u808c\u8089 / \u90e8\u4f4d", "\u901a\u9053"), "\u8e29\u8e0f\u89d2\u5ea6\u901a\u9053\u5c0d\u61c9\u8868\u683c")
        self.angleReportMappingTable.setMinimumHeight(360)
        reportTextContentLayout.addWidget(self.angleReportMappingTable)
        reportTextContentLayout.addWidget(QLabel("\u3010\u91cd\u9ede\u7d50\u679c\u3011", objectName="sectionTitle"))
        self.angleReportHighlightsTable = report_table(("\u9805\u76ee", "\u7d50\u679c"), "\u8e29\u8e0f\u89d2\u5ea6\u91cd\u9ede\u7d50\u679c\u8868\u683c")
        self.angleReportHighlightsTable.setMinimumHeight(150)
        reportTextContentLayout.addWidget(self.angleReportHighlightsTable)
        reportTextContentLayout.addWidget(QLabel("\u3010\u901a\u9053\u524d\u5f8c\u6bb5\u6578\u503c\u3011", objectName="sectionTitle"))
        self.angleReportChannelTable = QTableWidget(0, 6)
        self.angleReportChannelTable.setHorizontalHeaderLabels((
            "\u808c\u8089 / \u901a\u9053", "RMS \u524d\u2192\u5f8c (\u00b5V)", "RMS %",
            "MDF \u524d\u2192\u5f8c (Hz)", "\u5cf0\u503c\u89d2\u5ea6\u8b8a\u5316", "\u5224\u8b80\u63d0\u793a",
        ))
        self.angleReportChannelTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleReportChannelTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.angleReportChannelTable.setAlternatingRowColors(True)
        self.angleReportChannelTable.setMinimumHeight(300)
        self.angleReportChannelTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleReportChannelTable.setAccessibleName("\u901a\u9053\u524d\u5f8c\u6bb5\u6578\u503c\u8868\u683c")
        reportTextContentLayout.addWidget(self.angleReportChannelTable)
        reportTextContentLayout.addWidget(QLabel("\u3010\u5224\u8b80\u9650\u5236\u3011", objectName="sectionTitle"))
        self.angleReportLimitationsTable = report_table(("\u6ce8\u610f\u4e8b\u9805",), "\u8e29\u8e0f\u89d2\u5ea6\u5224\u8b80\u9650\u5236\u8868\u683c")
        self.angleReportLimitationsTable.setMinimumHeight(150)
        reportTextContentLayout.addWidget(self.angleReportLimitationsTable)
        reportTextContentLayout.addStretch()
        self.angleReportDetailsScroll.setWidget(reportTextContent)
        reportTextLayout.addWidget(self.angleReportDetailsScroll, 1)
        self.angleReportTabs.addTab(reportTextPage, "\u6578\u503c\u8207\u5224\u8b80")
        angleReportLayout.addWidget(self.angleReportTabs, 1)
        self.angleViews.addTab(angleReportPage, "\u5b8c\u6574\u5831\u544a")
        angleLayout.addWidget(self.angleViews, 1)
        self.summaryTabs.insertTab(2, anglePage, "踩踏角度分析")
        self.angleEmgButton.clicked.connect(self.chooseAngleEmg)
        self.anglePedalButton.clicked.connect(self.chooseAnglePedal)
        self.angleRunButton.clicked.connect(self.runAngleAnalysis)
        self.angleExportButton.clicked.connect(self.exportAngleAnalysis)
        self.angleMuscleCombo.currentIndexChanged.connect(self.refreshAngleCurve)
        self.anglePhaseSpin.valueChanged.connect(self.refreshAnglePhase)
        self.angleComparisonTable.itemSelectionChanged.connect(self.showSelectedAngleMuscle)
        self.angleReportCycleMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportCycleTrends)
        self.angleReportCycleAngleCombo.currentIndexChanged.connect(self.refreshAngleReportCycleTrends)
        self.angleReportPhaseMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportPhaseTable)
        self.angleReportFatigueMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportFatigueTrends)
        self.angleReportProfileMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportProfiles)
        self.angleAutoScaleCheck.toggled.connect(lambda checked: self.angleScaleSpin.setEnabled(not checked))
        self.angleAutoOffsetCheck.toggled.connect(lambda checked: self.angleOffsetSpin.setEnabled(not checked))
        self.angleAdvancedToggle.toggled.connect(self.setAngleAdvancedVisible)
        self.lockAnalysisPlots(
            self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
            self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
            self.comparisonPlot, self.anglePhasePlot, self.angleHeatPlot,
            self.angleCurvePlot, self.angleReportRmsPlot, self.angleReportMdfPlot,
            self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot,
            *self.angleReportFatigueTrendPlots,
        )
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

    @staticmethod
    def lockAnalysisPlots(*plots) -> None:
        """Keep report charts readable; body-map zoom remains intentionally separate."""
        for plot in plots:
            item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
            item.setMouseEnabled(x=False, y=False)
            item.setMenuEnabled(False)

    def applySummaryPlotTheme(self, *_args) -> None:
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        for widget in (self.summaryPlots, self.ridePlots, self.comparisonPlot, self.angleCurvePlot,
                       self.angleHeatPlot, self.anglePhasePlot, self.angleReportRmsPlot,
                       self.angleReportMdfPlot, self.angleReportProfiles,
                       self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot,
                       *self.angleReportFatigueTrendPlots):
            widget.setBackground(tokens["surface_background"])
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
                     self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
                     self.comparisonPlot.getPlotItem(), self.angleCurvePlot.getPlotItem(),
                     self.angleHeatPlot.getPlotItem(), self.anglePhasePlot.getPlotItem(),
                     self.angleReportRmsPlot.getPlotItem(), self.angleReportMdfPlot.getPlotItem(),
                     self.angleReportCycleLeftPlot.getPlotItem(), self.angleReportCycleRightPlot.getPlotItem(),
                     *(plot.getPlotItem() for plot in self.angleReportFatigueTrendPlots)):
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
        self.calibrateButton = QPushButton("測量 20 秒基準")
        FluentThemeManager.set_property(self.calibrateButton, "fluentAppearance", "danger")
        self.calibrationLabel = QLabel("基準：尚未開始", objectName="muted")
        FluentThemeManager.set_property(self.startButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.stopButton, "fluentAppearance", "danger")
        for widget in (self.hostEdit, self.testButton, self.startButton, self.calibrateButton, self.stopButton):
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
        self.countdownLabel = QLabel(objectName="countdown")
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
        self.pathEdit = QLineEdit(self.defaultOutputDirectory())
        self.pathEdit.setPlaceholderText("選擇收集檔案的輸出資料夾")
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
        self.importBaselineButton = QPushButton("匯入基準 CSV")
        self.thresholdButton = QPushButton("判定門檻")
        FluentThemeManager.set_property(self.importBaselineButton, "fluentAppearance", "primary")
        FluentThemeManager.set_property(self.thresholdButton, "fluentAppearance", "warning")
        controls.addWidget(self.importBaselineButton)
        controls.addWidget(self.thresholdButton)
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
        self.importBaselineButton.clicked.connect(self.importBaselineCsv)
        self.thresholdButton.clicked.connect(self.editThresholds)
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
            add_point(self.rideSession, "trainer", {
                "time_s": self.rideTime(), "resistance_level": self.resistanceSpin.value(),
                "resistance_native_commanded": self.commandedNativeResistance,
                "resistance_native_reported": data.resistance, "power_watts": data.powerWatts,
                "speed_kph": data.speedKph, "cadence_rpm": data.cadenceRpm,
                "heart_rate_bpm": data.heartRate, "session_distance_meters": sessionDistanceMeters,
                "energy_kcal": data.energyKcal})
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
        filename, _ = QFileDialog.getOpenFileName(self, "匯入原始 EMG 基準 CSV", self.defaultOutputDirectory(), "CSV files (*.csv)")
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
        QMessageBox.information(self, "基準 CSV 已匯入", "已用前 20 秒資料（捨棄前 10 秒）建立基準；數值已顯示在各肌肉列。")

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

    def editThresholds(self) -> None:
        warning, accepted = QInputDialog.getDouble(self, "研究用判定門檻", "警告偏離百分比：", self.baselineManager.warning_percent, 0.1, 1000, 1)
        if not accepted:
            return
        focus, accepted = QInputDialog.getDouble(self, "研究用判定門檻", "重點察看偏離百分比：", self.baselineManager.focus_percent, warning + 0.1, 1000, 1)
        if accepted:
            self.baselineManager.warning_percent, self.baselineManager.focus_percent = warning, focus
            self.baselineManager.save()

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

    def setThresholdsEnabled(self, enabled: bool) -> None:
        self.baselineManager.thresholds_enabled = enabled
        self.baselineManager.save()
        self.updateSensorRows()

    def applyStyles(self) -> None:
        FluentThemeManager(QApplication.instance()).apply()

    def defaultOutputDirectory(self) -> str:
        return str(Path.cwd() / "recordings")

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
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        candidate = directory / f"{safeId}_EMG_{timestamp}.csv"
        suffix = 2
        while candidate.exists() or infoOutputPath(candidate).exists() or (
            self.useTrainerCheck.isChecked() and self.trainerIsConnected and trainerOutputPath(candidate).exists()) or (
            self.usePedalCheck.isChecked() and self.pedalIsConnected and pedalOutputPath(candidate).exists()):
            candidate = directory / f"{safeId}_EMG_{timestamp}_{suffix:02d}.csv"
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
        self.hostEdit.setEnabled(not streaming and not testing)
        self.testButton.setEnabled(not streaming and not testing)
        self.recordCheck.setEnabled(False)
        self.durationMinutesSpin.setEnabled(not streaming)
        self.durationSecondsSpin.setEnabled(not streaming)
        self.pathEdit.setEnabled(not streaming and self.recordCheck.isChecked())
        self.browseButton.setEnabled(not streaming and self.recordCheck.isChecked())
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
        self.latestRideSession = session
        self.collectedRideSession = session
        self.angleEmgPath = self.collectionOutput if self.collectionOutput and self.collectionOutput.is_file() else None
        self.anglePedalPath = self.pedalOutput if self.pedalOutput and self.pedalOutput.is_file() else None
        self.summarySourceLabel.setText("資料來源：本次騎乘，分析完成")
        if self.collectionOutput:
            path = self.collectionOutput.with_suffix(".analysis.json")
            try:
                session["aligned_1s"] = analyze(session)["aligned"]
                save_session(path, session)
                self.appendLog(f"騎乘分析已保存：{path.name}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "無法保存分析", str(exc))
        self.refreshRideAnalysis()
        self.refreshComparison()

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
        filename, _ = QFileDialog.getOpenFileName(self, "匯入 EMG 分析 CSV", self.defaultOutputDirectory(), "EMG CSV (*.csv)")
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
        path, _ = QFileDialog.getOpenFileName(self, "選擇原始 EMG CSV", self.defaultOutputDirectory(), "CSV (*.csv)")
        if path:
            self.angleEmgPath = Path(path)
            self.angleEmgButton.setText(f"EMG：{self.angleEmgPath.name}")
            self.angleEmgButton.setToolTip(str(self.angleEmgPath))
            self.angleAnalysisResult = None
            self.angleExportButton.setEnabled(False)
            self.updateAngleFileStatus()

    def chooseAnglePedal(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇踏板或校正角度 CSV", self.defaultOutputDirectory(), "CSV (*.csv)")
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
            self.angleMuscleCombo.addItem(
                f"{entry.location_zh} ({entry.muscle}) · {entry.channel_label}", entry.channel
            )
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
        self.refreshAngleReport()
        self.angleExportButton.setEnabled(True)

    def refreshAngleReport(self) -> None:
        """Present the reproducible, PPT-style numerical findings in Qt."""
        if self.angleAnalysisResult is None:
            self.clearAngleReportTables()
            return
        result = self.angleAnalysisResult
        summary = result["summary"].copy()
        sync, settings = result["sync"], result["settings"]
        if summary.empty:
            self.clearAngleReportTables()
            return

        ordered = summary.sort_values(["side", "location_zh", "channel_label"])
        top_rms = summary.loc[summary.rms_change_percent.idxmax()]
        lowest_mdf = summary.loc[summary.mdf_change_hz.idxmin()]
        rms_up = int((summary.rms_change_percent > 0).sum())
        mdf_down = int((summary.mdf_change_hz < 0).sum())
        phase_name = sync["angle_label"]
        calibration = "\u5df2\u6821\u6b63\u89d2\u5ea6" if sync["angle_calibrated"] else "\u76f8\u5c0d\u8e29\u8e0f\u76f8\u4f4d"
        lines = [
            "\u8e29\u8e0f\u89d2\u5ea6 EMG \u5b8c\u6574\u5206\u6790\u5831\u544a",
            "=" * 38,
            "",
            "\u3010\u6e2c\u8a66\u8207\u540c\u6b65\u3011",
            f"\u89d2\u5ea6\u57fa\u6e96\uff1a{phase_name}\uff08{calibration}\uff09",
            f"\u6709\u6548\u8e29\u8e0f\u5708\uff1a{sync['valid_cycles']} \u5708",
            f"\u524d\u6bb5\uff1a{settings['start'][0]:g}\u2013{settings['start'][1]:g} \u79d2\uff1b\u5f8c\u6bb5\uff1a{settings['end'][0]:g}\u2013{settings['end'][1]:g} \u79d2",
            f"EMG \u6642\u9418\u6bd4\u4f8b\uff1a{sync['scale']:.5f}\uff1b\u8d77\u9ede\u504f\u79fb\uff1a{sync['offset_s']:+.3f} \u79d2",
            f"\u5074\u5225\u4f86\u6e90\uff1a{sync.get('side_source', '\u76ee\u524d\u8a2d\u5b9a')}",
            "",
            "\u3010\u901a\u9053\u8207\u8cbc\u7247\u5c0d\u61c9\u3011",
        ]
        for entry in ordered.itertuples():
            lines.append(f"{entry.location_zh}\uff1a{entry.channel_label}")

        lines.extend([
            "",
            "\u3010\u91cd\u9ede\u7d50\u679c\u3011",
            f"RMS \u4e0a\u5347\u901a\u9053\uff1a{rms_up}/{len(summary)}\uff1bMDF \u4e0b\u964d\u901a\u9053\uff1a{mdf_down}/{len(summary)}\u3002",
            f"\u6700\u5927 RMS \u8b8a\u5316\uff1a{top_rms.location_zh} ({top_rms.channel_label}) "
            f"{top_rms.rms_change_percent:+.1f}% ({top_rms.rms_uv_start:.1f} \u2192 {top_rms.rms_uv_end:.1f} \u00b5V)\u3002",
            f"\u6700\u5927 MDF \u4e0b\u964d\uff1a{lowest_mdf.location_zh} ({lowest_mdf.channel_label}) "
            f"{lowest_mdf.mdf_change_hz:+.1f} Hz ({lowest_mdf.mdf_start:.1f} \u2192 {lowest_mdf.mdf_end:.1f} Hz)\u3002",
            "",
            "\u3010\u901a\u9053\u524d\u5f8c\u6bb5\u6578\u503c\u3011",
            "\u8acb\u53c3\u7167\u4e0b\u65b9\u8868\u683c\u3002",
        ])

        weak = ordered[ordered.norm_uv < 10]
        lines.extend(["", "\u3010\u5224\u8b80\u9650\u5236\u3011"])
        if not sync["angle_calibrated"]:
            lines.append("\u76ee\u524d\u70ba\u76f8\u5c0d\u8e29\u8e0f\u76f8\u4f4d\uff1b\u82e5\u8981\u89e3\u91cb TDC \u7d55\u5c0d\u89d2\u5ea6\uff0c\u9700\u52a0\u5165\u53ef\u91cd\u73fe\u7684\u5f71\u7247\u6216\u66f2\u67c4\u540c\u6b65\u6821\u6b63\u3002")
        lines.append("\u6bcf\u689d\u808c\u8089\u4ee5\u81ea\u8eab 99 \u767e\u5206\u4f4d\u6b63\u898f\u5316\uff1b\u53ef\u6bd4\u8f03\u540c\u4e00\u808c\u8089\u7684\u6642\u9593\u8b8a\u5316\uff0c\u4e0d\u53ef\u8de8\u808c\u8089\u6392\u540d\u51fa\u529b\u3002")
        lines.append("EMG \u632f\u5e45\u4e0d\u7b49\u65bc\u808c\u529b\u3001\u529b\u77e9\u6216\u8e29\u8e0f\u6548\u7387\uff1b\u5fc5\u9808\u8207\u8e0f\u677f\u529f\u7387\u3001\u52d5\u4f5c\u5f71\u7247\u4e00\u8d77\u89e3\u91cb\u3002")
        if not weak.empty:
            labels = "\u3001".join(f"{row.location_zh} ({row.channel_label})" for row in weak.itertuples())
            lines.append(f"\u8a0a\u865f\u504f\u5f31\u3001\u9700\u8b39\u614e\u89e3\u8b80\uff1a{labels}\u3002")
        self.setAngleReportTableRows(self.angleReportSyncTable, (
            ("\u89d2\u5ea6\u57fa\u6e96", f"{phase_name}\uff08{calibration}\uff09"),
            ("\u6709\u6548\u8e29\u8e0f\u5708", f"{sync['valid_cycles']} \u5708"),
            ("\u5206\u6790\u5340\u9593", f"\u524d\u6bb5 {settings['start'][0]:g}\u2013{settings['start'][1]:g} \u79d2\uff1b\u5f8c\u6bb5 {settings['end'][0]:g}\u2013{settings['end'][1]:g} \u79d2"),
            ("EMG \u6642\u9418\u540c\u6b65", f"\u6bd4\u4f8b {sync['scale']:.5f}\uff1b\u8d77\u9ede\u504f\u79fb {sync['offset_s']:+.3f} \u79d2"),
            ("\u5074\u5225\u4f86\u6e90", sync.get("side_source", "\u76ee\u524d\u8a2d\u5b9a")),
        ))
        self.setAngleReportTableRows(
            self.angleReportMappingTable,
            tuple((entry.location_zh, entry.channel_label) for entry in ordered.itertuples()),
        )
        self.setAngleReportTableRows(self.angleReportHighlightsTable, (
            ("RMS \u4e0a\u5347\u901a\u9053", f"{rms_up}/{len(summary)}"),
            ("MDF \u4e0b\u964d\u901a\u9053", f"{mdf_down}/{len(summary)}"),
            ("\u6700\u5927 RMS \u8b8a\u5316",
             f"{top_rms.location_zh} ({top_rms.channel_label}) {top_rms.rms_change_percent:+.1f}% "
             f"({top_rms.rms_uv_start:.1f} \u2192 {top_rms.rms_uv_end:.1f} \u00b5V)"),
            ("\u6700\u5927 MDF \u4e0b\u964d",
             f"{lowest_mdf.location_zh} ({lowest_mdf.channel_label}) {lowest_mdf.mdf_change_hz:+.1f} Hz "
             f"({lowest_mdf.mdf_start:.1f} \u2192 {lowest_mdf.mdf_end:.1f} Hz)"),
        ))
        limitations = []
        if not sync["angle_calibrated"]:
            limitations.append(("\u76ee\u524d\u70ba\u76f8\u5c0d\u8e29\u8e0f\u76f8\u4f4d\uff1b\u82e5\u8981\u89e3\u91cb TDC \u7d55\u5c0d\u89d2\u5ea6\uff0c\u9700\u52a0\u5165\u53ef\u91cd\u73fe\u7684\u5f71\u7247\u6216\u66f2\u67c4\u540c\u6b65\u6821\u6b63\u3002",))
        limitations.append(("\u6bcf\u689d\u808c\u8089\u4ee5\u81ea\u8eab 99 \u767e\u5206\u4f4d\u6b63\u898f\u5316\uff1b\u53ef\u6bd4\u8f03\u540c\u4e00\u808c\u8089\u7684\u6642\u9593\u8b8a\u5316\uff0c\u4e0d\u53ef\u8de8\u808c\u8089\u6392\u540d\u51fa\u529b\u3002",))
        limitations.append(("EMG \u632f\u5e45\u4e0d\u7b49\u65bc\u808c\u529b\u3001\u529b\u77e9\u6216\u8e29\u8e0f\u6548\u7387\uff1b\u5fc5\u9808\u8207\u8e29\u677f\u529f\u7387\u3001\u52d5\u4f5c\u5f71\u7247\u4e00\u8d77\u89e3\u91cb\u3002",))
        if not weak.empty:
            labels = "\u3001".join(f"{row.location_zh} ({row.channel_label})" for row in weak.itertuples())
            limitations.append((f"\u8a0a\u865f\u504f\u5f31\u3001\u9700\u8b39\u614e\u89e3\u8b80\uff1a{labels}\u3002",))
        self.setAngleReportTableRows(self.angleReportLimitationsTable, limitations)
        self.refreshAngleReportChannelTable(ordered)
        self.refreshAngleReportCharts(ordered)
        self.refreshAngleReportProfiles()

    def clearAngleReportTables(self) -> None:
        for table in (self.angleReportSyncTable, self.angleReportMappingTable,
                      self.angleReportHighlightsTable, self.angleReportChannelTable,
                      self.angleReportLimitationsTable):
            table.clearContents()
            table.setRowCount(0)

    @staticmethod
    def setAngleReportTableRows(table: QTableWidget, rows) -> None:
        table.clearContents()
        table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()
        content_height = table.horizontalHeader().height() + sum(
            table.rowHeight(row) for row in range(table.rowCount())
        ) + 6
        table.setMinimumHeight(max(110, content_height))

    def refreshAngleReportChannelTable(self, ordered) -> None:
        """Present each channel's early/late measurements in readable columns."""
        table = self.angleReportChannelTable
        table.clearContents()
        table.setRowCount(len(ordered))
        for row_index, entry in enumerate(ordered.itertuples()):
            rms_up = entry.rms_change_percent > 0
            mdf_down = entry.mdf_change_hz < 0
            if rms_up and mdf_down:
                interpretation = "RMS\u2191\u3001MDF\u2193\uff1a\u75b2\u52de\u76f8\u95dc\u8da8\u52e2"
            elif rms_up:
                interpretation = "RMS\u2191\uff1bMDF\u672a\u4e0b\u964d"
            elif mdf_down:
                interpretation = "MDF\u2193\uff1bRMS\u672a\u4e0a\u5347"
            else:
                interpretation = "\u524d\u5f8c\u8b8a\u5316\u4e0d\u4e00\u81f4"
            values = (
                f"{entry.location_zh} / {entry.channel_label}",
                f"{entry.rms_uv_start:.1f} \u2192 {entry.rms_uv_end:.1f}",
                f"{entry.rms_change_percent:+.1f}%",
                f"{entry.mdf_start:.1f} \u2192 {entry.mdf_end:.1f}",
                f"{entry.peak_angle_shift:+.0f}\u00b0",
                interpretation,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if column else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()
        content_height = table.horizontalHeader().height() + sum(
            table.rowHeight(row) for row in range(table.rowCount())
        ) + 6
        table.setMinimumHeight(max(110, content_height))

    def refreshAngleReportCharts(self, ordered) -> None:
        """Draw PPT-style grouped columns: one muscle, left/right side by side."""
        muscle_order = ("RF", "VM", "VL", "BF", "GAS", "SOL", "TA")
        groups = [muscle for muscle in muscle_order if muscle in set(ordered.muscle)]
        groups.extend(muscle for muscle in ordered.muscle.dropna().unique() if muscle not in groups)
        if not groups:
            return
        labels, left_rows, right_rows = [], [], []
        for muscle in groups:
            pair = ordered[ordered.muscle == muscle]
            exemplar = pair.iloc[0]
            muscle_name = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            # The x axis groups left/right bars by anatomy; retain the
            # conventional EMG abbreviation so GAS/SOL/etc. are explicit.
            labels.append(f"{muscle_name}\n({muscle})")
            left_rows.append(pair[pair.side == "L"].iloc[0] if not pair[pair.side == "L"].empty else None)
            right_rows.append(pair[pair.side == "R"].iloc[0] if not pair[pair.side == "R"].empty else None)
        positions = np.arange(len(groups), dtype=float)
        chart_specs = (
            (self.angleReportRmsPlot, "rms_change_percent", "RMS \u8b8a\u5316\uff08%\uff09", "%"),
            (self.angleReportMdfPlot, "mdf_change_hz", "MDF \u8b8a\u5316\uff08Hz\uff09", " Hz"),
        )
        for plot, column, axis_label, unit in chart_specs:
            plot.clear()
            left_values = np.array([float(getattr(entry, column)) if entry is not None else np.nan for entry in left_rows])
            right_values = np.array([float(getattr(entry, column)) if entry is not None else np.nan for entry in right_rows])
            plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#6b7280", width=1)))
            plot.addItem(pg.BarGraphItem(x=positions - .19, height=left_values, width=.34,
                                         brush=pg.mkBrush("#1684d7"), pen=pg.mkPen("#1684d7")))
            plot.addItem(pg.BarGraphItem(x=positions + .19, height=right_values, width=.34,
                                         brush=pg.mkBrush("#d97706"), pen=pg.mkPen("#d97706")))
            # Write the signed change directly above/below every bar.  The
            # colours retain the left/right convention used by the chart.
            for x_values, values, color in ((positions - .19, left_values, "#0969ad"),
                                            (positions + .19, right_values, "#a45100")):
                for x, value in zip(x_values, values, strict=True):
                    if not np.isfinite(value):
                        continue
                    label = pg.TextItem(f"{value:+.1f}{unit}", color=color, anchor=(.5, 1.15 if value >= 0 else -.15))
                    label.setFont(QFont("Segoe UI", 8))
                    label.setPos(float(x), float(value))
                    plot.addItem(label)
            plot.setLabel("left", axis_label)
            plot.getAxis("bottom").setTicks([list(zip(positions, labels, strict=True))])
            plot.getAxis("bottom").setHeight(42)
            all_values = np.r_[left_values, right_values]
            lower, upper = float(np.nanmin(all_values)), float(np.nanmax(all_values))
            # Extra space keeps the signed labels inside the plot area.
            padding = max(3.0, (upper - lower) * .22)
            plot.setYRange(min(0, lower - padding), max(0, upper + padding), padding=0)
            plot.setXRange(-.7, len(groups) - .3, padding=0)
            plot.showGrid(x=False, y=True, alpha=.18)

    def refreshAngleReportFatigueTrends(self) -> None:
        """Match PPT slides 26/27 with separate left/right cycle trends."""
        plots = self.angleReportFatigueTrendPlots
        for plot in plots:
            plot.clear()
            if plot.getPlotItem().legend is None:
                plot.addLegend(offset=(8, 8), colCount=2)
            else:
                plot.getPlotItem().legend.clear()
        if self.angleAnalysisResult is None:
            return
        cycles = self.angleAnalysisResult["cycles"]
        if cycles.empty:
            return
        selected_muscle = self.angleReportFatigueMuscleCombo.currentData()
        metric_plots = (("rms_uv", plots[0]), ("mdf_hz", plots[1]))
        for metric, plot in metric_plots:
            metric_zh = "RMS" if metric == "rms_uv" else "MDF"
            for side, side_zh, color in (("L", "\u5de6\u8173", "#1684d7"), ("R", "\u53f3\u8173", "#d97706")):
                subset = cycles[cycles.side == side]
                if selected_muscle:
                    subset = subset[subset.muscle == selected_muscle]
                if subset.empty:
                    continue
                row = subset.sort_values("time_s")
                values = row[metric].to_numpy(float)
                # Same smoothing window as the PPT trends: 9 pedal cycles.
                smoothed = row[metric].rolling(9, min_periods=3, center=True).mean().to_numpy(float)
                x = np.arange(1, len(values) + 1, dtype=float)
                plot.plot(x, smoothed, pen=pg.mkPen(color, width=2), name=side_zh)
            muscle_label = selected_muscle or "\u5168\u90e8\u808c\u8089"
            plot.setTitle(f"{muscle_label}\uff1a\u9010\u5708 {metric_zh} \u8da8\u52e2\uff08\u85cd\uff1a\u5de6\u8173\uff1b\u6a58\uff1a\u53f3\u8173\uff1b9 \u5708\u79fb\u52d5\u5e73\u5747\uff09")

    def refreshAngleReportProfiles(self) -> None:
        """Create PPT-like early/late angle profiles, paired by muscle and side."""
        if self.angleAnalysisResult is None:
            return
        result = self.angleAnalysisResult
        summary, profiles = result["summary"], result["profiles"]
        self.angleReportProfiles.clear()
        muscle_order = ("RF", "VM", "VL", "BF", "GAS", "SOL", "TA")
        available = [muscle for muscle in muscle_order if muscle in set(summary.muscle)]
        available.extend(muscle for muscle in summary.muscle.dropna().unique() if muscle not in available)
        selected_profile = self.angleReportProfileMuscleCombo.currentData()
        self.angleReportProfileMuscleCombo.blockSignals(True)
        self.angleReportProfileMuscleCombo.clear()
        for muscle in available:
            exemplar = summary[summary.muscle == muscle].iloc[0]
            label = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            self.angleReportProfileMuscleCombo.addItem(f"{label} ({muscle})", muscle)
        if selected_profile is not None:
            index = self.angleReportProfileMuscleCombo.findData(selected_profile)
            if index >= 0:
                self.angleReportProfileMuscleCombo.setCurrentIndex(index)
        self.angleReportProfileMuscleCombo.blockSignals(False)
        selected_profile = self.angleReportProfileMuscleCombo.currentData()
        shown_muscles = [selected_profile] if selected_profile else available[:1]
        for row, muscle in enumerate(shown_muscles):
            for col, side in enumerate(("L", "R")):
                matches = summary[(summary.muscle == muscle) & (summary.side == side)]
                if matches.empty:
                    continue
                entry = matches.iloc[0]
                plot = self.angleReportProfiles.addPlot(row=row, col=col)
                plot.setTitle(
                    f"{entry.location_zh} {entry.channel_label}  |  RMS {entry.rms_change_percent:+.1f}%"
                )
                for phase, color, label in (("start", "#1684d7", "\u524d\u6bb5"),
                                            ("end", "#d13438", "\u5f8c\u6bb5")):
                    curve = profiles[(profiles.channel == entry.channel) & (profiles.phase == phase)]
                    plot.plot(curve.angle_bin_center.to_numpy(float), curve.mean_percent.to_numpy(float),
                              pen=pg.mkPen(color, width=2), name=label if row == 0 and col == 0 else None)
                plot.setXRange(0, 360, padding=0)
                plot.setYRange(0, 105, padding=0)
                plot.setLabel("bottom", "\u8e29\u8e0f\u89d2\u5ea6", units="\u00b0")
                if col == 0:
                    plot.setLabel("left", "\u76f8\u5c0d\u6d3b\u5316", units="%")
                plot.showGrid(x=True, y=True, alpha=.16)
                self.lockAnalysisPlots(plot)
        self.angleReportProfiles.setMinimumHeight(420)
        self.applySummaryPlotTheme()
        selected = self.angleReportCycleMuscleCombo.currentData()
        selected_table = self.angleReportPhaseMuscleCombo.currentData()
        selected_fatigue = self.angleReportFatigueMuscleCombo.currentData()
        self.angleReportCycleMuscleCombo.blockSignals(True)
        self.angleReportPhaseMuscleCombo.blockSignals(True)
        self.angleReportFatigueMuscleCombo.blockSignals(True)
        self.angleReportCycleMuscleCombo.clear()
        self.angleReportPhaseMuscleCombo.clear()
        self.angleReportFatigueMuscleCombo.clear()
        for muscle in available:
            exemplar = summary[summary.muscle == muscle].iloc[0]
            label = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            self.angleReportCycleMuscleCombo.addItem(f"{label} ({muscle})", muscle)
            self.angleReportPhaseMuscleCombo.addItem(f"{label} ({muscle})", muscle)
            self.angleReportFatigueMuscleCombo.addItem(f"{label} ({muscle})", muscle)
        if selected is not None:
            index = self.angleReportCycleMuscleCombo.findData(selected)
            if index >= 0:
                self.angleReportCycleMuscleCombo.setCurrentIndex(index)
        if selected_table is not None:
            index = self.angleReportPhaseMuscleCombo.findData(selected_table)
            if index >= 0:
                self.angleReportPhaseMuscleCombo.setCurrentIndex(index)
        if selected_fatigue is not None:
            index = self.angleReportFatigueMuscleCombo.findData(selected_fatigue)
            if index >= 0:
                self.angleReportFatigueMuscleCombo.setCurrentIndex(index)
        self.angleReportCycleMuscleCombo.blockSignals(False)
        self.angleReportPhaseMuscleCombo.blockSignals(False)
        self.angleReportFatigueMuscleCombo.blockSignals(False)
        self.refreshAngleReportCycleTrends()
        self.refreshAngleReportPhaseTable()
        self.refreshAngleReportFatigueTrends()

    def refreshAngleReportCycleTrends(self) -> None:
        """Match PPT slides 6/8/10/12/14/16/18: eight phase trends per side."""
        for plot in (self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot):
            plot.clear()
            if plot.getPlotItem().legend is None:
                plot.addLegend(offset=(8, 8), colCount=4)
            else:
                plot.getPlotItem().legend.clear()
        if self.angleAnalysisResult is None:
            return
        muscle = self.angleReportCycleMuscleCombo.currentData()
        if not muscle:
            return
        result = self.angleAnalysisResult
        cycles, summary = result["cycles"], result["summary"]
        settings = result["settings"]
        bins = int(settings["bins"])
        bin_centers = (np.arange(bins) + .5) * 360 / bins
        bin_columns = [f"bin_{index}" for index in range(bins)]
        target_angles = tuple(range(0, 360, 45))
        selected_angle = self.angleReportCycleAngleCombo.currentData()
        displayed_angles = target_angles if selected_angle is None else (int(selected_angle),)
        line_colors = ("#1684d7", "#e95f54", "#7e57c2", "#00897b",
                       "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
        for side, plot, side_zh in (("L", self.angleReportCycleLeftPlot, "\u5de6\u8173"),
                                    ("R", self.angleReportCycleRightPlot, "\u53f3\u8173")):
            matches = summary[(summary.muscle == muscle) & (summary.side == side)]
            if matches.empty:
                plot.setTitle(f"{side_zh}\uff1a\u7121\u6b64\u808c\u8089\u8cc7\u6599")
                continue
            entry = matches.iloc[0]
            rows = cycles[cycles.channel == entry.channel].sort_values("cycle").reset_index(drop=True)
            angle_title = "\u9010\u5708 8 \u89d2\u5ea6\u6d3b\u5316" if selected_angle is None else f"{int(selected_angle)}\u00b0\u9010\u5708\u6d3b\u5316"
            plot.setTitle(f"{entry.location_zh} {entry.channel_label} \u2014 {angle_title}\uff085 \u5708\u79fb\u52d5\u5e73\u5747\uff09")
            x = np.arange(1, len(rows) + 1, dtype=float)
            start_count = int((rows.phase == "start").sum())
            end_indices = np.flatnonzero(rows.phase.to_numpy() == "end")
            if start_count:
                start_region = pg.LinearRegionItem((1, start_count), orientation="vertical", movable=False,
                                                   brush=pg.mkBrush(22, 132, 215, 24), pen=None)
                start_region.setZValue(-10)
                plot.addItem(start_region)
            if len(end_indices):
                end_region = pg.LinearRegionItem((int(end_indices[0]) + 1, len(rows)), orientation="vertical", movable=False,
                                                 brush=pg.mkBrush(209, 52, 56, 24), pen=None)
                end_region.setZValue(-10)
                plot.addItem(end_region)
            for angle, color in zip(target_angles, line_colors, strict=True):
                if angle not in displayed_angles:
                    continue
                distance = np.abs((bin_centers - angle + 180) % 360 - 180)
                columns = [column for column, keep in zip(bin_columns, distance <= 15 + 1e-9, strict=True) if keep]
                values = rows[columns].mean(axis=1).to_numpy(float)
                weights = np.convolve(np.ones(len(values)), np.ones(5), mode="same")
                smoothed = np.convolve(values, np.ones(5), mode="same") / weights
                plot.plot(x, smoothed, pen=pg.mkPen(color, width=2), name=f"{angle}\u00b0")
            plot.setXRange(1, max(2, len(rows)), padding=.02)
            plot.setYRange(0, 105, padding=0)
            plot.showGrid(x=True, y=True, alpha=.16)
            self.lockAnalysisPlots(plot)

    def refreshAngleReportPhaseTable(self) -> None:
        """Match PPT slides 7/9/11/13/15/17/19 with an eight-angle table."""
        table = self.angleReportPhaseTable
        table.clearContents()
        if self.angleAnalysisResult is None:
            table.setRowCount(0)
            return
        muscle = self.angleReportPhaseMuscleCombo.currentData()
        if not muscle:
            table.setRowCount(0)
            return
        result = self.angleAnalysisResult
        summary, profiles = result["summary"], result["profiles"]
        bins = int(result["settings"]["bins"])
        centers = (np.arange(bins) + .5) * 360 / bins
        target_angles = tuple(range(0, 360, 45))
        by_side = {
            side: summary[(summary.muscle == muscle) & (summary.side == side)].iloc[0]
            if not summary[(summary.muscle == muscle) & (summary.side == side)].empty else None
            for side in ("L", "R")
        }

        def phase_value(entry, phase: str, angle: int) -> float | None:
            if entry is None:
                return None
            curve = profiles[(profiles.channel == entry.channel) & (profiles.phase == phase)]
            if curve.empty:
                return None
            distance = np.abs((curve.angle_bin_center.to_numpy(float) - angle + 180) % 360 - 180)
            values = curve.mean_percent.to_numpy(float)[distance <= 15 + 1e-9]
            return float(np.mean(values)) if len(values) else None

        table.setRowCount(len(target_angles) + 2)
        for row, angle in enumerate(target_angles):
            left_start = phase_value(by_side["L"], "start", angle)
            left_end = phase_value(by_side["L"], "end", angle)
            right_start = phase_value(by_side["R"], "start", angle)
            right_end = phase_value(by_side["R"], "end", angle)
            values = (
                f"{angle}\u00b0 (\u00b115\u00b0)",
                f"{left_start:.1f}%" if left_start is not None else "\u2014",
                f"{left_end:.1f}%" if left_end is not None else "\u2014",
                f"{left_end - left_start:+.1f}%" if left_start is not None and left_end is not None else "\u2014",
                f"{right_start:.1f}%" if right_start is not None else "\u2014",
                f"{right_end:.1f}%" if right_end is not None else "\u2014",
                f"{right_end - right_start:+.1f}%" if right_start is not None and right_end is not None else "\u2014",
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))

        for row, label, start_key, end_key, unit in (
            (len(target_angles), "RMS", "rms_uv_start", "rms_uv_end", " \u00b5V"),
            (len(target_angles) + 1, "MDF", "mdf_start", "mdf_end", " Hz"),
        ):
            values = [label]
            for side in ("L", "R"):
                entry = by_side[side]
                if entry is None:
                    values.extend(("\u2014", "\u2014", "\u2014"))
                    continue
                start, end = float(getattr(entry, start_key)), float(getattr(entry, end_key))
                values.extend((f"{start:.1f}{unit}", f"{end:.1f}{unit}", f"{end - start:+.1f}{unit}"))
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))

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
        folder = QFileDialog.getExistingDirectory(self, "選擇角度分析匯出資料夾", self.defaultOutputDirectory())
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
        self.infoRecorder = CollectionInfoRecorder(infoOutputPath(output), self.collectionSubject, output, self.trainerOutput)
        try:
            self.infoRecorder.start()
        except OSError as exc:
            self.infoRecorder = None
            QMessageBox.critical(self, "資料檔建立失敗", str(exc))
            return
        self.beginRideAnalysis()
        self.bikeOnlySession = True
        self.beginTrainerRecording()
        self.beginPedalRecording()
        self.setStreaming(True)
        self.setEmgStatus("僅收集騎乘裝置資料", "streaming")
        self.startCountdown()

    def stopStream(self) -> None:
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
            frequency = recorder.rows / elapsed if elapsed > 0 else 0.0
            self.infoRecorder.append("阻力計資料", [
                f"阻力計資料列數：{recorder.rows}",
                f"阻力計資料抓取頻率（平均）：{frequency:.3f} Hz",
                f"阻力計收集時間：{elapsed:.6f} 秒",
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
            frequency = recorder.rows / elapsed if elapsed else 0.0
            self.infoRecorder.append("踏板資料", [
                f"踏板資料檔：{recorder.outputPath.name}",
                f"踏板封包數：{recorder.rows}",
                f"踏板平均頻率：{frequency:.3f} Hz",
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
        lines = [
            f"基站序號：{metadata['base_serial']}",
            f"EMG 資料抓取頻率：{metadata['sample_rate_hz']:.6f} Hz",
            f"每個封包樣本數：{metadata['samples_per_frame']}",
            f"封包間隔：{metadata['frame_interval_seconds']:.6f} 秒",
            "EMG 測試部位：",
        ]
        lines.extend(
            "  S{slot:02d}：{chinese_name}（{english_name}） | SID：{sid}".format(
                **channel
            )
            for channel in metadata["channels"]
        )
        self.infoRecorder.append("EMG 資料", lines)

        baseline_lines = [
            "feature=filtered_rms_entire_final_10_seconds; live_window=200 ms",
            f"filter=high-pass {self.filterSettings.highpass_hz:g} Hz, low-pass {self.filterSettings.lowpass_hz:g} Hz",
            f"research_thresholds={'enabled' if self.baselineManager.thresholds_enabled else 'disabled'}; warning=±{self.baselineManager.warning_percent:g}%; focus=±{self.baselineManager.focus_percent:g}%",
            "Clinical interpretation and thresholds require clinician confirmation.",
        ]
        for channel in metadata["channels"]:
            record = self.baselineManager.records.get(channel["slot"])
            if record:
                baseline_lines.append(f"S{channel['slot']:02d}: {record.rms_mv:.6f} mV; {record.method}; {record.source}; {record.timestamp}")
        self.infoRecorder.append("EMG baseline comparison", baseline_lines)

    def configuredDurationSeconds(self) -> int:
        return self.durationMinutesSpin.value() * 60 + self.durationSecondsSpin.value()

    def updateCountdownLabel(self) -> None:
        if self.countdownMode == "calibration":
            minutes, seconds = divmod(max(0, self.remainingSeconds), 60)
            suffix = "" if self.remainingSeconds > 0 else "（等待完成最後樣本）"
            self.countdownLabel.setText(f"基準倒數：{minutes:02d}:{seconds:02d}{suffix}")
            self.countdownLabel.show()
            return
        if self.remainingSeconds <= 0:
            if self.configuredDurationSeconds() <= 0:
                self.countdownLabel.hide()
                return
            self.countdownLabel.setText("計時：00:00")
            self.countdownLabel.show()
            return
        minutes, seconds = divmod(self.remainingSeconds, 60)
        self.countdownLabel.setText(f"計時：{minutes:02d}:{seconds:02d}")
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
            self.infoRecorder.append("EMG 測試摘要", [
                f"EMG 樣本數：{samples}",
                f"EMG 收集時間：{elapsed:.6f} 秒",
                f"EMG 資料時間長度：{samples / sampleRate:.6f} 秒" if sampleRate else "EMG 資料時間長度：0 秒",
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
            self.lockAnalysisPlots(plot)
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
            self.lockAnalysisPlots(plot)
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
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TrignoWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
