"""P715 Bluetooth identifiers and packet decoders.

This module is deliberately UI-free so decoding can also be used from a
notebook or a future command-line collector.
"""
from __future__ import annotations

import time

CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_FEATURE = "00002a65-0000-1000-8000-00805f9b34fb"
ADVANCED_DATA_STREAM = "ec22dc02-dd0f-4df7-b022-7c41471bcc7a"
TARGET_TEXT = "P715"
ADVANCED_NOTIFY_CHARACTERISTICS = [
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    "8ce5cc02-0a4d-11e9-ab14-d663bd873d93",
    "8ce5cc03-0a4d-11e9-ab14-d663bd873d93",
    "ec22dc02-dd0f-4df7-b022-7c41471bcc7a",
    "8ce5ee02-0a4d-11e9-ab14-d663bd873d93",
]


def decode_p715(data: bytearray, last_crank: dict | None) -> tuple[dict, dict | None]:
    """Decode the standard Bluetooth Cycling Power Measurement (0x2A63)."""
    d = bytearray(data)
    if len(d) < 4:
        raise ValueError("Cycling Power packet is shorter than 4 bytes")

    flags = int.from_bytes(d[0:2], "little")
    power = int.from_bytes(d[2:4], "little", signed=True)
    position = 4
    balance = None
    balance_reference = None
    cadence = None
    crank_revs = None
    crank_event_time = None

    if flags & 0x0001 and position < len(d):
        balance = d[position] / 2
        balance_reference = "left" if flags & 0x0002 else "unknown"
        position += 1
    if flags & 0x0004:
        position += 2
    if flags & 0x0010:
        position += 6
    if flags & 0x0020 and position + 4 <= len(d):
        crank_revs = int.from_bytes(d[position:position + 2], "little")
        crank_event_time = int.from_bytes(d[position + 2:position + 4], "little")
        if last_crank is not None:
            rev_diff = (crank_revs - last_crank["revs"]) % 65536
            time_diff = (crank_event_time - last_crank["time"]) % 65536
            if rev_diff > 0 and time_diff > 0:
                cadence = rev_diff * 60 * 1024 / time_diff
        last_crank = {"revs": crank_revs, "time": crank_event_time}

    left_power = right_power = None
    if balance is not None and balance_reference == "left":
        left_power = power * balance / 100
        right_power = power - left_power

    return {
        "timestamp": time.time(),
        "power_w": power,
        "cadence_rpm": cadence,
        "balance_percent": balance,
        "balance_reference": balance_reference,
        "left_power_w": left_power,
        "right_power_w": right_power,
        "crank_revs": crank_revs,
        "crank_event_time": crank_event_time,
        "sync_reference_hex": d[-4:].hex(" "),
        "raw_hex": d.hex(" "),
    }, last_crank


def decode_advanced_packet(data: bytearray) -> dict:
    """Preserve a vendor packet without claiming unverified field meanings."""
    d = bytearray(data)
    if len(d) < 6:
        raise ValueError("Advanced packet is shorter than its crank reference")
    result = {
        "advanced_timestamp": time.time(),
        "frame_type_hex": d[0:2].hex(" "),
        "sync_reference_hex": d[2:6].hex(" "),
        "payload_hex": d[6:].hex(" "),
        "raw_hex": d.hex(" "),
    }
    for index, value in enumerate(d[6:], start=6):
        result[f"payload_byte_{index:02d}"] = value
    return result
