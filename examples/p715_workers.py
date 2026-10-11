"""Qt worker threads for P715 Bluetooth operations."""
from __future__ import annotations

import asyncio
import threading
import time

from bleak import BleakClient, BleakScanner
from PySide6.QtCore import QThread, Signal

from examples.p715_protocol import (
    ADVANCED_DATA_STREAM,
    ADVANCED_NOTIFY_CHARACTERISTICS,
    CYCLING_POWER_FEATURE,
    CYCLING_POWER_MEASUREMENT,
    TARGET_TEXT,
    decode_advanced_packet,
    decode_p715,
)


class ScanWorker(QThread):
    found = Signal(list)
    status = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.status.emit("正在掃描附近的藍牙裝置…")
            devices = asyncio.run(BleakScanner.discover(timeout=8, return_adv=True))
            choices = []
            for address, (device, adv) in devices.items():
                name = adv.local_name or device.name or "未命名裝置"
                if TARGET_TEXT.lower() in name.lower():
                    choices.append({"name": name, "address": address, "rssi": adv.rssi})
            self.found.emit(choices)
        except Exception as exc:
            self.failed.emit(str(exc))


class ConnectionCheckWorker(QThread):
    verified = Signal(str)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, address: str) -> None:
        super().__init__()
        self.address = address

    def run(self) -> None:
        try:
            asyncio.run(self._check())
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _check(self) -> None:
        self.status.emit("正在測試 P715 連線…")
        async with BleakClient(self.address) as client:
            await client.read_gatt_char(CYCLING_POWER_FEATURE)
        self.verified.emit(self.address)


class GattProbeWorker(QThread):
    status = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, address: str) -> None:
        super().__init__()
        self.address = address

    def run(self) -> None:
        try:
            asyncio.run(self._probe())
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _probe(self) -> None:
        self.status.emit("正在連線並探測 P715 的完整藍牙服務…")
        known = {
            "00001818-0000-1000-8000-00805f9b34fb": "Cycling Power Service",
            "00001800-0000-1000-8000-00805f9b34fb": "Generic Access Service",
            "00001801-0000-1000-8000-00805f9b34fb": "Generic Attribute Service",
            "0000180a-0000-1000-8000-00805f9b34fb": "Device Information Service",
            "00002a5d-0000-1000-8000-00805f9b34fb": "Sensor Location",
            "00002a63-0000-1000-8000-00805f9b34fb": "Cycling Power Measurement",
            "00002a65-0000-1000-8000-00805f9b34fb": "Cycling Power Feature",
            "00002a66-0000-1000-8000-00805f9b34fb": "Cycling Power Control Point",
            "0000180f-0000-1000-8000-00805f9b34fb": "Battery Service",
            "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
            "00002a00-0000-1000-8000-00805f9b34fb": "Device Name",
            "00002a01-0000-1000-8000-00805f9b34fb": "Appearance",
            "00002a04-0000-1000-8000-00805f9b34fb": "Connection Parameters",
            "00002aa6-0000-1000-8000-00805f9b34fb": "Central Address Resolution",
            "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
            "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
            "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
            "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
            "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
            "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
            "0000fe59-0000-1000-8000-00805f9b34fb": "Nordic Secure DFU (不要寫入)",
            "8ec90003-f315-4f60-9fb8-838830daea50": "DFU Control Point (不要寫入)",
            "6e400001-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART Service",
            "6e400002-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART RX (寫入端)",
            "6e400003-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART TX (通知端)",
        }
        async with BleakClient(self.address) as client:
            lines = ["P715 完整藍牙服務探測結果", "", "判讀方式：若看到非標準 UUID、或含 notify 的未知特徵值，可能是進階數據通道。", ""]
            for service in client.services:
                lines.append(f"Service: {service.uuid}  [{known.get(service.uuid.lower(), '未知／廠商服務')}]")
                for characteristic in service.characteristics:
                    name = known.get(characteristic.uuid.lower(), "未知／廠商特徵值")
                    lines.append(f"  └─ {characteristic.uuid}  [{name}]  ({', '.join(characteristic.properties)})")
                    for descriptor in characteristic.descriptors:
                        lines.append(f"      descriptor: {descriptor.uuid}")
                lines.append("")
        self.completed.emit("\n".join(lines))


class AdvancedMonitorWorker(QThread):
    status = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, address: str, seconds: int = 30) -> None:
        super().__init__()
        self.address = address
        self.seconds = seconds
        self.packets: dict[str, list[tuple[float, str]]] = {uuid: [] for uuid in ADVANCED_NOTIFY_CHARACTERISTICS}

    def run(self) -> None:
        try:
            asyncio.run(self._monitor())
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _monitor(self) -> None:
        self.status.emit(f"正在被動監聽進階資料 {self.seconds} 秒。請持續踩踏。")
        subscribed: list[str] = []
        async with BleakClient(self.address) as client:
            available = {c.uuid.lower() for service in client.services for c in service.characteristics}
            for uuid in ADVANCED_NOTIFY_CHARACTERISTICS:
                if uuid not in available:
                    continue

                def callback(_sender: int, data: bytearray, source_uuid: str = uuid) -> None:
                    self.packets[source_uuid].append((time.time(), bytes(data).hex(" ")))

                await client.start_notify(uuid, callback)
                subscribed.append(uuid)
            if not subscribed:
                raise RuntimeError("沒有可監聽的廠商通知特徵值。")
            await asyncio.sleep(self.seconds)
            for uuid in subscribed:
                await client.stop_notify(uuid)

        lines = ["P715 進階資料被動監聽結果（未寫入任何指令）", "", "如果某 UUID 在踩踏時有封包，請把整段結果貼回來，我們再判讀格式。", ""]
        for uuid in ADVANCED_NOTIFY_CHARACTERISTICS:
            packets = self.packets[uuid]
            lines.extend([uuid, f"  收到封包：{len(packets)} 筆"])
            lines.extend(f"  {hex_data}" for _timestamp, hex_data in packets[:8])
            if len(packets) > 8:
                lines.append(f"  …略過其餘 {len(packets) - 8} 筆")
            lines.append("")
        self.completed.emit("\n".join(lines))


class RecordWorker(QThread):
    data_received = Signal(dict)
    status = Signal(str)
    collection_started = Signal(int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, address: str, duration_seconds: int) -> None:
        super().__init__()
        self.address = address
        self.duration_seconds = duration_seconds
        self.stop_requested = threading.Event()
        self.records: list[dict] = []
        self.advanced_records: list[dict] = []
        self.last_crank: dict | None = None

    def stop(self) -> None:
        self.stop_requested.set()

    def run(self) -> None:
        try:
            asyncio.run(self._collect())
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _collect(self) -> None:
        self.status.emit("正在連線到 P715…")
        async with BleakClient(self.address) as client:
            loop = asyncio.get_running_loop()
            first_power_packet = asyncio.Event()

            def callback(_sender: int, data: bytearray) -> None:
                try:
                    result, self.last_crank = decode_p715(data, self.last_crank)
                    self.records.append(result)
                    self.data_received.emit(result)
                    if not first_power_packet.is_set():
                        loop.call_soon_threadsafe(first_power_packet.set)
                except Exception as exc:
                    self.status.emit(f"略過一筆無法解析的資料：{exc}")

            def advanced_callback(_sender: int, data: bytearray) -> None:
                try:
                    self.advanced_records.append(decode_advanced_packet(data))
                except Exception as exc:
                    self.status.emit(f"略過一筆無法解析的進階資料：{exc}")

            await client.start_notify(CYCLING_POWER_MEASUREMENT, callback)
            advanced_started = False
            try:
                await client.start_notify(ADVANCED_DATA_STREAM, advanced_callback)
                advanced_started = True
            except Exception as exc:
                self.status.emit(f"進階資料通道未啟用，仍會收集功率資料：{exc}")
            self.status.emit("已連線，正在等待第一筆踏板資料。請開始踩踏。")
            try:
                while not self.stop_requested.is_set() and not first_power_packet.is_set():
                    await asyncio.sleep(0.1)
                if self.stop_requested.is_set():
                    return
                self.status.emit("已收到資料，現在開始正式收集與倒數。")
                self.collection_started.emit(self.duration_seconds)
                started = time.monotonic()
                while not self.stop_requested.is_set() and time.monotonic() - started < self.duration_seconds:
                    await asyncio.sleep(0.1)
            finally:
                await client.stop_notify(CYCLING_POWER_MEASUREMENT)
                if advanced_started:
                    await client.stop_notify(ADVANCED_DATA_STREAM)
        self.completed.emit(self.records)
