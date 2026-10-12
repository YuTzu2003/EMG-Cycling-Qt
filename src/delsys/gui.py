from __future__ import annotations
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget,)
from delsys.collection import CollectionInfoRecorder, SubjectInfo, infoOutputPath
from delsys.emg.body_map import BodyMapPanel
from delsys.emg.sensor_mapping import (SensorMapping,duplicateMappingSlots,loadMappings,saveMappings,)
from delsys.emg.trigno import CHANNEL_COUNT, SAMPLE_RATE
from delsys.emg.workers import ConnectionWorker, StreamWorker
from delsys.resistance.recording import TrainerCsvRecorder, trainerOutputPath
from delsys.resistance.trainer import BikeTelemetry, NumericRange, TrainerWorker
from delsys.pedal.recording import PedalCsvRecorder, pedalOutputPath
from delsys.pedal.worker import PedalWorker
from delsys.pedal.fit_export import exportPedalFit
from delsys.cycling_bluetooth import CyclingDeviceScanWorker
from delsys.fluent_theme import FluentThemeManager

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().parents[2] / "sensor_mapping.json"


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


class MappingEditorDialog(QDialog):
    def __init__( self, mappings: list[SensorMapping], active_slots: set[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯感測器／肌肉對應")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("修改後按「儲存並套用」。SDK Slot 是固定編號，無法變更。"))
        headings = QGridLayout()
        for column, title in enumerate(("Slot", "狀態", "中文肌肉名稱", "English EMG name", "縮寫")):
            headings.addWidget(QLabel(title, objectName="editorHeader"), 0, column)
        layout.addLayout(headings)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        self.edits: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        for row, mapping in enumerate(mappings):
            grid.addWidget(QLabel(f"S{mapping.slot:02d}"), row, 0)
            state = QLabel("● 啟用" if mapping.slot in active_slots else "未啟用")
            state.setStyleSheet("color: #16a34a; font-weight: 700;" if mapping.slot in active_slots else "color: #94a3b8;")
            grid.addWidget(state, row, 1)
            chinese, english, abbreviation = QLineEdit(mapping.chinese_name), QLineEdit(mapping.english_name), QLineEdit(mapping.abbreviation)
            grid.addWidget(chinese, row, 2)
            grid.addWidget(english, row, 3)
            grid.addWidget(abbreviation, row, 4)
            self.edits.append((chinese, english, abbreviation))
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

    def accept(self) -> None:
        duplicates = duplicateMappingSlots(self.mappings())
        if duplicates:
            conflicts = "\n".join(f"{muscle.title()} ({side.upper() or '?' }): "+ ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items())
            QMessageBox.warning(self,"重複肌肉對應","同一側的同一肌肉只能對應一個感測器：\n" + conflicts,)
            return
        super().accept()

class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EMG Cycling Monitor")
        self.resize(1500, 920)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
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
        self.curves: dict[int, tuple[pg.PlotItem, pg.PlotDataItem]] = {}
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
        self.bikeOnlySession = False
        self.commandedNativeResistance: float | None = None
        self.currentSampleRate = SAMPLE_RATE
        self.lastTrainerDistanceMeters: int | None = None
        self.collectionDistanceBaselineMeters: int | None = None
        self.collectionDistanceMeters: int | None = None
        self.distanceSessionActive = False
        self.buildUi()
        self.connectTrainerSignals()
        self.trainerWorker.start()
        self.connectPedalSignals()
        self.pedalWorker.start()
        self.connectionStateTimer = QTimer(self)
        self.connectionStateTimer.setInterval(500)
        self.connectionStateTimer.timeout.connect(self.refreshConnectionStates)
        self.connectionStateTimer.start()
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
        self.settingsTab = self.createSettingsTab()
        self.startButton.setText("開始收集資料")
        self.tabs.addTab(self.settingsTab, "設定")
        self.tabs.addTab(self.collectionTab, "收集資料")
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
        FluentThemeManager.set_property(self.startButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.stopButton, "fluentAppearance", "danger")
        for widget in (self.hostEdit, self.testButton, self.startButton, self.stopButton):
            controls.addWidget(widget)
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
        self.bodyMap.setFixedWidth(590)
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
        self.bodyMap.setMappings(self.mappings, selected, active)

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
        # An EMG SDK connection exists only while StreamWorker is running.
        self.setEmgConnectionState(bool(self.streamWorker and self.streamWorker.isRunning()))

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

    def setEmgConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.emgConnectionLabel, "EMG", connected)

    @staticmethod
    def setConnectionIndicator(label: QLabel, name: str, connected: bool) -> None:
        state = "連線" if connected else "未連線"
        label.setText(f"● {name}：{state}")
        FluentThemeManager.set_property(label, "fluentSeverity", "success" if connected else "danger")

    def editMappings(self) -> None:
        active_slots = {index + 1 for index, active in enumerate(self.sensorActive) if active}
        dialog = MappingEditorDialog(self.mappings, active_slots, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.mappings = dialog.mappings()
            saveMappings(MAPPING_PATH, self.mappings)
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
            self.sensorRows[index].setStyleSheet(
                "QFrame#sensorRow { background: #f0fdf4; border: 1px solid #16a34a; }"
                if detected
                else ""
            )
        if hasattr(self, "activeSlotsLabel"):
            active_text = ", ".join(f"S{index + 1:02d}" for index, active in enumerate(self.sensorActive) if active)
            self.activeSlotsLabel.setText(f"目前啟用感測器：{active_text or '無'}")
        self.syncMuscleMap()

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
        self.updateSensorRows()
        for index, check in enumerate(self.sensorChecks):
            check.setChecked(self.sensorActive[index])

    def startStream(self) -> None:
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
        self.bikeOnlySession = False
        self.setStreaming(False)
        self.setEmgStatus("騎乘裝置資料收集完成", "connected")

    def streamStatusChanged(self, text: str) -> None:
        self.setEmgStatus(text, "streaming" if text == "串流中" else "working")
        if text == "串流中":
            self.beginTrainerRecording()
            self.beginPedalRecording()
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
            ])

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

    def configuredDurationSeconds(self) -> int:
        return self.durationMinutesSpin.value() * 60 + self.durationSecondsSpin.value()

    def updateCountdownLabel(self) -> None:
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
        self.remainingSeconds = self.configuredDurationSeconds()
        self.updateCountdownLabel()
        if self.remainingSeconds > 0:
            self.countdownTimer.start()

    def updateCountdown(self) -> None:
        self.remainingSeconds -= 1
        self.updateCountdownLabel()
        if self.remainingSeconds <= 0:
            self.countdownTimer.stop()
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
        self.sampleLabel.setText(f"{len(self.histories[activeChannels[0]]):,} samples in view")

    def emgRateReady(self, sampleRate: float, samplesPerFrame: int, frameInterval: float) -> None:
        self.currentSampleRate = sampleRate
        self.emgRateLabel.setText(
            f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samplesPerFrame} samples/frame"
        )

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
        QMessageBox.critical(self, title, message)

    def streamFinished(self) -> None:
        worker, self.streamWorker = self.streamWorker, None
        if worker:
            worker.deleteLater()
        self.countdownTimer.stop()
        self.closeTrainerRecording()
        self.closePedalRecording()
        self.setStreaming(False)
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
            curve = plot.plot(pen=pg.mkPen(COLORS[channel % len(COLORS)], width=1))
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self.curves[channel] = (plot, curve)
        self.refreshPlots()

    def refreshPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        duration, count = 10, int(10 * self.currentSampleRate)
        for channel, (plot, curve) in self.curves.items():
            values = np.asarray(list(self.histories[channel])[-count:], dtype=np.float32) * 1000
            xAxis = (np.arange(len(values)) - len(values) + 1) / self.currentSampleRate
            limit = 1.0
            if len(values):
                limit = max(0.05, min(20, float(np.percentile(np.abs(values), 99.5)) * 1.25))
            plot.setXRange(-duration, 0, padding=0)
            plot.setYRange(-limit, limit, padding=0)
            curve.setData(xAxis, values)

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
