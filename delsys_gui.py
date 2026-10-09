#!/usr/bin/env python3
"""Qt monitor for the Delsys Trigno SDK Server EMG TCP stream."""

from __future__ import annotations

import csv
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from trigno_emg import TrignoClient, TrignoError, configure_console_encoding


CHANNEL_COUNT = 16
SAMPLE_RATE = 2000.0
RMS_WINDOW_SECONDS = 0.1
RMS_WINDOW_SAMPLES = int(SAMPLE_RATE * RMS_WINDOW_SECONDS)


class ConnectionWorker(QThread):
    """Check that all SDK Server sockets can be opened without starting a stream."""

    connected = pyqtSignal(str)
    sensor_states_ready = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    def run(self) -> None:
        try:
            with TrignoClient(host=self.host, timeout=3.0) as client:
                self.connected.emit(client.connect())
                self.sensor_states_ready.emit(*client.sensor_states(CHANNEL_COUNT))
        except (OSError, TrignoError, ValueError) as exc:
            self.failed.emit(str(exc))


class StreamWorker(QThread):
    status_changed = pyqtSignal(str)
    server_connected = pyqtSignal(str)
    sensor_states_ready = pyqtSignal(object, object)
    frames_ready = pyqtSignal(object)
    stream_error = pyqtSignal(str)

    def __init__(self, host: str, output_path: Path | None) -> None:
        super().__init__()
        self.host = host
        self.output_path = output_path
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        client = TrignoClient(host=self.host, timeout=5.0)
        csv_file = None
        writer = None
        sample_index = 0
        emit_buffer: list[tuple[float, ...]] = []
        last_emit = time.monotonic()

        try:
            self.status_changed.emit("正在建立串流連線…")
            with client:
                self.server_connected.emit(client.connect())
                self.status_changed.emit("正在讀取感測器狀態…")
                self.sensor_states_ready.emit(*client.sensor_states(CHANNEL_COUNT))

                if self.output_path:
                    self.output_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_file = self.output_path.open("x", newline="", encoding="utf-8")
                    writer = csv.writer(csv_file)
                    writer.writerow(
                        ["sample", "time_seconds"]
                        + [f"sensor_{i}_emg" for i in range(1, CHANNEL_COUNT + 1)]
                    )

                client.start()
                self.status_changed.emit("串流中")
                while not self._stop_event.is_set():
                    frames = client.read_emg_frames(CHANNEL_COUNT)
                    if not frames:
                        continue

                    if writer:
                        for frame in frames:
                            writer.writerow(
                                [sample_index, sample_index / SAMPLE_RATE, *frame]
                            )
                            sample_index += 1

                    emit_buffer.extend(frames)
                    now = time.monotonic()
                    if len(emit_buffer) >= 100 or now - last_emit >= 0.05:
                        self.frames_ready.emit(np.asarray(emit_buffer, dtype=np.float32))
                        emit_buffer.clear()
                        last_emit = now

                if emit_buffer:
                    self.frames_ready.emit(np.asarray(emit_buffer, dtype=np.float32))
                client.stop()
        except (OSError, TrignoError, ValueError) as exc:
            self.stream_error.emit(str(exc))
        finally:
            if csv_file:
                csv_file.close()


class TrignoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Trigno EMG Monitor")
        self.resize(1180, 780)
        self.stream_worker: StreamWorker | None = None
        self.test_worker: ConnectionWorker | None = None
        self.sample_count = 0
        self.selected_channel = 0
        self.history = [deque(maxlen=40_000) for _ in range(CHANNEL_COUNT)]
        self._build_ui()
        self._set_streaming(False)
        self._set_status("未測試", "idle")

    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Trigno EMG", objectName="title")
        self.status_label = QLabel(objectName="status")
        self.spec_label = QLabel("TCP 2000 Hz · float32", objectName="muted")
        header.addWidget(title)
        header.addWidget(self.status_label)
        header.addStretch()
        header.addWidget(self.spec_label)
        layout.addLayout(header)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Server"))
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setMaximumWidth(210)
        self.test_button = QPushButton("測試連線")
        self.start_button = QPushButton("開始串流", objectName="primary")
        self.stop_button = QPushButton("停止")
        controls.addWidget(self.host_edit)
        controls.addWidget(self.test_button)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        layout.addLayout(controls)

        recording = QHBoxLayout()
        self.record_check = QCheckBox("儲存 CSV")
        self.record_check.setChecked(True)
        self.path_edit = QLineEdit(self._default_output_path())
        self.browse_button = QPushButton("瀏覽")
        recording.addWidget(self.record_check)
        recording.addWidget(self.path_edit)
        recording.addWidget(self.browse_button)
        layout.addLayout(recording)

        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        self.table = self._create_channel_table()
        splitter.addWidget(self.table)

        chart_container = QWidget()
        chart_layout = QVBoxLayout(chart_container)
        chart_layout.setContentsMargins(4, 0, 0, 0)
        chart_layout.setSpacing(5)
        chart_controls = QHBoxLayout()
        self.plot_title = QLabel("通道 1", objectName="plotTitle")
        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 20)
        self.window_spin.setValue(5)
        self.window_spin.setSuffix(" 秒")
        self.y_range_spin = QDoubleSpinBox()
        self.y_range_spin.setRange(0.05, 20.0)
        self.y_range_spin.setDecimals(2)
        self.y_range_spin.setSingleStep(0.25)
        self.y_range_spin.setValue(1.0)
        self.y_range_spin.setPrefix("± ")
        self.y_range_spin.setSuffix(" mV")
        self.auto_y_check = QCheckBox("自動縮放")
        self.zoom_in_button = QPushButton("放大")
        self.zoom_out_button = QPushButton("縮小")
        self.counter_label = QLabel("0 samples", objectName="muted")
        chart_controls.addWidget(self.plot_title)
        chart_controls.addStretch()
        chart_controls.addWidget(QLabel("時間窗"))
        chart_controls.addWidget(self.window_spin)
        chart_controls.addWidget(QLabel("Y 範圍"))
        chart_controls.addWidget(self.y_range_spin)
        chart_controls.addWidget(self.zoom_in_button)
        chart_controls.addWidget(self.zoom_out_button)
        chart_controls.addWidget(self.auto_y_check)
        chart_controls.addWidget(self.counter_label)
        chart_layout.addLayout(chart_controls)

        self.plots = pg.GraphicsLayoutWidget()
        self.plots.setBackground("#ffffff")
        self.plots.addLabel("原始 EMG", row=0, col=0, color="#374151", size="11pt")
        self.raw_plot = self.plots.addPlot(row=1, col=0)
        self.plots.addLabel(
            "EMG envelope — RMS 100 ms", row=2, col=0, color="#374151", size="11pt"
        )
        self.envelope_plot = self.plots.addPlot(row=3, col=0)
        self._configure_plots()
        chart_layout.addWidget(self.plots)
        splitter.addWidget(chart_container)
        splitter.setSizes([300, 880])
        layout.addWidget(splitter)

        self._apply_styles()
        self.test_button.clicked.connect(self.test_connection)
        self.start_button.clicked.connect(self.start_stream)
        self.stop_button.clicked.connect(self.stop_stream)
        self.browse_button.clicked.connect(self.choose_output)
        self.record_check.toggled.connect(self._record_toggled)
        self.table.cellClicked.connect(self.select_channel)
        self.window_spin.valueChanged.connect(self.refresh_plots)
        self.y_range_spin.valueChanged.connect(self.refresh_plots)
        self.auto_y_check.toggled.connect(self._auto_y_toggled)
        self.zoom_in_button.clicked.connect(lambda: self._zoom_y_axis(0.5))
        self.zoom_out_button.clicked.connect(lambda: self._zoom_y_axis(2.0))
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(100)
        self.plot_timer.timeout.connect(self.refresh_plots)
        self.plot_timer.start()

    def _create_channel_table(self) -> QTableWidget:
        table = QTableWidget(CHANNEL_COUNT, 4)
        table.setHorizontalHeaderLabels(["通道", "狀態", "目前 (mV)", "RMS 100ms"])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for column in range(1, 4):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        for row in range(CHANNEL_COUNT):
            for column, value in enumerate((str(row + 1), "未測試", "0", "0")):
                table.setItem(row, column, QTableWidgetItem(value))
        table.selectRow(0)
        return table

    def _configure_plots(self) -> None:
        for plot in (self.raw_plot, self.envelope_plot):
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setMenuEnabled(False)
            plot.setMouseEnabled(x=True, y=False)
            plot.disableAutoRange(axis="y")
        self.raw_plot.setLabel("left", "EMG", units="mV")
        self.raw_plot.hideAxis("bottom")
        self.envelope_plot.setLabel("left", "RMS", units="mV")
        self.envelope_plot.setLabel("bottom", "時間", units="s")
        self.envelope_plot.setXLink(self.raw_plot)
        self.raw_curve = self.raw_plot.plot(pen=pg.mkPen("#6a8cff", width=1))
        self.raw_curve.setDownsampling(auto=True, method="peak")
        self.raw_curve.setClipToView(True)
        self.envelope_curve = self.envelope_plot.plot(pen=pg.mkPen("#b7795c", width=1.5))

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QWidget#root { background: #f7f8fa; color: #202124; }
            QLabel#title { font-size: 22px; font-weight: 700; }
            QLabel#status { padding: 4px 10px; background: #e5e7eb; border-radius: 9px; }
            QLabel#muted { color: #6b7280; }
            QLabel#plotTitle { font-size: 16px; font-weight: 600; }
            QLineEdit, QSpinBox, QDoubleSpinBox { padding: 6px; border: 1px solid #d1d5db; border-radius: 5px; background: white; }
            QPushButton { padding: 7px 14px; border: 1px solid #cbd5e1; border-radius: 5px; background: white; }
            QPushButton#primary { color: white; border: 0; background: #2563eb; }
            QPushButton:disabled { color: #9ca3af; background: #e5e7eb; }
            QTableWidget { border: 1px solid #e5e7eb; background: white; gridline-color: #eef0f2; }
            QHeaderView::section { padding: 6px; border: 0; background: #eef2f7; font-weight: 600; }
            """
        )

    @staticmethod
    def _default_output_path() -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(Path.cwd() / "recordings" / f"emg_{stamp}.csv")

    def _set_status(self, text: str, state: str) -> None:
        colors = {
            "idle": ("#e5e7eb", "#374151"),
            "working": ("#fef3c7", "#92400e"),
            "connected": ("#dcfce7", "#166534"),
            "streaming": ("#dbeafe", "#1d4ed8"),
            "error": ("#fee2e2", "#b91c1c"),
        }
        background, foreground = colors[state]
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"padding: 4px 10px; background: {background}; color: {foreground}; border-radius: 9px;"
        )

    def _set_streaming(self, streaming: bool) -> None:
        self.start_button.setEnabled(not streaming and self.test_worker is None)
        self.stop_button.setEnabled(streaming)
        self.host_edit.setEnabled(not streaming and self.test_worker is None)
        self.test_button.setEnabled(not streaming and self.test_worker is None)
        self.record_check.setEnabled(not streaming)
        enabled = not streaming and self.record_check.isChecked()
        self.path_edit.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)

    def _record_toggled(self, checked: bool) -> None:
        enabled = checked and self.stream_worker is None
        self.path_edit.setEnabled(enabled)
        self.browse_button.setEnabled(enabled)

    def choose_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "選擇 CSV 儲存位置", self.path_edit.text(), "CSV (*.csv)"
        )
        if path:
            self.path_edit.setText(path)

    def test_connection(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server 位址。")
            return
        self._set_status("正在測試連線…", "working")
        self.test_worker = ConnectionWorker(host)
        self.test_worker.connected.connect(self._test_connected)
        self.test_worker.sensor_states_ready.connect(self._test_sensor_states)
        self.test_worker.failed.connect(self._connection_failed)
        self.test_worker.finished.connect(self._test_finished)
        self._set_streaming(False)
        self.test_worker.start()

    def _test_connected(self, banner: str) -> None:
        self._set_status("Server 已連線，讀取感測器狀態…", "connected")
        self.status_label.setToolTip(banner)

    def _test_sensor_states(self, paired: list[int], active: list[int]) -> None:
        self._sensor_states(paired, active)
        self._set_status(
            f"連線成功：{len(paired)} 已配對 / {len(active)} 使用中", "connected"
        )

    def _test_finished(self) -> None:
        worker = self.test_worker
        self.test_worker = None
        if worker:
            worker.deleteLater()
        self._set_streaming(False)

    def _connection_failed(self, message: str) -> None:
        self._set_status(f"連線失敗：{message}", "error")

    def start_stream(self) -> None:
        if self.test_worker is not None:
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server 位址。")
            return
        output = None
        if self.record_check.isChecked():
            raw_path = self.path_edit.text().strip()
            if not raw_path:
                QMessageBox.warning(self, "缺少檔名", "請選擇 CSV 儲存位置。")
                return
            output = Path(raw_path).expanduser()
            if output.exists():
                QMessageBox.warning(self, "檔案已存在", "請選擇新的 CSV 檔名。")
                return

        self.sample_count = 0
        for values in self.history:
            values.clear()
        self._reset_sensor_states()
        self.stream_worker = StreamWorker(host, output)
        self.stream_worker.status_changed.connect(self._stream_status)
        self.stream_worker.server_connected.connect(self._stream_connected)
        self.stream_worker.sensor_states_ready.connect(self._sensor_states)
        self.stream_worker.frames_ready.connect(self._frames_received)
        self.stream_worker.stream_error.connect(self._stream_error)
        self.stream_worker.finished.connect(self._stream_finished)
        self._set_streaming(True)
        self.stream_worker.start()

    def stop_stream(self) -> None:
        if self.stream_worker:
            self._set_status("正在停止串流…", "working")
            self.stop_button.setEnabled(False)
            self.stream_worker.request_stop()

    def _stream_status(self, message: str) -> None:
        state = "streaming" if message == "串流中" else "working"
        self._set_status(message, state)

    def _stream_connected(self, banner: str) -> None:
        self._set_status("Server 已連線，準備串流…", "connected")
        self.status_label.setToolTip(banner)

    def _reset_sensor_states(self) -> None:
        for row in range(CHANNEL_COUNT):
            self.table.item(row, 1).setText("查詢中")
            self.table.item(row, 2).setText("0")
            self.table.item(row, 3).setText("0")

    def _sensor_states(self, paired: list[int], active: list[int]) -> None:
        for row in range(CHANNEL_COUNT):
            number = row + 1
            state = "使用中" if number in active else "已配對" if number in paired else "未配對"
            self.table.item(row, 1).setText(state)

    def _frames_received(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] != CHANNEL_COUNT:
            return
        self.sample_count += len(frames)
        for channel in range(CHANNEL_COUNT):
            self.history[channel].extend(frames[:, channel].tolist())
            values = np.asarray(self.history[channel], dtype=np.float64)
            short_window = values[-RMS_WINDOW_SAMPLES:]
            latest = values[-1] if len(values) else 0.0
            rms = float(np.sqrt(np.mean(short_window * short_window))) if len(short_window) else 0.0
            self.table.item(channel, 2).setText(f"{latest * 1000:.6f}")
            self.table.item(channel, 3).setText(f"{rms * 1000:.6f}")
        self.counter_label.setText(f"{self.sample_count:,} samples")

    def select_channel(self, row: int, _column: int) -> None:
        self.selected_channel = row
        self.plot_title.setText(f"通道 {row + 1}")
        self.refresh_plots()

    def _auto_y_toggled(self, enabled: bool) -> None:
        self.y_range_spin.setEnabled(not enabled)
        self.zoom_in_button.setEnabled(not enabled)
        self.zoom_out_button.setEnabled(not enabled)
        self.refresh_plots()

    def _zoom_y_axis(self, factor: float) -> None:
        """Expand or contract the raw EMG range while retaining a sensible limit."""
        new_value = min(20.0, max(0.05, self.y_range_spin.value() * factor))
        self.y_range_spin.setValue(new_value)

    @staticmethod
    def _nice_y_limit(values: np.ndarray, minimum: float) -> float:
        """Choose a stable 1/2/5-based plotting range without outlier-driven jumps."""
        if not len(values):
            return minimum
        target = max(minimum, float(np.percentile(np.abs(values), 99.5)) * 1.25)
        exponent = 10 ** np.floor(np.log10(target))
        normalized = target / exponent
        multiplier = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
        return float(multiplier * exponent)

    @staticmethod
    def _rms_envelope(values: np.ndarray) -> np.ndarray:
        if len(values) < RMS_WINDOW_SAMPLES:
            return np.array([], dtype=np.float32)
        squared = values.astype(np.float64) ** 2
        cumulative = np.concatenate(([0.0], np.cumsum(squared)))
        return np.sqrt(
            (cumulative[RMS_WINDOW_SAMPLES:] - cumulative[:-RMS_WINDOW_SAMPLES])
            / RMS_WINDOW_SAMPLES
        ).astype(np.float32)

    def refresh_plots(self) -> None:
        duration = self.window_spin.value()
        count = int(SAMPLE_RATE * duration)
        values = np.asarray(
            list(self.history[self.selected_channel])[-count:], dtype=np.float32
        ) * 1000
        if self.auto_y_check.isChecked():
            y_limit = min(20.0, self._nice_y_limit(values, minimum=0.05))
            self.y_range_spin.blockSignals(True)
            self.y_range_spin.setValue(min(20.0, y_limit))
            self.y_range_spin.blockSignals(False)
        else:
            y_limit = self.y_range_spin.value()
        self.raw_plot.setXRange(-duration, 0, padding=0)
        self.raw_plot.setYRange(-y_limit, y_limit, padding=0)
        if not len(values):
            self.raw_curve.setData([], [])
            self.envelope_curve.setData([], [])
            return

        x = (np.arange(len(values), dtype=np.float32) - len(values) + 1) / SAMPLE_RATE
        self.raw_curve.setData(x, values)
        envelope = self._rms_envelope(values)
        if not len(envelope):
            self.envelope_curve.setData([], [])
            return
        envelope_x = x[RMS_WINDOW_SAMPLES - 1 :]
        envelope_limit = self._nice_y_limit(envelope, minimum=0.02)
        self.envelope_plot.setYRange(0, envelope_limit, padding=0)
        stride = 10  # Render a 200 Hz envelope; source data remains untouched.
        self.envelope_curve.setData(envelope_x[::stride], envelope[::stride])

    def _stream_error(self, message: str) -> None:
        self._set_status(f"串流錯誤：{message}", "error")
        QMessageBox.critical(self, "Trigno 串流錯誤", message)

    def _stream_finished(self) -> None:
        worker = self.stream_worker
        self.stream_worker = None
        if worker:
            worker.deleteLater()
        self._set_streaming(False)
        if not self.status_label.text().startswith("串流錯誤"):
            self._set_status("已停止", "idle")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.stream_worker:
            self.stream_worker.request_stop()
            if not self.stream_worker.wait(6000):
                event.ignore()
                QMessageBox.warning(self, "仍在停止", "請稍候 Server 結束連線後再關閉。")
                return
        event.accept()


def main() -> int:
    configure_console_encoding()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TrignoWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
