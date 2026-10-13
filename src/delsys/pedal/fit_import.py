"""Read standard cycling record fields from a Garmin-compatible FIT file."""
from __future__ import annotations

import math
from pathlib import Path

from fit_tool import FitFile


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _left_balance(value: object) -> float | None:
    """Convert FIT's left/right-balance byte to a left-side percentage."""
    number = _finite_number(value)
    if number is None:
        return None
    raw = int(round(number))
    percentage = 100 - (raw & 0x7F) if raw & 0x80 else raw & 0x7F
    return float(percentage) if 0 <= percentage <= 100 else None


def load_pedal_fit(path: Path) -> list[dict]:
    """Return power/cadence/balance records suitable for the app's summary."""
    fit = FitFile.from_file(str(path))
    rows: list[dict] = []
    for record in fit.records:
        message = record.message
        if getattr(message, "name", "") != "record":
            continue
        timestamp = _finite_number(getattr(message, "timestamp", None))
        if timestamp is None:
            continue
        power = _finite_number(getattr(message, "power", None))
        cadence = _finite_number(getattr(message, "cadence", None))
        left = _left_balance(getattr(message, "left_right_balance", None))
        if power is None and cadence is None and left is None:
            continue
        row = {
            "timestamp": timestamp / 1000.0,
            "power_w": power,
            "cadence_rpm": cadence,
            "left_balance_percent": left,
            "right_balance_percent": None if left is None else 100 - left,
            "left_power_w": None,
            "right_power_w": None,
        }
        if power is not None and left is not None:
            row["left_power_w"] = power * left / 100.0
            row["right_power_w"] = power - row["left_power_w"]
        rows.append(row)
    if not rows:
        raise ValueError("This FIT file has no usable cycling record data.")
    return rows
