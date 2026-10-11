from __future__ import annotations
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget,)
from delsys.body_map import BodyMapPanel
from delsys.sensor_mapping import SensorMapping, loadMappings, saveMappings
from delsys.trainer import BikeTelemetry, NumericRange, TrainerWorker
from delsys.trigno import CHANNEL_COUNT, SAMPLE_RATE
from delsys.workers import ConnectionWorker, StreamWorker

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().parents[2] / "sensor_mapping.json"


class SensorLegendToggle(QWidget):
    toggled = pyqtSignal(bool)
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


class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EMG Cycling Monitor")
        self.resize(1500, 920)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
        self.trainerWorker = TrainerWorker()
        self.trainerIsConnected = False
        self.histories = [deque(maxlen=int(SAMPLE_RATE * 20)) for _ in range(CHANNEL_COUNT)]
        self.mappings = loadMappings(MAPPING_PATH)
        self.sensorChecks: list[SensorLegendToggle] = []
        self.sensorIcons: list[QLabel] = []
        self.sensorRows: list[QFrame] = []
        self.sensorActive = [False] * CHANNEL_COUNT
        self.sensorSerials: dict[int, str] = {}
        self.curves: dict[int, tuple[pg.PlotItem, pg.PlotDataItem]] = {}
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
        controls = QHBoxLayout()
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
        controls.addWidget(resistance)
        self.resistancePanel = resistance
        self.resistanceStepLevels = 1
        self.scanTrainerButton.clicked.connect(self.scanTrainer)
        self.connectTrainerButton.clicked.connect(self.connectTrainer)
        self.decreaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(-1))
        self.increaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(1))
        self.setTrainerControls(False)
        return tab

    def createResistanceTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        panels = QHBoxLayout()
        panels.addWidget(self.resistancePanel, 1)
        layout.addLayout(panels)
        return tab

    def createSettingsTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
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
        statusRow.addWidget(QLabel("Legacy EMG · 2000 Hz · float32", objectName="muted"))
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
        self.recordCheck = QCheckBox("儲存 CSV")
        self.recordCheck.setChecked(True)
        self.pathEdit = QLineEdit(self.defaultOutputPath())
        self.browseButton = QPushButton("選擇檔案")
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
        self.recordCheck.toggled.connect(self.recordToggled)
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
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }"
        )
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
        selected = {
            index + 1 for index, check in enumerate(self.sensorChecks) if check.isChecked()
        }
        active = {
            index + 1 for index, isActive in enumerate(self.sensorActive) if isActive
        }
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

    def trainerReady(self) -> None:
        self.setTrainerStatus("藍牙服務已就緒，正在自動掃描…", "working")
        self.trainerWorker.scan()

    def resistanceSet(self, value: float) -> None:
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
            self.sensorRows[index].setToolTip(f"S{mapping.slot:02d} · {title}")
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
            QPushButton { padding: 7px 10px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QPushButton#bodyViewButton { padding: 5px 12px; border-radius: 0; }
            QPushButton#bodyViewButton:checked { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; }
            QPushButton#primary { background: #2563eb; border: 0; color: white; font-weight: 600; }
            QPushButton#record { background: #dc2626; border: 0; color: white; font-weight: 600; }
            QPushButton:disabled { color: #94a3b8; background: #e2e8f0; }
            QCheckBox { font-weight: 700; }""")

    def defaultOutputPath(self) -> str:
        return str(Path.cwd() / "recordings" / f"emg_{datetime.now():%Y%m%d_%H%M%S}.csv")

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
        self.recordCheck.setEnabled(not streaming)
        self.durationMinutesSpin.setEnabled(not streaming)
        self.durationSecondsSpin.setEnabled(not streaming)
        self.pathEdit.setEnabled(not streaming and self.recordCheck.isChecked())
        self.browseButton.setEnabled(not streaming and self.recordCheck.isChecked())

    def recordToggled(self, checked: bool) -> None:
        self.pathEdit.setEnabled(checked and self.streamWorker is None)
        self.browseButton.setEnabled(checked and self.streamWorker is None)

    def chooseOutput(self) -> None:
        path, unused = QFileDialog.getSaveFileName(self, "選擇 CSV 檔案", self.pathEdit.text(), "CSV (*.csv)")
        if path:
            self.pathEdit.setText(path)

    def testConnection(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
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
        output = Path(self.pathEdit.text()).expanduser() if self.recordCheck.isChecked() else None
        if output and output.exists():
            QMessageBox.warning(self, "檔案已存在", "請指定尚不存在的 CSV 檔案，避免覆寫。")
            return
        for history in self.histories:
            history.clear()
        self.streamWorker = StreamWorker(host, output)
        self.streamWorker.status_changed.connect(lambda text: self.setEmgStatus(text, "streaming" if text == "串流中" else "working"))
        self.streamWorker.connected.connect(lambda banner: self.emgStatus.setToolTip(banner))
        self.streamWorker.base_ready.connect(lambda serial: self.baseLabel.setText(f"Base: {serial}"))
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.failed.connect(lambda message: QMessageBox.critical(self, "Trigno 串流錯誤", message))
        self.streamWorker.finished.connect(self.streamFinished)
        self.setStreaming(True)
        self.streamWorker.start()
        self.startCountdown()

    def stopStream(self) -> None:
        if self.streamWorker:
            self.setEmgStatus("正在停止…", "working")
            self.stopButton.setEnabled(False)
            self.streamWorker.request_stop()

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
        for channel, history in enumerate(self.histories):
            history.extend(frames[:, channel].tolist())
        self.sampleLabel.setText(f"{len(self.histories[0]):,} samples in view")

    def streamFinished(self) -> None:
        worker, self.streamWorker = self.streamWorker, None
        if worker:
            worker.deleteLater()
        self.countdownTimer.stop()
        self.setStreaming(False)
        self.setEmgStatus("已停止", "idle")

    def rebuildPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        selected = [index for index, check in enumerate(self.sensorChecks) if check.isChecked()]
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
        duration, count = 10, int(10 * SAMPLE_RATE)
        for channel, (plot, curve) in self.curves.items():
            values = np.asarray(list(self.histories[channel])[-count:], dtype=np.float32) * 1000
            xAxis = (np.arange(len(values)) - len(values) + 1) / SAMPLE_RATE
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