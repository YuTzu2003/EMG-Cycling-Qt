from __future__ import annotations
import csv
import threading
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import QThread, Signal
from modules.emg.sensor_mapping import SensorMapping, csvColumnNames
from .device_stream import CHANNEL_COUNT, TrignoClient, TrignoError

class ConnectionWorker(QThread):
    connected = Signal(str)
    base_ready = Signal(str)
    sensor_states_ready = Signal(object, object, object)
    failed = Signal(str)

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
    status_changed = Signal(str)
    connected = Signal(str)
    base_ready = Signal(str)
    sensor_states_ready = Signal(object, object, object)
    frames_ready = Signal(object)
    sample_rate_ready = Signal(float, int, float)
    collection_metadata_ready = Signal(object)
    collection_completed = Signal(int, float, float)
    failed = Signal(str)

    def __init__(self,host: str,output_path: Path | None,mappings: list[SensorMapping],) -> None:
        super().__init__()
        self.host, self.output_path, self.mappings = host, output_path, mappings
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        csv_file = None
        output_opened = False
        try:
            with TrignoClient(self.host) as client:
                self.status_changed.emit("正在連線…")
                self.connected.emit(client.connect())
                baseSerial = client.base_serial()
                self.base_ready.emit(baseSerial)
                sampleRate, samplesPerFrame, frameInterval = client.emg_frame_config()
                self.sample_rate_ready.emit(sampleRate, samplesPerFrame, frameInterval)
                active = client.active_sensors()
                if not active:
                    raise TrignoError("找不到啟用中的 EMG 感測器，請先在 Trigno Control Utility 啟用感測器。")
                serials = client.sensor_serials(active)
                self.sensor_states_ready.emit(active, active, serials)
                activeIndices = [slot - 1 for slot in active]
                activeMappings = [self.mappings[index] for index in activeIndices]
                self.collection_metadata_ready.emit({
                    "base_serial": baseSerial,
                    "sample_rate_hz": sampleRate,
                    "samples_per_frame": samplesPerFrame,
                    "frame_interval_seconds": frameInterval,
                    "channels": [
                        {
                            "slot": slot,
                            "sid": serials.get(slot, "Unknown SID"),
                            "chinese_name": self.mappings[slot - 1].chinese_name,
                            "english_name": self.mappings[slot - 1].english_name,
                            "abbreviation": self.mappings[slot - 1].abbreviation,
                        }
                        for slot in active
                    ],
                })
                writer = None
                if self.output_path is not None:
                    self.output_path.parent.mkdir(parents=True, exist_ok=True)
                    csv_file = self.output_path.open("x", newline="", encoding="utf-8-sig")
                    output_opened = True
                    writer = csv.writer(csv_file)
                    writer.writerow(["time_seconds", *csvColumnNames(activeMappings)])
                client.start()
                collectionStarted = time.monotonic()
                self.status_changed.emit("串流中")
                sampleIndex = 0
                buffer: list[tuple[float, ...]] = []
                last_emit = time.monotonic()
                while not self._stop_event.is_set():
                    frames = client.read_emg_frames()
                    for frame in frames:
                        timeSeconds = sampleIndex / sampleRate
                        if writer is not None:
                            writer.writerow([timeSeconds, *(frame[index] for index in activeIndices)])
                        sampleIndex += 1
                    buffer.extend(frames)
                    if buffer and (len(buffer) >= 100 or time.monotonic() - last_emit >= 0.05):
                        self.frames_ready.emit(np.asarray(buffer, dtype=np.float32))
                        buffer.clear()
                        last_emit = time.monotonic()
                if buffer:
                    self.frames_ready.emit(np.asarray(buffer, dtype=np.float32))
                client.stop()
                elapsedSeconds = time.monotonic() - collectionStarted
                self.collection_completed.emit(sampleIndex, elapsedSeconds, sampleRate)
        except (OSError, TrignoError, ValueError, FileExistsError) as exc:
            message = str(exc)
            if not output_opened and isinstance(exc, (OSError, FileExistsError)):
                message = f"CSV 輸出失敗：{message}"
            self.failed.emit(message)
        finally:
            if csv_file:
                csv_file.close()
