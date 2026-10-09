from __future__ import annotations
import csv
import threading
import time
from pathlib import Path
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from .sensor_mapping import csvColumns, loadProjectMappings
from .trigno import CHANNEL_COUNT, SAMPLE_RATE, TrignoClient, TrignoError

class ConnectionWorker(QThread):
    connected = pyqtSignal(str)
    base_ready = pyqtSignal(str)
    sensor_states_ready = pyqtSignal(object, object, object)
    failed = pyqtSignal(str)

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    def run(self) -> None:
        try:
            with TrignoClient(self.host, timeout=3.0) as client:
                self.connected.emit(client.connect())
                self.base_ready.emit(client.base_serial())
                active = client.active_sensors()
                self.sensor_states_ready.emit(active, active, client.sensor_serials(active))
        except (OSError, TrignoError, ValueError) as exc:
            self.failed.emit(str(exc))

class StreamWorker(QThread):
    status_changed = pyqtSignal(str)
    connected = pyqtSignal(str)
    base_ready = pyqtSignal(str)
    sensor_states_ready = pyqtSignal(object, object, object)
    frames_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, host: str, output_path: Path | None) -> None:
        super().__init__()
        self.host, self.output_path = host, output_path
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        csv_file = None
        try:
            with TrignoClient(self.host) as client:
                self.status_changed.emit("正在連線…")
                self.connected.emit(client.connect())
                self.base_ready.emit(client.base_serial())
                # Start data collection before status discovery.  The EMG socket
                # buffers the initial frames while the short control queries run.
                client.start()
                self.status_changed.emit("串流中")
                active = client.active_sensors()
                self.sensor_states_ready.emit(active, active, client.sensor_serials(active))
                activeIndices = [slot - 1 for slot in active]
                writer, sample_index = None, 0
                if self.output_path:
                    self.output_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_file = self.output_path.open("x", newline="", encoding="utf-8")
                    writer = csv.writer(csv_file)
                    allColumns = csvColumns(loadProjectMappings())
                    writer.writerow(["sample", "time_seconds"] + [allColumns[index] for index in activeIndices])
                buffer: list[tuple[float, ...]] = []
                last_emit = time.monotonic()
                while not self._stop_event.is_set():
                    frames = client.read_emg_frames()
                    if writer:
                        for frame in frames:
                            writer.writerow(
                                [sample_index, sample_index / SAMPLE_RATE]
                                + [frame[index] for index in activeIndices]
                            )
                            sample_index += 1
                    buffer.extend(frames)
                    if buffer and (len(buffer) >= 100 or time.monotonic() - last_emit >= 0.05):
                        self.frames_ready.emit(np.asarray(buffer, dtype=np.float32))
                        buffer.clear()
                        last_emit = time.monotonic()
                if buffer:
                    self.frames_ready.emit(np.asarray(buffer, dtype=np.float32))
                client.stop()
        except (OSError, TrignoError, ValueError, FileExistsError) as exc:
            self.failed.emit(str(exc))
        finally:
            if csv_file:
                csv_file.close()