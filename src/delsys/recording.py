from __future__ import annotations
import csv
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from .sensor_mapping import SensorMapping, csvColumns

if TYPE_CHECKING:
    from .trainer import BikeTelemetry

@dataclass(frozen=True)
class SubjectInfo:
    patientId: str = ""
    name: str = ""
    gender: str = "未指定"

def writeSubjectMetadata(writer: csv.writer, subject: SubjectInfo) -> None:
    """Store the subject fields in the CSV preamble, never in the file name."""
    writer.writerow(["# patient_id", subject.patientId or "未填寫"])
    writer.writerow(["# patient_name", subject.name or "未填寫"])
    writer.writerow(["# gender", subject.gender or "未指定"])

def writeCsvPreamble(writer: csv.writer,*,baseSerial: str,sampleRate: float,samplesPerFrame: int,frameIntervalSeconds: float,activeSlots: Sequence[int],serials: dict[int, str],mappings: Sequence[SensorMapping],subject: SubjectInfo,) -> list[str]:
    columns = csvColumns(list(mappings))
    activeColumns = [columns[slot - 1] for slot in activeSlots]
    writer.writerow(["# format", "emg-cycling-csv-v1"])
    writer.writerow(["# emg_sample_rate_hz", f"{sampleRate:.6g}"])
    writer.writerow(["# samples_per_frame", samplesPerFrame])
    writer.writerow(["# frame_interval_seconds", f"{frameIntervalSeconds:.6g}"])
    writer.writerow(["# base_serial", baseSerial])
    writeSubjectMetadata(writer, subject)
    writer.writerow(["# active_slots", *[f"S{slot:02d}" for slot in activeSlots]])
    writer.writerow(["# channel_map", "slot", "sensor_sid", "abbreviation", "english_name", "chinese_name", "csv_column"])
    for slot in activeSlots:
        mapping = mappings[slot - 1]
        writer.writerow(["# channel",f"S{slot:02d}",serials.get(slot, "Unknown SID"),mapping.abbreviation,mapping.english_name,mapping.chinese_name,columns[slot - 1],])
    writer.writerow(["sample", "time_seconds", *activeColumns])
    return activeColumns


def writeCsvSummary(writer: csv.writer,*,samples: int,elapsedSeconds: float,sampleRate: float,) -> None:
    writer.writerow(["# collection_samples", samples])
    writer.writerow(["# collection_elapsed_seconds", f"{elapsedSeconds:.6f}"])
    writer.writerow(["# data_duration_seconds", f"{samples / sampleRate:.6f}" if sampleRate else "0"])


def trainerOutputPath(emgOutputPath: Path) -> Path:
    marker = "_EMG_"
    if marker in emgOutputPath.stem:
        return emgOutputPath.with_name(emgOutputPath.stem.replace(marker, "_Resistance_", 1) + emgOutputPath.suffix)
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Resistance{emgOutputPath.suffix}")

def infoOutputPath(emgOutputPath: Path) -> Path:
    marker = "_EMG_"
    if marker in emgOutputPath.stem:
        return emgOutputPath.with_name(emgOutputPath.stem.replace(marker, "_Info_", 1) + ".txt")
    return emgOutputPath.with_name(f"{emgOutputPath.stem}_Info.txt")

class CollectionInfoRecorder:
    def __init__(self, outputPath: Path, subject: SubjectInfo, emgPath: Path, trainerPath: Path | None) -> None:
        self.outputPath = outputPath
        self.subject = subject
        self.emgPath = emgPath
        self.trainerPath = trainerPath

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        with self.outputPath.open("x", encoding="utf-8") as output:
            output.write("EMG Cycling collection information\n")
            output.write(f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
            output.write("[Subject]\n")
            output.write(f"Patient ID: {self.subject.patientId or '未填寫'}\n")
            output.write(f"Name: {self.subject.name or '未填寫'}\n")
            output.write(f"Gender: {self.subject.gender or '未指定'}\n\n")
            output.write("[Output files]\n")
            output.write(f"EMG: {self.emgPath.name}\n")
            if self.trainerPath:
                output.write(f"Resistance: {self.trainerPath.name}\n")

    def append(self, heading: str, lines: Sequence[str]) -> None:
        with self.outputPath.open("a", encoding="utf-8") as output:
            output.write(f"\n[{heading}]\n")
            output.write("\n".join(lines) + "\n")

class TrainerCsvRecorder:
    def __init__(self, outputPath: Path, emgOutputPath: Path, subject: SubjectInfo) -> None:
        self.outputPath = outputPath
        self.emgOutputPath = emgOutputPath
        self.subject = subject
        self._file = None
        self._writer: csv.writer | None = None
        self._startedAt = 0.0
        self.rows = 0

    def start(self) -> None:
        self.outputPath.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.outputPath.open("x", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file)
        self._startedAt = time.monotonic()
        self._writer.writerow([
            "time_seconds", "resistance_level", "resistance_native_commanded",
            "resistance_native_reported", "speed_kph", "cadence_rpm", "power_watts",
            "heart_rate_bpm", "distance_meters", "energy_kcal", "trainer_elapsed_seconds",])

    def append(self, data: "BikeTelemetry", appResistanceLevel: int, commandedResistance: float | None) -> None:
        if self._writer is None or self._file is None:
            return
        self._writer.writerow([
            f"{time.monotonic() - self._startedAt:.6f}",
            appResistanceLevel, commandedResistance, data.resistance, data.speedKph,
            data.cadenceRpm, data.powerWatts, data.heartRate, data.distanceMeters, data.energyKcal,
            data.elapsedSeconds,
        ])
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