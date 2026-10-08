#!/usr/bin/env python3
"""Simple Qt monitor for the Trigno SDK Server EMG stream."""

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
    QApplication, QCheckBox, QFileDialog, QHeaderView, QHBoxLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from trigno_emg import TrignoClient, TrignoError, configure_console_encoding

CHANNEL_COUNT = 16
SAMPLE_RATE = 2000.0


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
        emit_buffer = []
        last_emit = time.monotonic()
        try:
            self.status_changed.emit("正在連線...")
            with client:
                self.server_connected.emit(client.connect())
                self.status_changed.emit("正在讀取感測器狀態...")
                self.sensor_states_ready.emit(*client.sensor_states(CHANNEL_COUNT))

                if self.output_path:
                    self.output_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_file = self.output_path.open("x", newline="", encoding="utf-8")
                    writer = csv.writer(csv_file)
                    writer.writerow(["sample", "time_seconds"] + [f"sensor_{i}_emg" for i in range(1, 17)])

                client.start()
                self.status_changed.emit("資料接收中")
                while not self._stop_event.is_set():
                    frames = client.read_emg_frames(CHANNEL_COUNT)
                    if not frames:
                        continue
                    if writer:
                        for frame in frames:
                            writer.writerow([sample_index, sample_index / SAMPLE_RATE, *frame])
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
        self.resize(1050, 680)
        self.worker: StreamWorker | None = None
        self.sample_count = 0
        self.selected_channel = 0
        self.history = [deque(maxlen=20_000) for _ in range(CHANNEL_COUNT)]
        self._build_ui()
        self._set_running(False)

    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        row = QHBoxLayout()
        title = QLabel("Trigno EMG", objectName="title")
        self.status_label = QLabel("待機", objectName="status")
        self.spec_label = QLabel("TCP 2000 Hz  ·  float32", objectName="muted")
        row.addWidget(title); row.addWidget(self.status_label); row.addStretch(); row.addWidget(self.spec_label)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.host_edit = QLineEdit("127.0.0.1"); self.host_edit.setMaximumWidth(190)
        self.start_button = QPushButton("開始", objectName="primary")
        self.stop_button = QPushButton("停止")
        row.addWidget(QLabel("Server")); row.addWidget(self.host_edit)
        row.addWidget(self.start_button); row.addWidget(self.stop_button); row.addStretch()
        layout.addLayout(row)

        row = QHBoxLayout()
        self.record_check = QCheckBox("儲存 CSV"); self.record_check.setChecked(True)
        self.path_edit = QLineEdit(self._default_output_path())
        self.browse_button = QPushButton("瀏覽")
        row.addWidget(self.record_check); row.addWidget(self.path_edit); row.addWidget(self.browse_button)
        layout.addLayout(row)

        content = QHBoxLayout(); content.setSpacing(14)
        self.table = QTableWidget(CHANNEL_COUNT, 4)
        self.table.setHorizontalHeaderLabels(["通道", "狀態", "目前值 (mV)", "RMS (mV)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for column in range(1, 4):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        for r in range(CHANNEL_COUNT):
            for c, value in enumerate((str(r + 1), "未連線", "0", "0")):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.selectRow(0)
        content.addWidget(self.table, 4)

        plot_column = QVBoxLayout()
        row = QHBoxLayout()
        self.plot_title = QLabel("通道 1", objectName="plotTitle")
        self.window_spin = QSpinBox(); self.window_spin.setRange(1, 10); self.window_spin.setValue(3); self.window_spin.setSuffix(" 秒")
        self.y_range_spin = QDoubleSpinBox()
        self.y_range_spin.setRange(0.05, 20.0)
        self.y_range_spin.setDecimals(2)
        self.y_range_spin.setSingleStep(0.25)
        self.y_range_spin.setValue(1.0)
        self.y_range_spin.setPrefix("± ")
        self.y_range_spin.setSuffix(" mV")
        self.counter_label = QLabel("0 samples", objectName="muted")
        row.addWidget(self.plot_title); row.addStretch()
        row.addWidget(QLabel("範圍")); row.addWidget(self.y_range_spin)
        row.addWidget(self.window_spin); row.addWidget(self.counter_label)
        plot_column.addLayout(row)
        self.plot = pg.PlotWidget(background="#ffffff")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setLabel("bottom", "時間", units="s")
        self.plot.setLabel("left", "EMG", units="mV")
        self.plot.disableAutoRange(axis="y")
        self.plot.setYRange(-1.0, 1.0, padding=0)
        self.curve = self.plot.plot(pen=pg.mkPen("#2563eb", width=1.4))
        plot_column.addWidget(self.plot)
        content.addLayout(plot_column, 7)
        layout.addLayout(content)

        self.setStyleSheet("""
            QWidget#root { background: #f7f8fa; color: #202124; }
            QLabel#title { font-size: 22px; font-weight: 700; }
            QLabel#status { padding: 4px 10px; background: #e5e7eb; border-radius: 9px; }
            QLabel#muted { color: #6b7280; }
            QLabel#plotTitle { font-size: 16px; font-weight: 600; }
            QLineEdit, QSpinBox { padding: 6px; border: 1px solid #d1d5db; border-radius: 5px; background: white; }
            QPushButton { padding: 7px 18px; border: 1px solid #cbd5e1; border-radius: 5px; background: white; }
            QPushButton#primary { color: white; border: 0; background: #2563eb; }
            QPushButton:disabled { color: #9ca3af; background: #e5e7eb; }
            QTableWidget { border: 1px solid #e5e7eb; background: white; gridline-color: #eef0f2; }
            QHeaderView::section { padding: 6px; border: 0; background: #eef2f7; font-weight: 600; }
        """)

        self.start_button.clicked.connect(self.start_stream)
        self.stop_button.clicked.connect(self.stop_stream)
        self.browse_button.clicked.connect(self.choose_output)
        self.record_check.toggled.connect(self._record_toggled)
        self.table.cellClicked.connect(self.select_channel)
        self.window_spin.valueChanged.connect(self.refresh_plot)
        self.y_range_spin.valueChanged.connect(self.refresh_plot)
        self.plot_timer = QTimer(self); self.plot_timer.setInterval(50)
        self.plot_timer.timeout.connect(self.refresh_plot); self.plot_timer.start()

    @staticmethod
    def _default_output_path() -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(Path.cwd() / "recordings" / f"emg_{stamp}.csv")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running); self.stop_button.setEnabled(running)
        self.host_edit.setEnabled(not running); self.record_check.setEnabled(not running)
        enabled = not running and self.record_check.isChecked()
        self.path_edit.setEnabled(enabled); self.browse_button.setEnabled(enabled)

    def _record_toggled(self, checked: bool) -> None:
        enabled = checked and self.worker is None
        self.path_edit.setEnabled(enabled); self.browse_button.setEnabled(enabled)

    def choose_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "選擇 CSV 儲存位置", self.path_edit.text(), "CSV (*.csv)")
        if path:
            self.path_edit.setText(path)

    def start_stream(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server 位址。")
            return
        output = None
        if self.record_check.isChecked():
            raw = self.path_edit.text().strip()
            if not raw:
                QMessageBox.warning(self, "缺少檔名", "請選擇 CSV 儲存位置。")
                return
            output = Path(raw).expanduser()
            if output.exists():
                QMessageBox.warning(self, "檔案已存在", "請選擇新的 CSV 檔名。")
                return
        self.sample_count = 0
        for values in self.history:
            values.clear()
        self._reset_sensor_states()
        self.worker = StreamWorker(host, output)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.server_connected.connect(self._server_connected)
        self.worker.sensor_states_ready.connect(self._sensor_states)
        self.worker.frames_ready.connect(self._frames_received)
        self.worker.stream_error.connect(self._stream_error)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True); self.worker.start()

    def stop_stream(self) -> None:
        if self.worker:
            self.status_label.setText("正在停止..."); self.stop_button.setEnabled(False)
            self.worker.request_stop()

    def _server_connected(self, banner: str) -> None:
        self.status_label.setText("已連線"); self.status_label.setToolTip(banner)

    def _reset_sensor_states(self) -> None:
        for row in range(CHANNEL_COUNT):
            self.table.item(row, 1).setText("查詢中")
            self.table.item(row, 2).setText("0"); self.table.item(row, 3).setText("0")

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
            one_second = values[-int(SAMPLE_RATE):]
            latest = values[-1] if len(values) else 0.0
            rms = float(np.sqrt(np.mean(one_second * one_second))) if len(one_second) else 0.0
            self.table.item(channel, 2).setText(f"{latest * 1000:.6f}")
            self.table.item(channel, 3).setText(f"{rms * 1000:.6f}")
        self.counter_label.setText(f"{self.sample_count:,} samples")

    def select_channel(self, row: int, _column: int) -> None:
        self.selected_channel = row; self.plot_title.setText(f"通道 {row + 1}"); self.refresh_plot()

    def refresh_plot(self) -> None:
        count = int(SAMPLE_RATE * self.window_spin.value())
        values = np.asarray(list(self.history[self.selected_channel])[-count:], dtype=np.float32) * 1000
        y_limit = self.y_range_spin.value()
        self.plot.setYRange(-y_limit, y_limit, padding=0)
        if not len(values):
            self.curve.setData([], []); return
        x = (np.arange(len(values), dtype=np.float32) - len(values) + 1) / SAMPLE_RATE
        self.curve.setData(x, values)

    def _stream_error(self, message: str) -> None:
        self.status_label.setText("錯誤"); QMessageBox.critical(self, "Trigno 串流錯誤", message)

    def _worker_finished(self) -> None:
        worker = self.worker; self.worker = None; self._set_running(False)
        if worker:
            worker.deleteLater()
        if self.status_label.text() != "錯誤":
            self.status_label.setText("已停止")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker:
            self.worker.request_stop()
            if not self.worker.wait(6000):
                event.ignore(); QMessageBox.warning(self, "仍在停止", "請稍候 Server 結束連線後再關閉。")
                return
        event.accept()


def main() -> int:
    configure_console_encoding()
    app = QApplication(sys.argv); app.setStyle("Fusion")
    window = TrignoWindow(); window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
