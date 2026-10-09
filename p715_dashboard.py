from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from bleak import BleakClient, BleakScanner
from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
    QDialog, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_FEATURE = "00002a65-0000-1000-8000-00805f9b34fb"
ADVANCED_DATA_STREAM = "ec22dc02-dd0f-4df7-b022-7c41471bcc7a"
TARGET_TEXT = "P715"
OUTPUT_DIR = Path(__file__).parent
ADVANCED_NOTIFY_CHARACTERISTICS = [
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",  # Nordic UART TX
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
        position += 2  # accumulated torque
    if flags & 0x0010:
        position += 6  # wheel revolution data
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
        # P715 appends a four-byte vendor reference after the standard fields.
        # It is shared with the advanced notification stream and is used only
        # as a synchronization key, not interpreted as a cycling measurement.
        "sync_reference_hex": d[-4:].hex(" "),
        "raw_hex": d.hex(" "),
    }, last_crank


def decode_advanced_packet(data: bytearray) -> dict:
    """Preserve the vendor payload while exposing safe, neutral fields for analysis."""
    d = bytearray(data)
    if len(d) < 6:
        raise ValueError("Advanced packet is shorter than its crank reference")
    result = {
        "advanced_timestamp": time.time(),
        "frame_type_hex": d[0:2].hex(" "),
        # These four bytes match P715's appended 0x2A63 vendor reference.
        # They are a sync key, not the standard crank-revolution fields.
        "sync_reference_hex": d[2:6].hex(" "),
        "payload_hex": d[6:].hex(" "),
        "raw_hex": d.hex(" "),
    }
    # Do not assign meanings such as phase or torque before they are verified.
    for index, value in enumerate(d[6:], start=6):
        result[f"payload_byte_{index:02d}"] = value
    return result


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
    """Connect and read a standard P715 characteristic before a test begins."""

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
            # 0x2A65 is the standard read-only Cycling Power Feature characteristic.
            await client.read_gatt_char(CYCLING_POWER_FEATURE)
        self.verified.emit(self.address)


class GattProbeWorker(QThread):
    """List every BLE service and characteristic exposed by the P715."""

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
        async with BleakClient(self.address) as client:
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
            lines = [
                "P715 完整藍牙服務探測結果",
                "",
                "判讀方式：若看到非標準 UUID、或含 notify 的未知特徵值，可能是進階數據通道。",
                "",
            ]
            for service in client.services:
                service_name = known.get(service.uuid.lower(), "未知／廠商服務")
                lines.append(f"Service: {service.uuid}  [{service_name}]")
                for characteristic in service.characteristics:
                    char_name = known.get(characteristic.uuid.lower(), "未知／廠商特徵值")
                    properties = ", ".join(characteristic.properties)
                    lines.append(f"  └─ {characteristic.uuid}  [{char_name}]  ({properties})")
                    for descriptor in characteristic.descriptors:
                        lines.append(f"      descriptor: {descriptor.uuid}")
                lines.append("")
        self.completed.emit("\n".join(lines))


class AdvancedMonitorWorker(QThread):
    """Passively listen to vendor notify characteristics. Never writes to the pedal."""

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
            available = {characteristic.uuid.lower() for service in client.services for characteristic in service.characteristics}
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

        lines = [
            "P715 進階資料被動監聽結果（未寫入任何指令）",
            "",
            "如果某 UUID 在踩踏時有封包，請把整段結果貼回來，我們再判讀格式。",
            "",
        ]
        for uuid in ADVANCED_NOTIFY_CHARACTERISTICS:
            packets = self.packets[uuid]
            lines.append(f"{uuid}")
            lines.append(f"  收到封包：{len(packets)} 筆")
            for _timestamp, hex_data in packets[:8]:
                lines.append(f"  {hex_data}")
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
        # This is intentionally retained as raw bytes until its vendor format
        # has been verified against a known reference measurement.
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
                # Passive subscription only: no vendor command is sent.
                await client.start_notify(ADVANCED_DATA_STREAM, advanced_callback)
                advanced_started = True
            except Exception as exc:
                self.status.emit(f"進階資料通道未啟用，仍會收集功率資料：{exc}")
            self.status.emit("已連線，正在等待第一筆踏板資料。請開始踩踏。")
            try:
                # A pedal can be connected but asleep. Do not spend the test
                # duration until a real Cycling Power Measurement arrives.
                while not self.stop_requested.is_set() and not first_power_packet.is_set():
                    await asyncio.sleep(0.1)
                if self.stop_requested.is_set():
                    return

                self.status.emit("已收到資料，現在開始正式收集與倒數。")
                self.collection_started.emit(self.duration_seconds)
                started = time.monotonic()
                while not self.stop_requested.is_set():
                    if time.monotonic() - started >= self.duration_seconds:
                        break
                    await asyncio.sleep(0.1)
            finally:
                await client.stop_notify(CYCLING_POWER_MEASUREMENT)
                if advanced_started:
                    await client.stop_notify(ADVANCED_DATA_STREAM)

        self.completed.emit(self.records)


class MetricCard(QFrame):
    def __init__(self, title: str, unit: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame { background: #f4f6f8; border-radius: 10px; padding: 8px; }")
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 13px; color: #5e6670;")
        self.value = QLabel("–")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setStyleSheet("font-size: 30px; font-weight: 600; color: #151a20;")
        unit_label = QLabel(unit)
        unit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        unit_label.setStyleSheet("font-size: 12px; color: #5e6670;")
        layout.addWidget(title_label)
        layout.addWidget(self.value)
        layout.addWidget(unit_label)

    def set_value(self, value: str) -> None:
        self.value.setText(value)


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def calculate_analysis(df: pd.DataFrame, ftp: int) -> dict:
    """Calculate metrics that can be derived honestly from P715 power packets."""
    work = df.sort_values("timestamp").copy()
    work["elapsed_s"] = work["timestamp"] - work["timestamp"].iloc[0]
    duration_s = float(work["elapsed_s"].iloc[-1])

    indexed = work.set_index(pd.to_datetime(work["timestamp"], unit="s"))
    power_1s = indexed["power_w"].resample("1s").mean().interpolate(limit_area="inside").fillna(0)
    cadence_1s = indexed["cadence_rpm"].resample("1s").mean()

    rolling_30s = power_1s.rolling(30, min_periods=30).mean().dropna()
    normalized_power = (
        float((rolling_30s.pow(4).mean()) ** 0.25)
        if not rolling_30s.empty
        else float(power_1s.mean())
    )
    intensity_factor = normalized_power / ftp if ftp > 0 else None
    tss = duration_s / 3600 * intensity_factor**2 * 100 if intensity_factor is not None else None
    energy_kj = float(power_1s.sum() / 1000)

    active = work[(work["power_w"] > 0) & (work["balance_percent"] > 0)]
    left_balance = float(active["balance_percent"].mean()) if not active.empty else None

    zone_limits = [0, ftp * 0.55, ftp * 0.75, ftp * 0.90, ftp * 1.05, ftp * 1.20, ftp * 1.50]
    zone_names = ["區間 1 主動恢復", "區間 2 耐力", "區間 3 節奏", "區間 4 閾值", "區間 5 最大攝氧量", "區間 6 無氧", "區間 7 神經肌肉爆發力"]
    zone_seconds = [0] * 7
    for power in power_1s:
        if power <= zone_limits[1]:
            index = 0
        elif power <= zone_limits[2]:
            index = 1
        elif power <= zone_limits[3]:
            index = 2
        elif power <= zone_limits[4]:
            index = 3
        elif power <= zone_limits[5]:
            index = 4
        elif power <= zone_limits[6]:
            index = 5
        else:
            index = 6
        zone_seconds[index] += 1

    zones = []
    total_seconds = max(len(power_1s), 1)
    for index, seconds in enumerate(zone_seconds):
        if index == 0:
            bounds = f"0–{round(zone_limits[1])} W"
        elif index == 6:
            bounds = f"> {round(zone_limits[6])} W"
        else:
            bounds = f"{round(zone_limits[index]) + 1}–{round(zone_limits[index + 1])} W"
        zones.append({"name": zone_names[index], "bounds": bounds, "seconds": seconds, "percent": seconds / total_seconds * 100})

    return {
        "work": work,
        "duration_s": duration_s,
        "average_power": float(work["power_w"].mean()),
        "max_power": float(work["power_w"].max()),
        "average_cadence": float(cadence_1s.mean()) if cadence_1s.notna().any() else None,
        "max_cadence": float(cadence_1s.max()) if cadence_1s.notna().any() else None,
        "left_balance": left_balance,
        "right_balance": 100 - left_balance if left_balance is not None else None,
        "normalized_power": normalized_power,
        "intensity_factor": intensity_factor,
        "tss": tss,
        "energy_kj": energy_kj,
        "zones": zones,
    }


class AnalysisDialog(QDialog):
    def __init__(self, df: pd.DataFrame, ftp: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("P715 事後分析")
        self.resize(940, 720)
        results = calculate_analysis(df, ftp)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        summary_page = QWidget()
        summary_layout = QVBoxLayout(summary_page)
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setStyleSheet("font-size: 14px;")
        balance_line = "無有效左右平衡資料"
        if results["left_balance"] is not None:
            balance_line = f"左 {results['left_balance']:.1f}%／右 {results['right_balance']:.1f}%"
        cadence_line = "無有效踏頻資料"
        if results["average_cadence"] is not None:
            cadence_line = f"平均 {results['average_cadence']:.1f} rpm，最高 {results['max_cadence']:.0f} rpm"
        zone_text = "\n".join(
            f"{zone['name']}（{zone['bounds']}）：{format_duration(zone['seconds'])}  {zone['percent']:.0f}%"
            for zone in results["zones"]
        )
        report.setPlainText(
            "騎乘摘要\n"
            f"總時間：{format_duration(results['duration_s'])}\n"
            f"平均功率：{results['average_power']:.1f} W\n"
            f"最高功率：{results['max_power']:.0f} W\n"
            f"踏頻：{cadence_line}\n"
            f"左右平衡：{balance_line}\n\n"
            "功率指標\n"
            f"FTP 設定：{ftp} W\n"
            f"Normalized Power (NP)：{results['normalized_power']:.1f} W\n"
            f"IF（強度係數）：{results['intensity_factor']:.3f}\n"
            f"TSS（訓練壓力分數）：{results['tss']:.1f}\n"
            f"機械功：{results['energy_kj']:.1f} kJ\n\n"
            "功率區間時間分布\n"
            f"{zone_text}\n\n"
            "註：NP、IF、TSS 與區間依此程式設定的 FTP 計算。機械功不是 Garmin 的熱量估計。"
        )
        summary_layout.addWidget(report)
        tabs.addTab(summary_page, "摘要與功率區間")

        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        figure = Figure(figsize=(10, 7), tight_layout=True)
        canvas = FigureCanvas(figure)
        axes = figure.subplots(3, 1, sharex=True)
        work = results["work"]
        for axis in axes:
            axis.spines[["top", "right"]].set_visible(False)
            axis.grid(axis="y", alpha=0.25)
        axes[0].plot(work["elapsed_s"], work["power_w"], color="#cf3cc6", linewidth=2)
        axes[0].fill_between(work["elapsed_s"], work["power_w"], color="#cf3cc6", alpha=0.25)
        axes[0].set_ylabel("Power (W)")
        axes[0].set_title("Power")
        axes[1].plot(work["elapsed_s"], work["cadence_rpm"], color="#f58b1f", linewidth=2)
        axes[1].fill_between(work["elapsed_s"], work["cadence_rpm"].fillna(0), color="#f58b1f", alpha=0.25)
        axes[1].set_ylabel("Cadence (rpm)")
        axes[1].set_title("Cadence")
        balance = work["balance_percent"].where(work["balance_percent"] > 0)
        axes[2].scatter(work["elapsed_s"], balance, color="#62d52e", label="Left", s=24)
        axes[2].scatter(work["elapsed_s"], 100 - balance, color="#18a8e6", label="Right", s=24)
        axes[2].axhline(50, color="gray", linestyle="--", linewidth=1)
        axes[2].set_ylim(0, 100)
        axes[2].set_ylabel("Balance (%)")
        axes[2].set_xlabel("Time (s)")
        axes[2].set_title("Left / Right Balance")
        axes[2].legend(frameon=False, ncols=2)
        chart_layout.addWidget(canvas)
        tabs.addTab(chart_page, "Charts")


class Dashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.scan_worker: ScanWorker | None = None
        self.connection_worker: ConnectionCheckWorker | None = None
        self.probe_worker: GattProbeWorker | None = None
        self.advanced_monitor_worker: AdvancedMonitorWorker | None = None
        self.record_worker: RecordWorker | None = None
        self.countdown_deadline: float | None = None
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(250)
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.verified_address: str | None = None
        self.latest_df: pd.DataFrame | None = None
        self.setWindowTitle("P715 踏板資料儀表板")
        self.setMinimumSize(860, 650)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(14)

        title = QLabel("P715 即時踏板資料")
        title.setStyleSheet("font-size: 25px; font-weight: 600;")
        layout.addWidget(title)

        connect_group = QGroupBox("藍牙連線")
        connect_layout = QHBoxLayout(connect_group)
        self.scan_button = QPushButton("掃描 P715")
        self.scan_button.clicked.connect(self.scan_devices)
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(360)
        self.device_box.currentIndexChanged.connect(self.clear_verification)
        self.verify_button = QPushButton("測試連線")
        self.verify_button.setEnabled(False)
        self.verify_button.clicked.connect(self.verify_connection)
        self.probe_button = QPushButton("探測進階資料")
        self.probe_button.setEnabled(False)
        self.probe_button.clicked.connect(self.probe_gatt)
        self.monitor_button = QPushButton("監聽進階資料 30 秒")
        self.monitor_button.setEnabled(False)
        self.monitor_button.clicked.connect(self.monitor_advanced_data)
        self.duration = QSpinBox()
        self.duration.setRange(10, 3600)
        self.duration.setValue(120)
        self.duration.setSuffix(" 秒")
        self.countdown_label = QLabel("剩餘時間：--:--")
        self.countdown_label.setStyleSheet(
            "font-size: 17px; font-weight: 600; color: #b53420; padding: 4px 8px;"
        )
        self.ftp = QSpinBox()
        self.ftp.setRange(50, 500)
        self.ftp.setValue(202)
        self.ftp.setSuffix(" W")
        self.start_button = QPushButton("開始收集")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_collection)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_collection)
        self.analysis_button = QPushButton("查看事後分析")
        self.analysis_button.setEnabled(False)
        self.analysis_button.clicked.connect(self.show_analysis)
        connect_layout.addWidget(self.scan_button)
        connect_layout.addWidget(self.device_box, 1)
        connect_layout.addWidget(self.verify_button)
        connect_layout.addWidget(self.probe_button)
        connect_layout.addWidget(self.monitor_button)
        connect_layout.addWidget(QLabel("測試時間"))
        connect_layout.addWidget(self.duration)
        connect_layout.addWidget(self.countdown_label)
        connect_layout.addWidget(QLabel("FTP"))
        connect_layout.addWidget(self.ftp)
        connect_layout.addWidget(self.start_button)
        connect_layout.addWidget(self.stop_button)
        connect_layout.addWidget(self.analysis_button)
        layout.addWidget(connect_group)

        self.status_label = QLabel("請先按「掃描 P715」。")
        self.status_label.setStyleSheet("padding: 6px; color: #254b79;")
        layout.addWidget(self.status_label)

        grid = QGridLayout()
        self.power_card = MetricCard("總功率", "W")
        self.cadence_card = MetricCard("踏頻", "rpm")
        self.left_balance_card = MetricCard("左腳平衡", "%")
        self.right_balance_card = MetricCard("右腳平衡", "%")
        self.left_power_card = MetricCard("左腳估計功率", "W")
        self.right_power_card = MetricCard("右腳估計功率", "W")
        for index, card in enumerate([
            self.power_card, self.cadence_card, self.left_balance_card,
            self.right_balance_card, self.left_power_card, self.right_power_card,
        ]):
            grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(grid)

        self.summary = QLabel("尚未完成任何測試。")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background: #eef6ff; padding: 10px; border-radius: 8px;")
        layout.addWidget(self.summary)

        table_group = QGroupBox("最新資料（最多 12 筆）")
        table_layout = QVBoxLayout(table_group)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["經過秒數", "總功率 (W)", "踏頻 (rpm)", "左腳 (%)", "右腳 (%)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table_layout.addWidget(self.table)
        layout.addWidget(table_group, 1)

    def scan_devices(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self.device_box.clear()
        self.clear_verification()
        self.verify_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.scan_worker = ScanWorker()
        self.scan_worker.status.connect(self.set_status)
        self.scan_worker.found.connect(self.show_devices)
        self.scan_worker.failed.connect(self.show_error)
        self.scan_worker.finished.connect(lambda: self.scan_button.setEnabled(True))
        self.scan_worker.start()

    def show_devices(self, choices: list[dict]) -> None:
        if not choices:
            self.set_status("找不到 P715。請確認踏板已喚醒、未被 Magene Utility 或 Chrome 佔用。")
            return
        for choice in choices:
            text = f"{choice['name']}  |  訊號 {choice['rssi']} dBm"
            self.device_box.addItem(text, choice["address"])
        self.verify_button.setEnabled(True)
        self.set_status(f"找到 {len(choices)} 個 P715。選好後請先按「測試連線」。")

    def clear_verification(self) -> None:
        self.verified_address = None
        self.start_button.setEnabled(False)
        self.probe_button.setEnabled(False)
        self.monitor_button.setEnabled(False)

    def verify_connection(self) -> None:
        address = self.device_box.currentData()
        if not address:
            QMessageBox.information(self, "尚未選擇裝置", "請先掃描並選擇 P715。")
            return
        self.clear_verification()
        self.scan_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self.connection_worker = ConnectionCheckWorker(address)
        self.connection_worker.status.connect(self.set_status)
        self.connection_worker.verified.connect(self.connection_verified)
        self.connection_worker.failed.connect(self.show_error)
        self.connection_worker.finished.connect(self.connection_check_finished)
        self.connection_worker.start()

    def connection_verified(self, address: str) -> None:
        self.verified_address = address
        self.start_button.setEnabled(True)
        self.probe_button.setEnabled(True)
        self.monitor_button.setEnabled(True)
        self.set_status("P715 連線測試成功。現在可以開始收集，或先探測進階資料。")

    def connection_check_finished(self) -> None:
        self.scan_button.setEnabled(True)
        self.verify_button.setEnabled(self.device_box.currentData() is not None)

    def probe_gatt(self) -> None:
        address = self.device_box.currentData()
        if address != self.verified_address:
            QMessageBox.information(self, "尚未確認連線", "請先按「測試連線」，再進行進階資料探測。")
            return
        self.scan_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self.probe_button.setEnabled(False)
        self.probe_worker = GattProbeWorker(address)
        self.probe_worker.status.connect(self.set_status)
        self.probe_worker.completed.connect(self.show_probe_result)
        self.probe_worker.failed.connect(self.show_error)
        self.probe_worker.finished.connect(self.probe_finished)
        self.probe_worker.start()

    def probe_finished(self) -> None:
        self.scan_button.setEnabled(True)
        self.verify_button.setEnabled(self.device_box.currentData() is not None)
        self.probe_button.setEnabled(self.verified_address is not None)
        self.monitor_button.setEnabled(self.verified_address is not None)

    def show_probe_result(self, text: str) -> None:
        self.set_status("探測完成。請查看探測結果視窗。")
        dialog = QDialog(self)
        dialog.setWindowTitle("P715 進階資料探測結果")
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        result = QPlainTextEdit()
        result.setReadOnly(True)
        result.setPlainText(text)
        layout.addWidget(result)
        dialog.exec()

    def monitor_advanced_data(self) -> None:
        address = self.device_box.currentData()
        if address != self.verified_address:
            QMessageBox.information(self, "尚未確認連線", "請先按「測試連線」，再監聽進階資料。")
            return
        self.scan_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self.probe_button.setEnabled(False)
        self.monitor_button.setEnabled(False)
        self.advanced_monitor_worker = AdvancedMonitorWorker(address)
        self.advanced_monitor_worker.status.connect(self.set_status)
        self.advanced_monitor_worker.completed.connect(self.show_advanced_monitor_result)
        self.advanced_monitor_worker.failed.connect(self.show_error)
        self.advanced_monitor_worker.finished.connect(self.advanced_monitor_finished)
        self.advanced_monitor_worker.start()

    def advanced_monitor_finished(self) -> None:
        self.scan_button.setEnabled(True)
        self.verify_button.setEnabled(self.device_box.currentData() is not None)
        self.probe_button.setEnabled(self.verified_address is not None)
        self.monitor_button.setEnabled(self.verified_address is not None)

    def show_advanced_monitor_result(self, text: str) -> None:
        self.set_status("進階資料監聽完成。請查看監聽結果視窗。")
        dialog = QDialog(self)
        dialog.setWindowTitle("P715 進階資料監聽結果")
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        result = QPlainTextEdit()
        result.setReadOnly(True)
        result.setPlainText(text)
        layout.addWidget(result)
        dialog.exec()

    def start_collection(self) -> None:
        address = self.device_box.currentData()
        if not address:
            QMessageBox.information(self, "尚未選擇裝置", "請先掃描並選擇 P715。")
            return
        if address != self.verified_address:
            QMessageBox.information(self, "尚未確認連線", "請先按「測試連線」，確認 P715 可正常連線後再開始收集。")
            return
        self.table.setRowCount(0)
        for card in [
            self.power_card, self.cadence_card, self.left_balance_card,
            self.right_balance_card, self.left_power_card, self.right_power_card,
        ]:
            card.set_value("–")
        self.start_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.countdown_deadline = None
        self.countdown_label.setText("剩餘時間：等待連線…")
        self.record_worker = RecordWorker(address, self.duration.value())
        self.record_worker.status.connect(self.set_status)
        self.record_worker.collection_started.connect(self.start_countdown)
        self.record_worker.data_received.connect(self.update_live_data)
        self.record_worker.completed.connect(self.finish_collection)
        self.record_worker.failed.connect(self.show_error)
        self.record_worker.finished.connect(self.record_finished)
        self.record_worker.start()

    def stop_collection(self) -> None:
        if self.record_worker and self.record_worker.isRunning():
            self.set_status("正在停止並儲存資料…")
            self.stop_button.setEnabled(False)
            self.countdown_label.setText("剩餘時間：正在停止…")
            self.record_worker.stop()

    def start_countdown(self, seconds: int) -> None:
        """Start only after BLE notifications are actually active."""
        self.countdown_deadline = time.monotonic() + seconds
        self.update_countdown()
        self.countdown_timer.start()

    def update_countdown(self) -> None:
        if self.countdown_deadline is None:
            return
        seconds_left = max(0, math.ceil(self.countdown_deadline - time.monotonic()))
        self.countdown_label.setText(
            f"剩餘時間：{seconds_left // 60}:{seconds_left % 60:02d}"
        )
        if seconds_left == 0:
            self.countdown_timer.stop()

    def update_live_data(self, result: dict) -> None:
        self.power_card.set_value(f"{result['power_w']:.0f}")
        self.cadence_card.set_value("–" if result["cadence_rpm"] is None else f"{result['cadence_rpm']:.0f}")
        if result["left_power_w"] is not None:
            left = result["balance_percent"]
            self.left_balance_card.set_value(f"{left:.0f}")
            self.right_balance_card.set_value(f"{100 - left:.0f}")
            self.left_power_card.set_value(f"{result['left_power_w']:.0f}")
            self.right_power_card.set_value(f"{result['right_power_w']:.0f}")

        elapsed = 0.0
        if self.record_worker and self.record_worker.records:
            elapsed = result["timestamp"] - self.record_worker.records[0]["timestamp"]
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            f"{elapsed:.1f}",
            f"{result['power_w']:.0f}",
            "–" if result["cadence_rpm"] is None else f"{result['cadence_rpm']:.1f}",
            "–" if result["balance_percent"] is None else f"{result['balance_percent']:.1f}",
            "–" if result["balance_percent"] is None else f"{100 - result['balance_percent']:.1f}",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)
        if self.table.rowCount() > 12:
            self.table.removeRow(0)
        self.table.scrollToBottom()

    def finish_collection(self, records: list[dict]) -> None:
        if not records:
            self.summary.setText("本次沒有收到資料。請確認 P715 已喚醒並且正在踩踏。")
            return
        df = pd.DataFrame(records)
        self.latest_df = df
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_path = OUTPUT_DIR / f"p715_{stamp}.csv"
        latest_path = OUTPUT_DIR / "p715_data.csv"
        df.to_csv(session_path, index=False)
        df.to_csv(latest_path, index=False)

        advanced_count = 0
        advanced_matched_count = 0
        if self.record_worker and self.record_worker.advanced_records:
            advanced_df = pd.DataFrame(self.record_worker.advanced_records)
            basic_columns = [
                "timestamp", "sync_reference_hex", "power_w",
                "cadence_rpm", "balance_percent", "left_power_w", "right_power_w",
            ]
            basic_for_merge = (
                df[basic_columns]
                .rename(columns={"timestamp": "standard_timestamp"})
                # The P715 vendor sync key can repeat. Keep one standard row
                # per key so the advanced CSV always retains one row per packet.
                .drop_duplicates("sync_reference_hex", keep="last")
            )
            advanced_df = advanced_df.merge(
                basic_for_merge,
                on="sync_reference_hex",
                how="left",
            )
            advanced_path = OUTPUT_DIR / f"p715_advanced_{stamp}.csv"
            advanced_latest_path = OUTPUT_DIR / "p715_advanced_data.csv"
            advanced_df.to_csv(advanced_path, index=False)
            advanced_df.to_csv(advanced_latest_path, index=False)
            advanced_count = len(advanced_df)
            advanced_matched_count = int(advanced_df["power_w"].notna().sum())

        active = df[(df["power_w"] > 0) & (df["balance_percent"] > 0)]
        parts = [
            f"已收集 {len(df)} 筆資料。",
            f"平均功率 {df['power_w'].mean():.1f} W，最高 {df['power_w'].max():.0f} W。",
        ]
        if df["cadence_rpm"].notna().any():
            parts.append(f"平均踏頻 {df['cadence_rpm'].mean():.1f} rpm。")
        if not active.empty:
            left = active["balance_percent"].mean()
            parts.append(f"左右平衡：左 {left:.1f}%／右 {100 - left:.1f}%。")
        if advanced_count:
            parts.append(
                f"另已同步儲存 {advanced_count} 筆未解碼的進階封包，"
                f"其中 {advanced_matched_count} 筆已對上功率資料。"
            )
        parts.append(f"已儲存：{session_path.name}，並更新 p715_data.csv。")
        self.summary.setText("\n".join(parts))
        self.analysis_button.setEnabled(True)
        self.countdown_timer.stop()
        self.countdown_label.setText("剩餘時間：完成")
        self.set_status("收集完成。")

    def show_analysis(self) -> None:
        if self.latest_df is None or self.latest_df.empty:
            QMessageBox.information(self, "尚無資料", "請先完成一段收集，再查看事後分析。")
            return
        dialog = AnalysisDialog(self.latest_df, self.ftp.value(), self)
        dialog.exec()

    def record_finished(self) -> None:
        self.countdown_timer.stop()
        self.start_button.setEnabled(self.verified_address is not None)
        self.scan_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def show_error(self, message: str) -> None:
        self.set_status(f"錯誤：{message}")
        QMessageBox.warning(self, "藍牙錯誤", message)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.record_worker and self.record_worker.isRunning():
            self.record_worker.stop()
            self.record_worker.wait(3000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Dashboard()
    window.show()
    sys.exit(app.exec())
