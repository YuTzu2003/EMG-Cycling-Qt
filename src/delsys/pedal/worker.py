from __future__ import annotations
import asyncio
from PyQt5.QtCore import QThread, pyqtSignal
from .protocol import CYCLING_POWER_FEATURE, CYCLING_POWER_MEASUREMENT, decodeCyclingPower
try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = BleakScanner = None

class PedalWorker(QThread):
    ready = pyqtSignal()
    devicesFound = pyqtSignal(object)
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    dataReceived = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = None
        self.lastCrank: dict | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.emit()
        self.loop.run_forever()
        self.loop.close()
        self.loop = None

    def submit(self, coroutine, callback=None) -> None:
        if self.loop is None:
            # The GUI can request a scan during application startup, before
            # this thread has installed its event loop.  Close the freshly
            # created coroutine so Python does not retain an unawaited task.
            coroutine.close()
            self.failed.emit("P715 worker is not ready")
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        def done(result):
            try:
                value = result.result()
                if callback:
                    callback(value)
            except Exception as exc:
                self.failed.emit(str(exc))
        future.add_done_callback(done)

    async def _scan(self) -> list[tuple[str, str]]:
        if BleakScanner is None:
            raise RuntimeError("bleak is not installed")
        devices = await BleakScanner.discover(timeout=6.0)
        return [(device.address, device.name or "Unknown device") for device in devices]

    def scan(self) -> None:
        self.submit(self._scan(), self.devicesFound.emit)

    async def _connect(self, address: str) -> str:
        if BleakClient is None:
            raise RuntimeError("bleak is not installed")
        await self._disconnect()
        self.client = BleakClient(address, disconnected_callback=self._handleDisconnected)
        await self.client.connect()
        # Reading the feature characteristic verifies this is a Cycling Power device.
        await self.client.read_gatt_char(CYCLING_POWER_FEATURE)
        self.lastCrank = None
        def notification(_sender: int, payload: bytearray) -> None:
            try:
                data, self.lastCrank = decodeCyclingPower(payload, self.lastCrank)
                self.dataReceived.emit(data)
            except Exception as exc:
                self.failed.emit(f"P715 packet decode failed: {exc}")
        await self.client.start_notify(CYCLING_POWER_MEASUREMENT, notification)
        return address

    def connectDevice(self, address: str) -> None:
        self.submit(self._connect(address), self.connected.emit)

    def _handleDisconnected(self, _client) -> None:
        """Bleak calls this immediately when the pedal drops its BLE link."""
        self.client = None
        self.disconnected.emit()

    async def _disconnect(self) -> None:
        if self.client is not None:
            if self.client.is_connected:
                try:
                    await self.client.stop_notify(CYCLING_POWER_MEASUREMENT)
                except Exception:
                    pass
                await self.client.disconnect()
            self.client = None

    def disconnectDevice(self) -> None:
        self.submit(self._disconnect(), lambda _value: self.disconnected.emit())

    def stopWorker(self) -> None:
        if self.loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)