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
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget, QInputDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox,)
from delsys.collection import CollectionInfoRecorder, SubjectInfo, infoOutputPath
from delsys.emg.body_map import BodyMapPanel
from delsys.emg.sensor_mapping import (SensorMapping,duplicateMappingSlots,loadMappings,saveMappings,)
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
from delsys.cycling_bluetooth import CyclingDeviceScanWorker
from delsys.fluent_theme import FluentThemeManager
from delsys.session_analysis import (add_point, analyze, compare_rms, export_html,
                                     load_session, new_session, peak_envelope,
                                     save_session)
from delsys.session_compare import compare_sessions

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().parents[2] / "sensor_mapping.json"


class EMGAnalysisImportWorker(QThread):
    """Build a replayable RMS session from a raw EMG CSV off the UI thread."""
    completed = Signal(object, str)
    failed = Signal(str)

    def __init__(self, path: Path, mappings: list[SensorMapping], baselines: dict,
                 subject: dict, settings: FilterSettings) -> None:
        super().__init__()
        self.path = path
        self.mappings = mappings
        self.baselines = baselines
        self.subject = subject
        self.settings = settings

    def run(self) -> None:
        try:
            times, columns, sample_rate = load_raw_emg_csv(self.path, limit_seconds=None)
            normalized = {name.strip().casefold(): name for name in columns}
            channel_columns: list[tuple[int, str]] = []
            for channel, mapping in enumerate(self.mappings):
                candidates = (f"S{mapping.slot:02d}_{mapping.english_name.strip()}",
                              mapping.english_name.strip(), mapping.abbreviation.strip())
                header = next((normalized[item.casefold()] for item in candidates
                               if item and item.casefold() in normalized), None)
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
                        "rms_mv": rms_values.get(compact_channel), **comparison,
                    })
            self.completed.emit(session, self.path.name)
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
    """A compact, non-interactive ring for one side's estimated pedal output."""

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
        self.setMinimumSize(170, 195)
        self.setMaximumSize(190, 215)
        self.setAccessibleName(f"{side}腳估計功率與平衡")

    def setValues(self, balance: float | None, power_w: float | None) -> None:
        self.balance = balance if balance is not None and 0 <= balance <= 100 else None
        self.power_w = power_w
        if self.balance is None or self.power_w is None:
            self.setAccessibleDescription("沒有有效的左右平衡資料。")
        else:
            self.setAccessibleDescription(
                f"平均估計功率 {self.power_w:.1f} W，平衡 {self.balance:.1f}%。"
            )
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
        diameter = min(content.width(), content.height() - 70)
        ring = QRectF(
            content.center().x() - diameter / 2, content.top() + 24,
            diameter, diameter,
        )
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
        painter.drawText(content.adjusted(0, 38, 0, -content.height() + 104),
                         Qt.AlignmentFlag.AlignHCenter, self.side)
        power_text = "--" if self.power_w is None else f"{self.power_w:.1f} W"
        painter.drawText(content.adjusted(0, 66, 0, -content.height() + 132),
                         Qt.AlignmentFlag.AlignHCenter, power_text)
        balance_text = "無有效資料" if self.balance is None else f"平衡 {self.balance:.1f}%"
        painter.drawText(content.adjusted(0, 94, 0, -content.height() + 160),
                         Qt.AlignmentFlag.AlignHCenter, balance_text)


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
            QMessageBox.warning(self, "基準值無效", "基準 RMS 必須是大於 0 的數值，或留白以移除。")
            return
        super().accept()


class PedalSummaryDialog(QDialog):
    """Show the current-session P715 summary without creating a second GUI."""
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
        # The SDK test worker is intentionally short-lived.  Keep its result
        # separate from the active streaming worker so the header does not
        # incorrectly revert to "未連線" after a successful test.
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
        self.collectedRidePath: Path | None = None
        self.loadedRidePath: Path | None = None
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
        self.tabs.addTab(self.summaryTab, "本次摘要")
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
        """Fluent summary workspace for the live ride, comparison and pedal detail."""
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
        self.importFitButton = QPushButton("匯入FIT檔")
        self.importFitButton.setAccessibleName("匯入 FIT 活動檔")
        self.importFitButton.setToolTip("匯入 .fit 檔並顯示功率、踏頻與左右平衡摘要")
        FluentThemeManager.set_property(self.importFitButton, "fluentAppearance", "primary")
        self.importEmgAnalysisButton = QPushButton("匯入 EMG 資料")
        self.importEmgAnalysisButton.setToolTip("匯入原始 EMG CSV，產生 RMS 平均、Peak 與包絡線分析")
        FluentThemeManager.set_property(self.importEmgAnalysisButton, "fluentAppearance", "primary")
        self.currentSessionSummaryButton = QPushButton("顯示本次收集")
        self.currentSessionSummaryButton.setEnabled(False)
        self.currentSessionSummaryButton.setToolTip("切換回本程式最新收集的踏板資料")
        self.loadRideButton = QPushButton("載入歷史騎乘")
        self.exportReportButton = QPushButton("匯出 HTML 報表")
        self.exportReportButton.setEnabled(False)
        self.loadReferenceButton = QPushButton("選取第一次騎乘")
        FluentThemeManager.set_property(self.loadReferenceButton, "fluentAppearance", "primary")
        actions.addWidget(self.importFitButton)
        actions.addWidget(self.importEmgAnalysisButton)
        actions.addWidget(self.currentSessionSummaryButton)
        actions.addWidget(self.loadRideButton)
        actions.addWidget(self.exportReportButton)
        actions.addWidget(self.loadReferenceButton)
        heading.addLayout(actions)
        heading.addSpacing(12)
        heading.addWidget(self.rideStateLabel)
        layout.addLayout(heading)
        metricStrip = QHBoxLayout()
        self.summaryMetricValues = {}
        for key, title, unit in (("duration", "騎乘時間", "秒"),
                                 ("muscles", "有效肌肉", "通道")):
            frame = QFrame(objectName="metricCard")
            column = QVBoxLayout(frame)
            column.setContentsMargins(12, 8, 12, 8)
            column.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            value.setAccessibleName(title)
            valueRow = QHBoxLayout()
            valueRow.setContentsMargins(0, 0, 0, 0)
            valueRow.addWidget(value)
            if unit:
                valueRow.addWidget(QLabel(unit, objectName="metricUnit"))
            valueRow.addStretch()
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
        self.normalizedCheck = QCheckBox("顯示 normalized RMS（RMS / baseline）")
        self.normalizedCheck.setToolTip("關閉時顯示 RMS - baseline，單位 mV")
        analysisControls = QHBoxLayout()
        analysisControls.addWidget(QLabel("FTP"))
        analysisControls.addWidget(self.analysisFtpSpin)
        analysisControls.addWidget(QLabel("僅供騎乘訓練指標計算", objectName="muted"))
        analysisControls.addSpacing(18)
        analysisControls.addWidget(self.normalizedCheck)
        analysisControls.addSpacing(18)
        analysisControls.addWidget(QLabel("EMG 相對阻力時間偏移"))
        self.alignmentOffsetSpin = QDoubleSpinBox()
        self.alignmentOffsetSpin.setRange(-30, 30)
        self.alignmentOffsetSpin.setDecimals(2)
        self.alignmentOffsetSpin.setSingleStep(0.1)
        self.alignmentOffsetSpin.setSuffix(" 秒")
        self.alignmentOffsetSpin.setToolTip("有共同可辨識事件時，調整 EMG 相對阻力資料的時間；正值讓 EMG 向後移。")
        self.alignmentOffsetSpin.setAccessibleName("EMG 相對阻力時間偏移秒數")
        analysisControls.addWidget(self.alignmentOffsetSpin)
        analysisControls.addStretch()
        layout.addLayout(analysisControls)
        self.summaryTabs = QTabWidget()
        summaryPage = QWidget()
        summaryLayout = QVBoxLayout(summaryPage)
        summaryLayout.setContentsMargins(8, 8, 8, 8)
        self.summaryDetails = QPlainTextEdit()
        self.summaryDetails.setReadOnly(True)
        self.summaryDetails.setPlaceholderText("完成一次踏板收集，或匯入 fit 檔後，摘要會顯示在這裡。")
        self.summaryDetails.setMinimumHeight(150)
        self.summaryDetails.setMaximumHeight(190)
        summaryLayout.addWidget(self.summaryDetails)
        pedalMetrics = QHBoxLayout()
        self.pedalMetricCards = {}
        for key, title, unit in (("normalized_power_w", "NP", "W"),
                                 ("intensity_factor", "IF", ""),
                                 ("peak_5s_w", "最佳 5 秒", "W"),
                                 ("coverage_percent", "資料涵蓋", "%")):
            card = QFrame(objectName="metricCard")
            column = QVBoxLayout(card)
            column.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            value.setAccessibleName(title)
            column.addWidget(value)
            if unit:
                column.addWidget(QLabel(unit, objectName="metricUnit"))
            pedalMetrics.addWidget(card, 1)
            self.pedalMetricCards[key] = value
        summaryLayout.addLayout(pedalMetrics)
        summaryLayout.addWidget(QLabel("功率區間 · 依 FTP 與已觀測秒數計算", objectName="sectionTitle"))
        self.pedalZoneTable = QTableWidget(7, 4)
        self.pedalZoneTable.setHorizontalHeaderLabels(("區間", "功率範圍", "時間", "佔有效資料"))
        self.pedalZoneTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.pedalZoneTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pedalZoneTable.setAccessibleName("踏板功率區間時間分布")
        self.pedalZoneTable.setMinimumHeight(210)
        summaryLayout.addWidget(self.pedalZoneTable)
        self.summaryTabs.addTab(summaryPage, "數值與功率區間")
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
        chartLayout.addWidget(self.summaryPlots, 1)
        chartLayout.addWidget(QLabel("左右腳估計功率與平衡", objectName="sectionTitle"))
        self.powerBalanceNote = QLabel(
            "依總功率與 P715 回報的左右平衡估算，非左右獨立扭力或踩踏相位量測。",
            objectName="muted",
        )
        self.powerBalanceNote.setWordWrap(True)
        chartLayout.addWidget(self.powerBalanceNote)
        rings = QHBoxLayout()
        rings.setSpacing(20)
        self.leftPowerBalanceRing = PowerBalanceRing("左腳")
        self.rightPowerBalanceRing = PowerBalanceRing("右腳")
        rings.addStretch()
        rings.addWidget(self.leftPowerBalanceRing, 1)
        rings.addWidget(self.rightPowerBalanceRing, 1)
        rings.addStretch()
        chartLayout.addLayout(rings)
        self.summaryTabs.addTab(chartPage, "圖表")
        ridePage = QWidget()
        rideLayout = QVBoxLayout(ridePage)
        self.rideEmptyLabel = QLabel("開始收集資料、匯入 EMG CSV，或載入歷史騎乘分析檔，即可查看肌肉 RMS 分析。", objectName="muted")
        self.rideEmptyLabel.setWordWrap(True)
        rideLayout.addWidget(self.rideEmptyLabel)
        self.rideMuscleTable = QTableWidget(0, 6)
        self.rideMuscleTable.setHorizontalHeaderLabels(("肌肉／側別", "平均 RMS", "最大 RMS", "基準比較", "前段 → 後段", "資料狀態"))
        self.rideMuscleTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rideMuscleTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rideMuscleTable.setAccessibleName("本次騎乘各肌肉 RMS 摘要")
        self.rideMuscleTable.setMaximumHeight(210)
        self.ridePlots = pg.GraphicsLayoutWidget()
        self.ridePlots.setBackground("#ffffff")
        self.ridePlots.setMinimumHeight(360)
        self.rmsAverageBarPlot = self.ridePlots.addPlot(
            row=0, col=0, title="各肌肉 RMS 平均值")
        self.rmsAverageBarPlot.setLabel("left", "RMS", units="mV")
        self.rmsPeakBarPlot = self.ridePlots.addPlot(
            row=0, col=1, title="各肌肉 RMS Peak 值")
        self.rmsPeakBarPlot.setLabel("left", "RMS", units="mV")
        self.emgEnvelopePlot = self.ridePlots.addPlot(
            row=1, col=0, colspan=2, title="EMG RMS Peak 包絡線（每 1 秒最高值）")
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
        self.summaryTabs.insertTab(1, comparePage, "兩次騎乘比較")
        self.summaryTabs.setCurrentIndex(0)
        layout.addWidget(self.summaryTabs, 1)
        self.importFitButton.clicked.connect(self.importFitSummary)
        self.importEmgAnalysisButton.clicked.connect(self.importEmgAnalysis)
        self.currentSessionSummaryButton.clicked.connect(self.showCurrentSessionSummary)
        self.analysisFtpSpin.valueChanged.connect(self.refreshSummaryAnalysis)
        self.normalizedCheck.toggled.connect(self.refreshRideAnalysis)
        self.envelopeMuscleCombo.currentIndexChanged.connect(self.refreshRideAnalysis)
        self.alignmentOffsetSpin.valueChanged.connect(self.setAlignmentOffset)
        self.loadRideButton.clicked.connect(self.loadRideHistory)
        self.exportReportButton.clicked.connect(self.exportRideReport)
        self.loadReferenceButton.clicked.connect(self.loadReferenceRide)
        self.comparisonTable.itemSelectionChanged.connect(self.updateComparisonPlot)
        return tab

    def applySummaryPlotTheme(self, *_args) -> None:
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        for widget in (self.summaryPlots, self.ridePlots, self.comparisonPlot):
            widget.setBackground(tokens["surface_background"])
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
                     self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
                     self.comparisonPlot.getPlotItem()):
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
            self.leftPowerBalanceRing.setThemeColors(
                tokens["success"], tokens["border_primary"], tokens["text_primary"],
            )
            self.rightPowerBalanceRing.setThemeColors(
                tokens["brand_background"], tokens["border_primary"], tokens["text_primary"],
            )

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
            self.bikeMetricValues["distance"].setText(
                f"{sessionDistanceMeters / 1000:.2f}"
            )
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
                self.trainerRecorder.append(
                    data,
                    self.resistanceSpin.value(),
                    self.commandedNativeResistance,
                    sessionDistanceMeters,
                )
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
        self.collectionDistanceMeters = max(
            0,
            totalDistanceMeters - self.collectionDistanceBaselineMeters,
        )
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
                self.baselineManager.set_manual(
                    slot, value, self.mappingIdentity(channel), serial,
                    self.currentSampleRate, self.filterSettings, "Edited in muscle mapping",
                )
            self.baselineManager.save()
            self.updateSensorRows()
            self.syncMuscleMap()
            self.rebuildPlots()

    def updateSensorRows(self) -> None:
        for index, mapping in enumerate(self.mappings):
            side = mapping.abbreviation.rsplit("-", 1)[-1] if "-" in mapping.abbreviation else ""
            title = f"{side} {mapping.english_name.upper()}" if side in {"R", "L"} else mapping.english_name.upper()
            detected = self.sensorActive[index]
            self.sensorChecks[index].setLabels(
                f"S{mapping.slot:02d} · {mapping.chinese_name}", mapping.english_name
            )
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
            comparison = f"normalized RMS：{ratio}" if self.normalizedCheck.isChecked() else f"差異：{delta}｜變化：{percent}"
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
        filename, _ = QFileDialog.getOpenFileName(
            self, "匯入原始 EMG 基準 CSV", self.defaultOutputDirectory(), "CSV files (*.csv)"
        )
        if not filename:
            return
        path = Path(filename)
        try:
            _, columns, sample_rate = load_raw_emg_csv(path)
            normalized = {name.strip().casefold(): name for name in columns}
            channel_columns: list[tuple[int, str]] = []
            for index, mapping in enumerate(self.mappings):
                english = mapping.english_name.strip()
                candidates = (f"S{mapping.slot:02d}_{english}", english, mapping.abbreviation.strip())
                header = next((normalized[candidate.casefold()] for candidate in candidates if candidate and candidate.casefold() in normalized), None)
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
        return
        self.setStyleSheet("""
            QWidget#root { background: #eef3f8; color: #1e293b; }
            QLabel#title { font-size: 24px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
            QLabel#muted { color: #64748b; }
            QLabel#sensorLegendTitle { color: #1e293b; font-size: 12px; font-weight: 400; }
            QLabel#sensorLegendEnglish { color: #6b6b6b; font-size: 11px; font-weight: 400; }
            QLabel#bodyCaption { color: #64748b; font-size: 10px; }
            QLabel#status { padding: 5px 10px; border-radius: 10px; }
            QLabel#connectionIndicator { padding: 5px 8px; font-weight: 700; }
            QLabel#countdown { min-width: 90px; font-size: 13px; color: #64748b; }
            QFrame#metricCard { background: white; border: 1px solid #dbe3ef; border-radius: 7px; }
            QLabel#metricTitle { color: #64748b; font-size: 11px; font-weight: 600; }
            QLabel#metricValue { color: #0f172a; font-size: 26px; font-weight: 700; }
            QLabel#metricUnit { color: #64748b; font-size: 13px; font-weight: 600; }
            QFrame#eventLogPanel { background: #111827; border: 1px solid #374151; border-radius: 7px; }
            QLabel#eventLogTitle { color: #f3f4f6; font-size: 13px; font-weight: 700; padding: 0 3px; }
            QPlainTextEdit#eventLog { background: transparent; color: #d1d5db; border: 0; padding: 3px; font-family: Consolas, monospace; }
            QFrame#panel { background: #f8fafc; border: 1px solid #dbe3ef; border-radius: 7px; }
            QFrame#sensorRow { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 7px; }
            QLineEdit, QSpinBox, QComboBox { padding: 6px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QComboBox QAbstractItemView { background: white; color: #1e293b; selection-background-color: #dbeafe; selection-color: #0f172a; outline: 0; }
            QComboBox QAbstractItemView::item { min-height: 24px; color: #1e293b; background: white; }
            QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected { color: #0f172a; background: #dbeafe; }
            QPushButton { padding: 7px 10px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QPushButton#bodyViewButton { padding: 5px 12px; border-radius: 0; }
            QPushButton#bodyViewButton:checked { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; }
            QPushButton#primary { background: #2563eb; border: 0; color: white; font-weight: 600; }
            QPushButton#record { background: #dc2626; border: 0; color: white; font-weight: 600; }
            QPushButton:disabled { color: #94a3b8; background: #e2e8f0; }
            QCheckBox { font-weight: 700; }""")

    def defaultOutputDirectory(self) -> str:
        return str(Path.cwd() / "recordings")

    def subjectInfo(self) -> SubjectInfo:
        return SubjectInfo(
            patientId=self.patientIdEdit.text().strip(),
            name=self.patientNameEdit.text().strip(),
            gender=self.genderCombo.currentText(),
        )

    def collectionSetupIsValid(self) -> bool:
        subject = self.subjectInfo()
        missing = []
        if not subject.patientId:
            missing.append("病人 ID")
        if not subject.name:
            missing.append("姓名")
        if missing:
            QMessageBox.warning(
                self,
                "缺少病人資訊",
                "開始收集前請填寫：" + "、".join(missing),
            )
            self.tabs.setCurrentWidget(self.settingsTab)
            (self.patientIdEdit if not subject.patientId else self.patientNameEdit).setFocus()
            return False

        duplicates = duplicateMappingSlots(self.mappings)
        if duplicates:
            conflicts = "\n".join(
                f"{muscle.title()} ({side.upper() or '?' }): "
                + ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items()
            )
            QMessageBox.warning(
                self,
                "重複肌肉對應",
                "請先修正感測器對應，再開始收集：\n" + conflicts,
            )
            self.tabs.setCurrentWidget(self.settingsTab)
            return False
        return True

    def outputPathForCollection(self, directory: Path) -> Path:
        """Generate a collision-free, patient-ID based EMG file name."""
        patientId = self.subjectInfo().patientId
        invalid = '\\/:*?"<>|'
        safeId = "".join(character for character in patientId if character not in invalid).strip()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        candidate = directory / f"{safeId}_EMG_{timestamp}.csv"
        suffix = 2
        while candidate.exists() or infoOutputPath(candidate).exists() or (
            self.useTrainerCheck.isChecked() and self.trainerIsConnected and trainerOutputPath(candidate).exists()
        ) or (
            self.usePedalCheck.isChecked() and self.pedalIsConnected and pedalOutputPath(candidate).exists()
        ):
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
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.rideSession = new_session(asdict(self.collectionSubject), baselines)
        self.rideSession["filter"] = asdict(self.filterSettings)
        self.rideSession["channels"] = {}
        self.loadedRidePath = None
        self.collectedRidePath = None
        self.alignmentOffsetSpin.blockSignals(True)
        self.alignmentOffsetSpin.setValue(0.0)
        self.alignmentOffsetSpin.blockSignals(False)
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
        self.summarySourceLabel.setText("資料來源：本次騎乘，分析完成")
        if self.collectionOutput:
            path = self.collectionOutput.with_suffix(".analysis.json")
            try:
                session["aligned_1s"] = analyze(session)["aligned"]
                save_session(path, session)
                self.loadedRidePath = path
                self.collectedRidePath = path
                self.appendLog(f"騎乘分析已保存：{path.name}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "無法保存分析", str(exc))
        self.refreshRideAnalysis()
        self.refreshComparison()

    def refreshLiveRideSummary(self) -> None:
        if self.rideSession is not None and self.tabs.currentWidget() is self.summaryTab:
            if self.summaryTabs.currentIndex() == 0:
                self.refreshRideAnalysis()
            elif self.summaryTabs.currentIndex() == 1 and self.referenceRideSession is not None:
                self.refreshComparison()
            elif self.summaryTabs.currentIndex() in (2, 3) and self.pedalRecorder is not None and self.pedalSummarySource == "current":
                if len(self.summaryRecords) != len(self.pedalRecorder.records):
                    self.summaryRecords = list(self.pedalRecorder.records)
                    self.refreshSummaryAnalysis()

    def refreshRideAnalysis(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        normalized = self.normalizedCheck.isChecked()
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
        self.emgEnvelopePlot.setTitle(
            f"EMG RMS Peak 包絡線（每 1 秒最高值）— {self.envelopeMuscleCombo.currentText()}"
            if selected_slot is not None else "EMG RMS Peak 包絡線（每 1 秒最高值）"
        )
        if selected_slot is not None:
            slot = str(selected_slot)
            rows = [row for row in session["emg"] if str(row.get("slot")) == slot]
            offset = result["emg_trainer_offset_s"]
            envelope = peak_envelope(rows)
            if envelope:
                stride = max(1, len(envelope) // 2000)
                self.emgEnvelopePlot.plot(
                    [point[0] + offset for point in envelope[::stride]],
                    [point[1] for point in envelope[::stride]],
                    pen=pg.mkPen(COLORS[(int(slot) - 1) % len(COLORS)], width=2),
                    name=f"S{int(slot):02d}",
                )

    def importEmgAnalysis(self) -> None:
        if self.emgAnalysisImportWorker is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "匯入 EMG 分析 CSV", self.defaultOutputDirectory(), "EMG CSV (*.csv)"
        )
        if not filename:
            return
        path = Path(filename)
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.importEmgAnalysisButton.setEnabled(False)
        worker = EMGAnalysisImportWorker(
            path, list(self.mappings), baselines, asdict(self.collectionSubject), self.filterSettings,
        )
        self.emgAnalysisImportWorker = worker
        worker.completed.connect(self.emgAnalysisImported)
        worker.failed.connect(self.emgAnalysisImportFailed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.emgAnalysisImportFinished)
        worker.start()

    def emgAnalysisImported(self, session: dict, filename: str) -> None:
        self.latestRideSession = session
        self.collectedRideSession = None
        self.loadedRidePath = None
        self.collectedRidePath = None
        self.summarySourceLabel.setText(f"資料來源：匯入 EMG CSV — {filename}")
        self.refreshRideAnalysis()
        self.summaryTabs.setCurrentIndex(0)

    def emgAnalysisImportFailed(self, message: str) -> None:
        QMessageBox.warning(self, "匯入 EMG 資料失敗", message)

    def emgAnalysisImportFinished(self) -> None:
        self.importEmgAnalysisButton.setEnabled(True)
        self.emgAnalysisImportWorker = None

    def loadRideHistory(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "載入歷史騎乘", self.pathEdit.text(), "分析檔 (*.analysis.json)")
        if not path:
            return
        try:
            self.latestRideSession = load_session(Path(path))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "無法載入騎乘分析", str(exc))
            return
        self.summarySourceLabel.setText(f"歷史騎乘：{Path(path).name}")
        self.loadedRidePath = Path(path)
        self.alignmentOffsetSpin.blockSignals(True)
        self.alignmentOffsetSpin.setValue(float(self.latestRideSession.get("emg_trainer_offset_s", 0)))
        self.alignmentOffsetSpin.blockSignals(False)
        self.refreshRideAnalysis()
        self.refreshComparison()
        self.summaryTabs.setCurrentIndex(0)

    def setAlignmentOffset(self, value: float) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        session["emg_trainer_offset_s"] = value
        self.refreshRideAnalysis()
        if self.rideSession is None and self.loadedRidePath is not None:
            try:
                session["aligned_1s"] = analyze(session)["aligned"]
                save_session(self.loadedRidePath, session)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "無法儲存對齊設定", str(exc))

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
        self.summaryTabs.setCurrentIndex(1)

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

    def exportRideReport(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        default = str((self.collectionOutput or Path(self.pathEdit.text()) / "cycling").with_suffix(".html"))
        path, _ = QFileDialog.getSaveFileName(self, "匯出 HTML 報表", default, "HTML (*.html)")
        if not path:
            return
        try:
            export_html(Path(path), session, self.normalizedCheck.isChecked(), self.referenceRideSession,
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
        self.infoRecorder = CollectionInfoRecorder(
            infoOutput, self.collectionSubject, output, self.trainerOutput
        )
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
        self.infoRecorder = CollectionInfoRecorder(
            infoOutputPath(output), self.collectionSubject, output, self.trainerOutput
        )
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
        if text == "\u4e32\u6d41\u4e2d":
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
            self.summaryDetails.clear()
            for label in self.pedalMetricCards.values():
                label.setText("--")
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
        self.summaryDetails.setPlainText("\n".join([
            *pedal_summary_lines(summary), "", *pedal_analysis_lines(self.summaryRecords, ftp),
        ]))
        for key, label in self.pedalMetricCards.items():
            value = analysis[key]
            label.setText("--" if value is None else f"{value:.0f}" if key in ("peak_5s_w", "coverage_percent") else f"{value:.2f}" if key == "intensity_factor" else f"{value:.0f}")
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
                f"\u529F\u7387 Power \uFF5C \u5E73\u5747 {average_power:.0f} W \uFF5C \u6700\u5927 {maximum_power:.0f} W"
            )
        else:
            self.powerAnalysisPlot.setTitle("\u529F\u7387 Power \uFF5C \u7121\u8DB3\u5920\u7684\u529F\u7387\u8CC7\u6599")
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
                f"\u8E0F\u983B Cadence \uFF5C \u5E73\u5747 {average_cadence:.0f} rpm \uFF5C \u6700\u5927 {maximum_cadence:.0f} rpm"
            )
        else:
            self.cadenceAnalysisPlot.setTitle("\u8E0F\u983B Cadence \uFF5C \u7121\u8DB3\u5920\u7684\u8E0F\u983B\u8CC7\u6599")
        valid = np.isfinite(left)
        if valid.any():
            self.balanceAnalysisPlot.plot(x[valid], left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#43a047", name="左")
            self.balanceAnalysisPlot.plot(x[valid], 100 - left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#1684d7", name="右")

    def showCurrentSessionSummary(self) -> None:
        if self.rideSession is not None and self.pedalRecorder is not None:
            self.setSummaryRecords(self.pedalRecorder.records, "資料來源：本次騎乘踏板即時資料")
            self.summaryTabs.setCurrentIndex(2)
            return
        if self.collectedRideSession is not None:
            if self.latestPedalRecords:
                self.summaryRecords = list(self.latestPedalRecords)
                self.pedalSummarySource = "current"
                self.refreshSummaryAnalysis()
            self.latestRideSession = self.collectedRideSession
            self.loadedRidePath = self.collectedRidePath
            self.alignmentOffsetSpin.blockSignals(True)
            self.alignmentOffsetSpin.setValue(float(self.latestRideSession.get("emg_trainer_offset_s", 0)))
            self.alignmentOffsetSpin.blockSignals(False)
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
        self.summaryTabs.setCurrentIndex(2)
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
            suffix = "" if self.remainingSeconds > 0 else "\uFF08\u7B49\u5F85\u5B8C\u6210\u6700\u5F8C\u6A23\u672C\uFF09"
            self.countdownLabel.setText(f"\u57FA\u6E96\u5012\u6578\uFF1A{minutes:02d}:{seconds:02d}{suffix}")
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
        self.emgRateLabel.setText(
            f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samplesPerFrame} samples/frame"
        )

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
        self.emgRateLabel.setText(
            f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samples:,} samples"
        )
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
            self.setEmgStatus(
                f"量測完成：{samples:,} samples · {sampleRate:.3f} Hz · 已輸出 {output}",
                "connected",
            )
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
                stats.setText(f"\u5E73\u5747\u7D55\u5C0D\u632F\u5E45\uFF1A{mean_amplitude:.3f} mV\n\u6700\u5927\u7D55\u5C0D\u632F\u5E45\uFF1A{max_amplitude:.3f} mV")
                stats.setPos(-0.15, limit * 0.94)
            else:
                stats.setText("\u7B49\u5F85 EMG \u6A23\u672C")
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
