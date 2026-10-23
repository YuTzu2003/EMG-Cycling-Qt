from __future__ import annotations

import csv
import math
from pathlib import Path
from emg_rehab.pedal.analysis import MAX_SESSION_SECONDS


def _number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def is_calibrated_angle_csv(path: Path) -> bool:
    """Identify the calibrated angle format accepted by angle analysis."""
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        headers = next(csv.reader(stream), [])
    return {"t_session_s", "right_pedal_deg", "left_pedal_deg",
            "crank_revolutions", "pedaling"} <= set(headers)


def load_pedal_csv(path: Path) -> list[dict]:
    """Read this application's pedal recording for the summary and angle views."""
    required = {"session_seconds", "timestamp", "crank_revolutions", "crank_event_time"}
    columns = ("power_w", "cadence_rpm", "left_balance_percent",
               "right_balance_percent", "left_power_w", "right_power_w",
               "crank_revolutions", "crank_event_time")
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError("踏板 CSV 缺少欄位：" + "、".join(sorted(missing)))
        rows = []
        minimum = maximum = None
        for line, source in enumerate(reader, start=2):
            timestamp = _number(source.get("timestamp"))
            session_seconds = _number(source.get("session_seconds"))
            if timestamp is None or session_seconds is None:
                raise ValueError(f"踏板 CSV 第 {line} 行的時間無效。")
            minimum = timestamp if minimum is None else min(minimum, timestamp)
            maximum = timestamp if maximum is None else max(maximum, timestamp)
            if not math.isfinite(maximum - minimum) or maximum - minimum > MAX_SESSION_SECONDS:
                raise ValueError("踏板資料時間範圍不可超過 24 小時。")
            row = {key: _number(source.get(key)) for key in columns}
            row.update(timestamp=timestamp, session_seconds=session_seconds)
            if row["left_balance_percent"] is None and row["right_balance_percent"] is not None:
                row["left_balance_percent"] = 100 - row["right_balance_percent"]
            if row["right_balance_percent"] is None and row["left_balance_percent"] is not None:
                row["right_balance_percent"] = 100 - row["left_balance_percent"]
            rows.append(row)
    if not rows:
        raise ValueError("踏板 CSV 沒有資料列。")
    return rows
