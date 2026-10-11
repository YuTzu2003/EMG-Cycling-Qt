from __future__ import annotations
import csv
import time
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from .trainer import BikeTelemetry

def trainerOutputPath(emgOutputPath: Path) -> Path:
    """Return the paired resistance CSV path for an EMG collection path."""
    marker = "_EMG_"
    if marker in emgOutputPath.stem:
        name = emgOutputPath.stem.replace(marker, "_Resistance_", 1)
        return emgOutputPath.with_name(name + emgOutputPath.suffix)
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Resistance{emgOutputPath.suffix}")

class TrainerCsvRecorder:
    header = (
        "time_seconds",
        "resistance_level",
        "resistance_native_commanded",
        "resistance_native_reported",
        "speed_kph",
        "cadence_rpm",
        "power_watts",
        "heart_rate_bpm",
        "distance_meters",
        "session_distance_meters",
        "energy_kcal",
        "trainer_elapsed_seconds",
    )

    def __init__(self, outputPath: Path) -> None:
        self.outputPath = outputPath
        self._file: TextIO | None = None
        self._writer: csv.writer | None = None
        self._startedAt = 0.0
        self.rows = 0

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.outputPath.open("x", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file)
        self._startedAt = time.monotonic()
        self._writer.writerow(self.header)

    def append(
        self,
        data: BikeTelemetry,
        appResistanceLevel: int,
        commandedResistance: float | None,
        sessionDistanceMeters: int | None,
    ) -> None:
        if self._writer is None or self._file is None:
            return
        self._writer.writerow(
            (
                f"{time.monotonic() - self._startedAt:.6f}",
                appResistanceLevel,
                commandedResistance,
                data.resistance,
                data.speedKph,
                data.cadenceRpm,
                data.powerWatts,
                data.heartRate,
                data.distanceMeters,
                sessionDistanceMeters,
                data.energyKcal,
                data.elapsedSeconds,
            )
        )
        self.rows += 1
        self._file.flush()

    def close(self) -> float:
        if self._file is None:
            return 0.0
        elapsed = time.monotonic() - self._startedAt
        self._file.close()
        self._file = None
        self._writer = None
        return elapsed
