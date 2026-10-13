"""Small, dependency-free summaries for a recorded Cycling Power session."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


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
    """Return the same basic P715 session measures used by the example dashboard.

    Missing cadence and balance values are excluded rather than converted to
    zero. Left/right estimates are reported only for active, balanced packets.
    """
    rows = [record for record in records if isinstance(record, Mapping)]
    timestamps = _values(rows, "timestamp")
    power = _values(rows, "power_w")
    cadence = _values(rows, "cadence_rpm")
    active = [
        record for record in rows
        if (_number(record, "power_w") or 0) > 0
        and (_number(record, "left_balance_percent") or 0) > 0
        and _number(record, "cadence_rpm") is not None
    ]
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
    """Format a human-readable, non-clinical session summary."""
    def value(key: str, unit: str, digits: int = 1) -> str:
        number = summary.get(key)
        return "--" if number is None else f"{float(number):.{digits}f} {unit}"

    lines = [f"資料筆數：{summary['sample_count']}"]
    lines.append(f"收集時間：{value('duration_seconds', '秒')}")
    lines.append(f"平均功率：{value('average_power_w', 'W')}　最高功率：{value('maximum_power_w', 'W', 0)}")
    lines.append(f"平均踏頻：{value('average_cadence_rpm', 'rpm')}　最高踏頻：{value('maximum_cadence_rpm', 'rpm', 0)}")
    lines.append(f"左右平衡：左 {value('average_left_balance_percent', '%')}　右 {value('average_right_balance_percent', '%')}")
    lines.append(f"左右估計功率：左 {value('average_left_power_w', 'W')}　右 {value('average_right_power_w', 'W')}")
    return lines


def analyze_pedal_records(records: Iterable[Mapping], ftp_w: int) -> dict:
    """Derive the non-clinical cycling measures shown by the example dashboard.

    The P715 packets are irregular.  For NP/IF/TSS and power zones, this
    builds one-second power bins and carries the last valid cadence across the
    packets between crank events, matching the dashboard's intent without a
    pandas dependency.
    """
    rows = [dict(record) for record in records if isinstance(record, Mapping)]
    rows.sort(key=lambda row: _number(row, "timestamp") or float("inf"))
    timed = [row for row in rows if _number(row, "timestamp") is not None]
    if not timed:
        return {"series": [], "power_1s": [], "zones": [], "normalized_power_w": None,
                "intensity_factor": None, "tss": None, "energy_kj": None}

    start = _number(timed[0], "timestamp") or 0.0
    series: list[dict[str, float | None]] = []
    last_cadence: float | None = None
    bins: dict[int, list[float]] = {}
    for row in timed:
        elapsed = max(0.0, (_number(row, "timestamp") or start) - start)
        power = _number(row, "power_w")
        cadence = _number(row, "cadence_rpm")
        if cadence is not None:
            last_cadence = cadence
        display_cadence = last_cadence if (power or 0) > 0 else None
        left = _number(row, "left_balance_percent")
        series.append({"elapsed_s": elapsed, "power_w": power, "cadence_rpm": display_cadence,
                       "left_balance_percent": left})
        if power is not None:
            bins.setdefault(int(elapsed), []).append(power)

    duration = int(max(point["elapsed_s"] or 0 for point in series))
    power_1s = [sum(bins.get(second, [0.0])) / len(bins.get(second, [0.0])) for second in range(duration + 1)]
    rolling = [sum(power_1s[index - 29:index + 1]) / 30 for index in range(29, len(power_1s))]
    normalized = (sum(value ** 4 for value in rolling) / len(rolling)) ** 0.25 if rolling else (
        sum(power_1s) / len(power_1s) if power_1s else None
    )
    ftp = max(0, int(ftp_w))
    intensity = normalized / ftp if normalized is not None and ftp else None
    tss = duration / 3600 * intensity ** 2 * 100 if intensity is not None else None
    limits = [0, ftp * .55, ftp * .75, ftp * .90, ftp * 1.05, ftp * 1.20, ftp * 1.50]
    zone_seconds = [0] * 7
    for power in power_1s:
        zone = next((index for index, limit in enumerate(limits[1:]) if power <= limit), 6)
        zone_seconds[zone] += 1
    names = ("區間 1 主動恢復", "區間 2 耐力", "區間 3 節奏", "區間 4 閾值", "區間 5 最大攝氧量", "區間 6 無氧", "區間 7 神經肌肉爆發力")
    total = max(1, len(power_1s))
    zones = [{"name": name, "seconds": seconds, "percent": seconds / total * 100}
             for name, seconds in zip(names, zone_seconds, strict=True)]
    return {"series": series, "power_1s": power_1s, "zones": zones,
            "normalized_power_w": normalized, "intensity_factor": intensity,
            "tss": tss, "energy_kj": sum(power_1s) / 1000}


def pedal_analysis_lines(records: Iterable[Mapping], ftp_w: int) -> list[str]:
    """Format advanced training values with their dependency on user-set FTP."""
    analysis = analyze_pedal_records(records, ftp_w)
    def value(key: str, unit: str, digits: int = 1) -> str:
        number = analysis[key]
        return "--" if number is None else f"{float(number):.{digits}f} {unit}"
    lines = [f"FTP 設定：{ftp_w} W（可手動調整）",
             f"Normalized Power (NP)：{value('normalized_power_w', 'W')}",
             f"強度係數 (IF)：{value('intensity_factor', '', 3).strip()}",
             f"訓練壓力 (TSS)：{value('tss', '', 1).strip()}",
             f"機械功：{value('energy_kj', 'kJ')}", "", "功率區間時間分布："]
    lines.extend(f"{zone['name']}：{int(zone['seconds'])} 秒（{float(zone['percent']):.0f}%）" for zone in analysis["zones"])
    return lines
