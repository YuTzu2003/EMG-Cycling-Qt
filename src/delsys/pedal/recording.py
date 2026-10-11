from __future__ import annotations
import csv
import time
from pathlib import Path
from typing import TextIO

def pedalOutputPath(emgOutputPath: Path) -> Path:
    name = emgOutputPath.stem.replace("_EMG_", "_Pedal_", 1)
    if name == emgOutputPath.stem:
        name += "_Pedal"
    return emgOutputPath.with_name(name + emgOutputPath.suffix)

class PedalCsvRecorder:
    def __init__(self, outputPath: Path) -> None:
        self.outputPath = outputPath
        self.file: TextIO | None = None
        self.writer: csv.DictWriter | None = None
        self.startedAt = 0.0
        self.rows = 0
        self.records: list[dict] = []

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.outputPath.open("x", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.file, fieldnames=("session_seconds", "timestamp", "power_w", "cadence_rpm", "left_balance_percent", "right_balance_percent", "left_power_w", "right_power_w", "crank_revolutions", "crank_event_time", "raw_hex"))
        self.writer.writeheader()
        self.startedAt = time.monotonic()

    def append(self, data: dict) -> None:
        if self.writer is None or self.file is None:
            return
        self.writer.writerow({"session_seconds": f"{time.monotonic() - self.startedAt:.6f}", **data})
        self.file.flush()
        self.rows += 1
        self.records.append(dict(data))

    def close(self) -> float:
        if self.file is None:
            return 0.0
        elapsed = time.monotonic() - self.startedAt
        self.file.close()
        self.file = None
        self.writer = None
        return elapsed