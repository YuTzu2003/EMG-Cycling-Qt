"""CSV ingestion shared by the website and desktop application."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np

from emg_rehab.emg.baseline import EMGPreprocessor, FilterSettings, RMSFeatureExtractor, load_raw_emg_csv
from emg_rehab.emg.sensor_mapping import SensorMapping, matchesCsvColumn
from emg_rehab.pedal.csv_import import load_pedal_csv
from emg_rehab.emg.session_analysis import add_point, compare_rms, new_session


def import_emg_csv(path: Path, mappings: list[SensorMapping], subject: dict,
                   baselines: dict | None = None, settings: FilterSettings | None = None) -> dict:
    baselines = baselines or {}
    settings = settings or FilterSettings()
    times, columns, sample_rate = load_raw_emg_csv(path, limit_seconds=None)
    channels = [(index, next((name for name in columns if matchesCsvColumn(mapping, name)), None))
                for index, mapping in enumerate(mappings)]
    channels = [(index, header) for index, header in channels if header is not None]
    if not channels:
        raise ValueError("CSV 沒有可辨識的 EMG 通道。")
    session = new_session(subject, baselines, sample_rate)
    session["filter"] = asdict(settings)
    session["raw_files"] = {"emg": path.name}
    session["channels"] = {
        str(index + 1): {"mapping_name": f"{mappings[index].english_name.strip()}|{mappings[index].abbreviation.strip()}",
                         "label": mappings[index].chinese_name, "sensor_serial": "Imported CSV"}
        for index, _ in channels}
    frames = np.column_stack([columns[header] for _, header in channels])
    preprocessor = EMGPreprocessor(len(channels), sample_rate, settings)
    extractor = RMSFeatureExtractor(len(channels), sample_rate)
    batch = max(1, round(sample_rate * .2))
    for start in range(0, len(frames), batch):
        end = min(len(frames), start + batch)
        rms_values = extractor.add(preprocessor.process(frames[start:end]))
        for compact, (index, _header) in enumerate(channels):
            mapping = mappings[index]
            record = baselines.get(str(index + 1))
            mapping_name = f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"
            compatible = bool(record and record.get("mapping_name") == mapping_name
                              and abs(float(record.get("sample_rate_hz", 0)) - sample_rate) <= .01
                              and record.get("filter") == asdict(settings))
            comparison = compare_rms(rms_values.get(compact), record.get("rms_mv") if compatible else None)
            if record and not compatible:
                comparison["quality"] = "基準不相容"
            add_point(session, "emg", {"time_s": float(times[end - 1]), "slot": index + 1,
                                        "rms_mv": rms_values.get(compact), **comparison})
    return session


def attach_pedal_csv(session: dict, path: Path) -> list[dict]:
    records = load_pedal_csv(path)
    for row in records:
        add_point(session, "pedal", {"time_s": row["session_seconds"], **row})
    session.setdefault("raw_files", {})["pedal"] = path.name
    return records
