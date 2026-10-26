"""Small, reusable Bluetooth controller for starting 攝影機 recordings.

The camera is deliberately controlled independently from the EMG data path:
an unavailable camera reports an error but never interrupts EMG acquisition.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen
from xml.sax.saxutils import escape as xml_escape

from bleak import BleakClient
from PySide6.QtCore import QThread, Signal


class GoProError(RuntimeError):
    """The camera could not be discovered or accept a Bluetooth command."""


# Open GoPro BLE Control & Query service.
GOPRO_COMMAND_UUID = "b5f90072-aa8d-11e3-9046-0002a5d5c51b"
GOPRO_COMMAND_RESPONSE_UUID = "b5f90073-aa8d-11e3-9046-0002a5d5c51b"
GOPRO_SETTING_UUID = "b5f90074-aa8d-11e3-9046-0002a5d5c51b"
GOPRO_WIFI_SSID_UUID = "b5f90002-aa8d-11e3-9046-0002a5d5c51b"
GOPRO_WIFI_PASSWORD_UUID = "b5f90003-aa8d-11e3-9046-0002a5d5c51b"
DEVICE_MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
SET_THIRD_PARTY_CLIENT_INFO = bytes((0x50,))
START_RECORDING = bytes((0x01, 0x01, 0x01))
STOP_RECORDING = bytes((0x01, 0x01, 0x00))
# Select video before opening the shutter; otherwise it can take a photo.
VIDEO_MODE = bytes((0x02, 0x01, 0x00))
# Setting 91 (LED) with the special value 0x42 is the Open GoPro BLE
# keep-alive message.  It is intentionally sent on the Setting characteristic,
# not on the Command characteristic.
BLE_KEEP_ALIVE = bytes((0x5B, 0x01, 0x42))
SET_WIFI_AP_ON = bytes((0x17, 0x01, 0x01))


def frame_control_packet(payload: bytes) -> bytes:
    """Frame a short Open 攝影機 payload for the GP-0072 data protocol."""
    if len(payload) >= 0x1FFF:
        raise ValueError("攝影機 BLE payload is too long")
    return bytes((0x20 | (len(payload) >> 8), len(payload) & 0xFF)) + payload


@dataclass(frozen=True)
class GoProResult:
    action: str
    address: str
    name: str
    timestamp: datetime
    details: tuple[str, ...] = ()
    wifi_ssid: str | None = None
    wifi_password: str | None = None


async def append_device_info(client: BleakClient, details: list[str]) -> None:
    """Read standard BLE metadata so logs identify the camera/firmware under test."""
    for label, uuid in (("Camera model", DEVICE_MODEL_UUID), ("Firmware revision", FIRMWARE_REVISION_UUID)):
        try:
            value = bytes(await client.read_gatt_char(uuid)).decode("utf-8", errors="replace").strip("\x00 ")
            details.append(f"{label}: {value or 'not reported'}")
        except Exception as exc:
            details.append(f"{label}: unavailable ({type(exc).__name__})")


async def read_wifi_credentials(client: BleakClient) -> tuple[str | None, str | None]:
    """Read the camera AP credentials over the paired BLE connection.

    Credentials remain in memory only and are deliberately never put in the
    event log or session file.
    """
    try:
        # Some HERO11 firmware revisions do not expose these optional
        # characteristics.  Check discovery first: WinRT otherwise raises an
        # opaque ``NoneType.status`` error when asked to read a missing UUID.
        if not client.services.get_characteristic(GOPRO_WIFI_SSID_UUID):
            return None, None
        if not client.services.get_characteristic(GOPRO_WIFI_PASSWORD_UUID):
            return None, None
        ssid = bytes(await client.read_gatt_char(GOPRO_WIFI_SSID_UUID)).decode("utf-8").strip("\x00 ")
        password = bytes(await client.read_gatt_char(GOPRO_WIFI_PASSWORD_UUID)).decode("utf-8").strip("\x00 ")
        return ssid or None, password or None
    except Exception:
        return None, None


async def send_recording_command(
    action: str, address: str | None = None, identifier: str | None = None,
) -> GoProResult:
    """Send the selected camera the minimal 攝影機 BLE control sequence."""
    if action not in {"verify", "start", "stop"}:
        raise ValueError("action must be 'verify', 'start', or 'stop'")
    if not address:
        raise GoProError("No 攝影機 is selected. Scan, select the camera, then choose '設定為 攝影機'.")
    details: list[str] = []

    async def write_command(client: BleakClient, label: str, payload: bytes) -> None:
        packet = frame_control_packet(payload)
        try:
            await client.write_gatt_char(GOPRO_COMMAND_UUID, packet, response=True)
        except Exception as exc:
            detail = str(exc).strip() or repr(exc)
            raise GoProError(f"{label} failed ({type(exc).__name__}): {detail}") from exc
        details.append(f"{label}: GATT acknowledged ({packet.hex(' ')})")

    try:
        async with BleakClient(address, timeout=8.0) as client:
            if not client.is_connected:
                raise GoProError(f"Could not connect to the selected camera ({address}).")
            details.append(f"BLE connected: {address}")
            await append_device_info(client, details)
            # On Windows the system pairing dialog is optional for some
            # firmware.  Match the reference project: try it, but let a
            # successful GATT connection proceed if Windows declines it.
            try:
                await client.pair()
                details.append("Windows Bluetooth pairing: completed")
            except Exception:
                details.append("Windows Bluetooth pairing: skipped or already paired")
            await write_command(client, "Set third-party client info", SET_THIRD_PARTY_CLIENT_INFO)
            if action == "start":
                await write_command(client, "Select video mode", VIDEO_MODE)
                await asyncio.sleep(0.2)
                await write_command(client, "Start-recording shutter", START_RECORDING)
                # Keep the BLE session alive long enough for the camera to
                # process the capture transition before this one-shot worker
                # disconnects.
                await asyncio.sleep(1.0)
            elif action == "stop":
                await write_command(client, "Stop-recording shutter", STOP_RECORDING)
            name = "攝影機"
    except GoProError:
        raise
    except Exception as exc:
        detail = str(exc).strip() or repr(exc)
        raise GoProError(f"Camera {action} command failed: {detail}") from exc
    return GoProResult(action, address, name, datetime.now().astimezone(), tuple(details))


async def keep_ready_session(
    address: str | None,
    start_requested: threading.Event,
    stop_recording_requested: threading.Event,
    disconnect_requested: threading.Event,
    completed: Signal,
    diagnostic: Signal,
) -> GoProResult:
    """Keep BLE connected after verification, then start/stop on request."""
    if not address:
        raise GoProError("No 攝影機 is selected. Scan, select the camera, then choose '設定為 攝影機'.")
    details: list[str] = []
    wifi_ssid: str | None = None
    wifi_password: str | None = None

    def result(action: str, result_details: tuple[str, ...]) -> GoProResult:
        return GoProResult(
            action, address, "攝影機", datetime.now().astimezone(), result_details, wifi_ssid, wifi_password,
        )

    async def write_command(client: BleakClient, label: str, payload: bytes) -> None:
        packet = frame_control_packet(payload)
        try:
            await client.write_gatt_char(GOPRO_COMMAND_UUID, packet, response=True)
        except Exception as exc:
            detail = str(exc).strip() or repr(exc)
            raise GoProError(f"{label} failed ({type(exc).__name__}): {detail}") from exc
        details.append(f"{label}: GATT acknowledged ({packet.hex(' ')})")

    try:
        async with BleakClient(address, timeout=8.0) as client:
            if not client.is_connected:
                raise GoProError(f"Could not connect to the selected camera ({address}).")
            details.append(f"BLE connected and held open: {address}")
            await append_device_info(client, details)
            try:
                def on_command_response(_sender: object, data: bytearray) -> None:
                    diagnostic.emit(f"Camera command response: {bytes(data).hex(' ')}")

                await client.start_notify(GOPRO_COMMAND_RESPONSE_UUID, on_command_response)
                details.append("Camera command-response notifications enabled")
            except Exception as exc:
                details.append(f"Camera command-response notifications unavailable: {type(exc).__name__}")
            try:
                await client.pair()
                details.append("Windows Bluetooth pairing: completed")
            except Exception:
                details.append("Windows Bluetooth pairing: skipped or already paired")
            wifi_ssid, wifi_password = await read_wifi_credentials(client)
            if wifi_ssid and wifi_password:
                details.append(f"Camera Wi-Fi credentials available: {wifi_ssid}")
            else:
                details.append("攝影機 Wi-Fi credentials unavailable")
            await write_command(client, "Set third-party client info", SET_THIRD_PARTY_CLIENT_INFO)
            try:
                await write_command(client, "Enable 攝影機 Wi-Fi AP", SET_WIFI_AP_ON)
            except GoProError as exc:
                # Recording control is still valid if this optional media
                # download setup step is unavailable on a camera firmware.
                details.append(f"Enable camera Wi-Fi AP: unavailable ({exc})")
            completed.emit(result("verify", tuple(details)))

            # Keep the verified BLE session warm.  It stays alive across
            # multiple collections until the user explicitly disconnects.
            next_keep_alive = asyncio.get_running_loop().time() + 3.0
            while not disconnect_requested.is_set():
                if asyncio.get_running_loop().time() >= next_keep_alive:
                    try:
                        await client.write_gatt_char(
                            GOPRO_SETTING_UUID, frame_control_packet(BLE_KEEP_ALIVE), response=True,
                        )
                    except Exception as exc:
                        # A keep-alive failure is useful diagnostics, but the
                        # pending collection must remain usable if the camera
                        # still accepts the shutter command.
                        diagnostic.emit(f"BLE keep-alive failed: {type(exc).__name__}: {exc}")
                    next_keep_alive = asyncio.get_running_loop().time() + 3.0
                if not start_requested.is_set():
                    await asyncio.sleep(0.05)
                    continue

                start_requested.clear()
                await write_command(client, "Select video mode", VIDEO_MODE)
                await asyncio.sleep(0.2)
                await write_command(client, "Start-recording shutter", START_RECORDING)
                completed.emit(result("start", tuple(details)))

                while not stop_recording_requested.is_set() and not disconnect_requested.is_set():
                    if asyncio.get_running_loop().time() >= next_keep_alive:
                        try:
                            await client.write_gatt_char(
                                GOPRO_SETTING_UUID, frame_control_packet(BLE_KEEP_ALIVE), response=True,
                            )
                        except Exception as exc:
                            diagnostic.emit(f"BLE keep-alive failed: {type(exc).__name__}: {exc}")
                        next_keep_alive = asyncio.get_running_loop().time() + 3.0
                    await asyncio.sleep(0.05)

                # Always stop the camera if an active recording is interrupted
                # by a manual disconnect or application shutdown.
                stop_recording_requested.clear()
                stop_details: list[str] = ["BLE session remained open after recording stop"]
                try:
                    await client.write_gatt_char(
                        GOPRO_COMMAND_UUID, frame_control_packet(STOP_RECORDING), response=True,
                    )
                except Exception as exc:
                    detail = str(exc).strip() or repr(exc)
                    raise GoProError(f"Stop-recording shutter failed ({type(exc).__name__}): {detail}") from exc
                stop_details.append(
                    f"Stop-recording shutter: GATT acknowledged ({frame_control_packet(STOP_RECORDING).hex(' ')})"
                )
                completed.emit(result("stop", tuple(stop_details)))
                await asyncio.sleep(0.05)
            return result("disconnect", ("BLE session closed",))
    except GoProError:
        raise
    except Exception as exc:
        detail = str(exc).strip() or repr(exc)
        raise GoProError(f"Camera recording session failed: {detail}") from exc


class GoProCommandWorker(QThread):
    """Run BLE work off the GUI thread."""

    completed = Signal(object)
    failed = Signal(str)
    diagnostic = Signal(str)

    def __init__(self, action: str, address: str | None = None, identifier: str | None = None) -> None:
        super().__init__()
        self.action = action
        self.address = address
        self.identifier = identifier
        self._start_requested = threading.Event()
        self._stop_recording_requested = threading.Event()
        self._disconnect_requested = threading.Event()

    def request_start(self) -> None:
        """Send start through the already-open BLE session."""
        self._stop_recording_requested.clear()
        self._start_requested.set()

    def request_stop(self) -> None:
        """Stop recording but preserve the ready Bluetooth connection."""
        self._stop_recording_requested.set()

    def request_disconnect(self) -> None:
        """Close the held Bluetooth connection, stopping active recording first."""
        self._disconnect_requested.set()

    def run(self) -> None:
        partial: Path | None = None
        try:
            if self.action == "connect":
                result = asyncio.run(
                    keep_ready_session(
                        self.address, self._start_requested, self._stop_recording_requested,
                        self._disconnect_requested, self.completed, self.diagnostic,
                    )
                )
                if result.action != "disconnect":
                    self.completed.emit(result)
            else:
                self.completed.emit(asyncio.run(send_recording_command(self.action, self.address, self.identifier)))
        except (GoProError, ValueError) as exc:
            self.failed.emit(str(exc))


class GoProDownloadWorker(QThread):
    """Download an MP4 from a 攝影機 Wi-Fi access point."""

    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    file_started = Signal(str)

    def __init__(
        self, destination: Path, selection: tuple[str, str] | None = None, collision: str = "rename",
    ) -> None:
        super().__init__()
        self.destination = destination
        self.selection = selection
        self.collision = collision

    @staticmethod
    def _newest_video(media: dict) -> tuple[str, str]:
        candidates: list[tuple[tuple[int, str], str, str]] = []
        for directory in media.get("media", []):
            folder = str(directory.get("d", ""))
            for file in directory.get("fs", []):
                name = str(file.get("n", ""))
                if folder and name.lower().endswith(".mp4"):
                    modified = file.get("mod", file.get("cre", 0))
                    try:
                        stamp = int(modified)
                    except (TypeError, ValueError):
                        stamp = 0
                    candidates.append(((stamp, name), folder, name))
        if not candidates:
            raise GoProError("No MP4 video was found on the camera.")
        _, folder, name = max(candidates)
        return folder, name

    @staticmethod
    def list_videos(media: dict) -> list[tuple[str, str]]:
        videos: list[tuple[str, str]] = []
        for directory in media.get("media", []):
            folder = str(directory.get("d", ""))
            for file in directory.get("fs", []):
                name = str(file.get("n", ""))
                if folder and name.lower().endswith(".mp4"):
                    videos.append((folder, name))
        return sorted(videos, key=lambda item: (item[0], item[1]), reverse=True)

    @staticmethod
    def fetch_media_list() -> dict:
        with urlopen("http://10.5.5.9:8080/gopro/media/list", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def run(self) -> None:
        try:
            # 10.5.5.9 is the documented HTTP address exposed by a GoPro AP.
            if self.selection is None:
                folder, name = self._newest_video(self.fetch_media_list())
            else:
                folder, name = self.selection
            self.destination.mkdir(parents=True, exist_ok=True)
            target = self.destination / name
            if target.exists() and self.collision == "skip":
                self.completed.emit(target)
                return
            if target.exists() and self.collision == "rename":
                index = 2
                while target.exists():
                    target = self.destination / f"{Path(name).stem}_{index:02d}{Path(name).suffix}"
                    index += 1
            url = "http://10.5.5.9:8080/videos/DCIM/{}/{}".format(quote(folder), quote(name))
            self.file_started.emit(target.name)
            partial = target.with_name(f"{target.name}.part")
            with urlopen(url, timeout=20) as response, partial.open("wb") as output:
                total = int(response.headers.get("Content-Length", "0"))
                received = 0
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    received += len(chunk)
                    self.progress.emit(received, total)
            partial.replace(target)
            partial = None
            self.completed.emit(target)
        except (GoProError, OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            if partial:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
            self.failed.emit(str(exc))


class GoProMediaListWorker(QThread):
    """Read the 攝影機 media list without downloading a file."""

    completed = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            videos = GoProDownloadWorker.list_videos(GoProDownloadWorker.fetch_media_list())
            if not videos:
                raise GoProError("No MP4 video was found on the camera.")
            self.completed.emit(videos)
        except (GoProError, OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            self.failed.emit(str(exc))


class GoProWifiConnectWorker(QThread):
    """Connect Windows to the 攝影機 AP without parsing localized netsh output."""

    connected = Signal()
    failed = Signal(str)

    def __init__(self, ssid: str, password: str) -> None:
        super().__init__()
        self.ssid = ssid
        self.password = password

    def run(self) -> None:
        profile_path: Path | None = None
        try:
            if not self.ssid or not self.password:
                raise GoProError("攝影機 Wi-Fi credentials are unavailable. Pair and test the camera again.")
            profile = f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{xml_escape(self.ssid)}</name>
  <SSIDConfig><SSID><name>{xml_escape(self.ssid)}</name></SSID><nonBroadcast>false</nonBroadcast></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
  <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{xml_escape(self.password)}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>'''
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as temporary:
                temporary.write(profile)
                profile_path = Path(temporary.name)
            # Do not inspect stdout: it is localized on zh_TW Windows. The
            # subsequent HTTP probe is the real connection success check.
            subprocess.run(
                ["netsh", "wlan", "add", "profile", f"filename={profile_path}", "user=current"],
                capture_output=True, timeout=15, check=False,
            )
            subprocess.run(
                ["netsh", "wlan", "connect", f"name={self.ssid}", f"ssid={self.ssid}"],
                capture_output=True, timeout=15, check=False,
            )
            for _ in range(20):
                try:
                    with urlopen("http://10.5.5.9:8080/gopro/media/list", timeout=2) as response:
                        response.read(1)
                    self.connected.emit()
                    return
                except (URLError, OSError, ValueError, json.JSONDecodeError):
                    self.msleep(1000)
            raise GoProError("Windows could not reach the 攝影機 Wi-Fi access point after 20 seconds.")
        except (GoProError, OSError, subprocess.SubprocessError) as exc:
            self.failed.emit(str(exc))
        finally:
            if profile_path:
                try:
                    profile_path.unlink(missing_ok=True)
                except OSError:
                    pass
