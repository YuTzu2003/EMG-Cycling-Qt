from __future__ import annotations
import json
import math
import os
import tempfile
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

SETTINGS_VERSION = 1
CALIBRATION_SECONDS = 20.0
DISCARD_SECONDS = 10.0
FEATURE_METHOD = "filtered_rms_entire_final_10_seconds"

@dataclass(frozen=True)
class FilterSettings:
    highpass_hz: float = 20.0
    lowpass_hz: float = 450.0
    notch_hz: float | None = None

    def validate(self, sample_rate_hz: float) -> None:
        if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
            raise ValueError("Sample rate must be positive.")
        nyquist = sample_rate_hz / 2
        if not 0 < self.highpass_hz < self.lowpass_hz < nyquist:
            raise ValueError("EMG filter cutoffs must be below Nyquist and ordered.")
        if self.notch_hz is not None and not 0 < self.notch_hz < nyquist:
            raise ValueError("Notch frequency must be below Nyquist.")


class _Biquad:
    """Direct-form-II transposed biquad with persistent stream state."""
    def __init__(self, b: tuple[float, float, float], a1: float, a2: float) -> None:
        self.b0, self.b1, self.b2 = b
        self.a1, self.a2 = a1, a2
        self.z1 = self.z2 = 0.0

    def process(self, values: np.ndarray) -> np.ndarray:
        result = np.empty_like(values, dtype=np.float64)
        z1, z2 = self.z1, self.z2
        for index, value in enumerate(values):
            output = self.b0 * value + z1
            z1 = self.b1 * value - self.a1 * output + z2
            z2 = self.b2 * value - self.a2 * output
            result[index] = output
        self.z1, self.z2 = z1, z2
        return result


def _rbj_filter(kind: str, frequency: float, sample_rate_hz: float, q: float = math.sqrt(0.5)) -> _Biquad:
    omega = 2 * math.pi * frequency / sample_rate_hz
    cosine, sine = math.cos(omega), math.sin(omega)
    alpha = sine / (2 * q)
    if kind == "highpass":
        b0, b1, b2 = (1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2
    elif kind == "lowpass":
        b0, b1, b2 = (1 - cosine) / 2, 1 - cosine, (1 - cosine) / 2
    elif kind == "notch":
        b0, b1, b2 = 1.0, -2 * cosine, 1.0
    else:
        raise ValueError(f"Unsupported filter type: {kind}")
    a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    return _Biquad((b0 / a0, b1 / a0, b2 / a0), a1 / a0, a2 / a0)


class EMGPreprocessor:
    """Per-channel causal band-pass (and optional notch) filter in volts."""
    def __init__(self, channel_count: int, sample_rate_hz: float, settings: FilterSettings) -> None:
        settings.validate(sample_rate_hz)
        self.channel_count = channel_count
        self.sample_rate_hz = sample_rate_hz
        self.settings = settings
        self._filters = [
            [_rbj_filter("highpass", settings.highpass_hz, sample_rate_hz),
             _rbj_filter("lowpass", settings.lowpass_hz, sample_rate_hz)]
            + ([_rbj_filter("notch", settings.notch_hz, sample_rate_hz, q=30.0)] if settings.notch_hz else [])
            for _ in range(channel_count)
        ]

    def process(self, frames: np.ndarray) -> np.ndarray:
        values = np.asarray(frames, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.channel_count:
            raise ValueError("Expected a two-dimensional frame array for every EMG channel.")
        filtered = np.empty_like(values)
        for channel, filters in enumerate(self._filters):
            series = values[:, channel]
            for section in filters:
                series = section.process(series)
            filtered[:, channel] = series
        return filtered

    def metadata(self) -> dict:
        return {"sample_rate_hz": self.sample_rate_hz, "filter": asdict(self.settings), "unit": "mV"}


class RMSFeatureExtractor:
    def __init__(self, channel_count: int, sample_rate_hz: float, window_seconds: float = 0.2) -> None:
        self.window_samples = max(1, round(window_seconds * sample_rate_hz))
        self._buffers = [np.empty(0, dtype=np.float64) for _ in range(channel_count)]

    def add(self, filtered_frames: np.ndarray) -> dict[int, float | None]:
        results: dict[int, float | None] = {}
        for channel in range(filtered_frames.shape[1]):
            values = np.asarray(filtered_frames[:, channel], dtype=np.float64)
            self._buffers[channel] = np.concatenate((self._buffers[channel], values))[-self.window_samples:]
            buffer = self._buffers[channel]
            results[channel] = None if len(buffer) < self.window_samples else float(np.sqrt(np.mean(buffer * buffer)) * 1000)
        return results


@dataclass
class BaselineRecord:
    slot: int
    rms_mv: float
    mapping_name: str
    sensor_serial: str
    timestamp: str
    sample_rate_hz: float
    filter: dict
    method: str = FEATURE_METHOD
    source: str = "measured"
    protocol: str = ""
    reason: str = ""


class CalibrationSession:
    def __init__(self, sample_rate_hz: float, channels: set[int]) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.channels = channels
        self.discard_samples = round(DISCARD_SECONDS * sample_rate_hz)
        self.capture_samples = round((CALIBRATION_SECONDS - DISCARD_SECONDS) * sample_rate_hz)
        self.received_samples = 0
        self._values = {channel: [] for channel in channels}

    @property
    def complete(self) -> bool:
        return self.received_samples >= self.discard_samples + self.capture_samples

    @property
    def progress_seconds(self) -> float:
        return min(CALIBRATION_SECONDS, self.received_samples / self.sample_rate_hz)

    def add(self, filtered_frames: np.ndarray) -> None:
        if self.complete:
            return
        available = self.discard_samples + self.capture_samples - self.received_samples
        frames = filtered_frames[:available]
        start, end = self.received_samples, self.received_samples + len(frames)
        keep_start = max(start, self.discard_samples)
        if end > self.discard_samples:
            offset = keep_start - start
            kept = frames[offset:]
            for channel in self.channels:
                self._values[channel].append(kept[:, channel].copy())
        self.received_samples = end

    def rms_mv(self, channel: int) -> float | None:
        values = np.concatenate(self._values[channel]) if self._values[channel] else np.empty(0)
        if len(values) != self.capture_samples or not np.isfinite(values).all():
            return None
        rms = float(np.sqrt(np.mean(values * values)) * 1000)
        return rms if math.isfinite(rms) else None


class BaselineManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[int, BaselineRecord] = {}
        self.warning_percent = 20.0
        self.focus_percent = 40.0
        self.thresholds_enabled = False
        self.protocol = ""
        self.filter_settings = asdict(FilterSettings())
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != SETTINGS_VERSION:
                return
            self.warning_percent = float(data.get("warning_percent", 20.0))
            self.focus_percent = float(data.get("focus_percent", 40.0))
            self.thresholds_enabled = bool(data.get("thresholds_enabled", False))
            self.protocol = str(data.get("protocol", ""))
            self.filter_settings = dict(data.get("filter_settings", asdict(FilterSettings())))
            FilterSettings(**self.filter_settings)
            self.records = {int(item["slot"]): BaselineRecord(**item) for item in data.get("baselines", [])}
            self._validate_thresholds()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.records = {}
            self.filter_settings = asdict(FilterSettings())

    def _validate_thresholds(self) -> None:
        if not 0 < self.warning_percent < self.focus_percent:
            raise ValueError("Warning threshold must be below focus threshold.")

    def save(self) -> None:
        self._validate_thresholds()
        data = {"version": SETTINGS_VERSION, "warning_percent": self.warning_percent,
                "focus_percent": self.focus_percent, "thresholds_enabled": self.thresholds_enabled,
                "protocol": self.protocol, "filter_settings": self.filter_settings,
                "baselines": [asdict(record) for record in self.records.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=self.path.stem, suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, self.path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def set_manual(self, slot: int, rms_mv: float, mapping_name: str, sensor_serial: str, sample_rate_hz: float, settings: FilterSettings, reason: str) -> None:
        if not math.isfinite(rms_mv) or rms_mv <= 0:
            raise ValueError("Baseline RMS must be greater than zero.")
        self.records[slot] = BaselineRecord(slot, rms_mv, mapping_name, sensor_serial, datetime.now(timezone.utc).isoformat(), sample_rate_hz, asdict(settings), source="manual", protocol=self.protocol, reason=reason)
        self.save()

    def set_measured(self, slot: int, rms_mv: float, mapping_name: str, sensor_serial: str, sample_rate_hz: float, settings: FilterSettings) -> None:
        self.records[slot] = BaselineRecord(slot, rms_mv, mapping_name, sensor_serial, datetime.now(timezone.utc).isoformat(), sample_rate_hz, asdict(settings), protocol=self.protocol)

    def set_imported(self, slot: int, rms_mv: float, mapping_name: str, sample_rate_hz: float, settings: FilterSettings, source_path: str) -> None:
        self.records[slot] = BaselineRecord(
            slot, rms_mv, mapping_name, "Imported CSV", datetime.now(timezone.utc).isoformat(),
            sample_rate_hz, asdict(settings), source="imported_csv", protocol=self.protocol,
            reason=source_path,
        )

    def compatible(self, record: BaselineRecord, mapping_name: str, sensor_serial: str, sample_rate_hz: float, settings: FilterSettings) -> bool:
        return (record.mapping_name == mapping_name
                and (record.sensor_serial in {"Imported CSV", "Manual mapping edit"} or record.sensor_serial == sensor_serial)
                and math.isclose(record.sample_rate_hz, sample_rate_hz, rel_tol=0, abs_tol=0.01)
                and record.filter == asdict(settings))

    def evaluate(self, slot: int, current_mv: float | None, mapping_name: str, sensor_serial: str, sample_rate_hz: float, settings: FilterSettings) -> dict:
        record = self.records.get(slot)
        result = {"current_mv": current_mv, "baseline_mv": record.rms_mv if record else None,
                  "delta_mv": None, "percent": None, "state": "unknown", "text": "無基準值"}
        if current_mv is None or not math.isfinite(current_mv):
            result["text"] = "資料不足"
        elif record is None:
            pass
        elif not self.compatible(record, mapping_name, sensor_serial, sample_rate_hz, settings):
            result["text"] = "基準不相容"
        elif record.rms_mv <= 1e-9:
            result["text"] = "基準值過低"
        else:
            delta = current_mv - record.rms_mv
            percent = delta / record.rms_mv * 100
            result.update(delta_mv=delta, percent=percent, state="normal", text="正常（研究提示）")
            if self.thresholds_enabled:
                magnitude = abs(percent)
                if magnitude >= self.focus_percent:
                    result.update(state="focus", text="重點察看（非診斷）")
                elif magnitude >= self.warning_percent:
                    result.update(state="warning", text="警告（非診斷）")
        return result


def load_raw_emg_csv(path: Path, limit_seconds: float | None = CALIBRATION_SECONDS) -> tuple[np.ndarray, dict[str, np.ndarray], float]:
    """Load a raw EMG CSV written by StreamWorker without changing it.

    The file must contain ``time_seconds`` followed by one or more numeric EMG
    columns.  By default, return the continuous first 20-second segment for
    baseline calibration.  Pass ``limit_seconds=None`` to load the full session
    for offline RMS analysis.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or "time_seconds" not in reader.fieldnames:
                raise ValueError("CSV must contain a time_seconds column.")
            names = [name for name in reader.fieldnames if name != "time_seconds"]
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        raise ValueError(f"Unable to read CSV: {exc}") from exc
    if not names or len(rows) < 3:
        raise ValueError("CSV has insufficient EMG samples.")
    try:
        times = np.asarray([float(row["time_seconds"]) for row in rows], dtype=np.float64)
        columns = {name: np.asarray([float(row[name]) for row in rows], dtype=np.float64) for name in names}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CSV EMG values must be numeric.") from exc
    if not np.isfinite(times).all() or any(not np.isfinite(values).all() for values in columns.values()):
        raise ValueError("CSV contains NaN or infinite values.")
    intervals = np.diff(times)
    if np.any(intervals <= 0):
        raise ValueError("CSV time_seconds must be strictly increasing.")
    sample_rate_hz = 1.0 / float(np.median(intervals))
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("Unable to determine CSV sample rate.")
    relative = times - times[0]
    if limit_seconds is None:
        keep = np.ones(len(relative), dtype=bool)
    else:
        if not math.isfinite(limit_seconds) or limit_seconds <= 0:
            raise ValueError("CSV limit_seconds must be positive or None.")
        keep = relative < limit_seconds
        if relative[keep][-1] < limit_seconds - (2 / sample_rate_hz):
            raise ValueError(f"CSV needs a continuous first {limit_seconds:g}-second EMG segment.")
    return relative[keep], {name: values[keep] for name, values in columns.items()}, sample_rate_hz
