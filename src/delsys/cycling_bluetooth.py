from __future__ import annotations
import asyncio
from PySide6.QtCore import QThread, Signal
try:
    from bleak import BleakScanner
except ImportError:
    BleakScanner = None

class CyclingDeviceScanWorker(QThread):
    devicesFound = Signal(object)
    failed = Signal(str)

    def stop(self) -> None:
        self.requestInterruption()

    async def _discover(self) -> list[object]:
        scanner = BleakScanner()
        await scanner.start()
        try:
            for _ in range(70):
                if self.isInterruptionRequested():
                    return []
                await asyncio.sleep(0.1)
            discovered = scanner.discovered_devices
            if hasattr(discovered, "values"):
                return list(discovered.values())
            return list(discovered)
        finally:
            await scanner.stop()

    def run(self) -> None:
        try:
            if BleakScanner is None:
                raise RuntimeError("bleak is not installed")
            devices = asyncio.run(self._discover())
            if not self.isInterruptionRequested():
                self.devicesFound.emit([(device.address, device.name or "Unknown device") for device in devices])
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))