from __future__ import annotations
import time

CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_FEATURE = "00002a65-0000-1000-8000-00805f9b34fb"
P715_NAME = "P715"

def decodeCyclingPower(data: bytearray, lastCrank: dict | None) -> tuple[dict, dict | None]:
    """Decode standard 0x2A63 packets and derive cadence from crank events."""
    packet = bytearray(data)
    if len(packet) < 4:
        raise ValueError("Cycling Power Measurement packet is shorter than 4 bytes")
    flags = int.from_bytes(packet[:2], "little")
    power = int.from_bytes(packet[2:4], "little", signed=True)
    offset = 4
    balance = None
    reference = None
    if flags & 0x0001 and offset < len(packet):
        balance = packet[offset] / 2
        reference = "left" if flags & 0x0002 else "unknown"
        offset += 1
    if flags & 0x0004:
        offset += 2
    if flags & 0x0010:
        offset += 6
    cadence = crankRevolutions = crankEventTime = None
    if flags & 0x0020 and offset + 4 <= len(packet):
        crankRevolutions = int.from_bytes(packet[offset : offset + 2], "little")
        crankEventTime = int.from_bytes(packet[offset + 2 : offset + 4], "little")
        if lastCrank is not None:
            turns = (crankRevolutions - lastCrank["revolutions"]) & 0xFFFF
            ticks = (crankEventTime - lastCrank["eventTime"]) & 0xFFFF
            if turns and ticks:
                cadence = turns * 60 * 1024 / ticks
        lastCrank = {"revolutions": crankRevolutions, "eventTime": crankEventTime}
    leftPower = rightPower = None
    if balance is not None and reference == "left":
        leftPower = power * balance / 100
        rightPower = power - leftPower
    return {
        "timestamp": time.time(), "power_w": power, "cadence_rpm": cadence,
        "left_balance_percent": balance if reference == "left" else None,
        "right_balance_percent": 100 - balance if balance is not None and reference == "left" else None,
        "left_power_w": leftPower, "right_power_w": rightPower,
        "crank_revolutions": crankRevolutions, "crank_event_time": crankEventTime,
        "raw_hex": packet.hex(" "),}, lastCrank
