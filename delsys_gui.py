#!/usr/bin/env python3
from __future__ import annotations
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,QVBoxLayout, QWidget,)
from delsys.sensor_mapping import SensorMapping, loadMappings, saveMappings
from delsys.trigno import CHANNEL_COUNT, SAMPLE_RATE
from delsys.workers import ConnectionWorker, StreamWorker

COLORS = ("#1684d7", "#e95f54", "#7e57c2", "#00897b", "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
MAPPING_PATH = Path(__file__).resolve().with_name("sensor_mapping.json")

class MappingEditorDialog(QDialog):
    """Edit all muscle names and abbreviations directly in the GUI."""

    def __init__(self, mappings: list[SensorMapping], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯感測器／肌肉對應")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("修改後按「儲存並套用」。SDK Slot 是固定編號，無法變更。"))
        headings = QGridLayout()
        for column, title in enumerate(("Slot", "中文肌肉名稱", "English EMG name", "縮寫")):
            headings.addWidget(QLabel(title, objectName="editorHeader"), 0, column)
        layout.addLayout(headings)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        self.edits: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        for row, mapping in enumerate(mappings):
            grid.addWidget(QLabel(f"S{mapping.slot:02d}"), row, 0)
            chineseEdit = QLineEdit(mapping.chinese_name)
            englishEdit = QLineEdit(mapping.english_name)
            abbreviationEdit = QLineEdit(mapping.abbreviation)
            grid.addWidget(chineseEdit, row, 1)
            grid.addWidget(englishEdit, row, 2)
            grid.addWidget(abbreviationEdit, row, 3)
            self.edits.append((chineseEdit, englishEdit, abbreviationEdit))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Save).setText("儲存並套用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mappings(self) -> list[SensorMapping]:
        result = []
        for slot, edits in enumerate(self.edits, 1):
            chinese, english, abbreviation = (edit.text().strip() for edit in edits)
            result.append(SensorMapping(slot, chinese or "未指定", english or "UNASSIGNED", abbreviation))
        return result

class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Trigno EMG Monitor")
        self.resize(1280, 820)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
        self.histories = [deque(maxlen=int(SAMPLE_RATE * 20)) for _ in range(CHANNEL_COUNT)]
        self.mappings = loadMappings(MAPPING_PATH)
        self.sensorChecks: list[QCheckBox] = []
        self.sensorDetails: list[QLabel] = []
        self.sensorIcons: list[QLabel] = []
        self.sensorRows: list[QFrame] = []
        self.sensorActive = [False] * CHANNEL_COUNT
        self.sensorPaired = [False] * CHANNEL_COUNT
        self.sensorSerials: dict[int, str] = {}
        self.curves: dict[int, tuple[pg.PlotItem, pg.PlotDataItem]] = {}
        self.buildUi()
        self.setStreaming(False)
        self.setStatus("尚未連線", "idle")

    def buildUi(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(QLabel("Trigno EMG", objectName="title"))
        self.statusLabel = QLabel(objectName="status")
        header.addWidget(self.statusLabel)
        self.baseLabel = QLabel("Base: 尚未確認", objectName="muted")
        header.addWidget(self.baseLabel)
        header.addStretch()
        header.addWidget(QLabel("Legacy EMG · 2000 Hz · float32", objectName="muted"))
        layout.addLayout(header)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("SDK Server"))
        self.hostEdit = QLineEdit("127.0.0.1")
        self.hostEdit.setMaximumWidth(200)
        self.testButton = QPushButton("測試連線")
        self.startButton = QPushButton("開始串流", objectName="primary")
        self.stopButton = QPushButton("停止")
        for item in (self.hostEdit, self.testButton, self.startButton, self.stopButton):
            controls.addWidget(item)
        controls.addSpacing(12)
        controls.addWidget(QLabel("顯示範圍"))
        self.windowSpin = QSpinBox()
        self.windowSpin.setRange(1, 20)
        self.windowSpin.setValue(5)
        self.windowSpin.setSuffix(" 秒")
        controls.addWidget(self.windowSpin)
        controls.addWidget(QLabel("Y 軸 ±"))
        self.yRangeSpin = QDoubleSpinBox()
        self.yRangeSpin.setRange(0.05, 20)
        self.yRangeSpin.setValue(1)
        self.yRangeSpin.setSuffix(" mV")
        controls.addWidget(self.yRangeSpin)
        self.autoYCheck = QCheckBox("自動縮放")
        controls.addWidget(self.autoYCheck)
        controls.addStretch()
        self.sampleLabel = QLabel("0 samples", objectName="muted")
        controls.addWidget(self.sampleLabel)
        layout.addLayout(controls)
        recording = QHBoxLayout()
        self.recordCheck = QCheckBox("儲存 CSV")
        self.recordCheck.setChecked(True)
        self.pathEdit = QLineEdit(self.defaultOutputPath())
        self.browseButton = QPushButton("選擇檔案")
        for item in (self.recordCheck, self.pathEdit, self.browseButton):
            recording.addWidget(item)
        layout.addLayout(recording)
        body = QHBoxLayout()
        sensorPanel = self.createSensorPanel()
        sensorPanel.setFixedWidth(380)
        body.addWidget(sensorPanel)
        charts = QWidget()
        chartLayout = QVBoxLayout(charts)
        chartLayout.setContentsMargins(0, 0, 0, 0)
        chartLayout.addWidget(QLabel("即時波形", objectName="sectionTitle"))
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
        self.windowSpin.valueChanged.connect(self.refreshPlots)
        self.yRangeSpin.valueChanged.connect(self.refreshPlots)
        self.autoYCheck.toggled.connect(self.autoYToggled)
        self.plotTimer = QTimer(self)
        self.plotTimer.setInterval(80)
        self.plotTimer.timeout.connect(self.refreshPlots)
        self.plotTimer.start()
        self.applyStyles()
        self.rebuildPlots()

    def createSensorPanel(self) -> QWidget:
        panel = QFrame(objectName="panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(QLabel("感測器與肌肉對應", objectName="sectionTitle"))
        note = QLabel("英文名稱與縮寫可直接在此編輯，儲存後立即套用。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        allButton, editButton = QPushButton("顯示全部啟用"), QPushButton("編輯對應")
        for item in (allButton, editButton):
            buttons.addWidget(item)
        layout.addLayout(buttons)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        sensorLayout = QVBoxLayout(content)
        sensorLayout.setContentsMargins(0, 0, 0, 0)
        sensorLayout.setSpacing(3)
        for index in range(CHANNEL_COUNT):
            row = QFrame(objectName="sensorRow")
            rowLayout = QHBoxLayout(row)
            rowLayout.setContentsMargins(8, 5, 8, 5)
            icon = QLabel("〽", objectName="sensorIcon")
            icon.setFixedSize(28, 28)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet(self.sensorIconStyle(index))
            check = QCheckBox()
            check.setChecked(index < 4)
            detail = QLabel(objectName="sensorDetail")
            textLayout = QVBoxLayout()
            textLayout.setSpacing(0)
            textLayout.addWidget(check)
            textLayout.addWidget(detail)
            rowLayout.addWidget(icon)
            rowLayout.addLayout(textLayout, 1)
            sensorLayout.addWidget(row)
            check.toggled.connect(self.rebuildPlots)
            self.sensorChecks.append(check)
            self.sensorDetails.append(detail)
            self.sensorIcons.append(icon)
            self.sensorRows.append(row)
        sensorLayout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        allButton.clicked.connect(lambda: self.setChecked(range(CHANNEL_COUNT)))
        editButton.clicked.connect(self.editMappings)
        self.updateSensorRows()
        return panel

    def sensorIconStyle(self, index: int) -> str:
        return f"background: {COLORS[index % len(COLORS)]}; color: white; border-radius: 5px; font-weight: bold; font-size: 16px;"

    def setChecked(self, indices) -> None:
        selected = set(indices)
        for index, check in enumerate(self.sensorChecks):
            check.setChecked(index in selected)

    def editMappings(self) -> None:
        dialog = MappingEditorDialog(self.mappings, self)
        if dialog.exec_() == QDialog.Accepted:
            self.mappings = dialog.mappings()
            saveMappings(MAPPING_PATH, self.mappings)
            self.updateSensorRows()
            self.rebuildPlots()

    def updateSensorRows(self) -> None:
        for index, mapping in enumerate(self.mappings):
            state = "啟用"
            self.sensorChecks[index].setText(f"S{mapping.slot:02d} · {self.englishTitle(mapping)}")
            serial = self.sensorSerials.get(mapping.slot, "未取得 SID")
            self.sensorDetails[index].setText(
                f"{mapping.chinese_name}  |  Slot {mapping.slot:02d} · {state} · SID: {serial}"
            )
            self.sensorRows[index].setVisible(self.sensorActive[index])
            self.sensorRows[index].setStyleSheet("")

    @staticmethod
    def englishTitle(mapping: SensorMapping) -> str:
        side = mapping.abbreviation.rsplit("-", 1)[-1] if "-" in mapping.abbreviation else ""
        prefix = f"{side} " if side in {"R", "L"} else ""
        return f"{prefix}{mapping.english_name.upper()}"

    def applyStyles(self) -> None:
        self.setStyleSheet("""
            QWidget#root { background: #f5f7fb; color: #1e293b; }
            QLabel#title { font-size: 24px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
            QLabel#muted, QLabel#sensorDetail { color: #64748b; }
            QLabel#sensorDetail { font-size: 10px; }
            QLabel#status { padding: 5px 10px; border-radius: 10px; }
            QFrame#panel, QFrame#sensorRow { background: white; border: 1px solid #e2e8f0; border-radius: 7px; }
            QLineEdit, QSpinBox, QDoubleSpinBox { padding: 6px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QPushButton { padding: 7px 10px; background: white; border: 1px solid #cbd5e1; border-radius: 5px; }
            QPushButton#primary { background: #2563eb; border: 0; color: white; font-weight: 600; }
            QPushButton:disabled { color: #94a3b8; background: #e2e8f0; }
            QCheckBox { font-weight: 700; }""")

    @staticmethod
    def defaultOutputPath() -> str:
        return str(Path.cwd() / "recordings" / f"emg_{datetime.now():%Y%m%d_%H%M%S}.csv")

    def setStatus(self, text: str, state: str) -> None:
        colors = {"idle": ("#e2e8f0", "#475569"), "working": ("#fef3c7", "#92400e"), "connected": ("#dcfce7", "#166534"), "streaming": ("#dbeafe", "#1d4ed8"), "error": ("#fee2e2", "#b91c1c")}
        background, foreground = colors[state]
        self.statusLabel.setText(text)
        self.statusLabel.setStyleSheet(f"padding: 5px 10px; border-radius: 10px; background: {background}; color: {foreground};")

    def setStreaming(self, streaming: bool) -> None:
        testing = self.testWorker is not None
        self.startButton.setEnabled(not streaming and not testing)
        self.stopButton.setEnabled(streaming)
        self.hostEdit.setEnabled(not streaming and not testing)
        self.testButton.setEnabled(not streaming and not testing)
        self.recordCheck.setEnabled(not streaming)
        self.pathEdit.setEnabled(not streaming and self.recordCheck.isChecked())
        self.browseButton.setEnabled(not streaming and self.recordCheck.isChecked())

    def recordToggled(self, checked: bool) -> None:
        self.pathEdit.setEnabled(checked and self.streamWorker is None)
        self.browseButton.setEnabled(checked and self.streamWorker is None)

    def chooseOutput(self) -> None:
        path, unusedFilter = QFileDialog.getSaveFileName(self, "選擇 CSV 檔案", self.pathEdit.text(), "CSV (*.csv)")
        if path:
            self.pathEdit.setText(path)

    def testConnection(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
            return
        self.setStatus("正在測試連線…", "working")
        self.testWorker = ConnectionWorker(host)
        self.testWorker.connected.connect(lambda banner: self.statusLabel.setToolTip(banner))
        self.testWorker.base_ready.connect(self.updateBaseSerial)
        self.testWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.testWorker.failed.connect(self.connectionFailed)
        self.testWorker.finished.connect(self.testFinished)
        self.setStreaming(False)
        self.testWorker.start()

    def testFinished(self) -> None:
        worker, self.testWorker = self.testWorker, None
        if worker:
            worker.deleteLater()
        if not self.statusLabel.text().startswith("連線失敗"):
            self.setStatus("已連線；感測器狀態已更新", "connected")
        self.setStreaming(False)

    def connectionFailed(self, message: str) -> None:
        self.setStatus(f"連線失敗：{message}", "error")

    def updateBaseSerial(self, serial: str) -> None:
        self.baseLabel.setText(f"Base: {serial}")

    def updateSensorStates(self, paired: list[int], active: list[int], serials: dict[int, str]) -> None:
        self.sensorPaired = [index + 1 in paired for index in range(CHANNEL_COUNT)]
        self.sensorActive = [index + 1 in active for index in range(CHANNEL_COUNT)]
        self.sensorSerials = serials
        self.updateSensorRows()
        self.setChecked(index for index, enabled in enumerate(self.sensorActive) if enabled)

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
        self.sampleLabel.setText("0 samples")
        self.streamWorker = StreamWorker(host, output)
        self.streamWorker.status_changed.connect(lambda text: self.setStatus(text, "streaming" if text == "串流中" else "working"))
        self.streamWorker.connected.connect(lambda banner: self.statusLabel.setToolTip(banner))
        self.streamWorker.base_ready.connect(self.updateBaseSerial)
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.failed.connect(self.streamFailed)
        self.streamWorker.finished.connect(self.streamFinished)
        self.setStreaming(True)
        self.streamWorker.start()

    def stopStream(self) -> None:
        if self.streamWorker:
            self.setStatus("正在停止…", "working")
            self.stopButton.setEnabled(False)
            self.streamWorker.request_stop()

    def framesReceived(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] != CHANNEL_COUNT:
            return
        for channel, history in enumerate(self.histories):
            history.extend(frames[:, channel].tolist())
        self.sampleLabel.setText(f"{len(self.histories[0]):,} samples in view")

    def streamFailed(self, message: str) -> None:
        self.setStatus(f"串流錯誤：{message}", "error")
        QMessageBox.critical(self, "Trigno 串流錯誤", message)

    def streamFinished(self) -> None:
        worker, self.streamWorker = self.streamWorker, None
        if worker:
            worker.deleteLater()
        self.setStreaming(False)
        if not self.statusLabel.text().startswith("串流錯誤"):
            self.setStatus("已停止", "idle")

    def autoYToggled(self, checked: bool) -> None:
        self.yRangeSpin.setEnabled(not checked)
        self.refreshPlots()

    def rebuildPlots(self) -> None:
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
            plot.setMouseEnabled(x=True, y=False)
            curve = plot.plot(pen=pg.mkPen(COLORS[channel % len(COLORS)], width=1))
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self.curves[channel] = (plot, curve)
        self.refreshPlots()

    def refreshPlots(self) -> None:
        duration, count = self.windowSpin.value(), int(self.windowSpin.value() * SAMPLE_RATE)
        for channel, (plot, curve) in self.curves.items():
            values = np.asarray(list(self.histories[channel])[-count:], dtype=np.float32) * 1000
            xAxis = (np.arange(len(values)) - len(values) + 1) / SAMPLE_RATE
            limit = self.yRangeSpin.value()
            if self.autoYCheck.isChecked() and len(values):
                limit = max(0.05, min(20, float(np.percentile(np.abs(values), 99.5)) * 1.25))
            plot.setXRange(-duration, 0, padding=0)
            plot.setYRange(-limit, limit, padding=0)
            curve.setData(xAxis, values)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.streamWorker:
            self.streamWorker.request_stop()
            if not self.streamWorker.wait(6000):
                event.ignore()
                return
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TrignoWindow()
    window.show()
    return app.exec_()

if __name__ == "__main__":
    raise SystemExit(main())