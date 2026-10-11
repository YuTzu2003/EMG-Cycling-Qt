from __future__ import annotations
import asyncio
import struct
from dataclasses import dataclass, replace
from PyQt5.QtCore import QThread, pyqtSignal

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = BleakScanner = None


FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
RESISTANCE_RANGE = "00002ad6-0000-1000-8000-00805f9b34fb"
POWER_RANGE = "00002ad8-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT = "00002a5b-0000-1000-8000-00805f9b34fb"
RESPONSE_CODE = 0x80
SUCCESS = 0x01

@dataclass(frozen=True)
class NumericRange:
    minimum: float
    maximum: float
    increment: float

    def align(self, value: float) -> float:
        steps = round((value - self.minimum) / self.increment)
        return self.minimum + steps * self.increment

@dataclass(frozen=True)
class BikeTelemetry:
    speedKph: float | None = None
    cadenceRpm: float | None = None
    resistance: float | None = None
    powerWatts: int | None = None
    heartRate: int | None = None
    distanceMeters: int | None = None
    energyKcal: int | None = None
    elapsedSeconds: int | None = None

def parseIndoorBikeData(data: bytearray) -> BikeTelemetry:
    if len(data) < 2:
        raise ValueError("Indoor Bike Data 封包長度不足。")

    flags = struct.unpack_from("<H", data)[0]
    offset = 2

    def read(fmt: str, scale: float = 1.0) -> float | int:
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise ValueError("Indoor Bike Data 封包不完整。")
        value = struct.unpack_from(fmt, data, offset)[0]
        offset += size
        return value / scale

    values: dict[str, float | int | None] = {
        "speedKph": None,
        "cadenceRpm": None,
        "resistance": None,
        "powerWatts": None,
        "heartRate": None,
        "distanceMeters": None,
        "energyKcal": None,
        "elapsedSeconds": None,
    }
    if not flags & 0x0001:
        values["speedKph"] = read("<H", 100.0)
    if flags & 0x0004:
        values["cadenceRpm"] = read("<H", 2.0)
    if flags & 0x0010:
        if offset + 3 > len(data):
            raise ValueError("Indoor Bike Data 距離欄位不完整。")
        values["distanceMeters"] = int.from_bytes(data[offset : offset + 3], "little")
        offset += 3
    if flags & 0x0020:
        values["resistance"] = read("<h", 10.0)
    if flags & 0x0040:
        values["powerWatts"] = int(read("<h"))
    if flags & 0x0100:
        values["energyKcal"] = int(read("<H"))
        read("<H")
        read("<B")
    if flags & 0x0200:
        values["heartRate"] = int(read("<B"))
    if flags & 0x0800:
        values["elapsedSeconds"] = int(read("<H"))
    return BikeTelemetry(**values)

class FtmsTrainer:
    def __init__(self, address: str, timeout: float = 15.0) -> None:
        self.address = address
        self.timeout = timeout
        self.client = None
        self.controlEvent = asyncio.Event()
        self.controlResponse: tuple[int, int] | None = None
        self.notificationsActive = False
        self.bikeNotificationsActive = False
        self.telemetryCallback = None
        self.latestTelemetry = BikeTelemetry()
        self.crankState: tuple[int, int] | None = None

    def requireBleak(self) -> None:
        if BleakClient is None or BleakScanner is None:
            raise RuntimeError("缺少 bleak 藍牙套件。請在專案資料夾執行 uv sync 後重新開啟程式。")

    async def connect(self) -> None:
        self.requireBleak()
        device = await BleakScanner.find_device_by_address(self.address, timeout=self.timeout)
        if device is None:
            raise RuntimeError(f"找不到阻力計 {self.address}。請喚醒設備後再試。")
        self.client = BleakClient(device, timeout=self.timeout, winrt={"use_cached_services": False})
        await self.client.connect()

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.notificationsActive = False
        self.bikeNotificationsActive = False

    def requireCharacteristic(self, uuid: str, name: str) -> None:
        if self.client is None or self.client.services.get_characteristic(uuid) is None:
            raise RuntimeError(f"找不到 {name}。請確認設備支援 FTMS，且沒有被其他 App 控制。")

    async def readRange(self, uuid: str, scale: float, name: str) -> NumericRange:
        self.requireCharacteristic(uuid, name)
        raw = await self.client.read_gatt_char(uuid)
        minimum, maximum, increment = struct.unpack("<hhh", raw)
        return NumericRange(minimum / scale, maximum / scale, increment / scale)

    async def resistanceRange(self) -> NumericRange:
        return await self.readRange(RESISTANCE_RANGE, 10.0, "Resistance Range")

    async def powerRange(self) -> NumericRange:
        return await self.readRange(POWER_RANGE, 1.0, "Power Range")

    def controlNotification(self, sender: int, data: bytearray) -> None:
        if len(data) >= 3 and data[0] == RESPONSE_CODE:
            self.controlResponse = (data[1], data[2])
            self.controlEvent.set()

    async def send(self, payload: bytes) -> None:
        self.requireCharacteristic(CONTROL_POINT, "Control Point")
        self.controlResponse = None
        self.controlEvent.clear()
        await self.client.write_gatt_char(CONTROL_POINT, payload, response=True)
        await asyncio.wait_for(self.controlEvent.wait(), timeout=5.0)
        if self.controlResponse is None:
            raise RuntimeError("阻力計未回覆 Control Point 指令。")

    async def acquireControl(self) -> None:
        self.requireCharacteristic(CONTROL_POINT, "Control Point")
        if not self.notificationsActive:
            await self.client.start_notify(CONTROL_POINT, self.controlNotification)
            self.notificationsActive = True
        await self.send(b"\x00")
        await self.send(b"\x07")

    def emitTelemetry(self, update: BikeTelemetry) -> None:
        values = {name: value for name, value in vars(update).items() if value is not None}
        self.latestTelemetry = replace(self.latestTelemetry, **values)
        if self.telemetryCallback is not None:
            self.telemetryCallback(self.latestTelemetry)

    def cadenceFromCrank(self, revolutions: int, eventTime: int) -> float | None:
        previous = self.crankState
        self.crankState = (revolutions, eventTime)
        if previous is None:
            return None
        previousRevolutions, previousTime = previous
        elapsed = (eventTime - previousTime) & 0xFFFF
        turns = (revolutions - previousRevolutions) & 0xFFFF
        if elapsed == 0:
            return None
        return turns * 60 * 1024 / elapsed

    def indoorBikeNotification(self, sender: int, data: bytearray) -> None:
        self.emitTelemetry(parseIndoorBikeData(data))

    def cyclingPowerNotification(self, sender: int, data: bytearray) -> None:
        if len(data) < 4:
            return
        flags, power = struct.unpack_from("<Hh", data)
        offset = 4
        if flags & 0x0001:
            offset += 1
        if flags & 0x0004:
            offset += 2
        if flags & 0x0010:
            offset += 6
        cadence = None
        if flags & 0x0020 and offset + 4 <= len(data):
            revolutions, eventTime = struct.unpack_from("<HH", data, offset)
            cadence = self.cadenceFromCrank(revolutions, eventTime)
        self.emitTelemetry(BikeTelemetry(powerWatts=power, cadenceRpm=cadence))

    def cscNotification(self, sender: int, data: bytearray) -> None:
        if not data:
            return
        flags = data[0]
        offset = 1 + (6 if flags & 0x01 else 0)
        if flags & 0x02 and offset + 4 <= len(data):
            revolutions, eventTime = struct.unpack_from("<HH", data, offset)
            self.emitTelemetry(BikeTelemetry(cadenceRpm=self.cadenceFromCrank(revolutions, eventTime)))

    async def startTelemetry(self) -> bool:
        if self.client is None:
            return False
        if not self.bikeNotificationsActive:
            notifications = (
                (INDOOR_BIKE_DATA, self.indoorBikeNotification),
                (CYCLING_POWER_MEASUREMENT, self.cyclingPowerNotification),
                (CSC_MEASUREMENT, self.cscNotification),
            )
            subscribed = False
            for uuid, callback in notifications:
                if self.client.services.get_characteristic(uuid) is not None:
                    await self.client.start_notify(uuid, callback)
                    subscribed = True
            self.bikeNotificationsActive = True
            return subscribed
        return True

    async def setResistance(self, level: float) -> float:
        supported = await self.resistanceRange()
        if not supported.minimum <= level <= supported.maximum:
            raise ValueError(f"阻力需介於 {supported.minimum:g} 到 {supported.maximum:g}。")
        actual = supported.align(level)
        await self.send(struct.pack("<Bh", 0x04, round(actual * 10)))
        return actual

    async def setFixedResistanceLevel(self, level: int) -> float:
        """Map the interface's 0–30 level to the trainer's fixed native resistance."""
        if not 0 <= level <= 30:
            raise ValueError("固定阻力等級必須介於 0 到 30。")
        supported = await self.resistanceRange()
        target = supported.minimum + (supported.maximum - supported.minimum) * level / 30
        return await self.setResistance(supported.align(target))

    async def setAppLevel(self, level: int, level30Watts: float) -> float:
        """Map a user-friendly 0–30 level to a reproducible ERG power target."""
        if not 0 <= level <= 30:
            raise ValueError("強度必須介於 0 到 30。")
        supported = await self.powerRange()
        if not supported.minimum <= level30Watts <= supported.maximum:
            raise ValueError(f"第 30 級目標功率需介於 {supported.minimum:g} 到 {supported.maximum:g} W。")
        target = supported.minimum + (level30Watts - supported.minimum) * level / 30
        actual = supported.align(target)
        await self.send(struct.pack("<Bh", 0x05, round(actual)))
        return actual

    async def setPower(self, watts: float) -> float:
        supported = await self.powerRange()
        if not supported.minimum <= watts <= supported.maximum:
            raise ValueError(f"功率需介於 {supported.minimum:g} 到 {supported.maximum:g} W。")
        actual = supported.align(watts)
        await self.send(struct.pack("<Bh", 0x05, round(actual)))
        return actual


class TrainerWorker(QThread):
    ready = pyqtSignal()
    scanReady = pyqtSignal(object)
    connected = pyqtSignal(object)
    disconnected = pyqtSignal()
    resistanceSet = pyqtSignal(float)
    fixedResistanceSet = pyqtSignal(int, float)
    telemetryReceived = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.trainer: FtmsTrainer | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.emit()
        if not self.isInterruptionRequested():
            self.loop.run_forever()
        self.loop.close()
        self.loop = None

    def submit(self, coroutine, callback) -> None:
        if self.loop is None:
            self.failed.emit("藍牙服務尚未就緒。")
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def complete(resultFuture) -> None:
            try:
                callback(resultFuture.result())
            except Exception as exc:
                self.failed.emit(str(exc))

        future.add_done_callback(complete)

    async def scanDevices(self) -> list[tuple[str, str]]:
        if BleakScanner is None:
            raise RuntimeError("缺少 bleak。請執行 uv sync。")
        try:
            devices = await BleakScanner.discover(timeout=8.0)
        except Exception as exc:
            if "powered on" in str(exc).lower() or "bluetooth radio" in str(exc).lower():
                raise RuntimeError("Windows 藍牙目前未開啟，請開啟藍牙並確認電腦有可用的Bluetooth再重新掃描。") from exc
            raise
        return [(device.address, device.name or "未知裝置") for device in devices]

    def scan(self) -> None:
        self.submit(self.scanDevices(), self.scanReady.emit)

    async def connectTrainer(self, address: str) -> dict:
        self.trainer = FtmsTrainer(address)
        await self.trainer.connect()
        await self.trainer.acquireControl()
        resistance = await self.trainer.resistanceRange()
        power = await self.trainer.powerRange()
        await self.trainer.setResistance(resistance.minimum)
        self.trainer.telemetryCallback = self.telemetryReceived.emit
        telemetryEnabled = await self.trainer.startTelemetry()
        return {
            "address": address,
            "resistance": resistance,
            "power": power,
            "nativeResistance": resistance.minimum,
            "telemetryEnabled": telemetryEnabled,
        }

    def connectDevice(self, address: str) -> None:
        self.submit(self.connectTrainer(address), self.connected.emit)

    def setResistanceValue(self, value: float) -> None:
        if self.trainer is None:
            self.failed.emit("請先連線阻力計。")
            return
        self.submit(self.trainer.setResistance(value), self.resistanceSet.emit)

    def setFixedResistanceLevel(self, level: int) -> None:
        if self.trainer is None:
            self.failed.emit("請先連線阻力計。")
            return

        def complete(actual: float) -> None:
            self.fixedResistanceSet.emit(level, actual)

        self.submit(self.trainer.setFixedResistanceLevel(level), complete)

    async def disconnectTrainer(self) -> None:
        if self.trainer:
            await self.trainer.disconnect()
        self.trainer = None

    def disconnectDevice(self) -> None:
        self.submit(self.disconnectTrainer(), lambda unused: self.disconnected.emit())

    def stopWorker(self) -> None:
        if self.loop is None:
            self.requestInterruption()
            return
        future = asyncio.run_coroutine_threadsafe(self.disconnectTrainer(), self.loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)