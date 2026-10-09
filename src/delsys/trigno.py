from __future__ import annotations
import socket
import struct

CHANNEL_COUNT = 16
SAMPLE_RATE = 2000.0
MESSAGE_END = b"\r\n\r\n"

class TrignoError(RuntimeError):
    """The SDK Server could not complete a protocol operation."""


class TrignoClient:
    def __init__(self, host: str = "127.0.0.1", timeout: float = 5.0) -> None:
        self.host, self.timeout = host, timeout
        self.command_socket: socket.socket | None = None
        self.emg_socket: socket.socket | None = None
        self.aux_socket: socket.socket | None = None
        self._command_buffer = b""
        self._emg_buffer = bytearray()
        self.byte_order = "<"
        self.streaming = False

    def connect(self) -> str:
        try:
            self.command_socket = socket.create_connection((self.host, 50040), self.timeout)
            self.command_socket.settimeout(self.timeout)
            banner = self.readCommandMessage()
            self.emg_socket = socket.create_connection((self.host, 50041), self.timeout)
            self.emg_socket.settimeout(1.0)
            self.aux_socket = socket.create_connection((self.host, 50042), self.timeout)
            self.aux_socket.settimeout(0.2)
            endianness = self.command("ENDIANNESS?").upper()
            self.byte_order = "<" if "LITTLE" in endianness else ">" if "BIG" in endianness else ""
            if not self.byte_order:
                raise TrignoError(f"無法辨識 Server 位元組序：{endianness}")
            return banner
        except (OSError, socket.timeout) as exc:
            self.close()
            raise TrignoError(f"無法連線到 Trigno SDK Server：{exc}") from exc

    def readCommandMessage(self) -> str:
        if self.command_socket is None:
            raise TrignoError("Command socket 尚未連線")
        while MESSAGE_END not in self._command_buffer:
            chunk = self.command_socket.recv(4096)
            if not chunk:
                raise TrignoError("Command socket 被 Server 關閉")
            self._command_buffer += chunk
        message, self._command_buffer = self._command_buffer.split(MESSAGE_END, 1)
        return message.decode("ascii", errors="replace").strip()

    def command(self, text: str) -> str:
        if self.command_socket is None:
            raise TrignoError("Command socket 尚未連線")
        self.command_socket.sendall(text.encode("ascii") + MESSAGE_END)
        return self.readCommandMessage()

    def sensor_states(self, sensor_count: int = CHANNEL_COUNT) -> tuple[list[int], list[int]]:
        paired, active = [], []
        for number in range(1, sensor_count + 1):
            if self.command(f"SENSOR {number} PAIRED?").upper() == "YES":
                paired.append(number)
                if self.command(f"SENSOR {number} ACTIVE?").upper() == "YES":
                    active.append(number)
        return paired, active

    def active_sensors(self, sensor_count: int = CHANNEL_COUNT) -> list[int]:
        return [
            number
            for number in range(1, sensor_count + 1)
            if self.command(f"SENSOR {number} ACTIVE?").upper() == "YES"
        ]

    def sensor_serials(self, sensor_numbers: list[int]) -> dict[int, str]:
        serials: dict[int, str] = {}
        for number in sensor_numbers:
            try:
                serials[number] = self.command(f"SENSOR {number} SERIAL?")
            except TrignoError:
                serials[number] = "Unknown SID"
        return serials

    def base_serial(self) -> str:
        return self.command("BASE SERIAL?")

    def start(self) -> None:
        response = self.command("START")
        if response.upper() != "OK":
            raise TrignoError(f"START 被 Server 拒絕：{response}")
        self.streaming = True

    def stop(self) -> None:
        if self.command_socket is None or not self.streaming:
            return
        response = self.command("STOP")
        self.streaming = False
        if response.upper() != "OK":
            raise TrignoError(f"STOP 被 Server 拒絕：{response}")

    def read_emg_frames(self, channel_count: int = CHANNEL_COUNT) -> list[tuple[float, ...]]:
        if self.emg_socket is None:
            raise TrignoError("EMG socket 尚未連線")
        try:
            chunk = self.emg_socket.recv(65536)
        except socket.timeout:
            return []
        if not chunk:
            raise TrignoError("EMG socket 被 Server 關閉")
        self._emg_buffer.extend(chunk)
        frame_size = channel_count * 4
        frame_count = len(self._emg_buffer) // frame_size
        if not frame_count:
            return []
        unpack = struct.Struct(f"{self.byte_order}{channel_count}f").unpack
        consumed = frame_count * frame_size
        frames = [unpack(self._emg_buffer[offset : offset + frame_size]) for offset in range(0, consumed, frame_size)]
        del self._emg_buffer[:consumed]
        return frames

    def capture(self,duration: float,sample_rate: float,channel_count: int,writer,active_indices: list[int],) -> int:
        target, count = round(duration * sample_rate), 0
        while count < target:
            for frame in self.read_emg_frames(channel_count):
                if count >= target:
                    break
                writer.writerow(
                    [count, count / sample_rate]
                    + [frame[index] for index in active_indices]
                )
                count += 1
        return count

    def close(self) -> None:
        for sock in (self.aux_socket, self.emg_socket, self.command_socket):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self.aux_socket = self.emg_socket = self.command_socket = None

    def __enter__(self) -> "TrignoClient":
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.stop()
        except (OSError, TrignoError):
            pass
        self.close()