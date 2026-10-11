from __future__ import annotations
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget,)

from delsys.sensor_mapping import SensorMapping, loadMappings, saveMappings
from delsys.recording import CollectionInfoRecorder, SubjectInfo, TrainerCsvRecorder, infoOutputPath, trainerOutputPath
from delsys.trainer import BikeTelemetry, NumericRange, TrainerWorker
from delsys.trigno import CHANNEL_COUNT, SAMPLE_RATE
from delsys.workers import ConnectionWorker, StreamWorker

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().parents[2] / "sensor_mapping.json"

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


class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EMG Cycling Monitor")
        self.resize(1320, 860)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
        self.trainerWorker = TrainerWorker()
        self.trainerIsConnected = False
        self.histories = [deque(maxlen=int(SAMPLE_RATE * 20)) for _ in range(CHANNEL_COUNT)]
        self.mappings = loadMappings(MAPPING_PATH)
        self.sensorChecks: list[QCheckBox] = []
        self.sensorDetails: list[QLabel] = []
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
        self.commandedNativeResistance: float | None = None
        self.currentSampleRate = SAMPLE_RATE
        self.buildUi()
        self.connectTrainerSignals()
        self.trainerWorker.start()
        self.setStreaming(False)
        self.setEmgStatus("尚未連線", "idle")
        self.setTrainerConnectionState(False)
        self.setEmgConnectionState(False)

    def buildUi(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 16, 20, 18)
        header = QHBoxLayout()
        header.addWidget(QLabel("EMG Cycling Monitor", objectName="title"))
        header.addStretch()
        self.trainerConnectionLabel = QLabel(objectName="connectionIndicator")
        self.emgConnectionLabel = QLabel(objectName="connectionIndicator")
        header.addWidget(self.trainerConnectionLabel)
        header.addWidget(self.emgConnectionLabel)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        self.trainerTab = self.createTrainerTab()
        self.resistanceTab = self.createResistanceTab()
        self.collectionTab = self.createCollectionTab()
        self.settingsTab = self.createSettingsTab()
        self.startButton.setText("開始收集資料")
        self.tabs.addTab(self.settingsTab, "設定")
        self.tabs.addTab(self.collectionTab, "收集資料")
        outer.addWidget(self.tabs, 1)

    def createTrainerTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        connection = QFrame(objectName="panel")
        grid = QGridLayout(connection)
        grid.addWidget(QLabel("藍牙裝置"), 0, 0)
        self.trainerDevices = QComboBox()
        self.trainerDevices.setEditable(True)
        self.trainerDevices.setPlaceholderText("掃描後選取，或直接輸入 Bluetooth MAC 位址")
        grid.addWidget(self.trainerDevices, 0, 1)
        self.scanTrainerButton = QPushButton("掃描")
        self.connectTrainerButton = QPushButton("連線", objectName="primary")
        grid.addWidget(self.scanTrainerButton, 0, 2)
        grid.addWidget(self.connectTrainerButton, 0, 3)
        self.trainerStatus = QLabel("藍牙服務準備中…", objectName="status")
        grid.addWidget(self.trainerStatus, 1, 0, 1, 4)
        self.trainerTelemetryLabel = QLabel("即時騎乘資料：尚未連線", objectName="muted")
        grid.addWidget(self.trainerTelemetryLabel, 2, 0, 1, 4)
        layout.addWidget(connection)

        self.scanTrainerButton.clicked.connect(self.scanTrainer)
        self.connectTrainerButton.clicked.connect(self.connectTrainer)
        return tab

    def createResistanceTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        resistance = QFrame(objectName="panel")
        resistanceLayout = QVBoxLayout(resistance)
        resistanceLayout.addWidget(QLabel("固定阻力", objectName="sectionTitle"))
        self.resistanceRangeLabel = QLabel("等級 0–30；連線後依設備原生阻力範圍換算", objectName="muted")
        resistanceLayout.addWidget(self.resistanceRangeLabel)
        resistanceRow = QHBoxLayout()
        self.resistanceSpin = QSpinBox()
        self.resistanceSpin.setRange(0, 30)
        self.resistanceSpin.setValue(0)
        self.resistanceSpin.setSingleStep(1)
        self.resistanceSpin.setSuffix(" / 30")
        self.resistanceSpin.setReadOnly(True)
        self.decreaseResistanceButton = QPushButton("− 1 級")
        self.increaseResistanceButton = QPushButton("+ 1 級")
        resistanceRow.addWidget(self.decreaseResistanceButton)
        resistanceRow.addWidget(self.resistanceSpin)
        resistanceRow.addWidget(self.increaseResistanceButton)
        resistanceLayout.addLayout(resistanceRow)
        self.nativeResistanceLabel = QLabel("原生阻力：0（固定模式）", objectName="muted")
        resistanceLayout.addWidget(self.nativeResistanceLabel)
        layout.addWidget(resistance)
        self.resistanceStepLevels = 1
        self.decreaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(-1))
        self.increaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(1))
        self.setTrainerControls(False)
        return tab

    def createSettingsTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        subjectSetup = QFrame(objectName="panel")
        subjectLayout = QVBoxLayout(subjectSetup)
        subjectLayout.addWidget(QLabel("測試者資訊", objectName="sectionTitle"))
        subjectRow = QHBoxLayout()
        subjectRow.setSpacing(10)
        subjectRow.addWidget(QLabel("病人 ID"))
        self.patientIdEdit = QLineEdit()
        self.patientIdEdit.setPlaceholderText("例如 P-001")
        self.patientIdEdit.setFixedWidth(180)
        subjectRow.addWidget(self.patientIdEdit)
        subjectRow.addWidget(QLabel("姓名"))
        self.patientNameEdit = QLineEdit()
        self.patientNameEdit.setPlaceholderText("測試者姓名")
        self.patientNameEdit.setFixedWidth(180)
        subjectRow.addWidget(self.patientNameEdit)
        subjectRow.addWidget(QLabel("性別"))
        self.genderCombo = QComboBox()
        self.genderCombo.addItems(("女性", "男性"))
        self.genderCombo.setCurrentIndex(0)
        self.genderCombo.setFixedWidth(100)
        subjectRow.addWidget(self.genderCombo)
        subjectRow.addStretch()
        subjectLayout.addLayout(subjectRow)
        layout.addWidget(subjectSetup)
        emgSetup = QFrame(objectName="panel")
        emgLayout = QVBoxLayout(emgSetup)
        emgLayout.addWidget(QLabel("Delsys EMG", objectName="sectionTitle"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Server"))
        row.addWidget(self.hostEdit)
        row.addWidget(self.testButton)
        editButton = QPushButton("編輯肌肉對應")
        editButton.clicked.connect(self.editMappings)
        row.addWidget(editButton)
        row.addStretch()
        emgLayout.addLayout(row)
        self.activeSlotsLabel = QLabel("目前啟用感測器：尚未測試連線", objectName="muted")
        emgLayout.addWidget(self.activeSlotsLabel)
        layout.addWidget(emgSetup)
        layout.addWidget(self.trainerTab)
        layout.addWidget(self.resistanceTab)
        layout.addStretch(1)
        eventLogPanel = QFrame(objectName="eventLogPanel")
        eventLogLayout = QVBoxLayout(eventLogPanel)
        eventLogLayout.setContentsMargins(10, 7, 10, 8)
        eventLogLayout.setSpacing(2)
        eventLogLayout.addWidget(QLabel("事件 Log", objectName="eventLogTitle"))
        self.eventLog = QPlainTextEdit(objectName="eventLog")
        self.eventLog.setReadOnly(True)
        self.eventLog.setMaximumBlockCount(200)
        self.eventLog.setFixedHeight(92)
        eventLogLayout.addWidget(self.eventLog)
        layout.addWidget(eventLogPanel)
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
        self.emgRateLabel = QLabel(
            "EMG 頻率：等待 SDK 查詢", objectName="muted"
        )
        statusRow.addWidget(self.emgRateLabel)
        self.emgStatus.hide()
        self.baseLabel.hide()
        controls = QHBoxLayout()
        self.hostEdit = QLineEdit("127.0.0.1")
        self.hostEdit.setMaximumWidth(200)
        self.testButton, self.startButton, self.stopButton = QPushButton("測試連線"), QPushButton("開始串流", objectName="record"), QPushButton("停止")
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
        layout.addLayout(metrics)
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
        panel.setFixedWidth(380)
        body.addWidget(panel)
        charts = QWidget()
        chartLayout = QVBoxLayout(charts)
        chartLayout.setContentsMargins(0, 0, 0, 0)
        chartHeader = QHBoxLayout()
        chartHeader.addWidget(QLabel("即時波形（固定顯示 10 秒）", objectName="sectionTitle"))
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
        self.rebuildPlots()
        return tab

    def createSensorPanel(self) -> QWidget:
        panel = QFrame(objectName="panel")
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("啟用感測器與肌肉對應", objectName="sectionTitle"))
        edit = QPushButton("編輯肌肉對應")
        layout.addWidget(edit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        items = QVBoxLayout(content)
        items.setContentsMargins(0, 0, 0, 0)
        for index in range(CHANNEL_COUNT):
            row = QFrame(objectName="sensorRow")
            rowLayout = QHBoxLayout(row)
            rowLayout.setContentsMargins(8, 6, 8, 6)
            icon = QLabel("〽", objectName="sensorIcon")
            icon.setFixedSize(28, 28)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet(f"background: {COLORS[index % len(COLORS)]}; color: white; border-radius: 5px; font-weight: bold; font-size: 16px;")
            check = QCheckBox()
            check.setChecked(index < 4)
            detail = QLabel(objectName="sensorDetail")
            textLayout = QVBoxLayout()
            textLayout.setSpacing(0)
            textLayout.addWidget(check)
            textLayout.addWidget(detail)
            rowLayout.addWidget(icon)
            rowLayout.addLayout(textLayout, 1)
            items.addWidget(row)
            check.toggled.connect(self.rebuildPlots)
            self.sensorChecks.append(check)
            self.sensorDetails.append(detail)
            self.sensorIcons.append(icon)
            self.sensorRows.append(row)
        items.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        edit.clicked.connect(self.editMappings)
        self.updateSensorRows()
        return panel

    def connectTrainerSignals(self) -> None:
        self.trainerWorker.ready.connect(self.trainerReady)
        self.trainerWorker.scanReady.connect(self.showTrainerDevices)
        self.trainerWorker.connected.connect(self.trainerConnected)
        self.trainerWorker.disconnected.connect(self.trainerDisconnected)
        self.trainerWorker.resistanceSet.connect(self.resistanceSet)
        self.trainerWorker.fixedResistanceSet.connect(self.fixedResistanceSet)
        self.trainerWorker.telemetryReceived.connect(self.updateTrainerTelemetry)
        self.trainerWorker.failed.connect(self.trainerFailed)

    def trainerReady(self) -> None:
        self.setTrainerStatus("藍牙服務已就緒，正在自動掃描…", "working")
        self.trainerWorker.scan()

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
        self.trainerTelemetryLabel.setText("即時騎乘資料：" + (" · ".join(fields) or "等待踩踏…"))
        if data.speedKph is not None:
            self.bikeMetricValues["speed"].setText(f"{data.speedKph:.1f}")
        if data.cadenceRpm is not None:
            self.bikeMetricValues["cadence"].setText(f"{data.cadenceRpm:.0f}")
        if data.powerWatts is not None:
            self.bikeMetricValues["power"].setText(f"{data.powerWatts}")
        if data.resistance is not None:
            self.bikeMetricValues["resistance"].setText(f"{data.resistance:g}")
        if self.trainerRecorder:
            try:
                self.trainerRecorder.append(
                    data, self.resistanceSpin.value(), self.commandedNativeResistance
                )
            except OSError as exc:
                self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
                self.trainerRecorder.close()
                self.trainerRecorder = None

    def resetBikeMetrics(self) -> None:
        for value in self.bikeMetricValues.values():
            value.setText("--")

    def scanTrainer(self) -> None:
        self.setTrainerStatus("正在掃描藍牙裝置…", "working")
        self.trainerWorker.scan()

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
            self.trainerTelemetryLabel.setText("即時騎乘資料：等待踩踏…")
        else:
            self.trainerTelemetryLabel.setText("即時騎乘資料：此設備未提供 Indoor Bike Data")

    def trainerDisconnected(self) -> None:
        self.setTrainerControls(False)
        self.trainerIsConnected = False
        self.setTrainerConnectionState(False)
        self.resetBikeMetrics()
        self.setTrainerStatus("阻力計已中斷連線", "idle")
        self.trainerTelemetryLabel.setText("即時騎乘資料：尚未連線")

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
        color = "#16a34a" if connected else "#dc2626"
        state = "連線" if connected else "未連線"
        label.setText(f"● {name}：{state}")
        label.setStyleSheet(f"padding: 5px 8px; color: {color}; font-weight: 700;")

    def editMappings(self) -> None:
        active_slots = {index + 1 for index, active in enumerate(self.sensorActive) if active}
        dialog = MappingEditorDialog(self.mappings, active_slots, self)
        if dialog.exec_() == QDialog.Accepted:
            self.mappings = dialog.mappings()
            saveMappings(MAPPING_PATH, self.mappings)
            self.updateSensorRows()
            self.rebuildPlots()

    def updateSensorRows(self) -> None:
        for index, mapping in enumerate(self.mappings):
            serial = self.sensorSerials.get(mapping.slot, "未取得 SID")
            side = mapping.abbreviation.rsplit("-", 1)[-1] if "-" in mapping.abbreviation else ""
            title = f"{side} {mapping.english_name.upper()}" if side in {"R", "L"} else mapping.english_name.upper()
            self.sensorChecks[index].setText(f"S{mapping.slot:02d} · {title}")
            self.sensorDetails[index].setText(f"{mapping.chinese_name} | Slot {mapping.slot:02d} · 啟用 · SID: {serial}")
            tooltip = (
                f"S{mapping.slot:02d} · {mapping.chinese_name}\n"
                f"{mapping.english_name}\n"
                f"SID：{serial}\n"
                "勾選後顯示此感測器的即時波形。"
            )
            self.sensorRows[index].setToolTip(tooltip)
            self.sensorIcons[index].setToolTip(tooltip)
            self.sensorChecks[index].setToolTip(tooltip)
            self.sensorDetails[index].setToolTip(tooltip)
            self.sensorRows[index].setVisible(self.sensorActive[index])
        if hasattr(self, "activeSlotsLabel"):
            active_text = ", ".join(f"S{index + 1:02d}" for index, active in enumerate(self.sensorActive) if active)
            self.activeSlotsLabel.setText(f"目前啟用感測器：{active_text or '無'}")

    def applyStyles(self) -> None:
        self.setStyleSheet("""
            QWidget#root { background: #f5f7fb; color: #1e293b; }
            QLabel#title { font-size: 24px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
            QLabel#muted, QLabel#sensorDetail { color: #64748b; }
            QLabel#sensorDetail { font-size: 10px; }
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
            QFrame#panel, QFrame#sensorRow { background: white; border: 1px solid #e2e8f0; border-radius: 7px; }
            QLineEdit, QSpinBox, QComboBox { padding: 6px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QComboBox QAbstractItemView { background: white; color: #1e293b; selection-background-color: #dbeafe; selection-color: #0f172a; outline: 0; }
            QComboBox QAbstractItemView::item { min-height: 24px; color: #1e293b; background: white; }
            QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected { color: #0f172a; background: #dbeafe; }
            QPushButton { padding: 7px 10px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
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

    def outputPathForCollection(self, directory: Path) -> Path:
        """Generate a collision-free, patient-ID based EMG file name."""
        patientId = self.subjectInfo().patientId
        invalid = '\\/:*?"<>|'
        safeId = "".join(character for character in patientId if character not in invalid).strip()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        candidate = directory / f"{safeId}_EMG_{timestamp}.csv"
        suffix = 2
        while candidate.exists() or infoOutputPath(candidate).exists() or (
            self.trainerIsConnected and trainerOutputPath(candidate).exists()
        ):
            candidate = directory / f"{safeId}_EMG_{timestamp}_{suffix:02d}.csv"
            suffix += 1
        return candidate

    def setStatusLabel(self, label: QLabel, text: str, state: str) -> None:
        colors = {"idle": ("#e2e8f0", "#475569"), "working": ("#fef3c7", "#92400e"), "connected": ("#dcfce7", "#166534"), "streaming": ("#dbeafe", "#1d4ed8"), "error": ("#fee2e2", "#b91c1c")}
        background, foreground = colors[state]
        label.setText(text)
        label.setStyleSheet(f"padding: 5px 10px; border-radius: 10px; background: {background}; color: {foreground};")

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
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
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
        self.trainerOutput = trainerOutput if self.trainerIsConnected else None
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
        self.streamWorker = StreamWorker(host, output, self.mappings, self.collectionSubject)
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

    def stopStream(self) -> None:
        if self.streamWorker:
            self.setEmgStatus("正在停止…", "working")
            self.stopButton.setEnabled(False)
            self.streamWorker.request_stop()

    def streamStatusChanged(self, text: str) -> None:
        self.setEmgStatus(text, "streaming" if text == "串流中" else "working")
        if text == "串流中":
            self.beginTrainerRecording()
            self.startCountdown()

    def beginTrainerRecording(self) -> None:
        if self.trainerOutput is None:
            self.appendLog("阻力計未連線；本次不輸出阻力計 CSV")
            return
        try:
            self.trainerRecorder = TrainerCsvRecorder(
                self.trainerOutput, self.collectionOutput, self.collectionSubject
            )
            self.trainerRecorder.start()
            self.appendLog(f"阻力計 CSV 開始輸出：{self.trainerOutput.name}")
        except OSError as exc:
            self.trainerRecorder = None
            self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
            QMessageBox.warning(self, "阻力計 CSV 輸出錯誤", str(exc))

    def closeTrainerRecording(self) -> None:
        if self.trainerRecorder is None:
            return
        recorder, self.trainerRecorder = self.trainerRecorder, None
        try:
            elapsed = recorder.close()
        except OSError as exc:
            self.appendLog(f"阻力計 CSV 結束寫入失敗：{exc}")
            return
        self.appendLog(f"阻力計 CSV 已輸出 {recorder.rows} 筆 telemetry")
        if self.infoRecorder:
            self.infoRecorder.append("Resistance summary", [
                f"Telemetry rows: {recorder.rows}",
                f"Collection elapsed seconds: {elapsed:.6f}",
            ])

    def collectionMetadataReady(self, metadata: dict) -> None:
        if self.infoRecorder is None:
            return
        lines = [
            f"Base serial: {metadata['base_serial']}",
            f"EMG sample rate Hz: {metadata['sample_rate_hz']:.6f}",
            f"Samples per frame: {metadata['samples_per_frame']}",
            f"Frame interval seconds: {metadata['frame_interval_seconds']:.6f}",
            "Active sensor mapping:",
        ]
        lines.extend(
            "  S{slot:02d} | {sid} | {chinese_name} | {english_name} | {abbreviation}".format(**channel)
            for channel in metadata["channels"]
        )
        self.infoRecorder.append("EMG acquisition", lines)

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
            self.infoRecorder.append("EMG summary", [
                f"Samples: {samples}",
                f"Collection elapsed seconds: {elapsed:.6f}",
                f"Data duration seconds: {samples / sampleRate:.6f}" if sampleRate else "Data duration seconds: 0",
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

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.streamWorker:
            self.streamWorker.request_stop()
            self.streamWorker.wait(6000)
        self.trainerWorker.stopWorker()
        self.trainerWorker.wait(6000)
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TrignoWindow()
    window.show()
    return app.exec_()

if __name__ == "__main__":
    raise SystemExit(main())
