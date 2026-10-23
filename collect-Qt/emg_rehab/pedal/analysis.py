from __future__ import annotations
import math
from collections.abc import Iterable, Mapping

MAX_SESSION_SECONDS = 24 * 60 * 60

def _number(record: Mapping, key: str) -> float | None:
    value = record.get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _values(records: Iterable[Mapping], key: str) -> list[float]:
    return [value for record in records if (value := _number(record, key)) is not None]

def summarize_pedal_records(records: Iterable[Mapping]) -> dict[str, float | int | None]:
    rows = [record for record in records if isinstance(record, Mapping)]
    timestamps = _values(rows, "timestamp")
    power = _values(rows, "power_w")
    cadence = _values(rows, "cadence_rpm")
    active = []
    last_cadence_at: float | None = None
    for record in sorted(rows, key=lambda row: _number(row, "timestamp") if _number(row, "timestamp") is not None else float("inf")):
        stamp = _number(record, "timestamp")
        if (_number(record, "cadence_rpm") or 0) > 0:
            last_cadence_at = stamp
        balance = _number(record, "left_balance_percent")
        if ((_number(record, "power_w") or 0) > 0 and balance is not None
                and 0 <= balance <= 100 and stamp is not None and last_cadence_at is not None
                and stamp - last_cadence_at <= 5):
            active.append(record)
    balance = _values(active, "left_balance_percent")
    left_power = _values(active, "left_power_w")
    right_power = _values(active, "right_power_w")

    def average(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None
    return {
        "sample_count": len(rows),
        "duration_seconds": max(timestamps) - min(timestamps) if len(timestamps) >= 2 else None,
        "average_power_w": average(power),
        "maximum_power_w": max(power) if power else None,
        "average_cadence_rpm": average(cadence),
        "maximum_cadence_rpm": max(cadence) if cadence else None,
        "average_left_balance_percent": average(balance),
        "average_right_balance_percent": None if not balance else 100 - average(balance),
        "average_left_power_w": average(left_power),
        "average_right_power_w": average(right_power),
    }


def pedal_summary_lines(summary: Mapping[str, float | int | None]) -> list[str]:
    def value(key: str, unit: str, digits: int = 1) -> str:
        number = summary.get(key)
        return "--" if number is None else f"{float(number):.{digits}f} {unit}"
    lines = [f"收集：{summary['sample_count']} 筆｜{value('duration_seconds', '秒')}"]
    lines.append(f"功率：平均 {value('average_power_w', 'W')}｜最高 {value('maximum_power_w', 'W', 0)}")
    lines.append(f"踏頻：平均 {value('average_cadence_rpm', 'rpm')}｜最高 {value('maximum_cadence_rpm', 'rpm', 0)}")
    lines.append(f"左右平衡：左 {value('average_left_balance_percent', '%')}｜右 {value('average_right_balance_percent', '%')}")
    lines.append(f"左右估計功率：左 {value('average_left_power_w', 'W')}｜右 {value('average_right_power_w', 'W')}")
    return lines

def analyze_pedal_records(records: Iterable[Mapping], ftp_w: int) -> dict:
    rows = [dict(record) for record in records if isinstance(record, Mapping)]
    rows.sort(key=lambda row: _number(row, "timestamp") if _number(row, "timestamp") is not None else float("inf"))
    timed = [row for row in rows if _number(row, "timestamp") is not None]
    if not timed:
        return {"series": [], "power_1s": [], "zones": [], "normalized_power_w": None,
                "intensity_factor": None, "tss": None, "energy_kj": None,
                "observed_seconds": 0, "missing_seconds": 0, "coverage_percent": None,
                "coasting_seconds": 0, "peak_5s_w": None, "peak_60s_w": None}
    span = _number(timed[-1], "timestamp") - _number(timed[0], "timestamp")
    if not math.isfinite(span) or span > MAX_SESSION_SECONDS:
        raise ValueError("踏板資料時間範圍不可超過 24 小時。")
    start = _number(timed[0], "timestamp") or 0.0
    series: list[dict[str, float | None]] = []
    last_cadence: float | None = None
    last_cadence_at: float | None = None
    bins: dict[int, list[float]] = {}
    for row in timed:
        elapsed = max(0.0, (_number(row, "timestamp") or start) - start)
        power = _number(row, "power_w")
        cadence = _number(row, "cadence_rpm")
        if cadence is not None:
            last_cadence = cadence
            last_cadence_at = elapsed
        display_cadence = last_cadence if (power or 0) > 0 and last_cadence_at is not None and elapsed - last_cadence_at <= 5 else None
        left = _number(row, "left_balance_percent")
        series.append({"elapsed_s": elapsed, "power_w": power, "cadence_rpm": display_cadence,
                       "left_balance_percent": left})
        if power is not None:
            bins.setdefault(int(elapsed), []).append(power)

    duration = int(max(point["elapsed_s"] or 0 for point in series))
    # Preserve missing seconds: a silent BLE link is not evidence of coasting.
    power_1s = [sum(bins[second]) / len(bins[second]) if second in bins else None
                for second in range(duration + 1)]
    observed = [value for value in power_1s if value is not None]
    def full_window_means(size: int) -> list[float]:
        means = []
        for index in range(size - 1, len(power_1s)):
            window = power_1s[index - size + 1:index + 1]
            if all(value is not None for value in window):
                means.append(sum(window) / size)
        return means
    rolling = full_window_means(30)
    normalized = (sum(value ** 4 for value in rolling) / len(rolling)) ** 0.25 if rolling else None
    ftp = max(0, int(ftp_w))
    intensity = normalized / ftp if normalized is not None and ftp else None
    coverage = len(observed) / len(power_1s) * 100
    tss = duration / 3600 * intensity ** 2 * 100 if intensity is not None and coverage >= 95 else None
    limits = [0, ftp * .55, ftp * .75, ftp * .90, ftp * 1.05, ftp * 1.20, ftp * 1.50]
    zone_seconds = [0] * 7
    for power in observed:
        zone = next((index for index, limit in enumerate(limits[1:]) if power <= limit), 6)
        zone_seconds[zone] += 1
    names = ("區間 1 主動恢復", "區間 2 耐力", "區間 3 節奏", "區間 4 閾值", "區間 5 最大攝氧量", "區間 6 無氧", "區間 7 神經肌肉爆發力")
    total = max(1, len(observed))
    zones = [{"name": name, "seconds": seconds, "percent": seconds / total * 100,
              "bounds": (f"≤ {limits[1]:.0f} W" if index == 0 else
                         f"> {limits[6]:.0f} W" if index == 6 else
                         f"> {limits[index]:.0f}–{limits[index + 1]:.0f} W")}
             for index, (name, seconds) in enumerate(zip(names, zone_seconds, strict=True))]
    return {"series": series, "power_1s": power_1s, "zones": zones,
            "normalized_power_w": normalized, "intensity_factor": intensity,
            "tss": tss, "energy_kj": sum(observed) / 1000,
            "observed_seconds": len(observed), "missing_seconds": len(power_1s) - len(observed),
            "coverage_percent": coverage, "coasting_seconds": sum(value == 0 for value in observed),
            "peak_5s_w": max(full_window_means(5), default=None),
            "peak_60s_w": max(full_window_means(60), default=None)}

def pedal_analysis_lines(records: Iterable[Mapping], ftp_w: int) -> list[str]:
    """Format advanced training values with their dependency on user-set FTP."""
    analysis = analyze_pedal_records(records, ftp_w)
    def value(key: str, unit: str, digits: int = 1) -> str:
        number = analysis[key]
        return "--" if number is None else f"{float(number):.{digits}f} {unit}"
    lines = [f"累積踩踏輸出：{value('energy_kj', 'kJ')}",
             f"短時間最高平均：5 秒 {value('peak_5s_w', 'W')}｜60 秒 {value('peak_60s_w', 'W')}",
             f"資料完整度：{analysis['observed_seconds']}／{analysis['observed_seconds'] + analysis['missing_seconds']} 秒（{value('coverage_percent', '%', 0)}）｜未收到 {analysis['missing_seconds']} 秒",
             f"零功率時間：{analysis['coasting_seconds']} 秒"]
    return lines
