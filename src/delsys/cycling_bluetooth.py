from __future__ import annotations
import asyncio
from PySide6.QtCore import QThread, Signal
try:
    from bleak import BleakScanner
except ImportError:  # pragma: no cover
    BleakScanner = None

class CyclingDeviceScanWorker(QThread):
    devicesFound = Signal(object)
    failed = Signal(str)

    def stop(self) -> None:
        """Request a prompt, cooperative stop of the Bluetooth scan."""
        self.requestInterruption()

    async def _discover(self) -> list[object]:
        """Scan in short intervals so closing the application never strands us."""
        scanner = BleakScanner()
        await scanner.start()
        try:
            # Do not use ``BleakScanner.discover(timeout=...)`` here: it cannot
            # be interrupted while Qt is closing the window on Windows.
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
            # Suppress expected backend errors caused by closing an active scan.
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))